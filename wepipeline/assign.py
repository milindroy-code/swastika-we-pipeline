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
    if lead.get("campaign") == "payout":  # money is leaving: call fast, whatever the heat score
        minutes = int((ctx.cfg.get("payout") or {}).get("first_call_minutes", 30))
        return (ctx.roll_to_working_hours(ctx.roll_to_working_hours(now) + timedelta(minutes=minutes)), "first_call",
                "💸 Payout requested: call before the funds leave")
    tier = lead.get("heat_tier") or "cold"
    reason = f"🔥 Hot new client: {lead['heat_reasons']}" if tier == "hot" and lead.get("heat_reasons") else "New client: introduce yourself"
    due = ctx.roll_to_working_hours(ctx.roll_to_working_hours(now) + first_call_sla(ctx, tier))
    return due, "first_call", reason


def engaged_enough(ctx: Ctx, lead: dict) -> bool:
    """Re-activation customers have a low hit rate, so they reach a GM only once the app shows engagement."""
    if lead.get("campaign") != "reactivation":
        return True
    return (lead.get("heat_tier") or "cold") in set((ctx.cfg.get("reactivation") or {}).get("assign_tiers", ["hot", "warm"]))


PRIORITY_SQL = ("CASE WHEN campaign='payout' THEN 0 WHEN heat_tier='hot' THEN 1 WHEN heat_tier='warm' THEN 2 ELSE 3 END")


def minutes_per_call(ctx: Ctx) -> int:
    return int(ctx.rules.get("minutes_per_call", 5))


def _unworked(ctx: Ctx, lead_id: str) -> bool:
    """No call logged and nothing pending except the automatic first call / system follow-up."""
    from .db import one

    return not (one(ctx.conn, "SELECT 1 FROM calls WHERE lead_id=? LIMIT 1", (lead_id,)) or one(
        ctx.conn, "SELECT 1 FROM followups WHERE lead_id=? AND status='pending' AND kind NOT IN ('first_call','system') LIMIT 1",
        (lead_id,)))


def _release(ctx: Ctx, lead_id: str, why: str) -> None:
    # the lead leaves the GM's queue, so warnings / escalations about it are no longer theirs to answer
    ctx.conn.execute("DELETE FROM alerts WHERE followup_id IN (SELECT id FROM followups WHERE lead_id=? AND status='pending')", (lead_id,))
    ctx.conn.execute("UPDATE followups SET status='cancelled', completed_at=?, reason=COALESCE(reason,'') || ? "
                     "WHERE lead_id=? AND status='pending'", (iso(ctx.now()), f" (released: {why})", lead_id))
    ctx.conn.execute("UPDATE leads SET assigned_to=NULL, assigned_at=NULL WHERE lead_id=?", (lead_id,))


def rebalance(ctx: Ctx) -> int:
    """When GMs are full and engaged leads (🔥 / 🌤️ / payout) are waiting, swap them in for unworked ❄️ cold leads.
    Cold leads a GM has called or promised a callback stay where they are."""
    load, cap = open_load(ctx), {m["id"]: m.get("capacity", 50) for m in ctx.callers()}
    free = sum(max(cap[g] - load.get(g, 0), 0) for g in cap)
    waiting = [l for l in rows(ctx.conn, "SELECT * FROM leads WHERE status='open' AND assigned_to IS NULL "
                                         "AND (campaign='payout' OR heat_tier IN ('hot','warm')) "
                                         f"ORDER BY {PRIORITY_SQL}, heat_score DESC") if engaged_enough(ctx, l)]
    need = max(len(waiting) - free, 0)
    if not need:
        return 0
    released = 0
    for lead in rows(ctx.conn, "SELECT lead_id FROM leads WHERE status='open' AND assigned_to IS NOT NULL "
                               "AND heat_tier='cold' AND campaign<>'payout' ORDER BY heat_score, assigned_at DESC"):
        if released >= need:
            break
        if _unworked(ctx, lead["lead_id"]):
            _release(ctx, lead["lead_id"], "swapped out for an engaged client")
            released += 1
    ctx.conn.commit()
    return released


def release_unengaged_reactivation(ctx: Ctx, lead_ids: list[str] | None = None) -> int:
    """Give GM capacity back: re-activation customers who are not engaged (below the assign tiers) and whom no GM has
    worked yet (no call, nothing pending except the automatic first call / system follow-up) go back to watching."""
    from .db import one

    allowed = set((ctx.cfg.get("reactivation") or {}).get("assign_tiers", ["hot", "warm"]))
    only = f" AND lead_id IN ({','.join('?' * len(lead_ids))})" if lead_ids else ""
    released = 0
    for lead in rows(ctx.conn, "SELECT lead_id, heat_tier FROM leads WHERE campaign='reactivation' AND status='open' "
                               f"AND assigned_to IS NOT NULL{only}", tuple(lead_ids or ())):
        if (lead["heat_tier"] or "cold") in allowed:
            continue
        if not _unworked(ctx, lead["lead_id"]):
            continue  # a GM has spoken to them or promised a callback: keep it with that GM
        _release(ctx, lead["lead_id"], "re-activation, not engaged")
        released += 1
    ctx.conn.commit()
    return released


def assign_new_leads(ctx: Ctx) -> dict:
    from .notify import notify

    load = open_load(ctx)
    assigned, hot, unassigned, watching = {}, {}, 0, 0
    pace = timedelta(minutes=minutes_per_call(ctx))
    slot = {}  # per GM: the next free first-call slot in this run
    # Hottest leads first, so they get a caller even when capacity runs out.
    for lead in rows(ctx.conn, "SELECT * FROM leads WHERE assigned_to IS NULL AND status='open' "
                               f"ORDER BY {PRIORITY_SQL}, heat_score DESC, first_seen_at, lead_id"):
        if not engaged_enough(ctx, lead):
            watching += 1  # held back until their app activity makes them warm or hot
            continue
        caller = pick_caller(ctx, lead["category"], load)
        if caller is None:
            unassigned += 1
            continue
        ctx.conn.execute("UPDATE leads SET assigned_to=?, assigned_at=? WHERE lead_id=?", (caller, iso(ctx.now()), lead["lead_id"]))
        load[caller] = load.get(caller, 0) + 1
        due, kind, reason = first_followup(ctx, lead)
        if kind == "first_call":  # many leads at once: space them at a realistic pace, highest priority first
            start = slot.get(caller) or ctx.roll_to_working_hours(ctx.now())
            due = ctx.roll_to_working_hours(max(due, start))
            slot[caller] = due + pace
        add_followup(ctx, lead["lead_id"], caller, due, kind, reason)
        assigned[caller] = assigned.get(caller, 0) + 1
        if lead["heat_tier"] == "hot":
            hot[caller] = hot.get(caller, 0) + 1
    ctx.conn.commit()

    hot_minutes = int(first_call_sla(ctx, "hot").total_seconds() // 60)
    for caller, n in assigned.items():
        h = hot.get(caller, 0)
        subject = f"{n} new client(s) assigned to you" + (f" · 🔥 {h} hot" if h else "")
        body = f"{n} new client(s) were added to your queue."
        if h:
            body += f" {h} are 🔥 hot: call them within {hot_minutes} minutes. They are at the top of your queue."
        notify(ctx, caller, "info", subject, body)
    if unassigned:
        for m in ctx.managers():
            notify(ctx, m["id"], "info", "Clients waiting for capacity",
                   f"{unassigned} client(s) could not be assigned because every GM is at capacity.",
                   once_per_day=True)
    return {"assigned": sum(assigned.values()), "hot": sum(hot.values()), "by_caller": assigned, "unassigned": unassigned,
            "watching_reactivation": watching}


def go_live_reset(ctx: Ctx) -> dict:
    """Start the calling day clean: re-time every unworked first call / system follow-up that is overdue or due today,
    per GM in priority order (payout, 🔥, 🌤️, ❄️) at the GM's pace, and clear the warnings and escalations those
    follow-ups produced (they were not real misses: nobody was calling yet). Worked follow-ups are not touched."""
    now = ctx.now()
    start = ctx.roll_to_working_hours(now + timedelta(minutes=2))
    end_of_day = iso(ctx.day_end(start.date()))
    pace = timedelta(minutes=minutes_per_call(ctx))
    moved = cleared = 0
    for gm in ctx.callers():
        items = rows(ctx.conn, f"""SELECT f.id, f.lead_id FROM followups f JOIN leads l ON l.lead_id = f.lead_id
                                   WHERE f.agent_id=? AND f.status='pending' AND l.status='open'
                                     AND f.kind IN ('first_call','system') AND f.due_at <= ?
                                   ORDER BY {PRIORITY_SQL.replace("campaign", "l.campaign").replace("heat_tier", "l.heat_tier")},
                                            l.heat_score DESC, f.due_at""", (gm["id"], end_of_day))
        due = start
        for it in items:
            if not _unworked(ctx, it["lead_id"]):
                continue
            ctx.conn.execute("UPDATE followups SET due_at=?, warned_at=NULL, escalated_at=NULL, missed=0 WHERE id=?",
                             (iso(due), it["id"]))
            cleared += ctx.conn.execute("DELETE FROM alerts WHERE followup_id=?", (it["id"],)).rowcount
            moved += 1
            due = ctx.roll_to_working_hours(due + pace)
    # alerts left behind by follow-ups that were released (cancelled) before any call: not real misses either
    cleared += ctx.conn.execute("""DELETE FROM alerts WHERE followup_id IN (SELECT id FROM followups
                                   WHERE status='cancelled' AND reason LIKE '%(released:%')""").rowcount
    ctx.conn.commit()
    return {"retimed": moved, "false_alerts_cleared": cleared, "first_call_at": iso(start)}
