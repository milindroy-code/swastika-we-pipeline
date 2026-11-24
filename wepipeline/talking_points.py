"""One talking point for a GM: the client's most useful recent action, with the time and, where known, the stock.

Per client (last 7 days of app activity, executed orders, and the Sarthi ideas live at the time):
    App login          last time in the app, and on how many of the last 7 days
    App surfaces       the parts of the app they used most (Sarthi, Search, Stock pages, Option chain, ...)
    Last key activity  the most important thing they did (key_activity: deposit attempt > Sarthi idea accepted > order
                       window left without an order > trade > login trouble > help > Sarthi ideas viewed > option chain >
                       stocks > login), the most important in the last 72 h, else in 7 days
    Talking point      one line to open the call with, built from it; plus the ask
WebEngage's activity feed has no event details: the stock of a trade comes from the executed orders, and a Sarthi idea
is the one the research desk had live at that time (the app doesn't say which idea was opened). Never names a stock to
buy or promises returns.
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
    if len(p) >= 3 and len(p[0]) == 2 and p[0].isupper() and ("CE" in p or "PE" in p):  # any option: EO / IO / CO / NC ...
        i = p.index("CE") if "CE" in p else p.index("PE")
        if i + 1 < len(p):
            strike = p[-1] if p[-1].replace(".", "").isdigit() else ""
            return " ".join(x for x in (p[i + 1], strike, p[i], "option") if x)
    if len(p) >= 2 and len(p[0]) == 2 and p[0].isupper() and p[0].endswith("F"):  # any future: EF / IF / CF ...
        return f"{p[1]} future"
    return scrip


def talking_points(ctx: Ctx, leads: list[dict], campaign: str | None = None) -> dict[str, dict]:
    """lead_id -> {app_login, app_surfaces, key_activity, talking_point, ask} for these leads. `campaign` = the list being
    worked (new / low_margin / ...), which decides the ask; else each client's own list."""
    list_campaign = campaign
    from .sarthi import target

    now = ctx.now()
    since = iso(now - timedelta(days=7))
    goal = int(target(ctx))
    from .orders import last_orders, sarthi_ideas

    orders = last_orders(ctx, [l["lead_id"] for l in leads])
    ideas = sarthi_ideas(ctx)
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

        key, point = key_activity(ctx, lid, evs, campaign, orders.get(lid.upper()), ideas)
        if campaign == "low_margin" and have is not None and 0 < have < goal:
            ask = f"Top up ₹{goal - have:,.0f} to reach ₹{goal:,} (has ₹{have:,.0f})"
        elif campaign == "low_margin":
            ask = f"Top up to ₹{goal:,}"
        elif campaign == "new":
            ask = f"₹{goal:,} se shuruaat karein (aaj ₹10,000 bhi chalega, baaki ki date callback mein set karein)"
        else:
            ask = ""
        out[lid] = {"app_login": login, "app_surfaces": surfaces, "key_activity": key, "talking_point": point, "ask": ask}
    return out


# The client's most useful recent action, most important first. The talking point is built from the most important one done
# in the last `fresh_hours`; if none, the most important in 7 days.
DEPOSIT, SARTHI_TAKEN, ORDER_LEFT, TRADED, LOGIN_FAIL, HELP_TAP, SARTHI_SEEN, OPTIONS, STOCKS, LOGIN = range(10)


def key_activity(ctx: Ctx, lid: str, evs: list, campaign: str, order: dict | None, ideas: list) -> tuple[str, str]:
    """(what the client last did that matters, for the GM; one line to open the call with) from 7 days of app events,
    their last executed order and the Sarthi ideas live at the time."""
    from .orders import idea_live_at

    now = ctx.now()
    at = lambda t: f"{_day(ctx, t)} {t:%H:%M}"  # noqa: E731
    hi = lambda t: f"{_day_hi(ctx, t)} {t:%H:%M} baje"  # noqa: E731
    c = []  # (rank, time, fact, line)

    def last(keys, pick=None):
        hits = [t for n, t in evs if any(n.startswith(k) for k in keys) and (pick is None or pick(t))]
        return hits[-1] if hits else None

    if t := last(ADD_FUNDS):
        c.append((DEPOSIT, t, f"Tried to add funds {at(t)}" + (" (no money received yet)" if campaign == "new" else ""),
                  f"Aapne {hi(t)} funds add karne ki koshish ki thi. Payment mein koi dikkat aayi? Main abhi UPI se saath mein karwa deta hoon."))
    if t := last(("CALL_ACCEPTED_MOB",)):
        idea = idea_live_at(ideas, t)
        what = f" ({idea['idea']}, {idea['segment']})" if idea else ""
        c.append((SARTHI_TAKEN, t, f"Accepted a Sarthi idea {at(t)}" + (f"; idea live then: {idea['idea']} ({idea['segment']})" if idea else ""),
                  f"Aapne {hi(t)} Sarthi ka idea accept kiya tha{what}. "
                  + ("Account mein funds add karke aise ideas turant le sakte hain. Abhi UPI se karwa doon?" if campaign == "new"
                     else "Trade le paaye? Margin ready ho toh agla idea turant le sakte hain.")))
    placed = max([x for x in [last(("ORDER_PLACED",)), order["at"] if order else None] if x], default=None)
    tries = [t for n, t in evs if any(n.startswith(k) for k in ORDER_TRIED) and (not placed or t > placed)]
    if tries:
        t = tries[-1]
        c.append((ORDER_LEFT, t, f"Opened the order window {len(tries)}x without placing an order (last {at(t)})",
                  f"Aap {hi(t)} order lagane gaye the par order place nahi hua. Kya margin kam pad raha tha? Funds add karke wahi trade le sakte hain."))
    if order:
        t, side = order["at"], "Bought" if order["side"] == "B" else "Sold"
        c.append((TRADED, t, f"{side} {order['qty']:g} {order['symbol']} {at(t)}",
                  f"Aapne {hi(t)} {order['symbol']} {'kharida' if order['side'] == 'B' else 'becha'} tha. "
                  + ("Agle trade ke liye account mein funds add kar lijiye. Abhi UPI se karwa doon?" if campaign == "new"
                     else "Kaisa raha? Agle trade ke liye margin ready rakhein.")))
    elif t := last(("ORDER_PLACED",)):
        c.append((TRADED, t, f"Placed an order in the app {at(t)}",
                  f"Aapne {hi(t)} app se order lagaya tha. Kaisa raha? Agle trade ke liye margin ready rakhein."))
    if t := last(("LOGIN_FAILED",)):
        c.append((LOGIN_FAIL, t, f"Login failed {at(t)}", f"Aapko {hi(t)} app login mein dikkat aayi thi. Main abhi login karwa deta hoon."))
    if t := last(HELP):
        c.append((HELP_TAP, t, f"Tapped for help in the app {at(t)}", f"Aapne {hi(t)} app mein help ke liye tap kiya tha. Bataiye, kis cheez mein madad chahiye?"))
    if t := last(("RECOMMENDATION_",)):
        idea = idea_live_at(ideas, t)
        c.append((SARTHI_SEEN, t, f"Checked Sarthi recommendations {at(t)}" + (f"; idea live then: {idea['idea']} ({idea['segment']})" if idea else ""),
                  f"Aap {hi(t)} Sarthi ke ideas dekh rahe the" + (f", jaise {idea['idea']} ({idea['segment']})" if idea else "")
                  + ". Kaunsa idea pasand aaya? Margin ho toh turant le sakte hain."))
    if t := last(("OPTION_CHAIN_SCREEN_OPENED",)):
        c.append((OPTIONS, t, f"Checked the option chain (F&O) {at(t)}",
                  f"Aap {hi(t)} option chain dekh rahe the. F&O mein trade karna hai? Sarthi ke F&O ideas bhi milte hain."))
    if t := last(("SEARCH_SCREEN_OPENED", "QUOTES_OPENED", "OVERVIEW_OPENED", "CHARTS_", "ADDED_TO_WATCHLIST")):
        c.append((STOCKS, t, f"Searched / looked at stocks {at(t)}",
                  f"Aap {hi(t)} app pe stocks dekh rahe the. Kis stock ya type ke trade mein interest hai?"))
    if evs:
        t = evs[-1][1]
        c.append((LOGIN, t, f"Opened the app {at(t)}", f"Aap {hi(t)} app mein aaye the. Trading shuru karne mein koi madad chahiye?"))
    if not c:
        return "No app activity in the last 7 days", (
            "Aapke account mein funds hain, par kuch din se app use nahi hua. Agla trade karne mein koi madad chahiye?"
            if campaign == "low_margin" else "Account khul gaya hai. App mein login ya pehla trade karne mein koi madad chahiye?")
    real = [x for x in c if x[0] != LOGIN]  # something they did beats just opening the app
    pool = real or c
    fresh = [x for x in pool if x[1] >= now - timedelta(hours=72)]
    best = min(fresh or pool, key=lambda x: (x[0], -x[1].timestamp()))
    return best[2], best[3]
