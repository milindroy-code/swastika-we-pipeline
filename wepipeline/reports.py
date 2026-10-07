"""Compliance metrics, the morning note and the end-of-day report."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta

from .context import Ctx, iso, parse_dt
from .db import one, rows
from .followups import KIND_LABELS, OUTCOMES

NPC_OUTCOMES = {k for k, v in OUTCOMES.items() if v.get("npc")}


def _day_bounds(ctx: Ctx, day: date) -> tuple[str, str]:
    start = datetime.combine(day, time(0), ctx.tz)
    return iso(start), iso(start + timedelta(days=1))


def classify(ctx: Ctx, f: dict, now: datetime) -> str:
    """on_time | late | missed | overdue | upcoming for one follow-up."""
    grace = timedelta(minutes=ctx.rules.get("warning_after_minutes", 15))
    due = parse_dt(f["due_at"], ctx.tz)
    if f["missed"]:
        return "missed"
    if f["status"] == "done":
        return "on_time" if parse_dt(f["completed_at"], ctx.tz) <= due + grace else "late"
    if f["status"] == "cancelled":
        return "cancelled"
    return "overdue" if now > due + grace else "upcoming"


def compliance(ctx: Ctx, day: date, team_of: str | None = None) -> list[dict]:
    """One row per GM for the given day. Only follow-ups already due count towards compliance."""
    now = ctx.now()
    cutoff = min(now, datetime.combine(day + timedelta(days=1), time(0), ctx.tz))
    lo, hi = _day_bounds(ctx, day)
    callers = [m for m in ctx.callers() if not team_of or ctx.sm_of(m) == team_of]
    out = []
    for m in callers:
        fus = rows(ctx.conn, "SELECT * FROM followups WHERE agent_id=? AND due_at>=? AND due_at<? AND status!='cancelled'", (m["id"], lo, hi))
        counts = {"on_time": 0, "late": 0, "missed": 0, "overdue": 0, "upcoming": 0}
        for f in fus:
            counts[classify(ctx, f, now)] += 1
        due = sum(1 for f in fus if parse_dt(f["due_at"], ctx.tz) <= cutoff)
        calls = rows(ctx.conn, "SELECT outcome FROM calls WHERE agent_id=? AND called_at>=? AND called_at<?", (m["id"], lo, hi))
        carry = one(ctx.conn, "SELECT COUNT(*) AS n FROM followups f JOIN leads l ON l.lead_id=f.lead_id "
                    "WHERE f.agent_id=? AND f.status='pending' AND l.status='open' AND f.due_at<?", (m["id"], lo))["n"]
        out.append({
            "caller_id": m["id"],
            "caller": m["name"],
            "title": m.get("title", ""),
            "manager": (ctx.member(ctx.sm_of(m)) or {}).get("name", ""),
            "open_leads": one(ctx.conn, "SELECT COUNT(*) AS n FROM leads WHERE assigned_to=? AND status='open'", (m["id"],))["n"],
            "new_leads": one(ctx.conn, "SELECT COUNT(*) AS n FROM leads WHERE assigned_to=? AND assigned_at>=? AND assigned_at<?", (m["id"], lo, hi))["n"],
            "due": due,
            **counts,
            "carry_over": carry,
            "compliance_pct": round(100 * counts["on_time"] / due, 1) if due else None,
            "calls": len(calls),
            "connected": sum(1 for c in calls if c["outcome"] not in NPC_OUTCOMES),
            "npc": sum(1 for c in calls if c["outcome"] in NPC_OUTCOMES),
            "converted": sum(1 for c in calls if c["outcome"] == "converted"),
        })
    return out


def misses(ctx: Ctx, day: date, agent_id: str | None = None, team_of: str | None = None) -> list[dict]:
    """Every follow-up that was escalated (missed) for the day, plus older ones still not done."""
    lo, hi = _day_bounds(ctx, day)
    items = rows(
        ctx.conn,
        """SELECT f.*, l.name, l.phone, l.category FROM followups f JOIN leads l ON l.lead_id=f.lead_id
           WHERE f.missed=1 AND ((f.due_at>=? AND f.due_at<?) OR (f.due_at<? AND f.status='pending' AND l.status='open'))
           ORDER BY f.agent_id, f.due_at""",
        (lo, hi, lo),
    )
    team = {m["id"] for m in ctx.callers() if ctx.sm_of(m) == team_of} if team_of else None
    out = []
    for f in items:
        if agent_id and f["agent_id"] != agent_id or team is not None and f["agent_id"] not in team:
            continue
        out.append({
            "caller": (ctx.member(f["agent_id"]) or {}).get("name", f["agent_id"]),
            "lead": f["name"] or f["lead_id"],
            "phone": f["phone"],
            "category": f["category"],
            "type": KIND_LABELS.get(f["kind"], f["kind"]),
            "due": parse_dt(f["due_at"], ctx.tz).strftime("%d %b %H:%M"),
            "state": "done late" if f["status"] == "done" else "still open",
            "completed": parse_dt(f["completed_at"], ctx.tz).strftime("%H:%M") if f["completed_at"] else "",
            "reason": f["reason"],
        })
    return out


HEADERS = {"caller": "GM", "manager": "SM", "open_leads": "open leads", "new_leads": "new leads", "on_time": "on time",
           "carry_over": "carry-over", "compliance_pct": "on-time %"}


def _table(items: list[dict], cols: list[str]) -> str:
    if not items:
        return "_None._"
    head = "| " + " | ".join(HEADERS.get(c, c) for c in cols) + " |\n|" + "---|" * len(cols)
    body = "\n".join("| " + " | ".join(str(i.get(c, "") or "") for c in cols) + " |" for i in items)
    return f"{head}\n{body}"


def morning_note(ctx: Ctx, member: dict, day: date) -> tuple[str, str]:
    from .followups import queue_for
    from .pitch import market_notes

    lo, hi = _day_bounds(ctx, day)
    blocks = "\n".join(f"- **{b['time']}**: {b['block']}" for b in ctx.cfg.get("day_structure", []))
    notes = market_notes(ctx, 5)
    market = "\n".join(f"- {n}" for n in notes) or "_No market notes yet._"
    first = member["name"].split(" ")[0]

    if ctx.is_sm(member):
        prev = day - timedelta(days=1)
        while not ctx.is_working_day(prev):
            prev -= timedelta(days=1)
        team = compliance(ctx, prev, team_of=member["id"])
        missed_y = misses(ctx, prev, team_of=member["id"])
        subject = f"Good morning {first}: team plan for {day:%a %d %b}"
        body = (
            f"Good morning {first},\n\nHere is how the day is structured for your team:\n{blocks}\n\n"
            f"**Follow-up compliance on {prev:%a %d %b}**\n"
            + _table(team, ["caller", "due", "on_time", "late", "missed", "compliance_pct", "calls", "converted"])
            + f"\n\n**Misses from {prev:%d %b} ({len(missed_y)})**\n"
            + _table(missed_y[:20], ["caller", "lead", "type", "due", "state"])
            + f"\n\n**Market notes for the team**\n{market}\n"
        )
        return subject, body

    queue = queue_for(ctx, member["id"])
    overdue = [q for q in queue if q["due_at"] < lo or parse_dt(q["due_at"], ctx.tz) < ctx.now()]
    today_q = [q for q in queue if lo <= q["due_at"] < hi and q not in overdue]
    new = one(ctx.conn, "SELECT COUNT(*) AS n FROM leads WHERE assigned_to=? AND status='open' AND npc_count=0 AND last_contact_at IS NULL", (member["id"],))["n"]

    def item(q):
        return {"time": parse_dt(q["due_at"], ctx.tz).strftime("%d %b %H:%M"), "lead": q["name"] or q["lead_id"],
                "phone": q["phone"], "type": KIND_LABELS.get(q["kind"], q["kind"]), "why": q["reason"] or ""}

    subject = f"Good morning {first}: your plan for {day:%a %d %b}"
    body = (
        f"Good morning {first},\n\nThis is how today is structured:\n{blocks}\n\n"
        f"**Your numbers today:** {len(today_q)} follow-ups scheduled · {len(overdue)} overdue/carry-over · "
        f"{new} leads not yet called\n\n"
        f"**Clear these first (overdue)**\n" + _table([item(q) for q in overdue[:15]], ["time", "lead", "phone", "type", "why"])
        + "\n\n**Scheduled today**\n" + _table([item(q) for q in today_q[:25]], ["time", "lead", "phone", "type", "why"])
        + f"\n\n**Market talking points**\n{market}\n\n"
        "Open the dashboard for each lead's app activity and pitch. Log every call so your next follow-up is scheduled automatically.\n"
    )
    return subject, body


def current_block(ctx: Ctx, now: datetime) -> tuple[dict, datetime, datetime] | None:
    """The routine block running at `now`, with its start and end (the next block's start, or end of day)."""
    blocks = sorted(ctx.cfg.get("day_structure", []), key=lambda b: b["time"])
    starts = [datetime.combine(now.date(), time.fromisoformat(b["time"]), ctx.tz) for b in blocks]
    for i, (b, start) in enumerate(zip(blocks, starts)):
        end = starts[i + 1] if i + 1 < len(starts) else ctx.day_end(now.date())
        if start <= now < end:
            return b, start, end
    return None


def routine_nudge(ctx: Ctx, member: dict, block: dict, start: datetime, end: datetime) -> tuple[str, str]:
    """Short hourly message to a GM: what this hour is for, and what their queue holds right now."""
    from .followups import queue_for

    now = ctx.now()
    grace = timedelta(minutes=ctx.rules.get("warning_after_minutes", 15))
    queue = queue_for(ctx, member["id"])
    overdue = [q for q in queue if parse_dt(q["due_at"], ctx.tz) + grace < now]
    this_hour = [q for q in queue if q not in overdue and parse_dt(q["due_at"], ctx.tz) < end]
    hot = sum(1 for q in overdue + this_hour if q["heat_tier"] == "hot")
    first = member["name"].split(" ")[0]
    subject = f"{start:%H:%M}: {block['block'].split(':')[0][:60]}"
    body = (f"{first}, {start:%H:%M}–{end:%H:%M}: {block['block']}\n\n"
            f"Your queue right now: **{len(overdue)} overdue** · **{len(this_hour)} due before {end:%H:%M}**"
            + (f" · 🔥 {hot} hot" if hot else "") + ". Hot leads are at the top of My day.")
    return subject, body


def eod_report(ctx: Ctx, day: date, member: dict) -> tuple[str, str]:
    if ctx.is_sm(member):
        team = compliance(ctx, day, team_of=member["id"])
        missed = misses(ctx, day, team_of=member["id"])
        totals = {k: sum(r[k] for r in team) for k in ["due", "on_time", "late", "missed", "overdue", "calls", "converted"]}
        pct = f"{100 * totals['on_time'] / totals['due']:.0f}%" if totals["due"] else "n/a"
        subject = f"EOD follow-up report {day:%d %b}: {totals['missed']} missed, {pct} on time"
        body = (
            f"**Team summary for {day:%a %d %b}**\n"
            f"Due {totals['due']} · on time {totals['on_time']} · late {totals['late']} · **missed {totals['missed']}** · "
            f"still overdue {totals['overdue']} · calls {totals['calls']} · conversions {totals['converted']} · on-time rate {pct}\n\n"
            + _table(team, ["caller", "open_leads", "new_leads", "due", "on_time", "late", "missed", "overdue", "carry_over", "compliance_pct", "calls", "connected", "npc", "converted"])
            + f"\n\n**Missed follow-ups ({len(missed)})**\n" + _table(missed, ["caller", "lead", "phone", "type", "due", "state", "completed", "reason"])
        )
        return subject, body

    row = next((r for r in compliance(ctx, day) if r["caller_id"] == member["id"]), None)
    missed = misses(ctx, day, agent_id=member["id"])
    subject = f"Your EOD report {day:%d %b}: {len(missed)} missed follow-up(s)"
    body = (
        f"Due {row['due']} · on time {row['on_time']} · late {row['late']} · **missed {row['missed']}** · "
        f"calls {row['calls']} · conversions {row['converted']}\n\n"
        f"**Missed follow-ups**\n" + _table(missed, ["lead", "phone", "type", "due", "state", "completed"])
        + "\n\nMissed items stay at the top of your queue tomorrow until they are called."
    )
    return subject, body
