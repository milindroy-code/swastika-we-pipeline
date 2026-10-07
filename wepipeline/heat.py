"""Heat score: how likely a lead is to act now, from the type, recency and frequency of their app activity.

Per activity:   weight (core / non-core) x recency x frequency
    recency   = 0.5 ** (days since last done / recency_half_life_days)
    frequency = min(1 + log2(times done), frequency_cap)
Lead score  = 100 x (1 - e^(-sum / score_scale)), so it saturates near 100 instead of growing without limit.
"""
from __future__ import annotations

import json
import math
from datetime import timedelta

from .activity import base_event, lead_user_ids, signal_lookup
from .context import Ctx, iso, parse_dt
from .db import rows

TIER_RANK = {"hot": 0, "warm": 1, "cold": 2}
TIER_BADGE = {"hot": "🔥 Hot", "warm": "🌤️ Warm", "cold": "❄️ Cold"}


def _settings(ctx: Ctx) -> dict:
    h = {"core_weight": 10, "non_core_weight": 3, "recency_half_life_days": 3, "frequency_cap": 4,
         "score_scale": 35, "lookback_days": 30, "tiers": {"hot": 70, "warm": 40},
         "first_call_sla_minutes": {"hot": 30, "warm": 120, "cold": 240}, "core_activities": []}
    h.update(ctx.cfg.get("heat") or {})
    if "first_call_sla_by_tier" in ctx.rules:  # admin override from the Admin page
        h["first_call_sla_minutes"] = {**h["first_call_sla_minutes"], **ctx.rules["first_call_sla_by_tier"]}
    return h


def is_core(ctx: Ctx, activity: str, declared: str = "") -> bool:
    if declared in {"core", "non-core"}:
        return declared == "core"
    core = {c.lower() for c in _settings(ctx)["core_activities"]}
    name = base_event(activity)
    return name in core or activity.lower() in core or signal_lookup(ctx).get(name) in core


def tier_for(ctx: Ctx, score: float) -> str:
    tiers = _settings(ctx)["tiers"]
    return "hot" if score >= tiers["hot"] else "warm" if score >= tiers["warm"] else "cold"


def first_call_sla(ctx: Ctx, tier: str) -> timedelta:
    sla = _settings(ctx)["first_call_sla_minutes"]
    return timedelta(minutes=sla.get(tier, ctx.rules.get("first_call_sla_minutes", 120)))


def contributions(ctx: Ctx, user_ids: list[str]) -> dict[str, dict]:
    """activity -> {count, last, type}. Aggregated activity files override raw events for the same activity."""
    if not user_ids:
        return {}
    h = _settings(ctx)
    since = iso(ctx.now() - timedelta(days=h["lookback_days"]))
    marks = ",".join("?" * len(user_ids))
    acts: dict[str, dict] = {}
    for ev in rows(ctx.conn, f"SELECT event_name, event_time, attrs FROM events WHERE user_id IN ({marks}) AND event_time>=?",
                   (*user_ids, since)):
        n = json.loads(ev["attrs"] or "{}").get("count", 1)
        a = acts.setdefault(ev["event_name"], {"count": 0, "last": ev["event_time"], "type": ""})
        a["count"] += int(n) if str(n).isdigit() else 1
        a["last"] = max(a["last"], ev["event_time"])
    for s in rows(ctx.conn, f"SELECT * FROM activity_summary WHERE user_id IN ({marks})", tuple(user_ids)):
        if s["last_at"] and s["last_at"] < since:
            continue
        cur = acts.get(s["activity"])
        acts[s["activity"]] = {"count": max(s["count"], cur["count"] if cur else 0),
                               "last": max(filter(None, [s["last_at"], cur and cur["last"]]), default=None),
                               "type": s["activity_type"] or ""}
    return acts


def score(ctx: Ctx, acts: dict[str, dict]) -> tuple[float, list[dict]]:
    h = _settings(ctx)
    now = ctx.now()
    parts = []
    for activity, a in acts.items():
        last = parse_dt(a["last"], ctx.tz)
        days = max((now - last).total_seconds() / 86400, 0) if last else h["lookback_days"]
        core = is_core(ctx, activity, a["type"])
        weight = h["core_weight"] if core else h["non_core_weight"]
        recency = 0.5 ** (days / h["recency_half_life_days"])
        frequency = min(1 + math.log2(max(a["count"], 1)), h["frequency_cap"])
        parts.append({"activity": activity, "count": a["count"], "last": a["last"], "core": core, "days": days,
                      "points": weight * recency * frequency})
    raw = sum(p["points"] for p in parts)
    parts.sort(key=lambda p: -p["points"])
    return round(100 * (1 - math.exp(-raw / h["score_scale"])), 1), parts


def describe(part: dict) -> str:
    days = part["days"]
    when = "today" if days < 1 else "yesterday" if days < 2 else f"{int(days)} d ago"
    return f"{part['activity']} ×{part['count']} ({'core' if part['core'] else 'non-core'}, last {when})"


def refresh_heat(ctx: Ctx) -> dict:
    """Re-score every open lead; pull forward the follow-up of any lead that just turned hot."""
    from .followups import add_followup, pending_for_lead
    from .notify import notify

    now = ctx.now()
    counts = {"hot": 0, "warm": 0, "cold": 0}
    became_hot = []
    for lead in rows(ctx.conn, "SELECT * FROM leads WHERE status='open'"):
        value, parts = score(ctx, contributions(ctx, lead_user_ids(ctx, lead)))
        tier = tier_for(ctx, value)
        counts[tier] += 1
        reasons = "; ".join(describe(p) for p in parts[:3])
        ctx.conn.execute("UPDATE leads SET heat_score=?, heat_tier=?, heat_reasons=? WHERE lead_id=?",
                         (value, tier, reasons, lead["lead_id"]))
        if tier == "hot" and lead["heat_tier"] != "hot":
            became_hot.append(lead["lead_id"])
            if lead["assigned_to"]:
                pending = pending_for_lead(ctx, lead["lead_id"])
                window = now + first_call_sla(ctx, "hot")
                if not pending or min(parse_dt(p["due_at"], ctx.tz) for p in pending) > window:
                    add_followup(ctx, lead["lead_id"], lead["assigned_to"], ctx.roll_to_working_hours(now), "system",
                                 f"🔥 Turned hot (score {value:.0f}): {describe(parts[0])}")
                notify(ctx, lead["assigned_to"], "info", f"🔥 Hot lead: {lead['name'] or lead['lead_id']}",
                       f"{lead['name'] or lead['lead_id']} turned hot (score {value:.0f}). {reasons}. Call them first.")
    ctx.conn.commit()
    return {**counts, "became_hot": len(became_hot)}
