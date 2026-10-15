"""Shared engine for drive dashboards: referral asks (referral_app.py) and Sarthi activation (sarthi_app.py).

A drive program is a pool of clients (drive_members, one row per client per program: a client can be in the referral
pool and the Sarthi list at once) that is
  * scored on several 0-100 tracks (trading, app activity, channel-campaign engagement, plus program-specific ones),
    final = strongest track + a share of the others, on the same 🔥 / 🌤️ / ❄️ tiers as app heat;
  * worked in campaigns: an SM starts a campaign, hands out drives (batches of asks per GM), and ends it;
  * kept out of the funding work: no follow-up SLAs, warnings, escalations or compliance numbers. Clients on a funding
    list (leads.status='open') are left to that call; drive-only clients have leads.status='drive'. Drive calls are
    tagged calls.program.
Program modules (referral.py, activation.py) register a Program and add their own sources, score and results.
"""
from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Callable

import pandas as pd

from .context import Ctx, iso, norm_client_id, parse_dt
from .db import get_setting, one, rows, set_setting

NO_REACH = {"not_picked", "busy", "switched_off"}
# Campaign engagement: an action's weight comes from the first keyword its name contains (config: <program>.engagement).
ENGAGEMENT_DEFAULTS = {
    "actions": [["unsubscrib", -10], ["opt out", -10], ["optout", -10], ["stop", -10], ["block", -10],
                ["activat", 15], ["enable", 15], ["turn on", 15],
                ["refer", 15], ["share", 15], ["interest", 15], ["form", 15], ["submit", 15], ["lead", 15], ["yes", 15],
                ["repl", 8], ["respon", 8], ["call back", 8], ["callback", 8],
                ["click", 5], ["tap", 5], ["cta", 5], ["visit", 5],
                ["open", 2], ["read", 2], ["view", 2], ["seen", 2], ["deliver", 0], ["sent", 0]],
    "half_life_days": 5,      # engagement this many days old counts half
    "scale": 12,              # engagement score = 100 * (1 - e^(-points / scale)): one reply + a click today ~ warm
}
TRADING_DEFAULTS = {"trades_full": 150, "trades_points": 40, "days_points": 25, "recent_days": 3, "recent_points": 10,
                    "app_points": 15, "others_share": 0.25}


@dataclass
class Program:
    key: str                                   # leads.campaign value, and the key in leads.extra
    label: str                                 # "Referral" / "Sarthi activation"
    source: str                                # leads.source for clients the program adds
    settings: Callable[[Ctx], dict]            # needs: cooldown_days, max_attempts, call_from, daily_per_gm, engagement
    score: Callable[..., tuple[float, str]]    # (ctx, extra, app_heat, user_id) -> (0-100, reason)
    keep: Callable[[dict], bool]               # extra -> stays in the pool though not in the latest pull
    notify: Callable[[int, datetime], tuple[str, str]]
    success_outcome: str                       # the call outcome that is a success on the call
    segment: Callable[..., str] | None = None   # (ctx, lead) -> client type / cohort, if the program has them
    segment_order: list[str] | None = None      # hand-out order of segments in a drive (default: SEGMENT_ORDER)
    never_hand_out: frozenset = frozenset()     # segments a drive never hands out (rested, excluded, not eligible)
    hold: Callable[..., str | None] | None = None  # (ctx, lead_id) -> reason to wait (e.g. Sarthi first), or None
    reopenable: frozenset = field(default_factory=lambda: frozenset(
        {"converted", "out_of_window", "unreachable", "referred", "not_interested", "activated"}))


PROGRAMS: dict[str, Program] = {}


def register(p: Program) -> Program:
    PROGRAMS[p.key] = p
    return p


def get(key: str) -> Program:
    if key not in PROGRAMS:  # programs register on import
        from . import activation, referral  # noqa: F401
    return PROGRAMS[key]


def program_settings(ctx: Ctx, key: str) -> dict:
    return get(key).settings(ctx)


def program_score(ctx: Ctx, key: str, extra: dict, app_heat: float = 0, user_id: str | None = None) -> tuple[float, str]:
    return get(key).score(ctx, extra, app_heat, user_id)


# ---------------------------------------------------------------- inputs
def query(ctx: Ctx, sql: str, params: tuple = ()) -> pd.DataFrame:
    import pyodbc

    from .clientdb import connection_string

    with pyodbc.connect(connection_string(ctx), timeout=60, readonly=True) as conn:
        cur = conn.cursor()
        cur.execute(sql, params)
        cols = [c[0] for c in cur.description]
        return pd.DataFrame.from_records(cur.fetchall(), columns=cols)


def query_ids(ctx: Ctx, sql: str, ids: list[str], chunk: int = 1000, retries: int = 2) -> pd.DataFrame:
    """Run `sql` (with an {ids} placeholder) for many client IDs over ONE connection, in chunks under SQL Server's
    2,100-parameter limit; a dropped link (common over VPN) reconnects and resumes from the failed chunk."""
    import pyodbc

    from .clientdb import connection_string

    ids = list(ids)
    frames, i, tries = [], 0, 0
    while i < len(ids):
        try:
            with pyodbc.connect(connection_string(ctx), timeout=60, readonly=True) as conn:
                cur = conn.cursor()
                while i < len(ids):
                    part = ids[i:i + chunk]
                    cur.execute(sql.format(ids=",".join("?" * len(part))), part)
                    cols = [c[0] for c in cur.description]
                    frames.append(pd.DataFrame.from_records(cur.fetchall(), columns=cols))
                    i += chunk
        except pyodbc.OperationalError:
            tries += 1
            if tries > retries:
                raise
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def read_file(path: Path) -> pd.DataFrame:
    """A query's output saved from SSMS / Excel, for when the server can't be reached from here."""
    if path.suffix.lower() in {".csv", ".txt"}:
        return pd.read_csv(path)
    return pd.read_excel(path)


def col(df: pd.DataFrame, *names: str) -> str | None:
    key = lambda v: re.sub(r"[^a-z0-9]", "", str(v).lower())  # noqa: E731
    lower = {key(c): c for c in df.columns}
    for n in names:
        if key(n) in lower:
            return lower[key(n)]
    return None


def to_int(v) -> int:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return 0


def fmt_day(ctx: Ctx, day: str | None) -> str:
    d = parse_dt(day, ctx.tz)
    return d.strftime("%d %b") if d else "n/a"


def info(lead: dict, key: str) -> dict:
    return (json.loads(lead.get("extra") or "{}")).get(key) or {}


def connection_status(ctx: Ctx) -> dict:
    """Can this machine reach the client database? With a plain-language reason when it can't."""
    import socket

    from .clientdb import _cfg, available, reachable

    if not available(ctx):
        return {"ok": False, "detail": "The client database isn't configured on this machine (no server / login)."}
    if reachable(ctx):
        return {"ok": True, "detail": "Client database reachable."}
    server = str(_cfg(ctx).get("server") or "").replace("tcp:", "").split(",")[0].split("\\")[0]
    try:
        mine = sorted({a[4][0] for a in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
                       if not a[4][0].startswith(("127.", "169.254."))})
    except OSError:
        mine = []
    same = any(ip.rsplit(".", 1)[0] == server.rsplit(".", 1)[0] for ip in mine)
    where = f" This computer is on {', '.join(mine)}." if mine else ""
    hint = ("" if same else f" The server {server} is on the office network: connect to the office LAN / Wi-Fi or the VPN,"
            " then refresh.")
    return {"ok": False, "detail": f"Can't reach the client database ({server}) from here.{where}{hint} "
                                   "Meanwhile, run the query in SSMS and upload its output."}


# ---------------------------------------------------------------- scoring
def trading_track(ctx: Ctx, data: dict, w: dict) -> tuple[float, str]:
    """0-90 from the trade books: volume (square-root curve), consistency (share of trading days) and recency."""
    trades, days = int(data.get("total_trades") or 0), int(data.get("active_days") or 0)
    window = int(data.get("window_days") or 60)
    trading_days = max(round(window * 5 / 7) - 2, 1)  # weekdays less a couple of exchange holidays
    last = parse_dt(data.get("last_trade"), ctx.tz)
    since = (ctx.now().date() - last.date()).days if last else None
    value = (min(trades / w["trades_full"], 1) ** 0.5 * w["trades_points"] + min(days / trading_days, 1) * w["days_points"]
             + (w["recent_points"] if since is not None and since <= w["recent_days"]
                else w["recent_points"] / 2 if since is not None and since <= 7 else 0))
    if not trades:
        return 0.0, ""
    when = "today" if since == 0 else f"{since} d ago" if since is not None else "n/a"
    return value, f"{trades} trades on {days} of ~{trading_days} trading days, last {when}"


def combine(tracks: list[float], share: float) -> float:
    """Strongest track + `share` of the others: strong on any one track can be hot; strong on several ranks first."""
    t = sorted((min(max(x, 0), 100) for x in tracks), reverse=True)
    return round(min(t[0] + share * sum(t[1:]), 100), 1) if t else 0.0


def action_weight(ctx: Ctx, key: str, action: str) -> float:
    a = str(action or "").lower()
    for k, w in program_settings(ctx, key)["engagement"]["actions"]:
        if k in a:
            return float(w)
    return 1.0  # an action we don't recognise still shows some interest


def engagement(ctx: Ctx, key: str, user_id: str) -> tuple[float, str]:
    """0-100 from this program's channel-campaign engagement: action weight x recency x repeats."""
    e = program_settings(ctx, key)["engagement"]
    pts, parts, channels, last = 0.0, {}, set(), None
    for r in rows(ctx.conn, "SELECT * FROM campaign_engagement WHERE program=? AND user_id=?", (key, user_id)):
        day = parse_dt(r["day"], ctx.tz)
        age = max((ctx.now() - day).total_seconds() / 86400, 0) if day else 30
        pts += action_weight(ctx, key, r["action"]) * 0.5 ** (age / e["half_life_days"]) * min(1 + math.log2(max(r["count"], 1)), 3)
        parts[r["action"]] = parts.get(r["action"], 0) + r["count"]
        channels.add(r["channel"])
        last = max(last or r["day"], r["day"])
    if not parts:
        return 0.0, ""
    value = round(100 * (1 - math.exp(-max(pts, 0) / e["scale"])), 1)
    top = ", ".join(f"{a} x{n}" for a, n in sorted(parts.items(), key=lambda x: -action_weight(ctx, key, x[0]))[:3])
    return value, f"campaigns ({'/'.join(sorted(channels))}): {top}, last {fmt_day(ctx, last)}"


def member(ctx: Ctx, key: str, lead_id: str) -> dict | None:
    return one(ctx.conn, "SELECT * FROM drive_members WHERE program=? AND lead_id=?", (key, lead_id))


def card(ctx: Ctx, key: str, m: dict) -> dict:
    """A membership merged with its client (name, phone, extra), shaped like a lead for the pitch: campaign = program."""
    lead = one(ctx.conn, "SELECT * FROM leads WHERE lead_id=?", (m["lead_id"],)) or {"lead_id": m["lead_id"], "we_user_id": m["lead_id"]}
    return {**lead, **{k: m[k] for k in ("heat_score", "heat_tier", "heat_reasons", "npc_count", "last_outcome", "last_contact_at",
                                          "assigned_to", "assigned_at", "segment")},
            "campaign": key, "member_status": m["status"], "next_at": m["next_at"], "lead_status": lead.get("status")}


def rescore(ctx: Ctx, key: str, lead_id: str) -> None:
    from .activity import lead_user_ids
    from .heat import contributions, score as app_score, tier_for

    lead = one(ctx.conn, "SELECT * FROM leads WHERE lead_id=?", (lead_id,)) or {"lead_id": lead_id, "we_user_id": lead_id, "extra": "{}"}
    app, _ = app_score(ctx, contributions(ctx, lead_user_ids(ctx, lead)))
    value, why = program_score(ctx, key, json.loads(lead.get("extra") or "{}"), app, lead_id)
    p = get(key)
    segment = p.segment(ctx, lead) if p.segment else None
    ctx.conn.execute("UPDATE drive_members SET heat_score=?, heat_tier=?, heat_reasons=?, segment=?, updated_at=? "
                     "WHERE program=? AND lead_id=?", (value, tier_for(ctx, value), why, segment, iso(ctx.now()), key, lead_id))


def refresh_scores(ctx: Ctx, key: str) -> int:
    """Re-score the open pool (app heat, engagement and Sarthi interest change as new files arrive and as they age)."""
    pool = rows(ctx.conn, "SELECT lead_id FROM drive_members WHERE program=? AND status='open'", (key,))
    for m in pool:
        rescore(ctx, key, m["lead_id"])
    ctx.conn.commit()
    return len(pool)


# ---------------------------------------------------------------- the pool
def upsert(ctx: Ctx, key: str, cid: str, sub: str, data: dict, name: str = "", phone: str = "", category: str = "") -> str:
    """Add or refresh one client in a program's pool, storing `data` under leads.extra[sub] (shared client facts).
    Returns added / reopened / updated / busy / asked_recently / skipped."""
    p = get(key)
    now = iso(ctx.now())
    lead = one(ctx.conn, "SELECT * FROM leads WHERE lead_id=?", (cid,))
    if lead is None:  # a client the funding work doesn't know: a drive-only client
        ctx.conn.execute(
            "INSERT INTO leads (lead_id, we_user_id, name, phone, category, source, status, campaign, extra, heat_score, heat_tier, "
            "first_seen_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (cid, cid, name or None, phone or None, category or "Not yet matched in client DB", p.source, "drive", "",
             json.dumps({sub: data}), 0, "cold", now, now))
    else:
        extra = json.loads(lead["extra"] or "{}")
        extra[sub] = {**(extra.get(sub) or {}), **data}
        ctx.conn.execute("UPDATE leads SET extra=?, name=COALESCE(name, ?), phone=COALESCE(phone, ?) WHERE lead_id=?",
                         (json.dumps(extra), name or None, phone or None, cid))
        if lead["status"] == "open":
            return "busy"  # on a funding / payout / re-activation list: that call comes first (the data is kept)
        if lead["status"] == "invalid":
            return "skipped"  # wrong number, account closed
    m = member(ctx, key, cid)
    if m is None:
        ctx.conn.execute("INSERT INTO drive_members (program, lead_id, status, joined_at, updated_at) VALUES (?,?,?,?,?)",
                         (key, cid, "open", now, now))
        rescore(ctx, key, cid)
        return "added"
    if m["status"] == "open":
        rescore(ctx, key, cid)
        return "updated"
    if m["status"] not in p.reopenable:
        return "skipped"
    asked = parse_dt(m["asked_at"], ctx.tz)
    if asked and asked > ctx.now() - timedelta(days=p.settings(ctx)["cooldown_days"]):
        return "asked_recently"
    ctx.conn.execute("UPDATE drive_members SET status='open', assigned_to=NULL, assigned_at=NULL, npc_count=0, next_at=NULL, "
                     "updated_at=? WHERE program=? AND lead_id=?", (now, key, cid))
    rescore(ctx, key, cid)
    return "reopened"


def close_member(ctx: Ctx, key: str, cid: str, status: str) -> bool:
    """End an open membership (e.g. Sarthi switched on outside a call)."""
    m = member(ctx, key, cid)
    if not m or m["status"] != "open":
        return False
    ctx.conn.execute("UPDATE drive_members SET status=?, next_at=NULL, updated_at=? WHERE program=? AND lead_id=?",
                     (status, iso(ctx.now()), key, cid))
    return True


def tally(out: dict, result: str) -> None:
    out[result] = out.get(result, 0) + 1


def drop_stale(ctx: Ctx, key: str, keep_ids: set[str]) -> int:
    """Unassigned pool clients no longer in the latest pull, and not kept by the program's own rule, leave the pool."""
    dropped = 0
    p = get(key)
    for m in rows(ctx.conn, "SELECT m.lead_id, l.extra FROM drive_members m LEFT JOIN leads l ON l.lead_id=m.lead_id "
                            "WHERE m.program=? AND m.status='open' AND m.assigned_to IS NULL", (key,)):
        if m["lead_id"] not in keep_ids and not p.keep(json.loads(m["extra"] or "{}")):
            ctx.conn.execute("UPDATE drive_members SET status='out_of_window', updated_at=? WHERE program=? AND lead_id=?",
                             (iso(ctx.now()), key, m["lead_id"]))
            dropped += 1
    return dropped


def pool(ctx: Ctx, key: str, assigned: bool | None = None) -> list[dict]:
    """Open members merged with their client details, highest score first."""
    q = "SELECT * FROM drive_members WHERE program=? AND status='open'"
    if assigned is True:
        q += " AND assigned_to IS NOT NULL"
    elif assigned is False:
        q += " AND assigned_to IS NULL"
    return [card(ctx, key, m) for m in rows(ctx.conn, q + " ORDER BY heat_score DESC, lead_id", (key,))]


ENGAGEMENT_COLS = {"id": ("ClientCode", "CLIENT_ID", "Client Code", "Client_Id", "User ID", "UserId", "user_id", "cuid"),
                   "campaign": ("Campaign", "Campaign Name", "CampaignName"), "channel": ("Channel", "Medium", "Source"),
                   "action": ("Action", "Event", "Event Name", "Engagement", "Status", "Activity"),
                   "count": ("Count", "Sum", "Times", "Events"), "day": ("Date", "Day", "Event Time", "Time", "Timestamp")}


def load_engagement(ctx: Ctx, key: str, df: pd.DataFrame, campaign: str = "", channel: str = "", source: str = "") -> dict:
    """A channel campaign's engagement export. Long form (one row per client x action, with optional campaign / channel /
    count / date columns) or wide form (one row per client, a numeric column per action: Opened, Clicked, Replied...).
    Rows are kept per client, campaign, channel, action and day, so overlapping uploads replace rather than add up.
    Engaged clients join the pool; every pool score updates."""
    p = get(key)
    c = {k: col(df, *names) for k, names in ENGAGEMENT_COLS.items()}
    if not c["id"]:
        raise ValueError(f"The file needs a client code column (found: {', '.join(map(str, df.columns))}).")
    today = iso(datetime.combine(ctx.today(), time(0), ctx.tz))
    if c["action"]:
        recs = [(r, str(r.get(c["action"]) or "").strip(), to_int(r.get(c["count"])) if c["count"] else 1)
                for r in df.to_dict("records")]
    else:  # wide: every other numeric column is an action
        skip = {v for v in c.values() if v}
        actions = [x for x in df.columns if x not in skip and pd.to_numeric(df[x], errors="coerce").notna().any()]
        if not actions:
            raise ValueError("No action column (e.g. Event / Action) or per-action count columns (Opened, Clicked...) found.")
        recs = [(r, str(a), to_int(r.get(a))) for r in df.to_dict("records") for a in actions]
    out = {"rows": len(df), "engaged": 0, "opted_out": 0, "actions": {}, "added": 0, "reopened": 0, "updated": 0, "busy": 0,
           "asked_recently": 0, "skipped": 0}
    touched: dict[str, set] = {}
    for r, action, n in recs:
        cid = norm_client_id(r.get(c["id"]))
        if not cid or not action or n <= 0:
            continue
        day = parse_dt(r.get(c["day"]), ctx.tz) if c["day"] else None
        camp = (str(r.get(c["campaign"]) or "").strip() if c["campaign"] else "") or campaign or f"{p.label} campaign"
        chan = (str(r.get(c["channel"]) or "").strip() if c["channel"] else "") or channel or "unknown"
        d = iso(datetime.combine(day.date(), time(0), ctx.tz)) if day else today
        ctx.conn.execute("""INSERT INTO campaign_engagement (program, user_id, campaign, channel, action, day, count, source_file)
                            VALUES (?,?,?,?,?,?,?,?) ON CONFLICT (program, user_id, campaign, channel, action, day)
                            DO UPDATE SET count=excluded.count, source_file=excluded.source_file""",
                         (key, cid, camp, chan, action, d, n, source))
        out["actions"][action] = out["actions"].get(action, 0) + n
        w = action_weight(ctx, key, action)
        if w > 0:
            touched.setdefault(cid, set()).add(camp)
        elif w < 0:
            out["opted_out"] += 1
    for cid, camps in touched.items():
        out["engaged"] += 1
        tally(out, upsert(ctx, key, cid, f"{key}_campaign", {"campaigns": sorted(camps), "updated_at": iso(ctx.now())}))
    ctx.conn.commit()
    refresh_scores(ctx, key)  # opt-outs and older engagement move scores too
    return out


def channel_rows(ctx: Ctx, key: str) -> list[dict]:
    """Per channel campaign: clients engaged, their tier now and how many were called (programs add their successes)."""
    out = []
    for g in rows(ctx.conn, "SELECT campaign, channel, COUNT(DISTINCT user_id) AS users, MAX(day) AS last FROM campaign_engagement "
                            "WHERE program=? GROUP BY campaign, channel ORDER BY MAX(day) DESC", (key,)):
        users = [r["user_id"] for r in rows(ctx.conn, "SELECT DISTINCT user_id FROM campaign_engagement WHERE program=? AND campaign=? "
                                                      "AND channel=?", (key, g["campaign"], g["channel"]))]
        marks = ",".join("?" * len(users))
        tiers = {r["heat_tier"]: r["n"] for r in rows(ctx.conn, f"SELECT heat_tier, COUNT(*) AS n FROM drive_members WHERE "
                                                                 f"lead_id IN ({marks}) AND program=? GROUP BY heat_tier", (*users, key))}
        called = one(ctx.conn, f"SELECT COUNT(DISTINCT lead_id) AS n FROM calls WHERE program=? AND lead_id IN ({marks})",
                     (key, *users))["n"]
        out.append({**g, "user_ids": users, "hot": tiers.get("hot", 0), "warm": tiers.get("warm", 0), "cold": tiers.get("cold", 0),
                    "called": called})
    return out


# ---------------------------------------------------------------- refresh bookkeeping
def last_refresh(ctx: Ctx, key: str, what: str = "refreshed") -> dict | None:
    raw = get_setting(ctx.conn, f"{key}_{what}_at")
    return json.loads(raw) if raw else None


def mark_refresh(ctx: Ctx, key: str, data: dict, what: str = "refreshed") -> dict:
    data = {"at": iso(ctx.now()), **data}
    set_setting(ctx.conn, f"{key}_{what}_at", json.dumps(data), iso(ctx.now()))
    return data


def is_stale(ctx: Ctx, key: str, hours: float) -> bool:
    at = parse_dt((last_refresh(ctx, key) or {}).get("at"), ctx.tz)
    return not at or ctx.now() - at >= timedelta(hours=hours)


# ---------------------------------------------------------------- campaigns
def campaigns(ctx: Ctx, key: str) -> list[dict]:
    return rows(ctx.conn, "SELECT * FROM drive_campaigns WHERE program=? ORDER BY started_at DESC, id DESC", (key,))


def active_campaign(ctx: Ctx, key: str) -> dict | None:
    return one(ctx.conn, "SELECT * FROM drive_campaigns WHERE program=? AND ended_at IS NULL ORDER BY started_at DESC, id DESC LIMIT 1",
               (key,))


def start_campaign(ctx: Ctx, key: str, name: str, by: str | None = None) -> dict:
    """Start a fresh campaign: the current one ends, every ask nobody has called yet goes back to the pool, and the pool
    is re-scored. Results from here on are reported against the new campaign."""
    returned = end_campaign(ctx, key)
    ctx.conn.execute("INSERT INTO drive_campaigns (program, name, started_at, started_by) VALUES (?,?,?,?)",
                     (key, name.strip() or f"{get(key).label} campaign {ctx.now():%d %b %Y}", iso(ctx.now()), by))
    ctx.conn.commit()
    refresh_scores(ctx, key)
    return {**active_campaign(ctx, key), "returned_to_pool": returned}


def end_campaign(ctx: Ctx, key: str) -> int:
    returned = end_drive(ctx, key)
    ctx.conn.execute("UPDATE drive_campaigns SET ended_at=? WHERE program=? AND ended_at IS NULL", (iso(ctx.now()), key))
    ctx.conn.commit()
    return returned


# ---------------------------------------------------------------- drives
def _start(ctx: Ctx, key: str) -> datetime:
    """First slot of a drive: the program's call-from time today (e.g. after the market close), else the next working day's."""
    now = ctx.now()
    at = time.fromisoformat(program_settings(ctx, key)["call_from"])
    day = now.date() if ctx.is_working_day(now.date()) else ctx.next_working_day(now.date())
    start = max(now, datetime.combine(day, at, ctx.tz))
    if start >= ctx.day_end(day) - timedelta(minutes=15):
        start = datetime.combine(ctx.next_working_day(day), at, ctx.tz)
    return ctx.roll_to_working_hours(start)


SEGMENT_ORDER = ["new", "resurrected", "active"]  # "new clients first": joined in the window, then returning, then the rest


def start_drive(ctx: Ctx, key: str, per_gm: int | None = None, gm_ids: list[str] | None = None, tiers: list[str] | None = None,
                start_now: bool = False, segments: list[str] | None = None, new_first: bool = True) -> dict:
    """Give each chosen GM up to `per_gm` asks from the pool: new clients first (unless new_first=False), hottest first
    within each client type. Each gets a suggested call time but no SLA."""
    from .assign import minutes_per_call
    from .notify import notify

    p = get(key)
    per_gm = p.settings(ctx)["daily_per_gm"] if per_gm is None else per_gm
    gms = [m for m in ctx.callers() if not gm_ids or m["id"] in gm_ids]
    if per_gm <= 0 or not gms:
        return {"assigned": 0}
    now = ctx.now()
    # a client handed to a GM in another drive (or put on a funding list since) waits: one ask at a time per client
    busy = {r["lead_id"] for r in rows(ctx.conn, "SELECT lead_id FROM drive_members WHERE program<>? AND status='open' "
                                                 "AND assigned_to IS NOT NULL", (key,))}
    busy |= {r["lead_id"] for r in rows(ctx.conn, "SELECT lead_id FROM leads WHERE status='open'")}
    held: dict[str, int] = {}
    candidates = []
    for m in rows(ctx.conn, "SELECT * FROM drive_members WHERE program=? AND status='open' AND assigned_to IS NULL "
                            "ORDER BY heat_score DESC, lead_id", (key,)):
        seg = m["segment"] or "active"
        if m["lead_id"] in busy or seg in p.never_hand_out or (tiers and (m["heat_tier"] or "cold") not in tiers) \
                or (segments and seg not in segments):
            continue
        reason = p.hold(ctx, m["lead_id"]) if p.hold else None
        if reason:  # e.g. switch Sarthi on first, ask for a referral a week later
            held[reason] = held.get(reason, 0) + 1
            continue
        candidates.append(m)
    if new_first:
        rank = {s: i for i, s in enumerate(p.segment_order or SEGMENT_ORDER)}
        candidates.sort(key=lambda m: (rank.get(m["segment"] or "active", 99), -(m["heat_score"] or 0), m["lead_id"]))
    camp = active_campaign(ctx, key) or start_campaign(ctx, key, f"{p.label} campaign {now:%d %b %Y}")
    start = ctx.roll_to_working_hours(now) if start_now else _start(ctx, key)
    pace = timedelta(minutes=minutes_per_call(ctx))
    room = {m["id"]: per_gm for m in gms}
    slot, given = {}, {}
    while candidates and room:
        for gm in sorted(room):
            if not candidates:
                break
            m = candidates.pop(0)
            due = slot.get(gm) or start
            slot[gm] = due + pace
            ctx.conn.execute("UPDATE drive_members SET assigned_to=?, assigned_at=?, next_at=?, drive_at=?, campaign_id=?, updated_at=? "
                             "WHERE program=? AND lead_id=?", (gm, iso(now), iso(ctx.roll_to_working_hours(due)), iso(now), camp["id"],
                                                               iso(now), key, m["lead_id"]))
            ctx.conn.execute("INSERT INTO drive_asks (program, campaign_id, lead_id, agent_id, assigned_at) VALUES (?,?,?,?,?)",
                             (key, camp["id"], m["lead_id"], gm, iso(now)))
            given[gm] = given.get(gm, 0) + 1
            room[gm] -= 1
            if room[gm] <= 0:
                room.pop(gm)
    ctx.conn.commit()
    for gm, n in given.items():
        subject, body = p.notify(n, start)
        notify(ctx, gm, "info", subject, body)
    return {"assigned": sum(given.values()), "by_caller": given, "from": iso(start), "campaign": camp["name"], "held": held}


def my_calls(ctx: Ctx, key: str, agent_id: str) -> list[dict]:
    """A GM's open asks in a program: due ones first (earliest), then the rest, hottest first."""
    now = iso(ctx.now())
    out = [card(ctx, key, m) for m in rows(ctx.conn, "SELECT * FROM drive_members WHERE program=? AND status='open' AND assigned_to=?",
                                           (key, agent_id))]
    for c in out:
        c["next_at"] = c["next_at"] or c["assigned_at"] or now
        c["due"] = c["next_at"] <= now
    return sorted(out, key=lambda c: (not c["due"], c["next_at"] if c["due"] else "", -(c["heat_score"] or 0)))


def end_drive(ctx: Ctx, key: str, gm_ids: list[str] | None = None) -> int:
    """Take unworked asks back into the pool. Asks a GM has already called stay with them."""
    back = 0
    for m in rows(ctx.conn, "SELECT * FROM drive_members WHERE program=? AND status='open' AND assigned_to IS NOT NULL", (key,)):
        if gm_ids and m["assigned_to"] not in gm_ids:
            continue
        if one(ctx.conn, "SELECT 1 FROM calls WHERE program=? AND lead_id=? AND called_at>=? LIMIT 1", (key, m["lead_id"], m["drive_at"] or "")):
            continue
        ctx.conn.execute("UPDATE drive_members SET assigned_to=NULL, assigned_at=NULL, next_at=NULL, updated_at=? WHERE program=? AND lead_id=?",
                         (iso(ctx.now()), key, m["lead_id"]))
        ctx.conn.execute("UPDATE drive_asks SET returned_at=? WHERE program=? AND lead_id=? AND returned_at IS NULL",
                         (iso(ctx.now()), key, m["lead_id"]))
        back += 1
    ctx.conn.commit()
    return back


# ---------------------------------------------------------------- calls
OUTCOME_STATUS = {"referral_given": "referred", "activated": "activated", "not_interested": "not_interested",
                  "wrong_number": "invalid"}


def log_call(ctx: Ctx, key: str, lead_id: str, agent_id: str, outcome: str, notes: str = "", next_at: datetime | None = None) -> dict:
    """Record a drive call (tagged calls.program) and move the membership on. No follow-up, SLA or escalation:
    not picked up -> try again after the admin's NPC retry hours (a few times, then let it be); a 'no' is final;
    'later' / call back -> the agreed time (or the next working day)."""
    from .followups import OUTCOMES

    if outcome not in OUTCOMES:
        raise ValueError(f"Unknown outcome {outcome!r}")
    m = member(ctx, key, lead_id)
    if not m:
        raise ValueError(f"{lead_id} is not on the {get(key).label} list")
    now = ctx.now()
    spec = OUTCOMES[outcome]
    ctx.conn.execute("INSERT INTO calls (lead_id, agent_id, called_at, outcome, notes, next_followup_at, sarthi_activated, program) "
                     "VALUES (?,?,?,?,?,?,?,?)", (lead_id, agent_id, iso(now), outcome, notes, iso(next_at),
                                                  int(outcome == "activated"), key))
    status, npc, nxt = m["status"], m["npc_count"], None
    if spec.get("npc"):
        last = parse_dt(m["last_contact_at"], ctx.tz)
        npc = npc if npc and last and now - last < timedelta(hours=1) else npc + 1  # a double tap is one attempt
        if npc >= get(key).settings(ctx)["max_attempts"]:
            status = "unreachable"
        else:
            nxt = ctx.roll_to_working_hours(now + timedelta(hours=ctx.rules.get("npc_retry_hours", 4)))
    else:
        npc = 0
        status = OUTCOME_STATUS.get(outcome, "open")
        if status == "open":
            nxt = next_at or ctx.roll_to_working_hours(now + timedelta(hours=float(ctx.rules.get("default_followup_hours", 24))))
    asked = iso(now) if not spec.get("npc") else m["asked_at"]
    ctx.conn.execute("UPDATE drive_members SET status=?, npc_count=?, last_outcome=?, last_contact_at=?, next_at=?, asked_at=?, "
                     "updated_at=? WHERE program=? AND lead_id=?",
                     (status, npc, outcome, iso(now), iso(nxt), asked, iso(now), key, lead_id))
    if outcome == "activated":  # Sarthi switched on during the call
        lead = one(ctx.conn, "SELECT extra FROM leads WHERE lead_id=?", (lead_id,))
        if lead:
            extra = json.loads(lead["extra"] or "{}")
            extra.update(sarthi_active=True, sarthi_activated_on=iso(now), sarthi_activated_by=agent_id)
            ctx.conn.execute("UPDATE leads SET extra=? WHERE lead_id=?", (json.dumps(extra), lead_id))
    if outcome == "wrong_number":
        ctx.conn.execute("UPDATE leads SET status='invalid', last_outcome='wrong_number' WHERE lead_id=? AND status='drive'", (lead_id,))
    ctx.conn.commit()
    return {"status": status, "npc_count": npc, "next_at": iso(nxt)}


def migrate_legacy(ctx: Ctx) -> int:
    """One-off: clients that drives put into leads.campaign (before memberships) become drive_members."""
    get("referral"), get("sarthi")  # both programs registered (their sources identify drive-only clients)
    moved = 0
    for lead in rows(ctx.conn, "SELECT * FROM leads WHERE campaign IN ('referral','sarthi')"):
        key = lead["campaign"]
        x = json.loads(lead["extra"] or "{}").get(key) or {}
        status = lead["status"] if lead["status"] != "drive" else "open"
        if not member(ctx, key, lead["lead_id"]):
            ctx.conn.execute(
                "INSERT INTO drive_members (program, lead_id, status, assigned_to, assigned_at, heat_score, heat_tier, heat_reasons, "
                "npc_count, last_outcome, last_contact_at, next_at, asked_at, drive_at, campaign_id, joined_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (key, lead["lead_id"], status, lead["assigned_to"], lead["assigned_at"], lead["heat_score"], lead["heat_tier"],
                 lead["heat_reasons"], lead["npc_count"], lead["last_outcome"], lead["last_contact_at"], x.get("next_at"),
                 x.get("asked_at"), x.get("drive_at"), x.get("campaign_id"), lead["first_seen_at"], iso(ctx.now())))
        funded = lead["last_outcome"] in ("deposited", "converted") and lead["source"] not in {p.source for p in PROGRAMS.values()}
        ctx.conn.execute("UPDATE leads SET status=?, campaign='', assigned_to=NULL, assigned_at=NULL, npc_count=0 WHERE lead_id=?",
                         ("converted" if funded else "drive", lead["lead_id"]))  # a funded client keeps its funded status
        ctx.conn.execute("UPDATE calls SET program=? WHERE lead_id=? AND program IS NULL", (key, lead["lead_id"]))
        moved += 1
    ctx.conn.commit()
    return moved


# ---------------------------------------------------------------- reports
def report_base(ctx: Ctx, key: str, campaign_id: int) -> dict:
    """Asks and calls in a campaign, per GM. Programs add their database-confirmed successes on top."""
    p = get(key)
    camp = one(ctx.conn, "SELECT * FROM drive_campaigns WHERE id=?", (campaign_id,))
    lo, hi = camp["started_at"], camp["ended_at"] or iso(ctx.now() + timedelta(days=1))
    asks = rows(ctx.conn, "SELECT a.*, l.name, m.heat_tier, m.heat_score, m.status, m.last_outcome, m.segment FROM drive_asks a "
                          "LEFT JOIN leads l ON l.lead_id=a.lead_id LEFT JOIN drive_members m ON m.program=a.program AND m.lead_id=a.lead_id "
                          "WHERE a.campaign_id=? ORDER BY a.assigned_at", (campaign_id,))
    asked = {a["lead_id"] for a in asks}
    calls = [c for c in rows(ctx.conn, "SELECT lead_id, agent_id, called_at, outcome, notes FROM calls WHERE program=? AND called_at>=? "
                                       "AND called_at<? ORDER BY called_at", (key, lo, hi)) if c["lead_id"] in asked]
    first_call, last_call = {}, {}
    for c in calls:
        first_call.setdefault((c["lead_id"], c["agent_id"]), c["called_at"])
        last_call[c["lead_id"]] = c
    by_gm = {}
    for a in asks:
        g = by_gm.setdefault(a["agent_id"], {"assigned": 0, "returned": 0, "called": set(), "reached": set(), "on_call": set(),
                                            "wins": 0})
        g["assigned"] += 1
        g["returned"] += 1 if a["returned_at"] else 0
    for c in calls:
        g = by_gm.get(c["agent_id"])
        if g:
            g["called"].add(c["lead_id"])
            if c["outcome"] not in NO_REACH:
                g["reached"].add(c["lead_id"])
            if c["outcome"] == p.success_outcome:
                g["on_call"].add(c["lead_id"])
    return {"campaign": camp, "lo": lo, "hi": hi, "asks": asks, "calls": calls, "first_call": first_call, "last_call": last_call,
            "by_gm": by_gm,
            "totals": {"assigned": len(asks), "returned": sum(1 for a in asks if a["returned_at"]),
                       "called": len({c["lead_id"] for c in calls}),
                       "reached": len({c["lead_id"] for c in calls if c["outcome"] not in NO_REACH}),
                       "on_call": len({c["lead_id"] for c in calls if c["outcome"] == p.success_outcome})}}


def credit(first_call: dict, client_id: str, when: str | None) -> str | None:
    """The GM who called this client in the campaign before `when` (their first such call)."""
    hits = [(t, gm) for (lid, gm), t in first_call.items() if lid == client_id and when and t <= when]
    return min(hits)[1] if hits else None


def finish_by_gm(by_gm: dict) -> dict:
    return {k: {**v, "called": len(v["called"]), "reached": len(v["reached"]), "on_call": len(v["on_call"])} for k, v in by_gm.items()}
