"""System-driven lead assignment: category match first, then the least-loaded GM under capacity."""
from __future__ import annotations

from datetime import timedelta

from .context import Ctx, iso, parse_dt
from .db import rows
from .followups import add_followup, seed_hints
from .heat import first_call_sla


def open_load(ctx: Ctx) -> dict[str, int]:
    load = {m["id"]: 0 for m in ctx.callers()}
    for r in rows(ctx.conn, "SELECT assigned_to, COUNT(*) AS n FROM leads WHERE status='open' AND assigned_to IS NOT NULL GROUP BY assigned_to"):
        if r["assigned_to"] in load:
            load[r["assigned_to"]] = r["n"]
    return load


def pick_caller(ctx: Ctx, category: str, load: dict[str, int]) -> str | None:
    available = [m for m in ctx.callers() if load.get(m["id"], 0) < m.get("capacity", 50)]
    if not available:
        return None
    specialists = [m for m in available if category and category in (m.get("categories") or [])]
    pool = specialists or available  # no specialist for this category: spread across everyone
    return min(pool, key=lambda m: (load.get(m["id"], 0) / m.get("capacity", 50), m["id"]))["id"]


def first_followup(ctx: Ctx, lead: dict):
    """Use follow-up info already in the sheet when present; otherwise schedule the first call."""
    now = ctx.now()
    hints = seed_hints(lead)
    promised = parse_dt(hints.get("next_followup_at"), ctx.tz)
    if promised:
        return max(promised, now), "promised", "Follow-up time carried over from sheet"
    npc_statuses = {s.lower() for s in ctx.cfg["sources"]["leads"].get("npc_statuses", [])}
    last_call = parse_dt(hints.get("last_call_at"), ctx.tz)
    if hints.get("last_call_status", "").strip().lower() in npc_statuses and last_call:
        retry = last_call + timedelta(hours=ctx.rules.get("npc_retry_hours", 4))
        return ctx.roll_to_working_hours(max(retry, now)), "npc_retry", "NPC recorded in sheet: retry"
    tier = lead.get("heat_tier") or "cold"
    reason = f"🔥 Hot new lead: {lead['heat_reasons']}" if tier == "hot" and lead.get("heat_reasons") else "New lead: introduce yourself"
    due = ctx.roll_to_working_hours(ctx.roll_to_working_hours(now) + first_call_sla(ctx, tier))
    return due, "first_call", reason


def assign_new_leads(ctx: Ctx) -> dict:
    from .notify import notify

    load = open_load(ctx)
    assigned, hot, unassigned = {}, {}, 0
    # Hottest leads first, so they get a caller even when capacity runs out.
    for lead in rows(ctx.conn, "SELECT * FROM leads WHERE assigned_to IS NULL AND status='open' "
                               "ORDER BY heat_score DESC, first_seen_at, lead_id"):
        caller = pick_caller(ctx, lead["category"], load)
        if caller is None:
            unassigned += 1
            continue
        ctx.conn.execute("UPDATE leads SET assigned_to=?, assigned_at=? WHERE lead_id=?", (caller, iso(ctx.now()), lead["lead_id"]))
        load[caller] = load.get(caller, 0) + 1
        add_followup(ctx, lead["lead_id"], caller, *first_followup(ctx, lead))
        assigned[caller] = assigned.get(caller, 0) + 1
        if lead["heat_tier"] == "hot":
            hot[caller] = hot.get(caller, 0) + 1
    ctx.conn.commit()

    hot_minutes = int(first_call_sla(ctx, "hot").total_seconds() // 60)
    for caller, n in assigned.items():
        h = hot.get(caller, 0)
        subject = f"{n} new lead(s) assigned to you" + (f" · 🔥 {h} hot" if h else "")
        body = f"{n} new lead(s) were added to your queue."
        if h:
            body += f" {h} are 🔥 hot: call them within {hot_minutes} minutes. They are at the top of your queue."
        notify(ctx, caller, "info", subject, body)
    if unassigned:
        for m in ctx.managers():
            notify(ctx, m["id"], "info", "Leads waiting for capacity",
                   f"{unassigned} lead(s) could not be assigned because every GM is at capacity.",
                   once_per_day=True)
    return {"assigned": sum(assigned.values()), "hot": sum(hot.values()), "by_caller": assigned, "unassigned": unassigned}
