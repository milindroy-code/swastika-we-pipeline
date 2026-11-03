"""Simple talking points for a GM: what the client did in the app, and one or two lines to open the call with.

Per client (last 7 days of app activity, plus the back-office trade books):
    App login     last time in the app, and on how many of the last 7 days
    App surfaces  the parts of the app they used most (Sarthi, Search, Stock pages, Option chain, ...)
    Lever         the friction they hit (tried adding funds, order window left without an order, ...) or else their last trade
    Talking points  one or two lines to read out, built from the lever and the surfaces, plus the ask
WebEngage's activity feed has no event details, so the stock name comes from the client's last trade, not from what
they looked at. Never names a stock to buy or promises returns.
"""
from __future__ import annotations

import json
import logging
import time as _time
from datetime import timedelta

from .context import Ctx, iso, parse_dt
from .db import rows

log = logging.getLogger(__name__)

# event name (without the " · " suffix) -> app surface, most specific first
SURFACES = [
    ("Sarthi ideas", ("RECOMMENDATION_", "CALL_ACCEPTED_MOB", "CALL_INITIATE_MOB", "CALL_MISSED_MOB", "CALL_IGNORED_MOB",
                      "SARTHI_TOGGLE_STATUS_MOB")),
    ("Search", ("SEARCH_SCREEN_OPENED",)),
    ("Stock pages", ("QUOTES_OPENED", "OVERVIEW_OPENED", "CHARTS_")),
    ("Option chain", ("OPTION_CHAIN_SCREEN_OPENED",)),
    ("Watchlist", ("WATCHLIST_OPENED", "ADDED_TO_WATCHLIST", "MTF_WATCHLIST_OPENED")),
    ("Orders", ("ORDER_OPENED", "PLACE_ORDER_OPENED", "PLACE_ORDER_POPUP", "ORDER_PLACED")),
    ("Portfolio", ("POSITION_OPENED", "HOLDING_SECTION_OPENED", "PORTFOLIO_POSITION_OPENED")),
    ("Funds", ("FUNDS_", "FUND_")),
    ("IPO", ("IPO_SCREEN_OPENED",)),
    ("Markets", ("MARKETS_OPENED",)),
]
ADD_FUNDS = ("FUNDS_PROCEED_TO_ADD", "FUNDS_ADD_SCREEN_OPEN", "AUTOPAY_INITIATE")
ORDER_TRIED = ("PLACE_ORDER_OPENED", "PLACE_ORDER_POPUP_OPENED")
HELP = ("ASSISTANCE_CTA_TAPPED", "ASSISTANCE_CALL_INITIATED")

LAST_TRADE_SQL = """
SELECT CLIENT_ID, Scrip, TradeDate FROM (
    SELECT T.CLIENT_ID, T.Scrip, T.TradeDate, ROW_NUMBER() OVER (PARTITION BY T.CLIENT_ID ORDER BY T.TradeDate DESC) AS rn
    FROM (
        SELECT E.CLIENT_ID, COALESCE(M.NSE_Scrip_Code, M.Scrip_Name) AS Scrip,
               CASE WHEN E.CNTR_TRADE_DATE > E.TRADE_DATE AND E.CNTR_TRADE_DATE <= GETDATE() THEN E.CNTR_TRADE_DATE ELSE E.TRADE_DATE END AS TradeDate
        FROM [192.168.0.18].[Capsfo].[pms].[CLIENT_FIFO_Process] E
        LEFT JOIN [192.168.0.18].[Capsfo].[pms].[Scrip_Map] M ON M.Scrip_CD = E.Scrip_CD
        WHERE E.CLIENT_ID IN ({ids}) AND E.TRADE_DATE <= GETDATE()
        UNION ALL
        SELECT F.CLIENT_ID, F.Scrip_Symbol,
               CASE WHEN F.CNTR_TRADE_DATE > F.TRADE_DATE AND F.CNTR_TRADE_DATE <= GETDATE() THEN F.CNTR_TRADE_DATE ELSE F.TRADE_DATE END
        FROM [192.168.0.18].[Capsfo].[pms].[FO_CLIENT_FIFO_Process] F
        WHERE F.CLIENT_ID IN ({ids}) AND F.TRADE_DATE <= GETDATE()
    ) T
) X WHERE rn = 1"""
_trade_cache: dict[str, tuple[float, dict | None]] = {}


def last_trades(ctx: Ctx, client_ids: list[str]) -> dict[str, dict]:
    """client -> {scrip, date} of the latest trade (equity or F&O), cached for an hour; {} when the server is unreachable."""
    from .clientdb import _query, available, reachable

    now, out, todo = _time.monotonic(), {}, []
    for c in client_ids:
        hit = _trade_cache.get(c)
        if hit and now - hit[0] < 3600:
            if hit[1]:
                out[c] = hit[1]
        else:
            todo.append(c)
    if todo and available(ctx) and reachable(ctx):
        try:
            for i in range(0, len(todo), 300):
                chunk = todo[i:i + 300]
                ids = ",".join("'" + c.replace("'", "") + "'" for c in chunk)
                found = {}
                for _, r in _query(ctx, LAST_TRADE_SQL.format(ids=ids)).iterrows():
                    d = parse_dt(str(r["TradeDate"])[:10], ctx.tz)
                    found[str(r["CLIENT_ID"]).strip().upper()] = {"scrip": " ".join(str(r["Scrip"] or "").split()), "date": d}
                for c in chunk:
                    _trade_cache[c] = (now, found.get(c.upper()))
                    if found.get(c.upper()):
                        out[c] = found[c.upper()]
        except Exception as e:  # the talking points still work from app activity alone
            log.warning("Last trades skipped: %s", e)
    return out


def _base(name: str) -> str:
    return name.split(" · ")[0]


def _day(ctx: Ctx, dt) -> str:
    today = ctx.now().date()
    return "today" if dt.date() == today else "yesterday" if dt.date() == today - timedelta(days=1) else f"{dt:%d %b}"


def _day_hi(ctx: Ctx, dt) -> str:
    """The same day in the Hinglish lines: aaj / kal / 27 Oct ko."""
    today = ctx.now().date()
    return "aaj" if dt.date() == today else "kal" if dt.date() == today - timedelta(days=1) else f"{dt:%d %b} ko"


def _scrip_words(scrip: str) -> str:
    """'EF HDFCBANK 23Nov2026' -> 'HDFCBANK future'; 'EO CE OIL 23Nov2026 480' -> 'OIL 480 CE option'; equity stays as is."""
    p = scrip.split()
    if len(p) >= 3 and p[0] == "EF":
        return f"{p[1]} future"
    if len(p) >= 4 and p[0] == "EO":
        return f"{p[2]} {p[4] if len(p) > 4 else ''} {p[1]} option".replace("  ", " ")
    return scrip


def talking_points(ctx: Ctx, leads: list[dict], campaign: str | None = None) -> dict[str, dict]:
    """lead_id -> {app_login, app_surfaces, lever, talking_points, ask} for these leads. `campaign` = the list being
    worked (new / low_margin / ...), which decides the ask; else each client's own list."""
    list_campaign = campaign
    from .sarthi import target

    now = ctx.now()
    since = iso(now - timedelta(days=7))
    goal = int(target(ctx))
    trades = last_trades(ctx, [l["lead_id"] for l in leads])
    out = {}
    for lead in leads:
        lid = lead["lead_id"]
        evs = [(_base(r["event_name"]), parse_dt(r["event_time"], ctx.tz))
               for r in rows(ctx.conn, "SELECT event_name, event_time FROM events WHERE user_id IN (?,?) AND event_time>=? "
                                       "ORDER BY event_time", (lid, lead.get("we_user_id") or lid, since))]
        evs = [(n, t) for n, t in evs if t]
        extra = lead.get("extra")
        extra = json.loads(extra) if isinstance(extra, str) and extra else (extra or {})
        campaign = list_campaign or lead.get("campaign") or ""
        have = extra.get("current_margin")
        try:
            have = float(have) if have not in (None, "") else None
        except (TypeError, ValueError):
            have = None

        # app login
        if evs:
            last = evs[-1][1]
            days = len({t.date() for _, t in evs})
            login = f"Last login {_day(ctx, last)} {last:%H:%M} · active {days} of last 7 days"
        else:
            login = "No app login in the last 7 days"

        # surfaces, most used first
        counts = {}
        for n, _ in evs:
            s = next((label for label, keys in SURFACES if any(n.startswith(k) for k in keys)), None)
            if s:
                counts[s] = counts.get(s, 0) + 1
        top = sorted(counts, key=lambda s: -counts[s])[:3]
        surfaces = " · ".join(f"{s} ({counts[s]}x)" for s in top) or "—"

        # the lever: friction first, else the last trade
        def last_of(keys):
            hits = [t for n, t in evs if any(n.startswith(k) for k in keys)]
            return (hits[-1], len(hits)) if hits else (None, 0)

        funds_t, _ = last_of(ADD_FUNDS)
        placed_t, _ = last_of(("ORDER_PLACED",))
        tries = [t for n, t in evs if any(n.startswith(k) for k in ORDER_TRIED) and (not placed_t or t > placed_t)]
        tried_t, tried_n = (tries[-1], len(tries)) if tries else (None, 0)  # order windows since the last order placed
        help_t, _ = last_of(HELP)
        login_fail_t, _ = last_of(("LOGIN_FAILED",))
        trade = trades.get(lid)
        points = []
        if funds_t and campaign in ("new", "low_margin"):
            lever = f"Friction: tried to add funds {_day(ctx, funds_t)} {funds_t:%H:%M}" + (", money not received" if campaign == "new" else "")
            points.append(f"Aapne {_day_hi(ctx, funds_t)} funds add karne ki koshish ki thi. Payment mein koi dikkat aayi? "
                          "Main abhi UPI se saath mein karwa deta hoon.")
        elif tried_t:
            lever = (f"Friction: opened the order window {tried_n}x without placing" + (" since their last order" if placed_t else "")
                     + f" (last {_day(ctx, tried_t)} {tried_t:%H:%M})")
            points.append("Aap order lagane gaye the par order place nahi hua. Kya margin kam pad raha tha? "
                          "Funds add karke wahi trade aaram se le sakte hain.")
        elif login_fail_t:
            lever = f"Friction: login failed {_day(ctx, login_fail_t)} {login_fail_t:%H:%M}"
            points.append("App mein login mein dikkat aayi thi. Main abhi login karwa deta hoon.")
        elif help_t:
            lever = f"Asked for help in the app {_day(ctx, help_t)} {help_t:%H:%M}"
            points.append("Aapne app mein help ke liye tap kiya tha. Bataiye, kis cheez mein madad chahiye?")
        elif trade:
            lever = f"Last trade: {_scrip_words(trade['scrip'])} ({_day(ctx, trade['date']) if trade['date'] else ''})"
            points.append(f"Aapne {_day_hi(ctx, trade['date']) if trade['date'] else 'pichhli baar'} {_scrip_words(trade['scrip'])} mein trade kiya tha. "
                          "Kaisa raha? Agle trade ke liye margin ready rakhein.")
        else:
            lever = "—"
        if len(points) < 2:  # a second line from what they use most
            if "Sarthi ideas" in top:
                points.append("Aap Sarthi ke ideas dekh rahe hain. Kaunsa idea pasand aaya? Margin ho toh ideas turant le sakte hain.")
            elif "Search" in top or "Stock pages" in top:
                points.append("Aap app pe stocks dekh rahe hain. Kis type ke trade mein interest hai, short term ya long term?")
            elif "Option chain" in top:
                points.append("Aap option chain dekh rahe hain. F&O ke liye margin ready ho toh Sarthi ke F&O ideas bhi milte hain.")
            elif not evs:
                points.append("Account khul gaya hai. App mein login ya pehla trade karne mein koi madad chahiye?")
            elif not points:
                points.append("Aap app regularly khol rahe hain. Trading ke liye kya plan hai? Main Sarthi ke ready ideas dikha deta hoon.")
        if campaign == "low_margin" and have is not None and 0 < have < goal:
            ask = f"Top up ₹{goal - have:,.0f} to reach ₹{goal:,} (has ₹{have:,.0f})"
        elif campaign == "low_margin":
            ask = f"Top up to ₹{goal:,}"
        elif campaign == "new":
            ask = f"Add ₹{goal:,} to start (₹10,000 today is fine, book the rest as a follow-up)"
        else:
            ask = ""
        out[lid] = {"app_login": login, "app_surfaces": surfaces, "lever": lever,
                    "talking_points": "\n".join(f"{i}. {p}" for i, p in enumerate(points[:2], 1)), "ask": ask}
    return out
