"""Recent calls per client, whether to call them now, and how calls move the priority score.

Calls come from Jarvis (jarvis_calls: a call of connect_seconds or more is a connect, incoming calls don't count as
attempts) and from the dashboard's own call log (a connect is any outcome that isn't an NPC or a wrong number).

Call now? (first match wins; every number is in config `call_check`):
    callback due (agreed time within 15 min, or past)       -> Callback due
    callback booked later                                    -> Callback at <time>
    daily cap: daily_cap attempts today                      -> Resting until tomorrow
    weekly cap: weekly_cap attempts in 7 days                -> Resting until the oldest drops out of the 7 days
    app activity after the last call                         -> Call now (back in the app)
    a connect in rest_after_connect_hours, no callback       -> Resting until that connect + rest_after_connect_hours
    unanswered_attempts+ attempts in unanswered_window_hours,
        no connect                                           -> Resting until the last attempt + unanswered_rest_hours
    otherwise                                                -> Call now (Never called when there is no call yet)

Score (call_adjustment, on top of the app-activity heat score; config `call_score`):
    no attempt in 7 days                                     -> +fresh_bonus      (fresh clients first)
    enough_connects+ connects and enough_talk_minutes+ talk
        in 7 days                                            -> -called_enough    (talked to enough this week)
    some_connects+ connects and some_talk_minutes+ talk      -> -some_talk        (talked to, still not funded)
    hard_to_reach_attempts+ attempts, no connect             -> -hard_to_reach
Payouts are left alone: money is leaving, they are called whatever the history.
"""
from __future__ import annotations

from datetime import datetime, time, timedelta

from .context import Ctx, iso, parse_dt
from .db import rows
from .followups import OUTCOMES

NOT_CONNECTED = {k for k, v in OUTCOMES.items() if v.get("npc")} | {"wrong_number"}
DEFAULTS = {"rest_after_connect_hours": 48, "unanswered_attempts": 3, "unanswered_window_hours": 48,
            "unanswered_rest_hours": 24, "daily_cap": 2, "weekly_cap": 6, "day_starts": "09:00", "connect_seconds": 30}
SCORE_DEFAULTS = {"fresh_bonus": 10, "enough_connects": 6, "enough_talk_minutes": 10, "called_enough": 40,
                  "some_connects": 3, "some_talk_minutes": 5, "some_talk": 15, "hard_to_reach_attempts": 5,
                  "hard_to_reach": 10, "skip_campaigns": ["payout", "referral", "sarthi"]}


def settings(ctx: Ctx) -> dict:
    return {**DEFAULTS, **(ctx.cfg.get("call_check") or {})}


def score_settings(ctx: Ctx) -> dict:
    return {**SCORE_DEFAULTS, **(ctx.cfg.get("call_score") or {})}


def _when(ctx: Ctx, dt: datetime) -> str:
    today = ctx.now().date()
    day = "today" if dt.date() == today else "tomorrow" if dt.date() == today + timedelta(days=1) else f"{dt:%a %d %b}"
    return f"{day} {dt:%H:%M}"


def _last_app_activity(ctx: Ctx, lead_ids: list[str]) -> dict[str, datetime]:
    """Latest app event per client (events are keyed by the client code)."""
    out = {}
    for i in range(0, len(lead_ids), 500):
        chunk = lead_ids[i:i + 500]
        for r in rows(ctx.conn, f"SELECT user_id, MAX(event_time) AS t FROM events WHERE user_id IN ({','.join('?' * len(chunk))}) "
                                "GROUP BY user_id", chunk):
            out[r["user_id"]] = parse_dt(r["t"], ctx.tz)
    return out


def _calls(ctx: Ctx, ids: list[str]) -> tuple[dict, dict, dict]:
    """Per client: [(time, connected, seconds, caller)] oldest first (outgoing only), incoming count, booked callback."""
    s, now = settings(ctx), ctx.now()
    since = iso(now - timedelta(days=8))
    calls: dict[str, list[tuple]] = {i: [] for i in ids}
    incoming: dict[str, int] = {}
    booked: dict[str, datetime] = {}
    upper = {i.upper(): i for i in ids}
    for k in range(0, len(ids), 500):
        chunk = ids[k:k + 500]
        marks = ",".join("?" * len(chunk))
        for r in rows(ctx.conn, f"SELECT lead_id, called_at, outcome FROM calls WHERE lead_id IN ({marks}) AND called_at>=?", (*chunk, since)):
            t = parse_dt(r["called_at"], ctx.tz)
            if t:
                calls[r["lead_id"]].append((t, r["outcome"] not in NOT_CONNECTED, 0, "dashboard"))
        ups = [c.upper() for c in chunk]
        for r in rows(ctx.conn, f"SELECT client_id, started_at, duration, caller, incoming FROM jarvis_calls "
                                f"WHERE client_id IN ({','.join('?' * len(ups))}) AND started_at>=?", (*ups, since)):
            lid = upper.get(r["client_id"])
            t = parse_dt(r["started_at"], ctx.tz)
            if not lid or not t:
                continue
            if r["incoming"]:
                if t >= now - timedelta(days=7):
                    incoming[lid] = incoming.get(lid, 0) + 1
            else:
                calls[lid].append((t, (r["duration"] or 0) >= s["connect_seconds"], r["duration"] or 0, r["caller"] or ""))
        for r in rows(ctx.conn, f"SELECT lead_id, MIN(due_at) AS due FROM followups WHERE lead_id IN ({marks}) AND status='pending' "
                                "AND kind='promised' GROUP BY lead_id", chunk):
            booked[r["lead_id"]] = parse_dt(r["due"], ctx.tz)
    for c in calls.values():
        c.sort(key=lambda x: x[0])
    return calls, incoming, booked


def call_history(ctx: Ctx, lead_ids: list[str]) -> dict[str, dict]:
    """lead_id -> the call counts and the Call now? verdict, for every id given (no calls = Never called)."""
    s, now = settings(ctx), ctx.now()
    ids = list(dict.fromkeys(lead_ids))
    calls, incoming, booked = _calls(ctx, ids)
    app = _last_app_activity(ctx, ids)
    start_h, start_m = (int(x) for x in s["day_starts"].split(":"))
    tomorrow = datetime.combine(now.date() + timedelta(days=1), time(start_h, start_m), now.tzinfo)
    plural = lambda n, w: f"{n} {w}{'s' * (n != 1)}"  # noqa: E731
    out = {}
    for i in ids:
        c = calls[i]
        within = lambda hours: [x for x in c if x[0] >= now - timedelta(hours=hours)]  # noqa: E731
        last48, week = within(48), within(24 * 7)
        today = [x for x in c if x[0].date() == now.date()]
        last = c[-1] if c else None
        talk = sum(x[2] for x in week)
        rec = {"attempts_48h": len(last48), "connects_48h": sum(x[1] for x in last48), "attempts_today": len(today),
               "attempts_7d": len(week), "connects_7d": sum(x[1] for x in week), "talk_min_7d": round(talk / 60, 1),
               "incoming_7d": incoming.get(i, 0),
               "last_call": (f"{_when(ctx, last[0])} · " + ("connected" if last[1] else "not connected")
                             + (f" {last[2] // 60}:{last[2] % 60:02d}" if last[2] else "") + (f" · {last[3]}" if last[3] else "")) if last else ""}
        rec["calls_48h"] = f"{plural(rec['attempts_48h'], 'attempt')} · {plural(rec['connects_48h'], 'connect')}"
        rec["calls_7d"] = (f"{plural(rec['attempts_7d'], 'call')} · {plural(rec['connects_7d'], 'connect')} · {rec['talk_min_7d']:g} min"
                           + (f" · {rec['incoming_7d']} incoming" if rec["incoming_7d"] else ""))
        due = booked.get(i)
        recent_connect = [x for x in c if x[1] and x[0] >= now - timedelta(hours=s["rest_after_connect_hours"])]
        unanswered = within(s["unanswered_window_hours"])
        if due and due <= now + timedelta(minutes=15):
            verdict, until = "Callback due", None
        elif due:
            verdict, until = f"Callback at {_when(ctx, due)}", due
        elif len(today) >= s["daily_cap"]:
            verdict, until = "Resting", tomorrow
        elif len(week) >= s["weekly_cap"]:
            verdict, until = "Resting", week[0][0] + timedelta(days=7)
        elif last and app.get(i) and app[i] > last[0]:
            verdict, until = "Call now (back in the app)", None
        elif recent_connect:
            verdict, until = "Resting", recent_connect[-1][0] + timedelta(hours=s["rest_after_connect_hours"])
        elif len(unanswered) >= s["unanswered_attempts"] and not any(x[1] for x in unanswered):
            verdict, until = "Resting", unanswered[-1][0] + timedelta(hours=s["unanswered_rest_hours"])
        else:
            verdict, until = ("Call now" if c else "Call now (never called)"), None
        if verdict == "Resting":
            verdict = f"Resting until {_when(ctx, until)}"
        rec.update(call_now=verdict, rest_until=iso(until) if until else None,
                   resting=verdict.startswith("Resting") or verdict.startswith("Callback at"))
        out[i] = rec
    return out


def call_adjustment(ctx: Ctx, rec: dict, campaign: str | None = None) -> tuple[float, str]:
    """Points added to (or taken off) the app-activity score from the last 7 days of calls, and why."""
    w = score_settings(ctx)
    if (campaign or "") in w["skip_campaigns"]:
        return 0.0, ""
    a, c, talk = rec["attempts_7d"], rec["connects_7d"], rec["talk_min_7d"]
    if a == 0:
        return float(w["fresh_bonus"]), "not called this week"
    if c >= w["enough_connects"] and talk >= w["enough_talk_minutes"]:
        return -float(w["called_enough"]), f"called enough this week ({c} connects, {talk:g} min): not a priority"
    if c >= w["some_connects"] and talk >= w["some_talk_minutes"]:
        return -float(w["some_talk"]), f"{c} connects, {talk:g} min this week, still not funded"
    if a >= w["hard_to_reach_attempts"] and c == 0:
        return -float(w["hard_to_reach"]), f"{a} calls this week, none connected"
    return 0.0, ""
