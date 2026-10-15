"""Referral drives: ask our most engaged clients to refer friends. Run from its own dashboard (referral_app.py),
on the shared drive engine (drives.py): pool, campaigns, drives, channel-campaign engagement and reports.

Who gets asked, and in what order, comes from a referral score (0-100, same 🔥 / 🌤️ / ❄️ tiers as app heat), the
strongest of three tracks plus a quarter of the other two:
  activity    trading volume / consistency / recency (the most-active-clients query), plus app heat
  history     how many clients they referred and how many of those traded, fading with the last referral's age
  engagement  referral-campaign engagement on any channel
Friends' numbers are taken only with the friend's consent. Any reward quoted is the admin's (Admin: rules).
Success: clients who joined through a referral (CRM lead source 37) after the campaign started, credited to the GM who
asked that referrer before the friend joined.
"""
from __future__ import annotations

import json
import logging
import math
import re
from datetime import datetime, timedelta

import pandas as pd

from . import drives
from .context import Ctx, iso, norm_client_id, parse_dt
from .db import one, rows
from .drives import col as _col, connection_status, read_file, to_int as _int  # noqa: F401  (re-exported)

log = logging.getLogger(__name__)

CAMPAIGN = "referral"
SOURCE = "Referral drive"

# The Growth team's most-active-clients query (8 Oct 2026). Read-only; runs on the client DB server through its
# linked trade server. Override with sources.client_db.referral_query.
REFERRAL_SQL = """
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
SELECT CLIENT_ID, TotalTrades, ActiveTradingDays, FirstTradeInPeriod, LastTradeDate,
       CASE WHEN TotalTrades >= 100 THEN 'Very High' WHEN TotalTrades >= 50 THEN 'High' WHEN TotalTrades >= 20 THEN 'Medium'
            WHEN TotalTrades >= 5 THEN 'Low' ELSE 'Very Low' END AS ActivityBucket
FROM ClientActivity
ORDER BY TotalTrades DESC
"""

# The Growth team's referral-history query (8 Oct 2026): one row per client who came in through a referral
# (LeadSourceId 37), with the referring client's code in LeadSubSource (confirmed) and the referred client's first trade / deposit.
# Override with sources.client_db.referral_history_query.
HISTORY_SQL = """
WITH referred_cte AS
(
    SELECT LeadSubSource AS refree, LeadId, Client_Id, AuthorizeDate, WebEngageUniqueId
    FROM Repl_CRMLive.dbo.tbl_Lead
    WHERE LeadSourceId = 37 AND Client_Id IS NOT NULL
)
SELECT R.*, lf.FTD, lf.LTD, lf.FirstDeposit, lf.Holdings
FROM referred_cte R
LEFT JOIN Repl_TargetReview.dbo.tbl_ClientFtdLtd lf ON R.Client_Id = lf.CLIENT_ID
WHERE AuthorizeDate IS NOT NULL
ORDER BY AuthorizeDate
"""

ENGAGEMENT_DEFAULTS = drives.ENGAGEMENT_DEFAULTS
SCORE_DEFAULTS = {**drives.TRADING_DEFAULTS,
                  # referral history, its own 0-100 track
                  "referrals_points": 60, "referrals_full": 5, "active_referrals_points": 40, "active_referrals_full": 3,
                  "history_half_life_days": 365, "history_floor": 0.5}


# ---------------------------------------------------------------- settings
def reward_hi(rewards: dict) -> str:
    """The official benefit, said the same way on every call (paid only once the friend's first trade happens)."""
    you, friend = int(rewards.get("reward_referrer") or 0), int(rewards.get("reward_friend") or 0)
    if not (you or friend):
        return ""
    parts = ([f"aapko ₹{you:,}"] if you else []) + ([f"aapke dost ko ₹{friend:,}"] if friend else [])
    return " aur ".join(parts) + " milenge, jab dost app pe apna pehla trade karega (programme T&C ke hisaab se)"


def settings(ctx: Ctx) -> dict:
    cfg = dict(ctx.cfg.get("referral") or {})
    rewards = {"reward_referrer": int(ctx.rules.get("referral_reward_referrer") or cfg.get("reward_referrer") or 0),
               "reward_friend": int(ctx.rules.get("referral_reward_friend") or cfg.get("reward_friend") or 0)}
    return {
        "buckets": [str(b).strip().lower() for b in cfg.get("buckets", ["Very High", "High", "Medium"])],
        "days": int(cfg.get("days", 60)),
        "daily_per_gm": int(ctx.rules.get("referral_daily_per_gm", cfg.get("daily_per_gm", 5))),
        "cooldown_days": int(ctx.rules.get("referral_cooldown_days", cfg.get("cooldown_days", 60))),
        "call_from": str(cfg.get("call_from", "15:45")),
        "refresh_hours": float(cfg.get("refresh_hours", 24)),
        "max_attempts": int(cfg.get("max_attempts", 3)),
        "scope": str(cfg.get("scope", "all")) if str(cfg.get("scope", "all")) in SCOPES else "all",
        "score": {**SCORE_DEFAULTS, **(cfg.get("score") or {})},
        "engagement": {**ENGAGEMENT_DEFAULTS, **(cfg.get("engagement") or {})},
        **rewards,
        "reward_hi": str(cfg.get("reward_hi") or "").strip() or reward_hi(rewards),
    }


# ---------------------------------------------------------------- inputs
def pull(ctx: Ctx) -> pd.DataFrame:
    """Run the most-active-clients query on the client database (read-only)."""
    src = (ctx.cfg.get("sources") or {}).get("client_db") or {}
    return drives.query(ctx, (src.get("referral_query") or REFERRAL_SQL).format(days=settings(ctx)["days"]))


def pull_history(ctx: Ctx) -> pd.DataFrame:
    """Run the referral-history query (read-only): one row per referred client."""
    src = (ctx.cfg.get("sources") or {}).get("client_db") or {}
    return drives.query(ctx, src.get("referral_history_query") or HISTORY_SQL)


def info(lead: dict) -> dict:
    return drives.info(lead, CAMPAIGN)


stats = info  # older name


# ---------------------------------------------------------------- scoring
def action_weight(ctx: Ctx, action: str) -> float:
    return drives.action_weight(ctx, CAMPAIGN, action)


def fmt_day(ctx: Ctx, day: str | None) -> str:
    return drives.fmt_day(ctx, day)


def engagement(ctx: Ctx, user_id: str) -> tuple[float, str]:
    return drives.engagement(ctx, CAMPAIGN, user_id)


def referral_score(ctx: Ctx, extra: dict, app_heat: float = 0, user_id: str | None = None) -> tuple[float, str]:
    """0-100: the strongest of activity (trading + app), referral history and campaign engagement, plus a quarter of the
    other two. A proven referrer, a heavy trader or a client who just shared the campaign link can each be hot alone."""
    w = settings(ctx)["score"]
    hist = extra.get("referral_history") or {}
    trading, why_trading = drives.trading_track(ctx, extra.get("referral") or {}, w)
    activity = min(trading + min(max(app_heat, 0), 100) / 100 * w["app_points"], 100)
    referred, active = int(hist.get("referred") or 0), int(hist.get("active") or 0)
    history = referral_quality_score(ctx, hist)
    last_ref = parse_dt(hist.get("last_referred"), ctx.tz)
    if history and last_ref:  # a referral two years ago says less than one last month
        age = max((ctx.now() - last_ref).days, 0)
        history *= max(0.5 ** (age / w["history_half_life_days"]), w["history_floor"])
    engaged, why_engaged = engagement(ctx, user_id) if user_id else (0.0, "")
    score = drives.combine([activity, history, engaged], w["others_share"])
    bits = [why_trading] if why_trading else []
    if app_heat >= 1:
        bits.append(f"app heat {app_heat:.0f}")
    if referred:
        mix = quality_mix(hist)
        bits.append(f"referred {referred} before ({mix or str(active) + ' made a first trade'}"
                    + (f", last {last_ref:%b %Y}" if last_ref else "") + ")")
    if why_engaged:
        bits.append(why_engaged)
    return score, "Referral score: " + ("; ".join(bits) or "no trading or referral history yet")


def referral_quality_score(ctx: Ctx, hist: dict) -> float:
    """0-100 from the quality of the clients they referred: high 3 points, medium 1.5, low 0.1, so two high-quality
    friends outrank ten who never traded. Before quality grades exist (summary uploads), it falls back to traded / referred."""
    q = quality_rules(ctx)
    if any(k in hist for k in ("high", "medium", "low")):
        pts = sum(q["weights"][g] * int(hist.get(g) or 0) for g in ("high", "medium", "low"))
    else:
        pts = q["weights"]["medium"] * int(hist.get("active") or 0) + q["weights"]["low"] * max(int(hist.get("referred") or 0) - int(hist.get("active") or 0), 0)
    return round(100 * (1 - math.exp(-pts / q["scale"])), 1)


def quality_mix(hist: dict) -> str:
    if not any(k in hist for k in ("high", "medium", "low")):
        return ""
    return ", ".join(f"{int(hist.get(g) or 0)} {g}" for g in ("high", "medium", "low") if hist.get(g)) + " quality"


def rescore(ctx: Ctx, lead_id: str) -> None:
    drives.rescore(ctx, CAMPAIGN, lead_id)


def refresh_scores(ctx: Ctx) -> int:
    return drives.refresh_scores(ctx, CAMPAIGN)


# ---------------------------------------------------------------- the pool
def _upsert(ctx: Ctx, cid: str, key: str, data: dict, name: str = "", phone: str = "", category: str = "") -> str:
    return drives.upsert(ctx, CAMPAIGN, cid, key, data, name, phone, category)


_tally = drives.tally


def load_candidates(ctx: Ctx, df: pd.DataFrame, source: str = "client DB") -> dict:
    """The most-active-clients query: the chosen activity buckets join the pool (Medium and up by default)."""
    s = settings(ctx)
    id_col = _col(df, "CLIENT_ID", "ClientCode", "Client Code")
    if not id_col:
        raise ValueError("The file needs a CLIENT_ID column (the referral query's output).")
    c = {k: _col(df, *names) for k, names in {
        "trades": ("TotalTrades", "Trades"), "days": ("ActiveTradingDays", "ActiveDays"), "first": ("FirstTradeInPeriod",),
        "last": ("LastTradeDate", "LastTrade"), "bucket": ("ActivityBucket", "Bucket"),
        "name": ("CLIENT_NAME", "ClientName", "Name"), "phone": ("MOBILE", "MOBILE_NO", "MobileNo", "Phone"),
    }.items()}
    now = ctx.now()
    out = {"rows": len(df), "eligible": 0, "added": 0, "reopened": 0, "updated": 0, "busy": 0, "asked_recently": 0, "skipped": 0}
    eligible = set()
    for r in df.to_dict("records"):
        cid = norm_client_id(r.get(id_col))
        data = trade_data(ctx, r, c, s["days"], source)
        if not cid:
            continue
        if data["bucket"].lower() not in s["buckets"]:
            if drives.member(ctx, CAMPAIGN, cid):  # already in the pool (engaged base): keep its trade count for the cohorts
                drives.tally(out, "trades_updated" if _upsert(ctx, cid, "referral", data) else "")
            continue
        out["eligible"] += 1
        eligible.add(cid)
        last = parse_dt(data["last_trade"], ctx.tz)
        category = "Traded - active (last 30 days)" if last and (now - last).days <= 30 else "Traded - dormant (30+ days)"
        _tally(out, _upsert(ctx, cid, "referral", data, str(r.get(c["name"]) or "").strip() if c["name"] else "",
                            str(r.get(c["phone"]) or "").strip() if c["phone"] else "", category))
    out["dropped"] = drives.drop_stale(ctx, CAMPAIGN, eligible)
    ctx.conn.commit()
    return out


def trade_data(ctx: Ctx, r: dict, c: dict, days: int, source: str) -> dict:
    """One row of the most-active-clients query as stored on the client (also used by the Sarthi activation pool)."""
    trades = _int(r.get(c["trades"])) if c["trades"] else 0
    bucket = str(r.get(c["bucket"]) or "").strip() if c["bucket"] else ""
    if not bucket:  # a file without the bucket column: work it out the same way the query does
        bucket = ("Very High" if trades >= 100 else "High" if trades >= 50 else "Medium" if trades >= 20
                  else "Low" if trades >= 5 else "Very Low")
    last = parse_dt(r.get(c["last"]), ctx.tz) if c["last"] else None
    return {"total_trades": trades, "active_days": _int(r.get(c["days"])) if c["days"] else 0, "last_trade": iso(last),
            "first_trade": iso(parse_dt(r.get(c["first"]), ctx.tz)) if c.get("first") else None,
            "bucket": bucket, "window_days": days, "pulled_at": iso(ctx.now()), "source": source}


QUALITY_DEFAULTS = {"high_deposit": 30000, "high_holdings": 100000, "high_recent_days": 30,
                    "weights": {"high": 3.0, "medium": 1.5, "low": 0.1}, "scale": 4.0}


def quality_rules(ctx: Ctx) -> dict:
    q = (ctx.cfg.get("referral") or {}).get("quality") or {}
    return {**QUALITY_DEFAULTS, **q, "weights": {**QUALITY_DEFAULTS["weights"], **(q.get("weights") or {})}}


def referred_quality(ctx: Ctx, deposit: float, holdings: float, ftd, ltd) -> str:
    """One referred client: high = deposited ₹30k+, holds ₹1L+ or traded in the last 30 days; medium = made a first trade;
    low = opened an account but never traded. (Backtested Oct 2026: referrers graded this way predicted the quality of
    their later referrals far better than a plain count.)"""
    q = quality_rules(ctx)
    last = parse_dt(ltd, ctx.tz)
    if deposit >= q["high_deposit"] or holdings >= q["high_holdings"] or (last and (ctx.now() - last).days <= q["high_recent_days"]):
        return "high"
    return "medium" if parse_dt(ftd, ctx.tz) else "low"


def _history_by_referrer(ctx: Ctx, df: pd.DataFrame) -> dict[str, dict]:
    """Either the raw history query (one row per referred client: refree, Client_Id, AuthorizeDate, FTD, FirstDeposit)
    or a summary (referrer, number referred, number active). Active = the referred client has made a first trade."""
    ref_by = _col(df, "refree", "referee", "referrer", "LeadSubSource", "REFERRER_ID", "ReferrerClientId")
    friend = _col(df, "Client_Id", "ClientId", "CLIENT_ID")
    if ref_by and friend:  # raw rows
        out: dict[str, dict] = {}
        ftd, dep, auth = _col(df, "FTD"), _col(df, "FirstDeposit"), _col(df, "AuthorizeDate")
        ltd, hold = _col(df, "LTD"), _col(df, "Holdings")
        num = lambda v: float(v) if v is not None and pd.notna(v) and str(v).strip() else 0.0  # noqa: E731
        for r in df.to_dict("records"):
            cid, fid = norm_client_id(r.get(ref_by)), norm_client_id(r.get(friend))
            if not cid or not fid or cid == fid:
                continue
            h = out.setdefault(cid, {"friends": set(), "active": set(), "funded": set(), "last_referred": None, "grade": {}})
            h["friends"].add(fid)
            h["grade"][fid] = referred_quality(ctx, num(r.get(dep)) if dep else 0, num(r.get(hold)) if hold else 0,
                                               r.get(ftd) if ftd else None, r.get(ltd) if ltd else None)
            if ftd and parse_dt(r.get(ftd), ctx.tz):
                h["active"].add(fid)
            if dep and _int(r.get(dep)) > 0:
                h["funded"].add(fid)
            when = iso(parse_dt(r.get(auth), ctx.tz)) if auth else None
            if when and (not h["last_referred"] or when > h["last_referred"]):
                h["last_referred"] = when
        return {cid: {"referred": len(h["friends"]), "active": len(h["active"]), "funded": len(h["funded"]),
                      "last_referred": h["last_referred"],
                      **{g: sum(1 for v in h["grade"].values() if v == g) for g in ("high", "medium", "low")}}
                for cid, h in out.items()}
    id_col = ref_by or _col(df, "CLIENT_ID", "ClientCode")
    n_col = _col(df, "ReferredCount", "TotalReferred", "Referrals", "NoOfReferrals", "Referred")
    a_col = _col(df, "ActiveReferred", "ActiveReferredCount", "ActiveReferrals", "ReferredActive", "Active")
    if not id_col or not n_col:
        raise ValueError("Upload the referral-history query's output (refree, Client_Id, FTD, ...) or a summary with the "
                         f"referrer's client code and the number referred (found: {', '.join(map(str, df.columns))}).")
    return {norm_client_id(r.get(id_col)): {"referred": _int(r.get(n_col)), "active": _int(r.get(a_col)) if a_col else 0}
            for r in df.to_dict("records") if norm_client_id(r.get(id_col)) and _int(r.get(n_col)) > 0}


def _store_outcomes(ctx: Ctx, df: pd.DataFrame) -> int:
    """Keep every referred client (raw history rows) so campaign results can be counted from the database."""
    ref_by = _col(df, "refree", "referee", "referrer", "LeadSubSource", "REFERRER_ID", "ReferrerClientId")
    friend = _col(df, "Client_Id", "ClientId", "CLIENT_ID")
    if not (ref_by and friend):
        return 0
    c = {k: _col(df, *n) for k, n in {"lead": ("LeadId",), "auth": ("AuthorizeDate",), "ftd": ("FTD",),
                                     "dep": ("FirstDeposit",), "hold": ("Holdings",), "ltd": ("LTD",)}.items()}
    n = 0
    for r in df.to_dict("records"):
        cid, fid = norm_client_id(r.get(ref_by)), norm_client_id(r.get(friend))
        if not cid or not fid or cid == fid:
            continue
        num = lambda k: float(r.get(c[k])) if c[k] and pd.notna(r.get(c[k])) and str(r.get(c[k])).strip() else None  # noqa: E731
        ctx.conn.execute(
            """INSERT INTO referral_outcomes (referred_id, referrer_id, lead_ref, authorized_at, first_trade, first_deposit, holdings,
               last_trade, updated_at) VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT (referred_id) DO UPDATE SET referrer_id=excluded.referrer_id,
               lead_ref=excluded.lead_ref, authorized_at=excluded.authorized_at, first_trade=excluded.first_trade,
               first_deposit=excluded.first_deposit, holdings=excluded.holdings, last_trade=excluded.last_trade, updated_at=excluded.updated_at""",
            (fid, cid, str(r.get(c["lead"]) or "") if c["lead"] else None,
             iso(parse_dt(r.get(c["auth"]), ctx.tz)) if c["auth"] else None,
             iso(parse_dt(r.get(c["ftd"]), ctx.tz)) if c["ftd"] else None, num("dep"), num("hold"),
             iso(parse_dt(r.get(c["ltd"]), ctx.tz)) if c["ltd"] else None, iso(ctx.now())))
        n += 1
    return n


def load_history(ctx: Ctx, df: pd.DataFrame, source: str = "referral history") -> dict:
    """Who referred before, and how many of their referrals became active. Proven referrers join the pool even when
    they aren't top traders: they've shown they will refer."""
    _store_outcomes(ctx, df)
    hist = _history_by_referrer(ctx, df)
    out = {"rows": len(df), "referrers": len(hist), "referred": sum(h["referred"] for h in hist.values()),
           "active": sum(h["active"] for h in hist.values()),
           "added": 0, "reopened": 0, "updated": 0, "busy": 0, "asked_recently": 0, "skipped": 0}
    for cid, h in hist.items():
        _tally(out, _upsert(ctx, cid, "referral_history", {**h, "updated_at": iso(ctx.now()), "source": source}))
    ctx.conn.commit()
    return out


def load_engagement(ctx: Ctx, df: pd.DataFrame, campaign: str = "", channel: str = "", source: str = "") -> dict:
    return drives.load_engagement(ctx, CAMPAIGN, df, campaign, channel, source)


def campaign_report(ctx: Ctx) -> list[dict]:
    """Per channel campaign: clients engaged, their tier now, and how many were called / gave referrals."""
    out = []
    for g in drives.channel_rows(ctx, CAMPAIGN):
        users = g.pop("user_ids")
        marks = ",".join("?" * len(users))
        g["gave"] = one(ctx.conn, f"SELECT COUNT(DISTINCT referrer_id) AS n FROM referrals WHERE referrer_id IN ({marks})", users)["n"]
        g["friends"] = one(ctx.conn, f"SELECT COUNT(*) AS n FROM referrals WHERE referrer_id IN ({marks})", users)["n"]
        out.append(g)
    return out


# ---------------------------------------------------------------- periodic refresh
def last_refresh(ctx: Ctx) -> dict | None:
    return drives.last_refresh(ctx, CAMPAIGN)


def is_stale(ctx: Ctx) -> bool:
    return drives.is_stale(ctx, CAMPAIGN, settings(ctx)["refresh_hours"])


def refresh(ctx: Ctx, force: bool = False) -> dict:
    """Re-pull the most active traders and the referral history (every `refresh_hours`, default daily), then re-score.
    Safe to call often: it does nothing until the data is stale, and says why when the server can't be reached."""
    if not force and not is_stale(ctx):
        return {"status": "fresh", "last": last_refresh(ctx)}
    conn = connection_status(ctx)
    if not conn["ok"]:
        return {"status": "unreachable", "detail": conn["detail"]}
    history = load_history(ctx, pull_history(ctx), source="client DB")  # first, so proven referrers stay in the pool
    base_df = pull_base(ctx)
    base = load_profiles(ctx, base_df, source="client DB")           # the engaged base: C2-C5
    pool = load_candidates(ctx, pull(ctx), source="client DB")       # trades for every member (cohorts need them)
    covered = {norm_client_id(v) for v in base_df[_col(base_df, "CLIENT_ID")]} if len(base_df) else set()
    rest = [m["lead_id"] for m in rows(ctx.conn, "SELECT lead_id FROM drive_members WHERE program=? AND status='open'", (CAMPAIGN,))
            if m["lead_id"] not in covered]  # dormant referrers, campaign responders: refresh their profile (RM, last trade) too
    others = load_profiles(ctx, pull_profiles(ctx, rest), add_engaged=False) if rest else {}
    done = drives.mark_refresh(ctx, CAMPAIGN, {"referrers": history["referrers"], "traders": pool["eligible"], "engaged_base": base["engaged"],
                                               "added": pool["added"] + history["added"] + base["added"], "dropped": pool.get("dropped", 0)})
    return {"status": "ok", "history": history, "pool": pool, "base": base, "profiles": others, "last": done}


def refresh_results(ctx: Ctx) -> dict:
    """Re-pull only the referral history (seconds): new referred clients, deposits and first trades since the last pull."""
    conn = connection_status(ctx)
    if not conn["ok"]:
        return {"status": "unreachable", "detail": conn["detail"]}
    res = load_history(ctx, pull_history(ctx), source="client DB")
    drives.mark_refresh(ctx, CAMPAIGN, {}, what="results")
    return {"status": "ok", **res}


# ---------------------------------------------------------------- drives and campaigns
def start_drive(ctx: Ctx, per_gm: int | None = None, gm_ids: list[str] | None = None, tiers: list[str] | None = None,
                start_now: bool = False, cohorts: list[str] | None = None, in_order: bool = True) -> dict:
    """Hand out referral asks: cohort order C1a, C1b, C2... (in_order), hottest first within each; held clients wait."""
    return drives.start_drive(ctx, CAMPAIGN, per_gm, gm_ids, tiers, start_now, cohorts, in_order)


assign_referrals = start_drive  # older name


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


def end_campaign(ctx: Ctx) -> int:
    return drives.end_campaign(ctx, CAMPAIGN)


def report(ctx: Ctx, campaign_id: int) -> dict:
    """Asks and calls per GM, referrals the GMs collected, and successful referrals from the database (clients who joined
    through a referral since the campaign started), credited to the GM who asked that referrer before the friend joined."""
    b = drives.report_base(ctx, CAMPAIGN, campaign_id)
    lo, hi = b["lo"], b["hi"]
    friends = rows(ctx.conn, "SELECT * FROM referrals WHERE created_at>=? AND created_at<?", (lo, hi))
    wins = rows(ctx.conn, "SELECT o.*, l.name AS referrer_name FROM referral_outcomes o LEFT JOIN leads l ON l.lead_id=o.referrer_id "
                          "WHERE o.authorized_at>=? AND o.authorized_at<? ORDER BY o.authorized_at DESC", (lo, hi))
    for w in wins:
        w["gm"] = drives.credit(b["first_call"], w["referrer_id"], w["authorized_at"])
        w["funded"] = bool(w["first_deposit"] and w["first_deposit"] > 0)
        w["traded"] = bool(w["first_trade"])
    by_gm = b["by_gm"]
    for g in by_gm.values():
        g.update(friends=0, funded=0, traded=0)
    for f in friends:
        if f["agent_id"] in by_gm:
            by_gm[f["agent_id"]]["friends"] += 1
    for w in wins:
        if w["gm"] in by_gm:
            g = by_gm[w["gm"]]
            g["wins"] += 1
            g["funded"] += w["funded"]
            g["traded"] += w["traded"]
    by_gm = {k: {**v, "gave": v["on_call"]} for k, v in drives.finish_by_gm(by_gm).items()}
    credited = [w for w in wins if w["gm"]]
    t = b["totals"]
    return {
        "campaign": b["campaign"], "asks": b["asks"], "last_call": b["last_call"], "friends": friends, "wins": wins, "by_gm": by_gm,
        "totals": {**t, "gave": t["on_call"], "friends": len(friends),
                   "wins": len(wins), "wins_credited": len(credited), "funded": sum(w["funded"] for w in wins),
                   "traded": sum(w["traded"] for w in wins), "credited_funded": sum(w["funded"] for w in credited),
                   "credited_traded": sum(w["traded"] for w in credited)},
    }


# ---------------------------------------------------------------- referrals collected
def record_referrals(ctx: Ctx, referrer_id: str, agent_id: str, text: str) -> int:
    """'Name, number' per line, as typed on the call. Returns how many friends were saved."""
    saved = 0
    for line in (text or "").splitlines():
        line = line.strip(" ,;-\t")
        if not line:
            continue
        m = re.search(r"\+?\d[\d\s-]{8,}\d", line)
        digits = re.sub(r"\D", "", m.group()) if m else ""
        phone = digits[-10:] if len(digits) >= 10 else ""
        name = (line[:m.start()] + line[m.end():]) if m and phone else line
        name = name.strip(" ,;-:()\t")
        ctx.conn.execute("INSERT INTO referrals (referrer_id, agent_id, name, phone, raw, created_at) VALUES (?,?,?,?,?,?)",
                         (referrer_id, agent_id, name or None, phone or None, line, iso(ctx.now())))
        saved += 1
    ctx.conn.commit()
    return saved


def referral_rows(ctx: Ctx, since: datetime | None = None) -> list[dict]:
    q = "SELECT r.*, l.name AS referrer_name FROM referrals r LEFT JOIN leads l ON l.lead_id=r.referrer_id"
    args: tuple = ()
    if since:
        q += " WHERE r.created_at >= ?"
        args = (iso(since),)
    return rows(ctx.conn, q + " ORDER BY r.created_at DESC", args)


def summary(ctx: Ctx, since: datetime) -> dict:
    """Drive numbers for the dashboard: pool, asks, outcomes and referrals in the period."""
    pool = rows(ctx.conn, "SELECT heat_tier, assigned_to FROM drive_members WHERE program=? AND status='open'", (CAMPAIGN,))
    calls = rows(ctx.conn, "SELECT agent_id, lead_id, outcome FROM calls WHERE called_at>=? AND program=?", (iso(since), CAMPAIGN))
    reached = {c["lead_id"] for c in calls if c["outcome"] not in drives.NO_REACH}
    gave = {c["lead_id"] for c in calls if c["outcome"] == "referral_given"}
    friends = one(ctx.conn, "SELECT COUNT(*) AS n FROM referrals WHERE created_at>=?", (iso(since),))["n"]
    by_gm = {}
    for c in calls:
        g = by_gm.setdefault(c["agent_id"], {"calls": 0, "reached": set(), "gave": set()})
        g["calls"] += 1
        if c["lead_id"] in reached:
            g["reached"].add(c["lead_id"])
        if c["outcome"] == "referral_given":
            g["gave"].add(c["lead_id"])
    friends_by = {r["agent_id"]: r["n"] for r in rows(ctx.conn, "SELECT agent_id, COUNT(*) AS n FROM referrals WHERE created_at>=? "
                                                                "GROUP BY agent_id", (iso(since),))}
    return {
        "pool": len([p for p in pool if not p["assigned_to"]]),
        "pool_tiers": {t: sum(1 for p in pool if not p["assigned_to"] and (p["heat_tier"] or "cold") == t) for t in ("hot", "warm", "cold")},
        "with_gms": len([p for p in pool if p["assigned_to"]]),
        "calls": len(calls), "clients_called": len({c["lead_id"] for c in calls}), "reached": len(reached),
        "gave": len(gave), "friends": friends,
        "by_gm": {g: {"calls": v["calls"], "reached": len(v["reached"]), "gave": len(v["gave"]), "friends": friends_by.get(g, 0)}
                  for g, v in by_gm.items()},
    }



# ---------------------------------------------------------------- cohorts
# Every client gets ONE primary cohort (one owner, one ask), assigned in this order; the other cohorts a client fits are
# kept as tags that personalise the call. Defined from the Oct 2026 referral analysis (11,273 referred leads, 2022-26).
COHORTS = {
    "C1a": "Proven referrer, active",
    "C1b": "Proven referrer, dormant",
    "C1c": "Recent referrer (31-180 days), friend not trading yet",
    "C2": "New client, 1-6 months, trading",
    "C3": "Active Sarthi user",
    "C4": "Heavy trader, Sarthi off",
    "C5": "Active light trader",
    "C6": "Dormant (re-activate first)",
    "rested": "Referred in the last 30 days",
    "excluded": "Bulk referrer, no referral traded",
    "not_eligible": "Not traded yet / under 30 days",
}
COHORT_ORDER = ["C1a", "C1b", "C1c", "C2", "C3", "C4", "C5", "C6"]
FOCUS = ["C1a", "C1b", "C1c", "C2", "C3", "C4", "C5"]  # all cohorts, handed out in COHORT_ORDER
TAG_LABELS = {"sarthi": "Sarthi user", "heavy": "Heavy trader", "new": "New client", "dormant": "Dormant", "proven": "Proven referrer",
              "funded30": "₹30k+ client"}

# Every open client who traded in the last 6 months and joined 30+ days ago: where C2-C5 come from. Read-only.
# Override with sources.client_db.referral_base_query.
BASE_SQL = """
SELECT C.CLIENT_ID, C.REGISTRATION_DATE, C.IsSarthiTIOn, C.IsAccountClosed, C.PreferredSegment, F.FirstDeposit, F.FTD, F.LTD, F.Holdings,
       C.Dealer_Id, E.EmployeeName AS RM_Name, E.BranchName AS RM_Branch,
       __CENTRAL__ AS IsCentralized
FROM [Repl_TargetReview].[dbo].[tbl_ClientNew] C
JOIN [Repl_TargetReview].[dbo].[tbl_ClientFtdLtd] F ON F.CLIENT_ID = C.CLIENT_ID
LEFT JOIN [Repl_TargetReview].[dbo].[tbl_EmployeeHeirarchy] E ON E.EmployeeId = C.Dealer_Id AND C.Dealer_Id > 0
WHERE F.FTD IS NOT NULL AND F.LTD >= DATEADD(DAY, -{engaged_days}, GETDATE())
  AND C.REGISTRATION_DATE <= DATEADD(DAY, -{min_tenure}, GETDATE())
  AND ISNULL(CAST(C.IsAccountClosed AS VARCHAR(10)), '0') NOT IN ('1', 'True', 'true')
"""
PROFILE_SQL = """
SELECT C.CLIENT_ID, C.REGISTRATION_DATE, C.IsSarthiTIOn, C.IsAccountClosed, C.PreferredSegment, F.FirstDeposit, F.FTD, F.LTD, F.Holdings,
       C.Dealer_Id, E.EmployeeName AS RM_Name, E.BranchName AS RM_Branch,
       __CENTRAL__ AS IsCentralized
FROM [Repl_TargetReview].[dbo].[tbl_ClientNew] C
LEFT JOIN [Repl_TargetReview].[dbo].[tbl_ClientFtdLtd] F ON F.CLIENT_ID = C.CLIENT_ID
LEFT JOIN [Repl_TargetReview].[dbo].[tbl_EmployeeHeirarchy] E ON E.EmployeeId = C.Dealer_Id AND C.Dealer_Id > 0
WHERE C.CLIENT_ID IN ({ids})
"""


def cohort_rules(ctx: Ctx) -> dict:
    cfg = (ctx.cfg.get("referral") or {}).get("cohorts") or {}
    return {"engaged_days": int(cfg.get("engaged_days", 180)), "active_days": int(cfg.get("active_days", 30)),
            "min_tenure": int(cfg.get("min_tenure_days", 30)), "new_until": int(cfg.get("new_until_days", 180)),
            "heavy_trades": int(cfg.get("heavy_trades", 5)), "rest_days": int(cfg.get("rest_days", 30)),
            "recent_referrer_days": int(cfg.get("recent_referrer_days", 180)),
            "bulk_referrals": int(cfg.get("bulk_referrals", 5)), "after_sarthi_days": int(cfg.get("after_sarthi_days", 7))}


def _facts(ctx: Ctx, lead: dict) -> dict:
    """What the cohort rules look at, from what the pulls stored on the client."""
    x = json.loads(lead.get("extra") or "{}")
    pr, hist, tr = x.get("referral_profile") or {}, x.get("referral_history") or {}, x.get("referral") or {}
    now = ctx.now()
    days = lambda v: (now - parse_dt(v, ctx.tz)).days if parse_dt(v, ctx.tz) else None  # noqa: E731
    ltd, reg = days(pr.get("ltd") or tr.get("last_trade")), days(pr.get("registered") or x.get("registration_date"))
    return {"known": bool(pr) or bool(tr), "traded": bool(pr.get("ftd") or tr.get("total_trades")), "ltd_days": ltd, "tenure": reg,
            "sarthi": bool(pr.get("sarthi_on") if pr else x.get("sarthi_active")), "trades60": int(tr.get("total_trades") or 0),
            "referred": int(hist.get("referred") or 0), "proven": int(hist.get("active") or 0) > 0,
            "last_ref_days": days(hist.get("last_referred")), "funded30": max(float(pr.get("first_deposit") or 0),
                                                                                float(pr.get("holdings") or 0)) >= 30000}


def cohort(ctx: Ctx, lead: dict) -> str:
    r, f = cohort_rules(ctx), _facts(ctx, lead)
    if f["referred"] >= r["bulk_referrals"] and not f["proven"]:
        return "excluded"
    if f["last_ref_days"] is not None and f["last_ref_days"] <= r["rest_days"]:
        return "rested"
    engaged = f["ltd_days"] is not None and f["ltd_days"] <= r["engaged_days"]
    if f["proven"]:
        return "C1a" if engaged else "C1b"
    if f["last_ref_days"] is not None and f["last_ref_days"] <= r["recent_referrer_days"]:
        return "C1c"  # referred 31-180 days ago, no friend has traded yet: the best window for a repeat referral
    if not f["traded"] or (f["tenure"] is not None and f["tenure"] <= r["min_tenure"]):
        return "not_eligible" if f["known"] else "C5"  # campaign responders with no profile yet: lowest active cohort
    if not engaged:
        return "C6"
    if f["tenure"] is not None and f["tenure"] <= r["new_until"]:
        return "C2"
    if f["sarthi"]:
        return "C3"
    if f["trades60"] >= r["heavy_trades"]:
        return "C4"
    return "C5"


def tags(ctx: Ctx, lead: dict) -> list[str]:
    """The other cohorts a client fits: used to personalise the one call, never for a second ask."""
    r, f = cohort_rules(ctx), _facts(ctx, lead)
    out = []
    if f["proven"]:
        out.append("proven")
    if f["sarthi"]:
        out.append("sarthi")
    if f["trades60"] >= r["heavy_trades"]:
        out.append("heavy")
    if f["tenure"] is not None and r["min_tenure"] < f["tenure"] <= r["new_until"]:
        out.append("new")
    if f["ltd_days"] is None or f["ltd_days"] > r["engaged_days"]:
        out.append("dormant")
    if f["funded30"]:
        out.append("funded30")
    return out


def owner(lead: dict) -> str:
    """'business' when the client has an RM (Dealer_Id > 0), else 'growth'. Unknown (no profile yet) counts as growth."""
    pr = json.loads(lead.get("extra") or "{}").get("referral_profile") or {}
    return "business" if int(pr.get("rm_id") or 0) > 0 else "growth"


OWNER_LABEL = {"business": "Business team (RM)", "growth": "Growth team"}

# Centralised client: the business "Vertical" rule (config `centralised`, see clientdb.centralised_sql).
SCOPES = {"all": "All clients", "central": "Centralised", "non_central": "Not centralised"}


def centralized(lead: dict) -> bool:
    pr = json.loads(lead.get("extra") or "{}").get("referral_profile") or {}
    return bool(pr.get("centralized"))


def in_scope(lead: dict, scope: str = "all") -> bool:
    return scope == "all" or centralized(lead) == (scope == "central")


def hold(ctx: Ctx, lead_id: str) -> str | None:
    """Business-team clients never enter GM drives (their RM calls them, from the Business list). Sarthi before referral:
    a client still on the Sarthi activation list waits; once Sarthi is on, the ask comes a week later."""
    lead = one(ctx.conn, "SELECT extra FROM leads WHERE lead_id=?", (lead_id,)) or {}
    scope = settings(ctx)["scope"]
    if not in_scope(lead, scope):
        return f"outside the drive's scope ({SCOPES[scope].lower()} only)"
    if owner(lead) == "business":
        return "has an RM: on the Business team list"
    m = drives.member(ctx, "sarthi", lead_id)
    if m and m["status"] == "open":
        return "waiting for Sarthi activation"
    wait = cohort_rules(ctx)["after_sarthi_days"]
    on = parse_dt(json.loads((lead or {}).get("extra") or "{}").get("sarthi_activated_on"), ctx.tz)
    act = one(ctx.conn, "SELECT activated_at FROM sarthi_activations WHERE client_id=?", (lead_id,))
    on = max(filter(None, [on, parse_dt((act or {}).get("activated_at"), ctx.tz)]), default=None)
    if on and ctx.now() - on < timedelta(days=wait):
        return "Sarthi just switched on: ask a week later"
    return None


def pull_base(ctx: Ctx) -> pd.DataFrame:
    src = (ctx.cfg.get("sources") or {}).get("client_db") or {}
    r = cohort_rules(ctx)
    from .clientdb import centralised_sql

    sql = (src.get("referral_base_query") or BASE_SQL).replace("__CENTRAL__", centralised_sql(ctx))
    return drives.query(ctx, sql.format(engaged_days=r["engaged_days"], min_tenure=r["min_tenure"]))


def pull_profiles(ctx: Ctx, ids: list[str]) -> pd.DataFrame:
    """Profiles for pool clients the base pull doesn't cover (past referrers who went dormant, campaign responders)."""
    ids = sorted({norm_client_id(i) for i in ids} - {""})
    from .clientdb import centralised_sql

    return drives.query_ids(ctx, PROFILE_SQL.replace("__CENTRAL__", centralised_sql(ctx)), ids)


def load_profiles(ctx: Ctx, df: pd.DataFrame, source: str = "client DB", add_engaged: bool = True) -> dict:
    """Client profiles (registration, first and last trade, Sarthi, deposit, holdings) drive the cohorts. Engaged clients
    join the pool (add_engaged); closed accounts leave it."""
    c = {k: _col(df, *n) for k, n in {"id": ("CLIENT_ID",), "reg": ("REGISTRATION_DATE",), "on": ("IsSarthiTIOn",),
                                      "closed": ("IsAccountClosed",), "dep": ("FirstDeposit",), "ftd": ("FTD",), "ltd": ("LTD",),
                                      "hold": ("Holdings",), "seg": ("PreferredSegment",), "rm": ("Dealer_Id", "DealerId"),
                                      "rm_name": ("RM_Name",), "rm_branch": ("RM_Branch",),
                                      "central": ("IsCentralized", "Centralized")}.items()}
    if not c["id"]:
        raise ValueError("The file needs CLIENT_ID plus REGISTRATION_DATE, FTD, LTD, IsSarthiTIOn (the client-profile query).")
    from .activation import is_on

    r = cohort_rules(ctx)
    out = {"rows": len(df), "engaged": 0, "closed": 0, "added": 0, "reopened": 0, "updated": 0, "busy": 0, "asked_recently": 0, "skipped": 0}
    num = lambda v: float(v) if v is not None and pd.notna(v) and str(v).strip() else 0.0  # noqa: E731
    text = lambda v: str(v).strip() if v is not None and pd.notna(v) else ""  # noqa: E731  (a missing RM is NaN, not "nan")
    for row in df.to_dict("records"):
        cid = norm_client_id(row.get(c["id"]))
        if not cid:
            continue
        if c["closed"] and is_on(row.get(c["closed"])):
            out["closed"] += drives.close_member(ctx, CAMPAIGN, cid, "invalid")
            continue
        ltd = parse_dt(row.get(c["ltd"]), ctx.tz) if c["ltd"] else None
        engaged = bool(ltd and (ctx.now() - ltd).days <= r["engaged_days"])
        data = {"registered": iso(parse_dt(row.get(c["reg"]), ctx.tz)) if c["reg"] else None,
                "ftd": iso(parse_dt(row.get(c["ftd"]), ctx.tz)) if c["ftd"] else None, "ltd": iso(ltd), "engaged": engaged,
                "sarthi_on": is_on(row.get(c["on"])) if c["on"] else False, "first_deposit": num(row.get(c["dep"])) if c["dep"] else 0,
                "holdings": num(row.get(c["hold"])) if c["hold"] else 0, "pulled_at": iso(ctx.now()), "source": source,
                "preferred_segment": str(row.get(c["seg"]) or "").strip() if c["seg"] else "",
                "rm_id": int(num(row.get(c["rm"]))) if c["rm"] else None,
                "rm_name": text(row.get(c["rm_name"])) if c["rm_name"] else "",
                "rm_branch": text(row.get(c["rm_branch"])) if c["rm_branch"] else "",
                "centralized": is_on(row.get(c["central"])) if c["central"] else None}
        member = drives.member(ctx, CAMPAIGN, cid)
        if not member and not (engaged and add_engaged):
            continue  # a dormant client who never referred: not a referral target now
        out["engaged"] += engaged
        drives.tally(out, _upsert(ctx, cid, "referral_profile", data))
    ctx.conn.commit()
    return out


# How each cohort is approached. Shown to the GM on the card; the talking points follow the same plan.
PLAN = {
    "C1a": ["GM thank-you call: the friends they referred started trading with us", "Ask for 1-2 more friends (with consent)",
            "Send their referral link on WhatsApp right after the call"],
    "C1b": ["Day 0: personalised WhatsApp / push: thank them for the friends they referred, and what's new in their segment",
            "Day 2-3: GM reconnect call: why did they stop, fix it, show a Sarthi idea in their segment. No referral push",
            "After their first trade back they move to 'Proven, active': the referral ask follows a week later"],
    "C1c": ["Thank them for the friend they referred; offer to help that friend fund / start trading",
            "Ask for one more friend (with consent): repeat referrals 1-6 months on trade most often",
            "Referral link on WhatsApp after the call"],
    "C2": ["GM onboarding check-in at 30-90 days: experience, any problem", "Ask for a referral only after helping",
           "Referral link on WhatsApp"],
    "C3": ["Share prompt in the app when a Sarthi idea closes", "GM call for hot / warm: 'your friend can get the same ideas'",
           "Track record as it is; no return promises"],
    "C4": ["Switch Sarthi on first (Sarthi list)", "Referral ask a week after it's on", "Push / WhatsApp; GM calls responders"],
    "C5": ["Push / WhatsApp referral reminder (fortnightly)", "GM calls only those who click or reply"],
    "C6": ["Not for referrals now: re-activation first"],
}


def snapshot(ctx: Ctx, lead: dict) -> dict:
    """The client's current state and preferences, as the GM sees them before the call."""
    f = _facts(ctx, lead)
    x = json.loads(lead.get("extra") or "{}")
    pr, h = x.get("referral_profile") or {}, x.get("referral_history") or {}
    ago = lambda d: ("today" if d == 0 else f"{d} days ago" if d < 60 else f"{d // 30} months ago") if d is not None else "unknown"  # noqa: E731
    from .sarthi import segment_from_preference

    return {"cohort": lead.get("segment") or cohort(ctx, lead), "tags": [TAG_LABELS[t] for t in tags(ctx, lead)],
            "last_trade": ago(f["ltd_days"]), "with_us": ago(f["tenure"]).replace(" ago", ""),
            "sarthi": "on" if f["sarthi"] else "off", "segment": segment_from_preference(pr.get("preferred_segment")) or "not set",
            "trades_60d": f["trades60"], "referred": h.get("referred", 0), "referred_traded": h.get("active", 0),
            "quality": {g: int(h.get(g) or 0) for g in ("high", "medium", "low")}, "quality_score": referral_quality_score(ctx, h) if h else 0,
            "last_referred": (parse_dt(h.get("last_referred"), ctx.tz).strftime("%b %Y") if h.get("last_referred") else ""),
            "holdings": float(pr.get("holdings") or 0), "first_deposit": float(pr.get("first_deposit") or 0),
            "owner": owner(lead), "rm_name": pr.get("rm_name") or ("" if not pr.get("rm_id") else f"RM #{pr.get('rm_id')}"),
            "rm_branch": pr.get("rm_branch") or "", "centralized": bool(pr.get("centralized"))}


def business_list(ctx: Ctx, cohorts: list[str] | None = None, scope: str = "all") -> list[dict]:
    """Referral asks owned by the Business team: open pool clients with an RM, by cohort order, hottest first."""
    order = {c: i for i, c in enumerate(COHORT_ORDER)}
    out = []
    for m in rows(ctx.conn, "SELECT * FROM drive_members WHERE program=? AND status='open'", (CAMPAIGN,)):
        if (m["segment"] or "C5") not in order or (cohorts and m["segment"] not in cohorts):
            continue
        c = drives.card(ctx, CAMPAIGN, m)
        if owner(c) != "business" or not in_scope(c, scope):
            continue
        out.append({**c, "snapshot": snapshot(ctx, c)})
    return sorted(out, key=lambda c: (order.get(c["segment"], 99), -(c["heat_score"] or 0), c["lead_id"]))


def cohort_summary(ctx: Ctx, scope: str = "all") -> list[dict]:
    """Open pool by cohort: waiting to be handed out, with GMs, and held (Sarthi first). scope: all / central / non_central."""
    rows_ = rows(ctx.conn, "SELECT lead_id, segment, assigned_to FROM drive_members WHERE program=? AND status='open'", (CAMPAIGN,))
    funding = {r["lead_id"] for r in rows(ctx.conn, "SELECT lead_id FROM leads WHERE status='open'")}
    sarthi_open = {r["lead_id"] for r in rows(ctx.conn, "SELECT lead_id FROM drive_members WHERE program='sarthi' AND status='open'")}
    extras = {r["lead_id"]: r["extra"] for r in rows(ctx.conn, "SELECT m.lead_id, l.extra FROM drive_members m JOIN leads l ON l.lead_id=m.lead_id "
                                                         "WHERE m.program=? AND m.status='open'", (CAMPAIGN,))}
    out = {k: {"cohort": k, "label": v, "in_pool": 0, "with_gms": 0, "held_sarthi": 0, "on_funding": 0, "business": 0, "growth": 0}
           for k, v in COHORTS.items()}
    for r_ in rows_:
        if not in_scope({"extra": extras.get(r_["lead_id"])}, scope):
            continue
        o = out.setdefault(r_["segment"] or "C5", {"cohort": r_["segment"], "label": r_["segment"], "in_pool": 0, "with_gms": 0,
                                                    "held_sarthi": 0, "on_funding": 0, "business": 0, "growth": 0})
        o[owner({"extra": extras.get(r_["lead_id"])})] += 1
        if r_["assigned_to"]:
            o["with_gms"] += 1
            continue
        o["in_pool"] += 1
        o["held_sarthi"] += r_["lead_id"] in sarthi_open
        o["on_funding"] += r_["lead_id"] in funding
    return [o for o in out.values() if o["in_pool"] or o["with_gms"]]


PROGRAM = drives.register(drives.Program(
    key=CAMPAIGN, label="Referral", source=SOURCE, settings=settings, score=referral_score,
    keep=lambda extra: bool((extra.get("referral_history") or {}).get("referred") or extra.get("referral_campaign")
                            or (extra.get("referral_profile") or {}).get("engaged")),
    notify=lambda n, start: (f"🤝 {n} referral call(s) for you",
                             f"{n} of our most engaged clients are on your referral list (Referral dashboard), from {start:%H:%M}. "
                             "Thank them, then ask for one or two friends who'd like to trade, with the friend's consent."),
    success_outcome="referral_given",
    segment=cohort, segment_order=COHORT_ORDER, never_hand_out=frozenset({"rested", "excluded", "not_eligible"}), hold=hold,
))
