"""Follow-up rules: scheduling, call outcomes, overdue warnings and escalations."""
from __future__ import annotations

import json
from datetime import datetime, timedelta

from .context import Ctx, iso, parse_dt
from .db import one, rows

# npc: counts as a not-picked-up attempt; status: closes the lead; needs_time: a callback time is required.
DRIVE_PROGRAMS = ("referral", "sarthi")  # leads.campaign values run as drives on their own dashboards

OUTCOMES = {
    "not_picked": {"label": "Not picked up (NPC)", "npc": True},
    "busy": {"label": "Busy / disconnected", "npc": True},
    "switched_off": {"label": "Switched off / not reachable", "npc": True},
    "callback_requested": {"label": "Asked to call back at a time", "needs_time": True},
    "interested": {"label": "Connected: interested"},
    "connected": {"label": "Connected: needs another follow-up"},
    "converted": {"label": "Converted (deposited / traded)", "status": "converted"},
    "referral_given": {"label": "Gave referral(s)", "status": "referred"},
    "activated": {"label": "Sarthi activated on the call", "status": "activated"},
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
    deposit_amount: float | None = None,
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
        raise ValueError(f"Unknown client {lead_id}")
    program = lead.get("campaign") or ""
    referral_ask = program in DRIVE_PROGRAMS  # a referral / Sarthi drive ask: own dashboard, no SLA or escalation

    now = ctx.now()
    call_id = ctx.conn.execute(
        "INSERT INTO calls (lead_id, agent_id, called_at, outcome, notes, next_followup_at, sarthi_activated, deposit_amount) "
        "VALUES (?,?,?,?,?,?,?,?) RETURNING id",
        (lead_id, agent_id, iso(now), outcome, notes, iso(next_at), int(bool(sarthi_activated)),
         float(deposit_amount) if deposit_amount else None),
    ).fetchone()["id"]
    ctx.conn.execute(
        "UPDATE followups SET status='done', completed_at=?, call_id=? WHERE lead_id=? AND status='pending'",
        (iso(now), call_id, lead_id),
    )

    npc_count = lead["npc_count"] + 1 if spec.get("npc") else 0
    last = parse_dt(lead.get("last_contact_at"), ctx.tz)
    if spec.get("npc") and lead["npc_count"] and last and now - last < timedelta(hours=1):
        npc_count = lead["npc_count"]  # tried again within the hour: the same attempt, not a new one
    status = spec.get("status", "open")
    next_followup = None

    extra = json.loads(lead.get("extra") or "{}")
    goal = int(ctx.rules.get("funding_target") or ctx.cfg.get("funding_target") or 0)
    if spec.get("npc") and referral_ask:  # a good client we only want a favour from: a few tries, then let it be
        from .drives import program_settings

        if npc_count >= program_settings(ctx, program)["max_attempts"]:
            status = "unreachable"
        else:
            retry = now + timedelta(hours=ctx.rules.get("npc_retry_hours", 4))
            next_followup = (ctx.roll_to_working_hours(retry), "npc_retry", f"Drive ask, NPC {npc_count}: retry")
    elif spec.get("npc"):
        max_attempts = ctx.rules.get("max_npc_attempts", 6)
        if npc_count >= max_attempts:
            if not extra.get("unreachable_revisited"):  # one more round after a pause, then stop
                extra["unreachable_revisited"] = True
                npc_count = 0
                revisit = now + timedelta(days=int(ctx.rules.get("unreachable_revisit_days", 7)))
                next_followup = (ctx.roll_to_working_hours(revisit), "system", f"Unreachable after {max_attempts} NPCs: one more round")
                _alert_manager(ctx, lead, agent_id, "info", f"{lead['name'] or lead_id}: {max_attempts} NPCs, paused; retried in "
                               f"{int(ctx.rules.get('unreachable_revisit_days', 7))} days")
            else:
                status = "unreachable"
                _alert_manager(ctx, lead, agent_id, "info", f"{lead['name'] or lead_id} marked unreachable after a second round of NPCs")
        else:
            retry = now + timedelta(hours=ctx.rules.get("npc_retry_hours", 4))
            next_followup = (ctx.roll_to_working_hours(retry), "npc_retry", f"NPC attempt {npc_count}: retry")

    if outcome == "converted" and goal:
        # the GM enters the money added on this call; it adds to what the account already had
        total = float(deposit_amount or 0) + float(extra.get("current_margin") or 0)
        extra["current_margin"] = total
        if total < goal:  # some (or unconfirmed) funding, not yet the target: keep following up
            status = "open"
            if (lead.get("campaign") or "") in {"", "new", "low_margin"}:  # re-activation / payout keep their list
                ctx.conn.execute("UPDATE leads SET campaign='low_margin' WHERE lead_id=?", (lead_id,))
            if not next_at:
                next_at = ctx.roll_to_working_hours(now + timedelta(hours=float(ctx.rules.get("default_followup_hours", 24))))
            notes = notes or f"Top up from ₹{total:,.0f} to ₹{goal:,}"
    if referral_ask and not spec.get("npc"):  # asked (whatever the answer): don't ask again within the cooldown
        extra.setdefault(program, {})["asked_at"] = iso(now)
    if outcome == "not_interested" and not referral_ask:  # a 'no' to a referral ask is final until the next cooldown
        if not extra.get("not_interested_once"):  # one more try after a pause; a second 'not interested' closes
            extra["not_interested_once"] = True
            status = "open"
            revisit = now + timedelta(days=int(ctx.rules.get("not_interested_revisit_days", 7)))
            next_followup = (ctx.roll_to_working_hours(revisit), "system", "Said not interested: one more try")
    if not spec.get("npc") and outcome not in {"not_interested", "wrong_number"}:
        extra.pop("unreachable_revisited", None)  # reached them: a later run of NPCs starts afresh
    if outcome in {"interested", "connected", "callback_requested", "converted"}:
        extra.pop("not_interested_once", None)
    if outcome in {"interested", "connected"} and not next_at:  # spoke to them, no time agreed: never leave them waiting
        next_at = ctx.roll_to_working_hours(now + timedelta(hours=float(ctx.rules.get("default_followup_hours", 24))))
        notes = notes or "Follow up (no time agreed)"

    if next_at and status == "open":
        next_followup = (next_at, "promised", notes.strip()[:200] or "Promised callback")
    if referral_ask:  # referral drives run on their own dashboard: no SLA, no escalation, just the next try
        ref = extra.setdefault(program, {})
        if next_followup and status == "open":
            ref["next_at"] = iso(next_followup[0])
        else:
            ref.pop("next_at", None)
        next_followup = None

    ctx.conn.execute(
        "UPDATE leads SET status=?, npc_count=?, last_contact_at=?, last_outcome=?, updated_at=? WHERE lead_id=?",
        (status, npc_count, iso(now), outcome, iso(now), lead_id),
    )
    if outcome == "activated":
        sarthi_activated = True
    if sarthi_activated:
        extra.update(sarthi_active=True, sarthi_activated_on=iso(now), sarthi_activated_by=agent_id)
    ctx.conn.execute("UPDATE leads SET extra=? WHERE lead_id=?", (json.dumps(extra), lead_id))
    ctx.conn.commit()
    if next_followup:
        add_followup(ctx, lead_id, agent_id, *next_followup)
    return {"call_id": call_id, "status": status, "npc_count": npc_count,
            "next_due": iso(next_followup[0]) if next_followup else None}


def expire_out_of_window(ctx: Ctx) -> int:
    """New and low-margin clients are worked within the registration window. Past it (and not funded at the target)
    they move to 'out of window' and their follow-ups stop. Re-activation and payout clients are not bound by it."""
    from .sarthi import window_days

    limit = window_days(ctx)
    expired = 0
    for lead in rows(ctx.conn, "SELECT lead_id, extra, campaign, category FROM leads WHERE status='open' "
                               "AND COALESCE(campaign,'') NOT IN ('reactivation','payout','referral','sarthi')"):
        extra = json.loads(lead["extra"] or "{}")
        reg = parse_dt(extra.get("registration_date"), ctx.tz)
        is_new = lead["campaign"] in {"new", "low_margin"} or (lead["category"] or "").startswith(("New - no deposit", "No deposit"))
        if not (is_new and reg) or (ctx.now().date() - reg.date()).days <= limit:
            continue
        ctx.conn.execute("UPDATE leads SET status='out_of_window', updated_at=? WHERE lead_id=?", (iso(ctx.now()), lead["lead_id"]))
        ctx.conn.execute("UPDATE followups SET status='cancelled', completed_at=?, reason=COALESCE(reason,'') || ' (out of the registration window)' "
                         "WHERE lead_id=? AND status='pending'", (iso(ctx.now()), lead["lead_id"]))
        expired += 1
    ctx.conn.commit()
    return expired


def reroll_outside_hours(ctx: Ctx) -> int:
    """Move future follow-ups that now fall outside working hours (e.g. after the start time changed)
    to the next working start, so they don't all turn overdue the moment the day begins."""
    moved = 0
    for f in rows(ctx.conn, "SELECT id, due_at FROM followups WHERE status='pending' AND due_at >= ? AND warned_at IS NULL",
                  (iso(ctx.now() - timedelta(minutes=1)),)):
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
    """One entry per client for a GM (its earliest pending follow-up), hottest first, then earliest due.
    A client can hold several pending follow-ups (a promised callback plus an earlier one pulled forward by
    app activity); one call closes them all, so the dialer shows the client once, with the others as notes."""
    out, seen = [], {}
    for f in _pending_rows(ctx, agent_id):
        if f["lead_id"] in seen:
            seen[f["lead_id"]]["also_pending"].append(f)
            continue
        f["also_pending"] = []
        seen[f["lead_id"]] = f
        out.append(f)
    return out


def _pending_rows(ctx: Ctx, agent_id: str) -> list[dict]:
    return rows(
        ctx.conn,
        """SELECT f.*, l.name, l.phone, l.category, l.npc_count, l.last_outcome, l.last_contact_at,
                  l.heat_score, l.heat_tier, l.heat_reasons, l.campaign
           FROM followups f JOIN leads l ON l.lead_id = f.lead_id
           WHERE f.agent_id=? AND f.status='pending' AND l.status='open'
           ORDER BY CASE WHEN l.campaign='payout' THEN 0 WHEN l.heat_tier='hot' THEN 0 WHEN l.heat_tier='warm' THEN 1 ELSE 2 END,
                    f.due_at, l.heat_score DESC""",
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
    # housekeeping: a cancelled follow-up (customer deposited, account closed, lead released) can't be missed
    ctx.conn.execute("DELETE FROM alerts WHERE followup_id IN (SELECT id FROM followups WHERE status='cancelled')")
    warn_min = ctx.rules.get("warning_after_minutes", 15)
    esc_min = ctx.rules.get("escalate_after_minutes", 60)
    warn_cut = iso(now - timedelta(minutes=warn_min))  # wall-clock pre-filter; lateness counts working minutes only
    warned = escalated = 0

    for f in rows(
        ctx.conn,
        """SELECT f.*, l.name, l.phone FROM followups f JOIN leads l ON l.lead_id=f.lead_id
           WHERE f.status='pending' AND l.status='open' AND f.due_at <= ? AND (f.warned_at IS NULL OR f.escalated_at IS NULL)""",
        (warn_cut,),
    ):
        late = ctx.working_minutes(parse_dt(f["due_at"], ctx.tz), now)
        if late < warn_min:
            continue  # due after hours / before a weekend: the clock runs from the next working start
        who = f["name"] or f["lead_id"]
        due = parse_dt(f["due_at"], ctx.tz).strftime("%d %b %H:%M")
        kind = KIND_LABELS.get(f["kind"], f["kind"])
        if f["warned_at"] is None:
            msg = f"{kind} for {who}{(' (' + f['phone'] + ')') if f['phone'] else ''} was due at {due} and is overdue. Call now."
            ctx.conn.execute("UPDATE followups SET warned_at=? WHERE id=?", (iso(now), f["id"]))
            ctx.conn.execute(
                "INSERT INTO alerts (followup_id, lead_id, agent_id, level, message, created_at) VALUES (?,?,?,?,?,?)",
                (f["id"], f["lead_id"], f["agent_id"], "warning", msg, iso(now)),
            )
            notify(ctx, f["agent_id"], "warning", f"Overdue follow-up: {who}", msg)
            warned += 1
        if f["escalated_at"] is None and late >= esc_min:
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

    soon = iso(now + timedelta(hours=2))
    open_leads = rows(
        ctx.conn,
        """SELECT l.*, (SELECT MIN(f.due_at) FROM followups f WHERE f.lead_id=l.lead_id AND f.status='pending') AS next_due
           FROM leads l WHERE status='open' AND assigned_to IS NOT NULL AND COALESCE(campaign,'') NOT IN ('referral','sarthi')""",
    )
    for lead in open_leads:
        if lead["next_due"] and lead["next_due"] <= max(soon, iso(due)):
            continue  # already being called soon
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
        if not reason and not lead["next_due"] and lead["last_contact_at"] and lead["last_contact_at"] < stale_cut:
            reason = f"No contact since {parse_dt(lead['last_contact_at'], ctx.tz):%d %b}: re-engage"
        if reason:
            add_followup(ctx, lead["lead_id"], lead["assigned_to"], due, "system", reason)
            created += 1
    return created


def seed_hints(lead: dict) -> dict:
    return json.loads(lead.get("extra") or "{}").get("_hints", {})
