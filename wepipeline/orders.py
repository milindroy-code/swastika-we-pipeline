"""Executed orders and Sarthi ideas, for the talking point (what the client last did, with the stock and the time).

Orders: [ReplResearchPanelDB].[dbo].[CompletedOrders] (one row per executed order: client, trading symbol, buy / sell,
quantity, fill time), copied incrementally by id for the dashboard's own clients and kept `keep_days`.
Sarthi ideas: [ReplResearchPanelDB].[dbo].[tblScripOrder] (the research desk's recommendations, ~15 a day): the app
doesn't say which idea a client opened, so the talking point names the idea that was live at the time.
Both read-only.
"""
from __future__ import annotations

import logging
import re
import time as _time
from datetime import datetime, timedelta

from .context import Ctx, iso, parse_dt
from .db import one, rows

log = logging.getLogger(__name__)

ORDERS_SQL = """
SELECT id, UPPER(LTRIM(RTRIM(userId))) AS client, trdSymbol, transType, fillShares, fillTime
FROM [ReplResearchPanelDB].[dbo].[CompletedOrders] WITH (NOLOCK) WHERE id > {lo} AND id <= {hi}"""
IDEAS_SQL = """
SELECT ScripSymbol, StrikePrice, CallOrPut, InstrumentName, SegmentName, BuySell, CreatedOn
FROM [ReplResearchPanelDB].[dbo].[tblScripOrder]
WHERE MessageType = 'Recommendation' AND IsDeleted = 0 AND CreatedOn >= DATEADD(DAY, -{days}, GETDATE())"""


def settings(ctx: Ctx) -> dict:
    return {"keep_days": 15, "first_rows": 800000, "chunk": 200000, **(ctx.cfg.get("orders") or {})}


def sync_orders(ctx: Ctx) -> dict:
    """Copy new executed orders (by id) for the dashboard's clients."""
    from .clientdb import _query, available, reachable

    if not (available(ctx) and reachable(ctx)):
        return {"status": "skipped", "detail": "client database not reachable"}
    s = settings(ctx)
    top = int(_query(ctx, "SELECT MAX(id) AS m FROM [ReplResearchPanelDB].[dbo].[CompletedOrders] WITH (NOLOCK)")["m"].iloc[0])
    last = one(ctx.conn, "SELECT MAX(id) AS m FROM app_orders")
    lo = int(last["m"]) if last and last["m"] else top - s["first_rows"]
    clients = {r["lead_id"].upper() for r in rows(ctx.conn, "SELECT lead_id FROM leads")}
    clients |= {r["lead_id"].upper() for r in rows(ctx.conn, "SELECT lead_id FROM drive_members")}
    read = kept = 0
    while lo < top:
        hi = min(lo + s["chunk"], top)
        df = _query(ctx, ORDERS_SQL.format(lo=lo, hi=hi))
        read += len(df)
        for r in df.itertuples(index=False):
            if r.client not in clients:
                continue
            try:
                t = datetime.strptime(str(r.fillTime).strip(), "%d-%m-%Y %H:%M:%S").replace(tzinfo=ctx.tz)
            except ValueError:
                continue
            ctx.conn.execute("INSERT OR IGNORE INTO app_orders (id, client_id, symbol, side, qty, fill_at) VALUES (?,?,?,?,?,?)",
                             (int(r.id), r.client, str(r.trdSymbol or "").strip(), str(r.transType or "").strip(),
                              float(r.fillShares or 0), iso(t)))
            kept += 1
        ctx.conn.commit()
        lo = hi
    ctx.conn.execute("DELETE FROM app_orders WHERE fill_at < ?", (iso(ctx.now() - timedelta(days=s["keep_days"])),))
    ctx.conn.commit()
    return {"status": "ok", "read": read, "kept": kept}


MONTHS = "JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC"


def symbol_words(sym: str) -> str:
    """'LAURUSLABS-EQ' -> 'LAURUSLABS'; 'NIFTY03NOV2623300CE' -> 'NIFTY 23300 CE option'; 'NATGASMINI24NOV26' -> 'NATGASMINI future'."""
    s = (sym or "").strip().upper()
    s = re.sub(r"-(EQ|BE|BZ|SM|ST)$", "", s)
    m = re.match(rf"^([A-Z&\-]+?)(\d{{2}}(?:{MONTHS})\d{{2}})([CP])(\d+(?:\.\d+)?)$", s)  # NIFTY03NOV26C23200
    if m:
        name, _, cp, strike = m.groups()
        return f"{name} {strike} {cp}E option"
    m = re.match(rf"^([A-Z&\-]+?)(\d{{2}}(?:{MONTHS})\d{{2}})(\d+(?:\.\d+)?)?(CE|PE)?$", s)
    if m:
        name, _, strike, cp = m.groups()
        return f"{name} {strike} {cp} option" if cp else f"{name} future"
    return s


_ideas_cache: dict = {}


def sarthi_ideas(ctx: Ctx, days: int = 9) -> list[dict]:
    """The research desk's recommendations of the last `days` (cached 30 min), oldest first; [] when unreachable."""
    from .clientdb import _query, available, reachable

    hit = _ideas_cache.get("ideas")
    if hit and _time.monotonic() - hit[0] < 1800:
        return hit[1]
    out = []
    if available(ctx) and reachable(ctx):
        try:
            for r in _query(ctx, IDEAS_SQL.format(days=days)).itertuples(index=False):
                t = parse_dt(str(r.CreatedOn)[:19], ctx.tz)
                strike = float(r.StrikePrice or 0)
                cp = str(r.CallOrPut or "").strip().upper()
                inst, segname = str(r.InstrumentName or "").upper(), str(r.SegmentName or "").upper()
                seg = "Commodity" if inst == "COMMODITY" else ("F&O" if segname in ("OPTIONS", "FUTURES") else "Equity")
                what = (f"{r.ScripSymbol} {strike:g} {cp}" if cp in ("CE", "PE") and strike else
                        f"{r.ScripSymbol} future" if segname == "FUTURES" else str(r.ScripSymbol))
                out.append({"at": t, "idea": f"{str(r.BuySell or '').strip()} {what}".strip(), "segment": seg})
        except Exception as e:
            log.warning("Sarthi ideas skipped: %s", e)
    out = sorted((x for x in out if x["at"]), key=lambda x: x["at"])
    _ideas_cache["ideas"] = (_time.monotonic(), out)
    return out


def idea_live_at(ideas: list[dict], t: datetime) -> dict | None:
    """The latest Sarthi idea published before `t` on the same day."""
    before = [x for x in ideas if x["at"] <= t and x["at"].date() == t.date()]
    return before[-1] if before else None


def last_orders(ctx: Ctx, client_ids: list[str], days: int = 7) -> dict[str, dict]:
    """client -> its latest executed order in `days` {symbol, side, qty, at}."""
    since = iso(ctx.now() - timedelta(days=days))
    out = {}
    ups = [c.upper() for c in client_ids]
    for i in range(0, len(ups), 500):
        chunk = ups[i:i + 500]
        for r in rows(ctx.conn, f"SELECT client_id, symbol, side, qty, MAX(fill_at) AS at FROM app_orders WHERE client_id IN "
                                f"({','.join('?' * len(chunk))}) AND fill_at>=? GROUP BY client_id", (*chunk, since)):
            out[r["client_id"]] = {"symbol": symbol_words(r["symbol"]), "side": r["side"], "qty": r["qty"], "at": parse_dt(r["at"], ctx.tz)}
    return out
