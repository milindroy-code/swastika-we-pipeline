"""Follow-up rules: scheduling, call outcomes, overdue warnings and escalations."""
from __future__ import annotations

import json
from datetime import datetime, timedelta

from .context import Ctx, iso, parse_dt
from .db import one, rows

# npc: counts as a not-picked-up attempt; status: closes the lead; needs_time: a callback time is required.
OUTCOMES = {
    "not_picked": {"label": "Not picked up (NPC)", "npc": True},
    "busy": {"label": "Busy / disconnected", "npc": True},
    "switched_off": {"label": "Switched off / not reachable", "npc": True},
    "callback_requested": {"label": "Asked to call back at a time", "needs_time": True},
    "interested": {"label": "Connected: interested"},
    "connected": {"label": "Connected: needs another follow-up"},
    "converted": {"label": "Converted (deposited / traded)", "status": "converted"},
    "not_interested": {"label": "Not interested", "status": "not_interested"},
    "wrong_number": {"label": "Wrong number", "status": "invalid"},
}

KIND_LABELS = {
    "first_call": "First call",
    "npc_retry": "NPC retry",
    "promised": "Promised callback",
    "system": "System follow-up",
}


def add_followup(ctx: Ctx, lead_id: str, agent_id: str, due: datetime, kind: str, reason: str = "") -> int:
    new_id = ctx.conn.execute(
        "INSERT INTO followups (lead_id, agent_id, due_at, kind, reason, created_at) VALUES (?,?,?,?,?,?) RETURNING id",
        (lead_id, agent_id, iso(due), kind, reason, iso(ctx.now())),
    ).fetchone()["id"]
    ctx.conn.commit()
    return new_id


def pending_for_lead(ctx: Ctx, lead_id: str) -> list[dict]:
    return rows(ctx.conn, "SELECT * FROM followups WHERE lead_id=? AND status='pending' ORDER BY due_at", (lead_id,))


def log_call(
    ctx: Ctx,
    lead_id: str,
    agent_id: str,
    outcome: str,
    notes: str = "",
    next_at: datetime | None = None,
    sarthi_activated: bool = False,
) -> dict:
    """Record a call, close the lead's pending follow-ups and schedule the next one per the rules.
    sarthi_activated: the GM switched Sarthi on for the customer during this call."""
    if outcome not in OUTCOMES:
        raise ValueError(f"Unknown outcome {outcome!r}")
    spec = OUTCOMES[outcome]
    if spec.get("needs_time") and not next_at:
        raise ValueError("A callback time is required for this outcome")
    if spec.get("npc"):
        next_at = None  # the retry time is the admin's rule; GMs cannot pick their own for an NPC
    lead = one(ctx.conn, "SELECT * FROM leads WHERE lead_id=?", (lead_id,))
    if not lead:
        raise ValueError(f"Unknown lead {lead_id}")

    now = ctx.now()
    call_id = ctx.conn.execute(
        "INSERT INTO calls (lead_id, agent_id, called_at, outcome, notes, next_followup_at, sarthi_activated) "
        "VALUES (?,?,?,?,?,?,?) RETURNING id",
        (lead_id, agent_id, iso(now), outcome, notes, iso(next_at), int(bool(sarthi_activated))),
    ).fetchone()["id"]
    ctx.conn.execute(
        "UPDATE followups SET status='done', completed_at=?, call_id=? WHERE lead_id=? AND status='pending'",
        (iso(now), call_id, lead_id),
    )

    npc_count = lead["npc_count"] + 1 if spec.get("npc") else 0
    status = spec.get("status", "open")
    next_followup = None

    if spec.get("npc"):
        max_attempts = ctx.rules.get("max_npc_attempts", 6)
        if npc_count >= max_attempts:
            status = "unreachable"
            _alert_manager(ctx, lead, agent_id, "info", f"{lead['name'] or lead_id} marked unreachable after {npc_count} NPC attempts")
        else:
            retry = now + timedelta(hours=ctx.rules.get("npc_retry_hours", 4))
            next_followup = (ctx.roll_to_working_hours(retry), "npc_retry", f"NPC attempt {npc_count}: retry")

    if next_at and status == "open":
        next_followup = (next_at, "promised", notes.strip()[:200] or "Promised callback")

    ctx.conn.execute(
        "UPDATE leads SET status=?, npc_count=?, last_contact_at=?, last_outcome=?, updated_at=? WHERE lead_id=?",
        (status, npc_count, iso(now), outcome, iso(now), lead_id),
    )
    if sarthi_activated:
        extra = json.loads(lead.get("extra") or "{}")
        extra.update(sarthi_active=True, sarthi_activated_on=iso(now), sarthi_activated_by=agent_id)
        ctx.conn.execute("UPDATE leads SET extra=? WHERE lead_id=?", (json.dumps(extra), lead_id))
    ctx.conn.commit()
    if next_followup:
        add_followup(ctx, lead_id, agent_id, *next_followup)
    return {"call_id": call_id, "status": status, "npc_count": npc_count,
            "next_due": iso(next_followup[0]) if next_followup else None}


def reroll_outside_hours(ctx: Ctx) -> int:
    """Move future follow-ups that now fall outside working hours (e.g. after the start time changed)
    to the next working start, so they don't all turn overdue the moment the day begins."""
    moved = 0
    for f in rows(ctx.conn, "SELECT id, due_at FROM followups WHERE status='pending' AND due_at > ?", (iso(ctx.now()),)):
        due = parse_dt(f["due_at"], ctx.tz)
        rolled = ctx.roll_to_working_hours(due)
        if rolled != due:
            ctx.conn.execute("UPDATE followups SET due_at=? WHERE id=?", (iso(rolled), f["id"]))
            moved += 1
    ctx.conn.commit()
    return moved


def reassign_lead(ctx: Ctx, lead_id: str, agent_id: str) -> None:
    now = iso(ctx.now())
    ctx.conn.execute("UPDATE leads SET assigned_to=?, assigned_at=?, updated_at=? WHERE lead_id=?", (agent_id, now, now, lead_id))
    ctx.conn.execute("UPDATE followups SET agent_id=? WHERE lead_id=? AND status='pending'", (agent_id, lead_id))
    ctx.conn.commit()


def queue_for(ctx: Ctx, agent_id: str) -> list[dict]:
    """Pending follow-ups for a GM, hottest leads first, then earliest due."""
    return rows(
        ctx.conn,
        """SELECT f.*, l.name, l.phone, l.category, l.npc_count, l.last_outcome, l.last_contact_at,
                  l.heat_score, l.heat_tier, l.heat_reasons
           FROM followups f JOIN leads l ON l.lead_id = f.lead_id
           WHERE f.agent_id=? AND f.status='pending' AND l.status='open'
           ORDER BY CASE l.heat_tier WHEN 'hot' THEN 0 WHEN 'warm' THEN 1 ELSE 2 END, f.due_at, l.heat_score DESC""",
        (agent_id,),
    )


# ---------------------------------------------------------------- overdue checks
def _manager_ids(ctx: Ctx, agent_id: str) -> list[str]:
    agent = ctx.member(agent_id) or {}
    return [ctx.sm_of(agent)] if ctx.sm_of(agent) else [m["id"] for m in ctx.managers()]


def _alert_manager(ctx: Ctx, lead: dict, agent_id: str, level: str, message: str, followup_id=None) -> None:
    from .notify import notify

    for mgr in _manager_ids(ctx, agent_id):
        ctx.conn.execute(
            "INSERT INTO alerts (followup_id, lead_id, agent_id, manager_id, level, message, created_at) VALUES (?,?,?,?,?,?,?)",
            (followup_id, lead["lead_id"], agent_id, mgr, level, message, iso(ctx.now())),
        )
        notify(ctx, mgr, level, f"[{level.upper()}] {message}", message)
    ctx.conn.commit()


def check_overdue(ctx: Ctx) -> dict:
    """Warn GMs about overdue follow-ups, then escalate to their SM and count a miss."""
    from .notify import notify

    now = ctx.now()
    warn_cut = iso(now - timedelta(minutes=ctx.rules.get("warning_after_minutes", 15)))
    esc_cut = iso(now - timedelta(minutes=ctx.rules.get("escalate_after_minutes", 60)))
    warned = escalated = 0

    for f in rows(
        ctx.conn,
        """SELECT f.*, l.name, l.phone FROM followups f JOIN leads l ON l.lead_id=f.lead_id
           WHERE f.status='pending' AND l.status='open' AND f.due_at <= ? AND (f.warned_at IS NULL OR f.escalated_at IS NULL)""",
        (warn_cut,),
    ):
        who = f["name"] or f["lead_id"]
        due = parse_dt(f["due_at"], ctx.tz).strftime("%d %b %H:%M")
        kind = KIND_LABELS.get(f["kind"], f["kind"])
        if f["warned_at"] is None:
            msg = f"{kind} for {who} ({f['phone']}) was due at {due} and is overdue. Call now."
            ctx.conn.execute("UPDATE followups SET warned_at=? WHERE id=?", (iso(now), f["id"]))
            ctx.conn.execute(
                "INSERT INTO alerts (followup_id, lead_id, agent_id, level, message, created_at) VALUES (?,?,?,?,?,?)",
                (f["id"], f["lead_id"], f["agent_id"], "warning", msg, iso(now)),
            )
            notify(ctx, f["agent_id"], "warning", f"Overdue follow-up: {who}", msg)
            warned += 1
        if f["escalated_at"] is None and f["due_at"] <= esc_cut:
            agent = (ctx.member(f["agent_id"]) or {}).get("name", f["agent_id"])
            msg = f"{agent} missed the {kind.lower()} for {who} due at {due}."
            ctx.conn.execute("UPDATE followups SET escalated_at=?, missed=1 WHERE id=?", (iso(now), f["id"]))
            _alert_manager(ctx, f, f["agent_id"], "escalation", msg, f["id"])
            escalated += 1
    ctx.conn.commit()
    return {"warned": warned, "escalated": escalated}


# ---------------------------------------------------------------- system follow-ups
def create_system_followups(ctx: Ctx) -> int:
    """Create follow-ups from app activity (trigger events) and for leads that have gone stale."""
    from .activity import base_event, lead_user_ids, signal_lookup

    now = ctx.now()
    due = ctx.roll_to_working_hours(now)
    triggers = ctx.cfg.get("triggers", {})
    by_event = signal_lookup(ctx)
    stale_cut = iso(now - timedelta(days=ctx.rules.get("stale_after_days", 3)))
    created = 0

    open_leads = rows(
        ctx.conn,
        """SELECT * FROM leads l WHERE status='open' AND assigned_to IS NOT NULL
           AND NOT EXISTS (SELECT 1 FROM followups f WHERE f.lead_id=l.lead_id AND f.status='pending')""",
    )
    for lead in open_leads:
        # newest of last contact / assignment / first seen (any can be missing on imported data)
        since = max(filter(None, [lead["last_contact_at"], lead["assigned_at"], lead["first_seen_at"]]), default="")
        last_sys = one(ctx.conn, "SELECT MAX(created_at) AS t FROM followups WHERE lead_id=? AND kind='system'", (lead["lead_id"],))
        if last_sys and last_sys["t"]:
            since = max(since, last_sys["t"])
        reason = None
        ids = lead_user_ids(ctx, lead)
        if ids and triggers:
            marks = ",".join("?" * len(ids))
            for ev in rows(
                ctx.conn,
                f"SELECT event_name, event_time FROM events WHERE user_id IN ({marks}) AND event_time > ? ORDER BY event_time DESC",
                (*ids, since),
            ):
                signal = by_event.get(base_event(ev["event_name"]))
                if signal in triggers:
                    when = parse_dt(ev["event_time"], ctx.tz).strftime("%d %b %H:%M")
                    reason = f"{triggers[signal]} ({when})"
                    break
        if not reason and lead["last_contact_at"] and lead["last_contact_at"] < stale_cut:
            reason = f"No contact since {parse_dt(lead['last_contact_at'], ctx.tz):%d %b}: re-engage"
        if reason:
            add_followup(ctx, lead["lead_id"], lead["assigned_to"], due, "system", reason)
            created += 1
    return created


def seed_hints(lead: dict) -> dict:
    return json.loads(lead.get("extra") or "{}").get("_hints", {})
