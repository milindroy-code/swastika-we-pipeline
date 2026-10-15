"""Sarthi activation drives: clients with Sarthi switched off are the target list; their engagement says who to call
first. Run from its own dashboard (sarthi_app.py) on the shared drive engine (drives.py).

Target list (Sarthi off in the client database, account open), from:
  new clients  everyone registered within the window (Admin: rules), the Growth team's focus: +boost, handed out first
  traders      the most-active-clients trade query joined to the Sarthi flag (Low activity and up by default)
  app users    clients active in the app (WebEngage uploads) whose Sarthi flag is off
  campaigns    clients who engaged with a Sarthi channel campaign (WhatsApp / SMS / email / push uploads)
Score (0-100, same 🔥 / 🌤️ / ❄️ tiers): the strongest of three tracks plus a quarter of the other two:
  activity     trading volume / consistency / recency, plus app heat
  interest     Sarthi itself: opened Sarthi ideas / accepted an idea without the switch on (fresher counts more)
  engagement   Sarthi channel-campaign engagement
Success: Sarthi switched on (SarthiActivationDate in the client database) after the campaign started, credited to the GM
who called that client before it was switched on. A GM can also mark "activated on the call".
"""
from __future__ import annotations

import json
import logging
import math
from datetime import datetime, timedelta

import pandas as pd

from . import drives
from .context import Ctx, iso, norm_client_id, parse_dt
from .db import one, rows
from .drives import col as _col, connection_status, read_file  # noqa: F401  (re-exported)

log = logging.getLogger(__name__)

CAMPAIGN = "sarthi"
SOURCE = "Sarthi activation drive"
TRUTHY = {"1", "true", "y", "yes"}

# The most-active-clients trade query (as for referrals) joined to the Sarthi flag on the same server. Read-only.
# Override with sources.client_db.sarthi_pool_query.
POOL_SQL = """
WITH AllTrades AS
(
    SELECT CLIENT_ID, TRADE_DATE FROM [192.168.0.18].[Capsfo].[pms].[CLIENT_FIFO_Process]
    WHERE TRADE_DATE >= DATEADD(DAY, -{days}, GETDATE())
    UNION ALL
    SELECT CLIENT_ID, TRADE_DATE FROM [192.168.0.18].[Capsfo].[pms].[FO_CLIENT_FIFO_Process]
    WHERE TRADE_DATE >= DATEADD(DAY, -{days}, GETDATE())
),
ClientActivity AS
(
    SELECT CLIENT_ID, COUNT(*) AS TotalTrades, COUNT(DISTINCT CAST(TRADE_DATE AS DATE)) AS ActiveTradingDays,
           MIN(TRADE_DATE) AS FirstTradeInPeriod, MAX(TRADE_DATE) AS LastTradeDate
    FROM AllTrades GROUP BY CLIENT_ID
)
SELECT A.CLIENT_ID, A.TotalTrades, A.ActiveTradingDays, A.FirstTradeInPeriod, A.LastTradeDate,
       CASE WHEN A.TotalTrades >= 100 THEN 'Very High' WHEN A.TotalTrades >= 50 THEN 'High' WHEN A.TotalTrades >= 20 THEN 'Medium'
            WHEN A.TotalTrades >= 5 THEN 'Low' ELSE 'Very Low' END AS ActivityBucket,
       C.IsSarthiTIOn, C.SarthiActivationDate, C.PreferredSegment, C.IsAccountClosed, C.REGISTRATION_DATE
FROM ClientActivity A
JOIN [Repl_TargetReview].[dbo].[tbl_ClientNew] C ON C.CLIENT_ID = A.CLIENT_ID
ORDER BY A.TotalTrades DESC
"""

# New clients: everyone registered within the window, with deposit / first trade (the Growth team's focus).
# Override with sources.client_db.sarthi_new_query.
NEW_SQL = """
SELECT C.CLIENT_ID, C.REGISTRATION_DATE, C.IsSarthiTIOn, C.SarthiActivationDate, C.PreferredSegment, C.IsAccountClosed,
       F.FirstDeposit, F.FTD
FROM [Repl_TargetReview].[dbo].[tbl_ClientNew] C
LEFT JOIN [Repl_TargetReview].[dbo].[tbl_ClientFtdLtd] F ON F.CLIENT_ID = C.CLIENT_ID
WHERE C.REGISTRATION_DATE >= DATEADD(DAY, -{days}, GETDATE())
"""

# Sarthi switched on recently, for every client: how campaign results are counted. Override: sarthi_activations_query.
ACTIVATIONS_SQL = """
SELECT CLIENT_ID, IsSarthiTIOn, SarthiActivationDate
FROM [Repl_TargetReview].[dbo].[tbl_ClientNew]
WHERE SarthiActivationDate >= DATEADD(DAY, -{days}, GETDATE())
"""

STATUS_SQL = """
SELECT CLIENT_ID, IsSarthiTIOn, SarthiActivationDate, PreferredSegment, IsAccountClosed, REGISTRATION_DATE
FROM [Repl_TargetReview].[dbo].[tbl_ClientNew] WHERE CLIENT_ID IN ({ids})
"""

SCORE_DEFAULTS = {**drives.TRADING_DEFAULTS,
                  "interest_scale": 8,          # Sarthi interest = 100 * (1 - e^(-points / scale))
                  "interest_half_life_days": 7,
                  "accepted_weight": 6,         # accepted a Sarthi idea (but can't receive them: Sarthi is off)
                  "viewed_weight": 2}           # opened Sarthi ideas / Sarthi home


def settings(ctx: Ctx) -> dict:
    cfg = dict(ctx.cfg.get("sarthi_activation") or {})
    return {
        "buckets": [str(b).strip().lower() for b in cfg.get("trade_buckets", ["Very High", "High", "Medium", "Low"])],
        "days": int(cfg.get("days", 60)),
        "min_app_heat": float(cfg.get("min_app_heat", 20)),
        "daily_per_gm": int(ctx.rules.get("sarthi_daily_per_gm", cfg.get("daily_per_gm", 10))),
        "cooldown_days": int(ctx.rules.get("sarthi_cooldown_days", cfg.get("cooldown_days", 30))),
        "call_from": str(cfg.get("call_from", "10:30")),
        "refresh_hours": float(cfg.get("refresh_hours", 24)),
        "max_attempts": int(cfg.get("max_attempts", 3)),
        "results_days": int(cfg.get("results_days", 120)),
        # client types: new = registered within the Growth team's window (Admin: rules), resurrected = an older client whose
        # trading restarted recently (first trade of the window within `comeback_days`) or a dormant / re-activation client
        "new_days": int(ctx.rules.get("registration_window_days") or ctx.cfg.get("registration_window_days") or 30),
        "comeback_days": int(cfg.get("comeback_days", 30)),
        "new_boost": float(cfg.get("new_client_boost", 15)),
        "score": {**SCORE_DEFAULTS, **(cfg.get("score") or {})},
        "engagement": {**drives.ENGAGEMENT_DEFAULTS, **(cfg.get("engagement") or {})},
    }


def is_on(value) -> bool:
    v = str(value if value is not None else "").strip().lower()
    if v in TRUTHY:
        return True
    try:
        return float(v) == 1  # 1.0 when a column with blanks comes back as numbers
    except ValueError:
        return False


# ---------------------------------------------------------------- scoring
SIGNALS = {"accepted": ("CALL_ACCEPTED_MOB",), "viewed": ("RECOMMENDATION_LISTING_OPENED_MOB", "RECOMMENDATION_LANDING_SCREEN_MOB")}


def interest(ctx: Ctx, user_id: str | None) -> tuple[float, str]:
    """0-100 from Sarthi use in the app without the switch on: accepted ideas weigh most, then opening Sarthi ideas."""
    if not user_id:
        return 0.0, ""
    w = settings(ctx)["score"]
    pts, counts = 0.0, {"accepted": 0, "viewed": 0}
    since = iso(ctx.now() - timedelta(days=30))
    for ev in rows(ctx.conn, "SELECT event_name, event_time, attrs FROM events WHERE user_id=? AND event_time>=?", (user_id, since)):
        base = ev["event_name"].split(" · ")[0]
        kind = next((k for k, names in SIGNALS.items() if base in names), None)
        if not kind:
            continue
        n = json.loads(ev["attrs"] or "{}").get("count", 1)
        n = int(n) if str(n).isdigit() else 1
        age = max((ctx.now() - parse_dt(ev["event_time"], ctx.tz)).total_seconds() / 86400, 0)
        pts += w[f"{kind}_weight"] * n * 0.5 ** (age / w["interest_half_life_days"])
        counts[kind] += n
    if not pts:
        return 0.0, ""
    value = round(100 * (1 - math.exp(-pts / w["interest_scale"])), 1)
    bits = ([f"accepted {counts['accepted']} Sarthi idea(s)"] if counts["accepted"] else []) + \
           ([f"opened Sarthi ideas {counts['viewed']}x"] if counts["viewed"] else [])
    return value, " and ".join(bits) + " in 30 days, with Sarthi off"


def sarthi_score(ctx: Ctx, extra: dict, app_heat: float = 0, user_id: str | None = None) -> tuple[float, str]:
    w = settings(ctx)["score"]
    trading, why_trading = drives.trading_track(ctx, extra.get("sarthi") or {}, w)
    activity = min(trading + min(max(app_heat, 0), 100) / 100 * w["app_points"], 100)
    keen, why_keen = interest(ctx, user_id)
    engaged, why_engaged = drives.engagement(ctx, CAMPAIGN, user_id) if user_id else (0.0, "")
    score = drives.combine([activity, keen, engaged], w["others_share"])
    s = settings(ctx)
    a = extra.get("sarthi") or {}
    fresh = extra.get("sarthi_new") or {}
    reg = parse_dt(a.get("registered") or fresh.get("registered") or extra.get("registration_date"), ctx.tz)
    new = bool(reg and (ctx.now().date() - reg.date()).days <= s["new_days"])
    if new:  # focus on new clients: joined within the window
        score = round(min(score + s["new_boost"], 100), 1)
    if new and fresh.get("first_deposit"):
        score = round(min(score + 5, 100), 1)  # funded new client: ready to use Sarthi's ideas straight away
    bits = [b for b in ((f"new client (joined {reg:%d %b}" + (f", deposited ₹{fresh['first_deposit']:,.0f}" if fresh.get("first_deposit") else "")
                         + ")" if new else ""), why_keen, why_trading,
                        f"app heat {app_heat:.0f}" if app_heat >= 1 else "", why_engaged) if b]
    return score, "Sarthi score: " + ("; ".join(bits) or "Sarthi off, little engagement yet")


def info(lead: dict) -> dict:
    return drives.info(lead, CAMPAIGN)


# ---------------------------------------------------------------- the pool
def _status(r: dict, c: dict) -> dict:
    return {"sarthi_on": is_on(r.get(c["on"])) if c.get("on") else False,
            "activated_on": r.get(c["date"]) if c.get("date") else None,
            "closed": is_on(r.get(c["closed"])) if c.get("closed") else False,
            "segment": str(r.get(c["seg"]) or "").strip() if c.get("seg") else "",
            "registered": r.get(c["reg"]) if c.get("reg") else None}


CLIENT_TYPES = {"new": "🆕 New (joined recently)", "active": "📈 Active", "resurrected": "♻️ Resurrected (came back)"}


def client_type(ctx: Ctx, lead: dict) -> str:
    """new: registered within the window · resurrected: older and trading again only recently, or dormant / on the
    re-activation list · active: everyone else."""
    s = settings(ctx)
    extra = json.loads(lead.get("extra") or "{}")
    a = extra.get("sarthi") or {}
    reg = parse_dt(a.get("registered") or (extra.get("sarthi_new") or {}).get("registered") or extra.get("registration_date"), ctx.tz)
    if reg and (ctx.now().date() - reg.date()).days <= s["new_days"]:
        return "new"
    first = parse_dt(a.get("first_trade"), ctx.tz)
    if first and (ctx.now().date() - first.date()).days <= s["comeback_days"] and int(a.get("window_days") or 60) > s["comeback_days"]:
        return "resurrected"  # no trade in the earlier part of the window: they came back
    if (lead.get("category") or "").startswith("Traded - dormant") or lead.get("campaign") == "reactivation":
        return "resurrected"
    return "active"


def _close_if_on(ctx: Ctx, cid: str, when) -> bool:
    """Sarthi is on: keep the client's flag current, and a pool client leaves the list as 'activated'."""
    lead = one(ctx.conn, "SELECT extra FROM leads WHERE lead_id=?", (cid,))
    if lead:
        extra = json.loads(lead["extra"] or "{}")
        if not extra.get("sarthi_active"):
            extra.update(sarthi_active=True, sarthi_activated_on=iso(parse_dt(when, ctx.tz)) if when else extra.get("sarthi_activated_on"))
            ctx.conn.execute("UPDATE leads SET extra=? WHERE lead_id=?", (json.dumps(extra), cid))
    return drives.close_member(ctx, CAMPAIGN, cid, "activated")


def load_traders(ctx: Ctx, df: pd.DataFrame, source: str = "client DB") -> dict:
    """The trade query joined to the Sarthi flag: traders in the chosen buckets with Sarthi off join the pool."""
    from .referral import trade_data

    s = settings(ctx)
    id_col = _col(df, "CLIENT_ID", "ClientCode", "Client Code")
    if not id_col:
        raise ValueError("The file needs a CLIENT_ID column (the Sarthi pool query's output).")
    c = {k: _col(df, *names) for k, names in {
        "trades": ("TotalTrades", "Trades"), "days": ("ActiveTradingDays", "ActiveDays"), "first": ("FirstTradeInPeriod",),
        "last": ("LastTradeDate", "LastTrade"), "bucket": ("ActivityBucket", "Bucket"),
        "on": ("IsSarthiTIOn", "SarthiTIOn", "SarthiOn", "Sarthi"), "date": ("SarthiActivationDate",),
        "closed": ("IsAccountClosed",), "seg": ("PreferredSegment",), "reg": ("REGISTRATION_DATE", "RegistrationDate"),
        "name": ("CLIENT_NAME", "ClientName", "Name"), "phone": ("MOBILE", "MOBILE_NO", "MobileNo", "Phone"),
    }.items()}
    if not c["on"]:
        raise ValueError("The file needs the Sarthi flag (IsSarthiTIOn) so only clients with Sarthi off are targeted.")
    out = {"rows": len(df), "sarthi_off": 0, "eligible": 0, "now_on": 0, "added": 0, "reopened": 0, "updated": 0, "busy": 0,
           "asked_recently": 0, "skipped": 0}
    eligible = set()
    for r in df.to_dict("records"):
        cid = norm_client_id(r.get(id_col))
        st = _status(r, c)
        if not cid or st["closed"]:
            continue
        if st["sarthi_on"]:
            out["now_on"] += _close_if_on(ctx, cid, st["activated_on"])
            continue
        out["sarthi_off"] += 1
        data = trade_data(ctx, r, c, s["days"], source)
        if data["bucket"].lower() not in s["buckets"]:
            continue
        out["eligible"] += 1
        eligible.add(cid)
        data.update(sarthi_on=False, segment=st["segment"], registered=iso(parse_dt(st["registered"], ctx.tz)))
        drives.tally(out, drives.upsert(ctx, CAMPAIGN, cid, "sarthi", data,
                                        str(r.get(c["name"]) or "").strip() if c["name"] else "",
                                        str(r.get(c["phone"]) or "").strip() if c["phone"] else "", "Traded - active (last 30 days)"))
    _mark_off(ctx, eligible)
    out["dropped"] = drives.drop_stale(ctx, CAMPAIGN, eligible | _app_keepers(ctx))
    ctx.conn.commit()
    return out


def _mark_off(ctx: Ctx, ids: set[str]) -> None:
    for cid in ids:  # the client DB says off: keep the flag current for the pitch
        lead = one(ctx.conn, "SELECT extra FROM leads WHERE lead_id=?", (cid,))
        if lead:
            extra = json.loads(lead["extra"] or "{}")
            if extra.get("sarthi_active") is not False:
                extra["sarthi_active"] = False
                ctx.conn.execute("UPDATE leads SET extra=? WHERE lead_id=?", (json.dumps(extra), cid))


def _app_keepers(ctx: Ctx) -> set[str]:
    return {r["lead_id"] for r in rows(ctx.conn, "SELECT m.lead_id, l.extra FROM drive_members m JOIN leads l ON l.lead_id=m.lead_id "
                                                 "WHERE m.program=? AND m.status='open'", (CAMPAIGN,))
            if (json.loads(r["extra"] or "{}").get("sarthi") or {}).get("via") == "app"}


def load_app_users(ctx: Ctx, status: pd.DataFrame | None = None) -> dict:
    """Clients active in the app (last 30 days of WebEngage uploads) with Sarthi off. The Sarthi flag comes from the
    client database when reachable (`status`: CLIENT_ID, IsSarthiTIOn...), else from what the client DB last told us."""
    from .activity import lead_user_ids
    from .heat import contributions, score as app_score

    s = settings(ctx)
    since = iso(ctx.now() - timedelta(days=30))
    users = sorted({norm_client_id(r["user_id"]) for r in rows(ctx.conn, "SELECT DISTINCT user_id FROM events WHERE event_time>=?", (since,))}
                   - {""})
    known = {}
    if status is not None and not status.empty:
        c = {k: _col(status, *n) for k, n in {"id": ("CLIENT_ID",), "on": ("IsSarthiTIOn",), "date": ("SarthiActivationDate",),
                                              "closed": ("IsAccountClosed",), "seg": ("PreferredSegment",),
                                              "reg": ("REGISTRATION_DATE",)}.items()}
        known = {norm_client_id(r.get(c["id"])): _status(r, c) for r in status.to_dict("records")}
    out = {"app_users": len(users), "sarthi_off": 0, "eligible": 0, "now_on": 0, "unknown": 0, "added": 0, "reopened": 0,
           "updated": 0, "busy": 0, "asked_recently": 0, "skipped": 0}
    for cid in users:
        st = known.get(cid)
        if st is None:
            lead = one(ctx.conn, "SELECT extra FROM leads WHERE lead_id=?", (cid,))
            flag = json.loads(lead["extra"] or "{}").get("sarthi_active") if lead else None
            if flag is None:
                out["unknown"] += 1  # not looked up yet: the next pull from the client database decides
                continue
            st = {"sarthi_on": bool(flag), "activated_on": None, "closed": False, "segment": "", "registered": None}
        if st["closed"]:
            continue
        if st["sarthi_on"]:
            out["now_on"] += _close_if_on(ctx, cid, st["activated_on"])
            continue
        out["sarthi_off"] += 1
        app, _ = app_score(ctx, contributions(ctx, lead_user_ids(ctx, {"lead_id": cid, "we_user_id": cid})))
        if app < s["min_app_heat"] and not interest(ctx, cid)[0]:
            continue  # barely active and never looked at Sarthi: not worth a call yet
        out["eligible"] += 1
        drives.tally(out, drives.upsert(ctx, CAMPAIGN, cid, "sarthi", {"via": "app", "sarthi_on": False, "segment": st["segment"],
                                                                        "registered": iso(parse_dt(st["registered"], ctx.tz)),
                                                                        "pulled_at": iso(ctx.now())}))
    ctx.conn.commit()
    return out


def load_new(ctx: Ctx, df: pd.DataFrame, source: str = "client DB") -> dict:
    """Every client registered within the window with Sarthi off joins the list (new clients first). Those on a funding
    list are left to that call, which already asks for Sarthi; they're counted as 'on funding lists'."""
    c = {k: _col(df, *n) for k, n in {"id": ("CLIENT_ID", "ClientCode"), "on": ("IsSarthiTIOn",), "date": ("SarthiActivationDate",),
                                      "closed": ("IsAccountClosed",), "seg": ("PreferredSegment",), "reg": ("REGISTRATION_DATE",),
                                      "dep": ("FirstDeposit",), "ftd": ("FTD",)}.items()}
    if not c["id"] or not c["reg"]:
        raise ValueError("The file needs CLIENT_ID and REGISTRATION_DATE (the new-clients query's output).")
    out = {"rows": len(df), "sarthi_off": 0, "now_on": 0, "added": 0, "reopened": 0, "updated": 0, "busy": 0, "asked_recently": 0,
           "skipped": 0}
    for r in df.to_dict("records"):
        cid = norm_client_id(r.get(c["id"]))
        st = _status(r, c)
        if not cid or st["closed"]:
            continue
        if st["sarthi_on"]:
            out["now_on"] += _close_if_on(ctx, cid, st["activated_on"])
            continue
        out["sarthi_off"] += 1
        dep = float(r.get(c["dep"]) or 0) if c["dep"] and pd.notna(r.get(c["dep"])) else 0
        data = {"via": "new", "sarthi_on": False, "segment": st["segment"], "registered": iso(parse_dt(st["registered"], ctx.tz)),
                "first_deposit": dep or None, "first_trade": iso(parse_dt(r.get(c["ftd"]), ctx.tz)) if c["ftd"] else None,
                "pulled_at": iso(ctx.now())}
        drives.tally(out, drives.upsert(ctx, CAMPAIGN, cid, "sarthi_new", data, category="New client"))
    ctx.conn.commit()
    return out


def pull_new(ctx: Ctx) -> pd.DataFrame:
    src = (ctx.cfg.get("sources") or {}).get("client_db") or {}
    return drives.query(ctx, (src.get("sarthi_new_query") or NEW_SQL).format(days=settings(ctx)["new_days"]))


def app_user_status(ctx: Ctx) -> pd.DataFrame:
    """The Sarthi flag for every recent app user, from the client database (chunks under the 2,100-parameter limit)."""
    since = iso(ctx.now() - timedelta(days=30))
    ids = sorted({norm_client_id(r["user_id"]) for r in rows(ctx.conn, "SELECT DISTINCT user_id FROM events WHERE event_time>=?", (since,))}
                 - {""})
    src = (ctx.cfg.get("sources") or {}).get("client_db") or {}
    return drives.query_ids(ctx, src.get("sarthi_status_query") or STATUS_SQL, ids)


def load_activations(ctx: Ctx, df: pd.DataFrame) -> dict:
    """Sarthi switched on (client DB): stored for the campaign report; pool clients who switched on leave the pool."""
    c = {k: _col(df, *n) for k, n in {"id": ("CLIENT_ID", "ClientCode"), "on": ("IsSarthiTIOn",), "date": ("SarthiActivationDate",
                                                                                                          "ActivationDate")}.items()}
    if not c["id"] or not c["date"]:
        raise ValueError("The file needs CLIENT_ID and SarthiActivationDate.")
    out = {"rows": len(df), "stored": 0, "closed_in_pool": 0}
    for r in df.to_dict("records"):
        cid, when = norm_client_id(r.get(c["id"])), parse_dt(r.get(c["date"]), ctx.tz)
        if not cid or not when or (c["on"] and not is_on(r.get(c["on"]))):
            continue
        ctx.conn.execute("""INSERT INTO sarthi_activations (client_id, activated_at, updated_at) VALUES (?,?,?)
                            ON CONFLICT (client_id) DO UPDATE SET activated_at=excluded.activated_at, updated_at=excluded.updated_at""",
                         (cid, iso(when), iso(ctx.now())))
        out["stored"] += 1
        out["closed_in_pool"] += _close_if_on(ctx, cid, when)
    ctx.conn.commit()
    return out


def load_engagement(ctx: Ctx, df: pd.DataFrame, campaign: str = "", channel: str = "", source: str = "") -> dict:
    return drives.load_engagement(ctx, CAMPAIGN, df, campaign, channel, source)


def channel_report(ctx: Ctx) -> list[dict]:
    out = []
    for g in drives.channel_rows(ctx, CAMPAIGN):
        users = g.pop("user_ids")
        marks = ",".join("?" * len(users))
        g["activated"] = one(ctx.conn, f"SELECT COUNT(*) AS n FROM sarthi_activations WHERE client_id IN ({marks}) AND activated_at>=?",
                             (*users, min((r["day"] for r in rows(ctx.conn, "SELECT day FROM campaign_engagement WHERE program=? "
                                                                              "AND campaign=?", (CAMPAIGN, g["campaign"]))), default="")))["n"]
        out.append(g)
    return out


# ---------------------------------------------------------------- refresh
def pull_pool(ctx: Ctx) -> pd.DataFrame:
    src = (ctx.cfg.get("sources") or {}).get("client_db") or {}
    return drives.query(ctx, (src.get("sarthi_pool_query") or POOL_SQL).format(days=settings(ctx)["days"]))


def pull_activations(ctx: Ctx) -> pd.DataFrame:
    src = (ctx.cfg.get("sources") or {}).get("client_db") or {}
    return drives.query(ctx, (src.get("sarthi_activations_query") or ACTIVATIONS_SQL).format(days=settings(ctx)["results_days"]))


def last_refresh(ctx: Ctx) -> dict | None:
    return drives.last_refresh(ctx, CAMPAIGN)


def is_stale(ctx: Ctx) -> bool:
    return drives.is_stale(ctx, CAMPAIGN, settings(ctx)["refresh_hours"])


def refresh(ctx: Ctx, force: bool = False) -> dict:
    """Activations first (who switched on), then traders with Sarthi off, then app users with Sarthi off. Every
    `refresh_hours` (default daily); safe to call often."""
    if not force and not is_stale(ctx):
        return {"status": "fresh", "last": last_refresh(ctx)}
    conn = connection_status(ctx)
    if not conn["ok"]:
        return {"status": "unreachable", "detail": conn["detail"]}
    acts = load_activations(ctx, pull_activations(ctx))
    traders = load_traders(ctx, pull_pool(ctx))
    new = load_new(ctx, pull_new(ctx))
    app = load_app_users(ctx, app_user_status(ctx))
    done = drives.mark_refresh(ctx, CAMPAIGN, {"traders": traders["eligible"], "app_users": app["eligible"],
                                               "new_clients": new["sarthi_off"], "new_on_funding_lists": new["busy"],
                                               "activated": acts["stored"], "added": traders["added"] + app["added"] + new["added"],
                                               "dropped": traders.get("dropped", 0)})
    return {"status": "ok", "activations": acts, "traders": traders, "new": new, "app": app, "last": done}


def refresh_results(ctx: Ctx) -> dict:
    conn = connection_status(ctx)
    if not conn["ok"]:
        return {"status": "unreachable", "detail": conn["detail"]}
    res = load_activations(ctx, pull_activations(ctx))
    drives.mark_refresh(ctx, CAMPAIGN, {}, what="results")
    return {"status": "ok", **res}


# ---------------------------------------------------------------- drives, campaigns, report
def start_drive(ctx: Ctx, per_gm: int | None = None, gm_ids: list[str] | None = None, tiers: list[str] | None = None,
                start_now: bool = False, segments: list[str] | None = None, new_first: bool = True) -> dict:
    return drives.start_drive(ctx, CAMPAIGN, per_gm, gm_ids, tiers, start_now, segments, new_first)


def my_calls(ctx: Ctx, agent_id: str) -> list[dict]:
    return drives.my_calls(ctx, CAMPAIGN, agent_id)


def log_call(ctx: Ctx, lead_id: str, agent_id: str, outcome: str, notes: str = "", next_at: datetime | None = None) -> dict:
    return drives.log_call(ctx, CAMPAIGN, lead_id, agent_id, outcome, notes, next_at)


def pool(ctx: Ctx, assigned: bool | None = None) -> list[dict]:
    return drives.pool(ctx, CAMPAIGN, assigned)


def end_drive(ctx: Ctx, gm_ids: list[str] | None = None) -> int:
    return drives.end_drive(ctx, CAMPAIGN, gm_ids)


def campaigns(ctx: Ctx) -> list[dict]:
    return drives.campaigns(ctx, CAMPAIGN)


def active_campaign(ctx: Ctx) -> dict | None:
    return drives.active_campaign(ctx, CAMPAIGN)


def start_campaign(ctx: Ctx, name: str, by: str | None = None) -> dict:
    return drives.start_campaign(ctx, CAMPAIGN, name, by)


def report(ctx: Ctx, campaign_id: int) -> dict:
    """Asks and calls per GM, plus activations from the client database since the campaign started: credited to the GM
    who called the client before Sarthi was switched on; the rest switched on without a call (asked or not)."""
    b = drives.report_base(ctx, CAMPAIGN, campaign_id)
    asked = {a["lead_id"] for a in b["asks"]}
    wins = rows(ctx.conn, "SELECT a.client_id, a.activated_at, l.name, m.segment FROM sarthi_activations a "
                          "LEFT JOIN leads l ON l.lead_id=a.client_id LEFT JOIN drive_members m ON m.program='sarthi' AND m.lead_id=a.client_id "
                          "WHERE a.activated_at>=? AND a.activated_at<? "
                          "ORDER BY a.activated_at DESC", (b["lo"], b["hi"]))
    on_call = {(c["lead_id"], c["agent_id"]): c["called_at"] for c in b["calls"] if c["outcome"] == "activated"}
    for w in wins:
        w["gm"] = drives.credit(b["first_call"], w["client_id"], w["activated_at"])
        w["asked"] = w["client_id"] in asked
    # activations a GM marked on the call that the replica hasn't shown yet still count for that GM
    seen = {w["client_id"] for w in wins}
    for (lid, gm), t in on_call.items():
        if lid not in seen:
            wins.append({"client_id": lid, "activated_at": t, "name": (one(ctx.conn, "SELECT name FROM leads WHERE lead_id=?", (lid,))
                                                                      or {}).get("name"), "gm": gm, "asked": True, "pending_db": True})
    by_gm = b["by_gm"]
    for w in wins:
        if w["gm"] in by_gm:
            by_gm[w["gm"]]["wins"] += 1
    t = b["totals"]
    credited = [w for w in wins if w["gm"]]
    return {"campaign": b["campaign"], "asks": b["asks"], "last_call": b["last_call"], "wins": wins,
            "by_gm": drives.finish_by_gm(by_gm),
            "totals": {**t, "activated_on_call": t["on_call"], "wins": len(wins), "wins_credited": len(credited),
                       "wins_asked": sum(1 for w in wins if w["asked"])},
            "by_type": {ty: {"asked": sum(1 for a in b["asks"] if (a["segment"] or "active") == ty),
                             "called": len({a["lead_id"] for a in b["asks"] if (a["segment"] or "active") == ty and a["lead_id"] in b["last_call"]}),
                             "switched_on": sum(1 for w in credited if (w.get("segment") or "active") == ty)} for ty in CLIENT_TYPES}}


def summary(ctx: Ctx) -> dict:
    pool = rows(ctx.conn, "SELECT m.heat_tier, m.assigned_to, m.segment, l.extra FROM drive_members m LEFT JOIN leads l ON l.lead_id=m.lead_id "
                          "WHERE m.program=? AND m.status='open'", (CAMPAIGN,))
    free = [p for p in pool if not p["assigned_to"]]
    def via(p):
        x = json.loads(p["extra"] or "{}")
        return "app" if (x.get("sarthi") or {}).get("via") == "app" else "trades" if (x.get("sarthi") or {}).get("total_trades") \
            else "new" if x.get("sarthi_new") else "campaign"
    return {"pool": len(free), "with_gms": len(pool) - len(free),
            "pool_tiers": {t: sum(1 for p in free if (p["heat_tier"] or "cold") == t) for t in ("hot", "warm", "cold")},
            "by_source": {s: sum(1 for p in free if via(p) == s) for s in ("trades", "app", "new", "campaign")},
            "new_on_funding_lists": (last_refresh(ctx) or {}).get("new_on_funding_lists", 0),
            "by_type": {t: n for t, n in ((t, sum(1 for p in free if (p["segment"] or "active") == t)) for t in CLIENT_TYPES)},
            "activated_30d": one(ctx.conn, "SELECT COUNT(*) AS n FROM sarthi_activations WHERE activated_at>=?",
                                 (iso(ctx.now() - timedelta(days=30)),))["n"]}


PROGRAM = drives.register(drives.Program(
    key=CAMPAIGN, label="Sarthi activation", source=SOURCE, settings=settings, score=sarthi_score,
    keep=lambda extra: bool(extra.get("sarthi_campaign") or extra.get("sarthi_new")) or (extra.get("sarthi") or {}).get("via") == "app",
    notify=lambda n, start: (f"⚡ {n} Sarthi activation call(s) for you",
                             f"{n} clients with Sarthi switched off are on your Sarthi list (Sarthi dashboard), from {start:%H:%M}. "
                             "Show them what Sarthi sends and switch it on with them on the call."),
    success_outcome="activated",
    segment=client_type,
))
