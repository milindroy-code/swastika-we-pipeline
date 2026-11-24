"""Clients who just booked a profit: the best moment to ask for a referral (they're happy with us right now).

From the back-office trade books (read-only): every closed equity and F&O trade carries its realised P&L (PL_AMT) and the
date it was closed (CNTR_TRADE_DATE). Per client over the last `days`: net realised profit, how many closed trades made
money, and the best single scrip-day (what the GM congratulates them on). The books are processed overnight, so the
latest closes are usually yesterday's.

Referral score boost (referral.referral_score): up to `points`, scaled by the profit (square root, full at `full_profit`),
full when the profit was booked in the last `fresh_days`, half up to `days`.
A stock closed by `bulk_clients` or more clients on one day (an IPO listing, a corporate action) is left out.
"""
from __future__ import annotations

import logging
from datetime import timedelta

import pandas as pd

from .context import Ctx, iso, parse_dt
from .db import one, rows

log = logging.getLogger(__name__)

CLOSES_SQL = """
SELECT E.CLIENT_ID, E.PL_AMT, E.CNTR_TRADE_DATE AS ClosedOn, COALESCE(M.NSE_Scrip_Code, M.Scrip_Name) AS Scrip
FROM [192.168.0.18].[Capsfo].[pms].[CLIENT_FIFO_Process] E
LEFT JOIN [192.168.0.18].[Capsfo].[pms].[Scrip_Map] M ON M.Scrip_CD = E.Scrip_CD
WHERE E.CNTR_TRADE_DATE >= '{since}' AND E.CNTR_TRADE_DATE <= GETDATE() AND E.TR_TYPE <> 'REVERSE' AND E.PL_AMT IS NOT NULL
UNION ALL
SELECT F.CLIENT_ID, F.PL_AMT, F.CNTR_TRADE_DATE, F.Scrip_Symbol
FROM [192.168.0.18].[Capsfo].[pms].[FO_CLIENT_FIFO_Process] F
WHERE F.CNTR_TRADE_DATE >= '{since}' AND F.CNTR_TRADE_DATE <= GETDATE() AND F.PL_AMT IS NOT NULL"""
CLIENTS_SQL = """
SELECT C.CLIENT_ID, {central} AS IsCentralized
FROM [Repl_TargetReview].[dbo].[tbl_ClientNew] C
WHERE C.CLIENT_ID IN ({ids}) AND ISNULL(CAST(C.IsAccountClosed AS VARCHAR(10)), '0') NOT IN ('1', 'True', 'true')"""


def settings(ctx: Ctx) -> dict:
    return {"days": 7, "fresh_days": 3, "min_profit": 1000, "points": 25, "full_profit": 25000, "bulk_clients": 1000,
            **((ctx.cfg.get("referral") or {}).get("profit_booking") or {})}


def _scrip_words(scrip) -> str:
    from .talking_points import _scrip_words as words

    return words(" ".join(str(scrip or "").split()))


def pull(ctx: Ctx) -> dict:
    """Re-read the last `days` of closed trades and keep every open client with a net profit of min_profit or more."""
    from .clientdb import _query, centralised_sql

    s, now = settings(ctx), ctx.now()
    since = (now - timedelta(days=s["days"])).strftime("%Y-%m-%d")
    df = _query(ctx, CLOSES_SQL.format(since=since))
    if df.empty:
        return {"closes": 0, "clients": 0}
    df["CLIENT_ID"] = df["CLIENT_ID"].astype(str).str.strip().str.upper()
    df["PL_AMT"] = pd.to_numeric(df["PL_AMT"], errors="coerce").fillna(0)
    df["day"] = pd.to_datetime(df["ClosedOn"]).dt.date
    # a stock sold by thousands of clients on one day is an IPO listing or a corporate action, not a trading win
    crowd = df.groupby(["Scrip", "day"])["CLIENT_ID"].transform("nunique")
    bulk = df[crowd >= s["bulk_clients"]].groupby(["Scrip", "day"])["CLIENT_ID"].nunique()
    df = df[~df["Scrip"].isin({k[0] for k in bulk.index})]  # the whole week of that stock: listing-day sales spill into the next days
    per = df.groupby("CLIENT_ID").agg(net=("PL_AMT", "sum"), wins=("PL_AMT", lambda x: int((x > 0).sum())), closes=("PL_AMT", "size"))
    per = per[per["net"] >= s["min_profit"]]
    win = df[df["CLIENT_ID"].isin(per.index) & (df["PL_AMT"] > 0)]
    best = (win.groupby(["CLIENT_ID", "Scrip", "day"])["PL_AMT"].sum().reset_index()
            .sort_values("PL_AMT", ascending=False).drop_duplicates("CLIENT_ID").set_index("CLIENT_ID"))
    last = win.groupby("CLIENT_ID")["day"].max()
    central = {}
    ids = list(per.index)
    for i in range(0, len(ids), 500):
        chunk = ",".join("'" + c.replace("'", "") + "'" for c in ids[i:i + 500])
        for r in _query(ctx, CLIENTS_SQL.format(central=centralised_sql(ctx), ids=chunk)).itertuples(index=False):
            central[str(r.CLIENT_ID).strip().upper()] = int(r.IsCentralized or 0)
    ctx.conn.execute("DELETE FROM profit_bookings")
    kept = 0
    for cid, r in per.iterrows():
        if cid not in central:  # closed account (or not in the client master)
            continue
        b = best.loc[cid] if cid in best.index else None
        ctx.conn.execute("INSERT INTO profit_bookings (client_id, net_profit, profit_closes, closes, best_scrip, best_profit, best_date, "
                         "last_profit_date, is_central, window_days, pulled_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                         (cid, round(float(r["net"]), 2), int(r["wins"]), int(r["closes"]),
                          _scrip_words(b["Scrip"]) if b is not None else None, round(float(b["PL_AMT"]), 2) if b is not None else None,
                          b["day"].isoformat() if b is not None else None, last[cid].isoformat() if cid in last.index else None,
                          central[cid], s["days"], iso(now)))
        kept += 1
    ctx.conn.commit()
    return {"closes": len(df), "clients": kept, "central": sum(1 for c in central.values() if c), "latest_close": str(df["day"].max()),
            "set_aside": {f"{k[0]} {k[1]:%d %b}": int(v) for k, v in bulk.items()}}


def booking(ctx: Ctx, client_id: str) -> dict | None:
    return one(ctx.conn, "SELECT * FROM profit_bookings WHERE client_id=?", (str(client_id).upper(),))


def last_pull(ctx: Ctx) -> str | None:
    r = one(ctx.conn, "SELECT MAX(pulled_at) AS t FROM profit_bookings")
    return r["t"] if r else None


def boost(ctx: Ctx, client_id: str) -> tuple[float, str]:
    """Referral score points for a recent profit, and the reason line."""
    b = booking(ctx, client_id)
    if not b or not b["last_profit_date"]:
        return 0.0, ""
    s = settings(ctx)
    age = (ctx.now().date() - parse_dt(b["last_profit_date"], ctx.tz).date()).days
    if age > s["days"]:
        return 0.0, ""
    pts = s["points"] * min(max(b["net_profit"], 0) / s["full_profit"], 1) ** 0.5 * (1 if age <= s["fresh_days"] else 0.5)
    return round(pts, 1), f"booked ₹{b['net_profit']:,.0f} profit in {b['window_days']} d" + (
        f" (best: {b['best_scrip']} +₹{b['best_profit']:,.0f} on {parse_dt(b['best_date'], ctx.tz):%d %b})" if b["best_scrip"] else "")


def talking_point(ctx: Ctx, client_id: str, reward_line: str = "") -> str:
    """One line for the GM to open the referral call with."""
    b = booking(ctx, client_id)
    if not b:
        return ""
    when = parse_dt(b["best_date"] or b["last_profit_date"], ctx.tz)
    day = "kal" if when and when.date() == ctx.now().date() - timedelta(days=1) else (f"{when:%d %b} ko" if when else "haal hi mein")
    what = f"{b['best_scrip']} mein ₹{b['best_profit']:,.0f} ka profit" if b["best_scrip"] else f"₹{b['net_profit']:,.0f} ka profit"
    return (f"Badhai ho! Aapne {day} {what} book kiya. Aapke jaise doston ko bhi yeh faayda mil sakta hai: "
            "kya aap 1-2 doston ka naam bata sakte hain jo trading karna chahte hain?" + (f" {reward_line}" if reward_line else ""))


def targets(ctx: Ctx, scope: str = "all", days: int | None = None, min_profit: float | None = None) -> list[dict]:
    """Profit bookers to ask, most recent and biggest first, with where they stand in the referral program."""
    from . import referral

    s = settings(ctx)
    days = days or s["days"]
    since = (ctx.now().date() - timedelta(days=days)).isoformat()
    cool = (ctx.now() - timedelta(days=referral.settings(ctx)["cooldown_days"])).isoformat()
    members = {r["lead_id"].upper(): r for r in rows(ctx.conn, "SELECT * FROM drive_members WHERE program=?", (referral.CAMPAIGN,))}
    names = {r["lead_id"].upper(): r for r in rows(ctx.conn, "SELECT lead_id, name, phone FROM leads")}
    gave: dict[str, list] = {}  # referral history (CRM leads with the referral source): who they referred, who referred them
    for r in rows(ctx.conn, "SELECT referrer_id, referred_id, first_trade FROM referral_outcomes"):
        gave.setdefault(r["referrer_id"].upper(), []).append(r)
    came = {r["referred_id"].upper(): r["referrer_id"].upper() for r in rows(ctx.conn, "SELECT referrer_id, referred_id FROM referral_outcomes")}
    out = []
    for b in rows(ctx.conn, "SELECT * FROM profit_bookings WHERE last_profit_date>=? AND net_profit>=? "
                            "ORDER BY last_profit_date DESC, net_profit DESC", (since, min_profit if min_profit is not None else s["min_profit"])):
        if (scope == "central" and not b["is_central"]) or (scope == "non_central" and b["is_central"]):
            continue
        m = members.get(b["client_id"])
        lead = names.get(b["client_id"]) or {}
        asked = bool(m and m["asked_at"] and m["asked_at"] >= cool)
        out.append({**b, "name": lead.get("name") or "", "phone": lead.get("phone") or "", "in_pool": bool(m),
                    "member_status": m["status"] if m else "", "assigned_to": m["assigned_to"] if m else None,
                    "heat_score": m["heat_score"] if m else None, "heat_tier": m["heat_tier"] if m else None,
                    "asked_recently": asked, "asked_at": m["asked_at"] if m else None,
                    "referred_count": len(gave.get(b["client_id"], [])),
                    "referred_traded": sum(1 for x in gave.get(b["client_id"], []) if x["first_trade"]),
                    "referred_by": came.get(b["client_id"], "")})
    return out
