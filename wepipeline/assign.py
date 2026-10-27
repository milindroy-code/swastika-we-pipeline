"""System-driven lead assignment: category match first, then the least-loaded GM under capacity."""
from __future__ import annotations

from datetime import timedelta

from .context import Ctx, iso, parse_dt
from .db import rows
from .followups import add_followup, seed_hints
from .heat import first_call_sla


def open_load(ctx: Ctx) -> dict[str, int]:
    load = {m["id"]: 0 for m in ctx.callers()}
    for r in rows(ctx.conn, "SELECT assigned_to, COUNT(*) AS n FROM leads WHERE status='open' AND assigned_to IS NOT NULL "
                           "AND COALESCE(campaign,'') NOT IN ('referral','sarthi') GROUP BY assigned_to"):
        if r["assigned_to"] in load:
            load[r["assigned_to"]] = r["n"]
    return load


def pick_caller(ctx: Ctx, category: str, load: dict[str, int], payout: bool = False, lead: dict | None = None,
                plans: dict[str, dict] | None = None, today: dict[str, int] | None = None) -> str | None:
    """The least-loaded GM with room. With plans (each GM's plan for the day), only GMs whose plan takes this client's
    list and priority, and who haven't reached their new clients for today."""
    available = [m for m in ctx.callers() if load.get(m["id"], 0) < m.get("capacity", 50)]
    if plans is not None and lead is not None:
        available = [m for m in available if plan_accepts(plans.get(m["id"]) or DEFAULT_PLAN, lead, (today or {}).get(m["id"], 0))]
    if not available and payout:
        available = list(ctx.callers())  # money is leaving: a payout is never held back for capacity or plans
    if not available:
        return None
    specialists = [m for m in available if category and category in (m.get("categories") or [])]
    pool = specialists or available  # no specialist for this category: spread across everyone
    return min(pool, key=lambda m: (load.get(m["id"], 0) / m.get("capacity", 50), m["id"]))["id"]


# ---------------------------------------------------------------- each GM's plan for the day
PLAN_LISTS = {"no_deposit": "New clients without deposit", "low_margin": "Low margin clients", "payout": "Payout customers",
              "sarthi_disabled": "Switched Sarthi off (15 days)", "sarthi_off": "Sarthi not on (new clients)",
              "reactivation": "Churned customers"}
PLAN_TIERS = {"hot": "🔥 Hot", "warm": "🌤️ Warm", "cold": "❄️ Cold"}
DEFAULT_PLAN = {"new_clients": None, "lists": list(PLAN_LISTS), "tiers": list(PLAN_TIERS)}  # nothing set: as before
# all set by the SM, per GM (set_day_plans); "lists" is in calling order (first = called first); a GM with no list or no
# priority gets no new clients


def list_key(campaign: str | None) -> str:
    return {"low_margin": "low_margin", "payout": "payout", "reactivation": "reactivation", "sarthi_disabled": "sarthi_disabled",
            "sarthi_off": "sarthi_off"}.get(campaign or "", "no_deposit")


def plan_accepts(plan: dict, lead: dict, taken_today: int) -> bool:
    if plan.get("new_clients") is not None and taken_today >= int(plan["new_clients"]):
        return False
    return list_key(lead.get("campaign")) in plan["lists"] and (lead.get("heat_tier") or "cold") in plan["tiers"]


MODES = {"plan": "SM plans each GM's day", "auto": "Automatic"}


def assignment_mode(ctx: Ctx) -> str:
    """'plan': new clients go out only by each GM's plan from their SM (no plan, no new clients; payouts always go out).
    'auto': every GM is filled up to capacity, hottest first; saved plans are kept but not used.
    Set by the admin (Admin: rules); until then config assignment.require_sm_plan decides."""
    from .db import get_setting

    v = get_setting(ctx.conn, "assignment_mode")
    if v in MODES:
        return v
    return "plan" if (ctx.cfg.get("assignment") or {}).get("require_sm_plan", False) else "auto"


def set_assignment_mode(ctx: Ctx, mode: str) -> dict:
    from .db import set_setting, write_retrying

    if mode not in MODES:
        raise ValueError(f"Unknown assignment mode: {mode}")
    write_retrying(ctx.conn, lambda: (set_setting(ctx.conn, "assignment_mode", mode, iso(ctx.now())), ctx.conn.commit()))
    return write_retrying(ctx.conn, lambda: assign_new_leads(ctx))  # switching to automatic fills GMs straight away


def day_plan(ctx: Ctx, gm_id: str) -> dict:
    """Today's plan; else the last one the GM saved (carried over, so they set it once and adjust); else no limits."""
    import json

    from .db import one

    r = one(ctx.conn, "SELECT plan, day FROM gm_plans WHERE gm_id=? AND day<=? ORDER BY day DESC LIMIT 1",
            (gm_id, ctx.today().isoformat()))
    if r:
        plan = {**DEFAULT_PLAN, **json.loads(r["plan"])}
    elif assignment_mode(ctx) == "plan":
        plan = {"new_clients": 0, "lists": [], "tiers": []}  # no SM plan yet: no new clients (payouts still go out)
    else:
        plan = dict(DEFAULT_PLAN)
    return {**plan, "saved_today": bool(r and r["day"] == ctx.today().isoformat()), "saved": bool(r)}


def _write_plan(ctx: Ctx, gm_id: str, **changes) -> None:
    """Today's plan row = the plan in force (today's, or the one carried over) with these changes."""
    import json

    from .db import write_retrying

    cur = day_plan(ctx, gm_id)
    plan = {k: cur[k] for k in DEFAULT_PLAN} | changes

    def write():
        ctx.conn.execute("INSERT INTO gm_plans (gm_id, day, plan, updated_at) VALUES (?,?,?,?) ON CONFLICT (gm_id, day) "
                         "DO UPDATE SET plan=excluded.plan, updated_at=excluded.updated_at",
                         (gm_id, ctx.today().isoformat(), json.dumps(plan), iso(ctx.now())))
        ctx.conn.commit()

    write_retrying(ctx.conn, write)


def set_day_plans(ctx: Ctx, plans: dict[str, dict]) -> dict:
    """The SM's plan for each GM today: {"new_clients": int | None, "lists": [...], "tiers": [...]}; carried over to the
    next days until changed. Hands out matching clients straight away."""
    from .db import write_retrying

    for gm_id, plan in plans.items():
        lists = [l for l in dict.fromkeys(plan.get("lists") or []) if l in PLAN_LISTS]  # the SM's order is the calling order
        tiers = [t for t in PLAN_TIERS if t in (plan.get("tiers") or [])]
        n = plan.get("new_clients")
        _write_plan(ctx, gm_id, new_clients=None if n is None else max(int(n), 0), lists=lists, tiers=tiers)
    return write_retrying(ctx.conn, lambda: assign_new_leads(ctx))


TIER_ORDER = {"hot": 0, "warm": 1, "cold": 2}


def plan_rank(plan: dict, lead: dict) -> tuple:
    """Where a client sits in a GM's day: the SM's cohort order first, then hot / warm / cold, then heat."""
    lists = plan.get("lists") or []
    k = list_key(lead.get("campaign"))
    return (lists.index(k) if k in lists else len(lists), TIER_ORDER.get(lead.get("heat_tier") or "cold", 2),
            -(lead.get("heat_score") or 0))


def assigned_today(ctx: Ctx) -> dict[str, int]:
    """New clients each GM has been given today (what their plan counts; payouts are always placed and don't count)."""
    midnight = iso(ctx.now().replace(hour=0, minute=0, second=0))
    return {r["assigned_to"]: r["n"] for r in rows(ctx.conn, "SELECT assigned_to, COUNT(*) AS n FROM leads WHERE assigned_at>=? "
                                                             "AND assigned_to IS NOT NULL AND COALESCE(campaign,'')<>'payout' "
                                                             "GROUP BY assigned_to", (midnight,))}


def todays_list_ids(ctx: Ctx) -> set[str]:
    """Clients on the latest published daily list of each cohort (Customer list > Today's lists)."""
    import json

    ids: set[str] = set()
    for r in rows(ctx.conn, """SELECT u.client_ids FROM uploads u WHERE u.id IN (SELECT MAX(upload_id) FROM cohort_runs
                               WHERE status='ready' AND upload_id IS NOT NULL GROUP BY cohort)"""):
        ids |= set(json.loads(r["client_ids"] or "[]"))
    return ids


def plan_pool(ctx: Ctx) -> list[dict]:
    """What SM plans assign from: today's daily lists (held or not), plus lists an SM released with Assign. Not the old
    hourly no-deposit pull, nor clients put back on hold by Clear overdue (unless they're on today's lists again)."""
    today = todays_list_ids(ctx)
    pool = rows(ctx.conn, "SELECT * FROM leads WHERE assigned_to IS NULL AND status='open' "
                          "AND COALESCE(campaign,'') NOT IN ('referral','sarthi') "  # referral asks have their own daily quota
                          f"ORDER BY {PRIORITY_SQL}, heat_score DESC, first_seen_at, lead_id")
    return [l for l in pool if l["lead_id"] in today or (not l["on_hold"] and l.get("released_at"))]


def waiting_for_plan(ctx: Ctx) -> dict[tuple[str, str], int]:
    """Clients an SM plan could assign now, by (list, priority): what the SM sees when planning."""
    out: dict[tuple[str, str], int] = {}
    for l in plan_pool(ctx):
        if not engaged_enough(ctx, l):
            continue  # churned clients wait until they're warm or hot
        k = (list_key(l["campaign"]), l["heat_tier"] or "cold")
        out[k] = out.get(k, 0) + 1
    return out


def first_followup(ctx: Ctx, lead: dict):
    """Use follow-up info already in the sheet when present; otherwise schedule the first call."""
    now = ctx.now()
    hints = seed_hints(lead)
    promised = parse_dt(hints.get("next_followup_at"), ctx.tz)
    if promised:
        return ctx.roll_to_working_hours(max(promised, now)), "promised", "Follow-up time carried over from sheet"
    npc_statuses = {s.lower() for s in ctx.cfg["sources"]["leads"].get("npc_statuses", [])}
    last_call = parse_dt(hints.get("last_call_at"), ctx.tz)
    if hints.get("last_call_status", "").strip().lower() in npc_statuses and last_call:
        retry = last_call + timedelta(hours=ctx.rules.get("npc_retry_hours", 4))
        return ctx.roll_to_working_hours(max(retry, now)), "npc_retry", "NPC recorded in sheet: retry"
    if lead.get("campaign") == "payout":  # money is leaving: call fast, whatever the heat score
        minutes = int((ctx.cfg.get("payout") or {}).get("first_call_minutes", 30))
        return (ctx.roll_to_working_hours(ctx.roll_to_working_hours(now) + timedelta(minutes=minutes)), "first_call",
                "💸 Payout requested: check in early (the payout proceeds as normal)")
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
    waiting = [l for l in rows(ctx.conn, "SELECT * FROM leads WHERE status='open' AND assigned_to IS NULL AND on_hold=0 "
                                         "AND (campaign='payout' OR heat_tier IN ('hot','warm')) AND COALESCE(campaign,'') NOT IN ('referral','sarthi') "
                                         f"ORDER BY {PRIORITY_SQL}, heat_score DESC") if engaged_enough(ctx, l)]
    need = max(len(waiting) - free, 0)
    if not need:
        return 0
    released = 0
    for lead in rows(ctx.conn, "SELECT lead_id FROM leads WHERE status='open' AND assigned_to IS NOT NULL "
                               "AND heat_tier='cold' AND COALESCE(campaign,'') NOT IN ('payout','referral','sarthi') ORDER BY heat_score, assigned_at DESC"):
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
    plans = {g["id"]: day_plan(ctx, g["id"]) for g in ctx.callers()} if assignment_mode(ctx) == "plan" else None
    today = assigned_today(ctx)
    assigned, hot, unassigned, watching = {}, {}, 0, 0
    pace = timedelta(minutes=minutes_per_call(ctx))
    slot = {}  # per GM: the next free first-call slot in this run
    # Hottest leads first, so they get a caller even when capacity runs out.
    if plans is not None:  # SM plans: today's lists, held or not (saving the plan is the assign step)
        pool = plan_pool(ctx)
    else:
        pool = rows(ctx.conn, "SELECT * FROM leads WHERE assigned_to IS NULL AND status='open' AND on_hold=0 "  # held: SM assigns
                              "AND COALESCE(campaign,'') NOT IN ('referral','sarthi') "  # referral asks have their own daily quota
                              f"ORDER BY {PRIORITY_SQL}, heat_score DESC, first_seen_at, lead_id")
    if plans:  # SM plans: in each SM's cohort order (a payout no plan takes still goes first: money is leaving)
        def order(lead):
            ranks = [plan_rank(p, lead) for p in plans.values() if list_key(lead.get("campaign")) in (p.get("lists") or [])]
            return min(ranks) if ranks else ((-1, 0, 0) if lead.get("campaign") == "payout" else (99, 0, 0))
        pool = sorted(pool, key=order)
    for lead in pool:
        if not engaged_enough(ctx, lead):
            watching += 1  # held back until their app activity makes them warm or hot
            continue
        # a payout skips plans and capacity (money is leaving), unless it's still held on today's list: then the SM's plan
        # decides when it goes out, like every other cohort
        urgent = lead.get("campaign") == "payout" and (plans is None or (not lead["on_hold"] and lead.get("released_at")))
        caller = pick_caller(ctx, lead["category"], load, payout=urgent, lead=lead, plans=plans, today=today)
        if caller is None:
            unassigned += 1
            continue
        ctx.conn.execute("UPDATE leads SET assigned_to=?, assigned_at=?, on_hold=0 WHERE lead_id=?", (caller, iso(ctx.now()), lead["lead_id"]))
        load[caller] = load.get(caller, 0) + 1
        if lead.get("campaign") != "payout":  # payouts are urgent and always placed: they don't use up the day's number
            today[caller] = today.get(caller, 0) + 1
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


def overdue_counts(ctx: Ctx) -> dict[str, int]:
    """Pending follow-ups past their due time, per GM (open clients only)."""
    return {r["agent_id"]: r["n"] for r in rows(ctx.conn, """SELECT f.agent_id, COUNT(*) AS n FROM followups f
            JOIN leads l ON l.lead_id = f.lead_id WHERE f.status='pending' AND l.status='open' AND f.due_at < ?
            GROUP BY f.agent_id""", (iso(ctx.now()),))}


def clear_overdue(ctx: Ctx, gm_ids: list[str] | None = None) -> dict:
    """Admin's "start fresh" for these GMs: every overdue follow-up is cleared (cancelled) with its warnings and
    escalations. Nobody is re-assigned: never-called clients go back to their list on hold (an SM can assign them again
    from Customer list), clients a GM has already worked stay with that GM. Call history is never touched.
    A few set-wide statements, retried if the scheduler holds the database."""
    from .db import write_retrying

    now = iso(ctx.now())
    wanted = list(gm_ids) if gm_ids else [g["id"] for g in ctx.callers()]
    if not wanted:
        return {"cleared": 0, "back_on_hold": 0, "kept": 0, "alerts_cleared": 0}
    gm_marks = ",".join("?" * len(wanted))
    overdue = f"""SELECT f.id FROM followups f JOIN leads l ON l.lead_id = f.lead_id WHERE f.status='pending' AND l.status='open'
                  AND f.due_at < ? AND f.agent_id IN ({gm_marks})"""
    # never called, and nothing pending but the automatic first call: these leave the GM's queue
    unworked = f"""SELECT DISTINCT f.lead_id FROM followups f JOIN leads l ON l.lead_id = f.lead_id
                   WHERE f.status='pending' AND l.status='open' AND f.due_at < ? AND f.agent_id IN ({gm_marks})
                     AND l.assigned_to = f.agent_id AND f.kind IN ('first_call','system')
                     AND NOT EXISTS (SELECT 1 FROM calls c WHERE c.lead_id = f.lead_id)
                     AND NOT EXISTS (SELECT 1 FROM followups o WHERE o.lead_id = f.lead_id AND o.status='pending'
                                     AND o.kind NOT IN ('first_call','system'))"""
    args = (now, *wanted)

    def write():
        released = [r["lead_id"] for r in rows(ctx.conn, unworked, args)]
        alerts = ctx.conn.execute(f"DELETE FROM alerts WHERE followup_id IN ({overdue})", args).rowcount
        cleared = ctx.conn.execute(f"UPDATE followups SET status='cancelled', completed_at=?, reason=COALESCE(reason,'') || "
                                   f"' (released: overdue cleared by admin)' WHERE id IN ({overdue})", (now, *args)).rowcount
        for i in range(0, len(released), 500):
            chunk = released[i:i + 500]
            ctx.conn.execute(f"UPDATE leads SET assigned_to=NULL, assigned_at=NULL, on_hold=1, updated_at=? "
                             f"WHERE lead_id IN ({','.join('?' * len(chunk))})", (now, *chunk))
        ctx.conn.commit()
        return {"cleared": cleared, "back_on_hold": len(released), "kept": cleared - len(released), "alerts_cleared": alerts}

    return write_retrying(ctx.conn, write)


UNWORKED_SQL = """SELECT l.lead_id, l.assigned_to FROM leads l WHERE l.status='open' AND l.assigned_to IN ({gms})
    AND COALESCE(l.campaign,'') NOT IN ('referral','sarthi')
    AND NOT EXISTS (SELECT 1 FROM calls c WHERE c.lead_id = l.lead_id)
    AND NOT EXISTS (SELECT 1 FROM followups o WHERE o.lead_id = l.lead_id AND o.status='pending'
                    AND o.kind NOT IN ('first_call','system'))"""


def unworked_counts(ctx: Ctx) -> dict[str, int]:
    """Clients each GM holds but hasn't called yet (no call, nothing pending but the automatic first call)."""
    gms = [g["id"] for g in ctx.callers()]
    if not gms:
        return {}
    out: dict[str, int] = {}
    for r in rows(ctx.conn, UNWORKED_SQL.format(gms=",".join("?" * len(gms))), tuple(gms)):
        out[r["assigned_to"]] = out.get(r["assigned_to"], 0) + 1
    return out


def take_back_unworked(ctx: Ctx, gm_ids: list[str]) -> dict:
    """Admin: take back every client these GMs haven't called yet (due or not). Their automatic first calls are cancelled
    and they go back on hold in their list, so nobody gets them until an SM plans or assigns them again. Clients a GM has
    called or promised a callback stay with them."""
    from .db import write_retrying

    if not gm_ids:
        return {"taken_back": 0}
    now = iso(ctx.now())

    def write():
        ids = [r["lead_id"] for r in rows(ctx.conn, UNWORKED_SQL.format(gms=",".join("?" * len(gm_ids))), tuple(gm_ids))]
        for i in range(0, len(ids), 500):
            ch = ids[i:i + 500]
            m = ",".join("?" * len(ch))
            ctx.conn.execute(f"DELETE FROM alerts WHERE followup_id IN (SELECT id FROM followups WHERE status='pending' AND lead_id IN ({m}))", ch)
            ctx.conn.execute(f"UPDATE followups SET status='cancelled', completed_at=?, reason=COALESCE(reason,'') || "
                             f"' (released: taken back by admin)' WHERE status='pending' AND lead_id IN ({m})", (now, *ch))
            ctx.conn.execute(f"UPDATE leads SET assigned_to=NULL, assigned_at=NULL, on_hold=1, released_at=NULL, updated_at=? "
                             f"WHERE lead_id IN ({m})", (now, *ch))
        ctx.conn.commit()
        return {"taken_back": len(ids)}

    return write_retrying(ctx.conn, write)


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
