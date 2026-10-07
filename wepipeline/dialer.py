"""Autodialer ordering: which customer a GM should call next, and how urgent it is.

Order: overdue (closest to escalating first) → due now → later today → upcoming, and within each group
🔥 hot before 🌤️ warm before ❄️ cold. Only overdue and due-now calls are dialled automatically;
later ones are offered with "call ahead".
"""
from __future__ import annotations

from datetime import timedelta

from .context import Ctx, iso, parse_dt
from .db import rows
from .followups import KIND_LABELS, queue_for
from .heat import TIER_RANK

OVERDUE, DUE_NOW, LATER_TODAY, UPCOMING = 0, 1, 2, 3
DUE_NOW_WINDOW = timedelta(minutes=10)


def _span(minutes: int) -> str:
    """45 min · 2 h 10 min · 6 d 21 h"""
    if minutes < 60:
        return f"{minutes} min"
    if minutes < 24 * 60:
        return f"{minutes // 60} h {minutes % 60} min" if minutes % 60 else f"{minutes // 60} h"
    return f"{minutes // 1440} d {minutes % 1440 // 60} h"


def dialer_queue(ctx: Ctx, agent_id: str) -> list[dict]:
    now = ctx.now()
    grace = timedelta(minutes=ctx.rules.get("warning_after_minutes", 15))
    escalate = timedelta(minutes=ctx.rules.get("escalate_after_minutes", 60))
    out = []
    for q in queue_for(ctx, agent_id):
        due = parse_dt(q["due_at"], ctx.tz)
        if due + grace < now:
            group = OVERDUE
        elif due <= now + DUE_NOW_WINDOW:
            group = DUE_NOW
        elif due.date() == now.date():
            group = LATER_TODAY
        else:
            group = UPCOMING
        late = int((now - due).total_seconds() // 60)
        escalates_in = int((due + escalate - now).total_seconds() // 60)
        if group == OVERDUE:
            urgency = (f"Overdue {_span(late)} · already escalated to your SM" if q["missed"]
                       else f"Overdue {_span(late)} · escalates to your SM in {max(escalates_in, 0)} min")
        elif group == DUE_NOW:
            urgency = "Due now" if late >= 0 else f"Due in {-late} min"
        else:
            urgency = f"Due {due:%H:%M}" if group == LATER_TODAY else f"Due {due:%a %d %b %H:%M}"
        out.append({**q, "group": group, "due": due, "late_min": late, "escalates_in": escalates_in,
                    "already_escalated": bool(q["missed"]), "urgency": urgency})
    # overdue: not-yet-escalated first, closest to escalating first (they can still be saved);
    # then already-escalated ones, hot first. Other groups: hot first, then earliest due.
    def key(x):
        tier = TIER_RANK.get(x["heat_tier"] or "cold", 2)
        if x["group"] == OVERDUE and not x["already_escalated"]:
            return (OVERDUE, 0, x["escalates_in"], tier, x["due"])
        return (x["group"], 1 if x["group"] == OVERDUE else 0, tier, 0, x["due"])

    out.sort(key=key)
    return out


def next_call(queue: list[dict], skipped: set, call_ahead: bool = False) -> dict | None:
    """The first callable item; skipped ones go to the back rather than disappearing."""
    callable_ = [q for q in queue if q["group"] <= DUE_NOW or call_ahead]
    fresh = [q for q in callable_ if q["lead_id"] not in skipped]
    return (fresh or callable_ or [None])[0]


def urgency_counts(queue: list[dict]) -> dict:
    return {
        "overdue": sum(1 for q in queue if q["group"] == OVERDUE),
        "escalating_soon": sum(1 for q in queue if q["group"] == OVERDUE and not q["already_escalated"] and q["escalates_in"] <= 15),
        "hot_waiting": sum(1 for q in queue if q["heat_tier"] == "hot" and q["group"] <= LATER_TODAY),
        "due_now": sum(1 for q in queue if q["group"] == DUE_NOW),
        "later_today": sum(1 for q in queue if q["group"] == LATER_TODAY),
    }


def reminders(ctx: Ctx, agent_id: str, queue: list[dict] | None = None) -> list[dict]:
    """Actionable reminders for a GM, one per customer, most important first:
    overdue warnings (⚠️), leads that just turned hot (🔥), callbacks / follow-ups due in the next 10 minutes (⏰)."""
    queue = dialer_queue(ctx, agent_id) if queue is None else queue
    by_lead = {q["lead_id"]: q for q in queue}
    out, seen = [], set()

    def add(item):
        if item["lead_id"] not in seen and item["lead_id"] in by_lead:  # only customers still waiting for a call
            seen.add(item["lead_id"])
            out.append(item)

    for a in rows(ctx.conn, "SELECT * FROM alerts WHERE agent_id=? AND manager_id IS NULL AND acknowledged_at IS NULL "
                            "AND level='warning' ORDER BY created_at", (agent_id,)):
        q = by_lead.get(a["lead_id"])
        text = f"{(q or {}).get('name') or a['lead_id']} · {q['urgency'].lower()}" if q else a["message"]
        add({"key": f"alert_{a['id']}", "lead_id": a["lead_id"], "icon": "⚠️", "text": text, "alert_id": a["id"]})
    for q in queue:
        if q["kind"] == "system" and (q["reason"] or "").startswith("🔥 Turned hot"):
            add({"key": f"hot_{q['id']}", "lead_id": q["lead_id"], "icon": "🔥",
                 "text": f"{q['name'] or q['lead_id']} just turned hot", "alert_id": None})
    for q in queue:
        if q["group"] == DUE_NOW and q["late_min"] < 0:
            add({"key": f"due_{q['id']}", "lead_id": q["lead_id"], "icon": "⏰",
                 "text": f"{KIND_LABELS.get(q['kind'], q['kind'])} with {q['name'] or q['lead_id']} at {q['due']:%H:%M}",
                 "alert_id": None})
    return out


def dismiss(ctx: Ctx, item: dict) -> None:
    """Mark a reminder as seen. Warnings are recorded in the database; the others only live in the session."""
    if item.get("alert_id"):
        ctx.conn.execute("UPDATE alerts SET acknowledged_at=? WHERE id=?", (iso(ctx.now()), item["alert_id"]))
        ctx.conn.commit()
