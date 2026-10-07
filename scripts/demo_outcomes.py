"""Build a DEMO follow-up outcomes report from SIMULATED calls, to show what the Excel report looks like.

Works on a copy of the database (the real one is never touched) and runs five working days through the
real rules: NPC retries, promised callbacks, overdue warnings and SM escalations. Each dummy GM gets a
different discipline level so the report shows contrast.

    python scripts/demo_outcomes.py            # DEMO Excel report only
    python scripts/demo_outcomes.py --keep     # also keep the demo database for the demo dashboard:
        $env:WEP_DATABASE="data/demo/demo.db"; $env:WEP_NOW="<printed time>"; $env:WEP_DEMO_LABEL="DEMO data"
        python -m streamlit run app.py --server.port 8502
"""
from __future__ import annotations

import random
import shutil
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from wepipeline.assign import assign_new_leads  # noqa: E402
from wepipeline.context import ROOT, load_config, parse_dt  # noqa: E402
from wepipeline.db import open_ctx, rows  # noqa: E402
from wepipeline.excel_report import write_report  # noqa: E402
from wepipeline.followups import check_overdue, create_system_followups, log_call, queue_for  # noqa: E402
from wepipeline.heat import refresh_heat  # noqa: E402
from wepipeline.jobs import send_eod_reports, send_morning_notes, send_routine_nudges  # noqa: E402

random.seed(11)
# chance a GM calls on time / late (otherwise the follow-up is missed and picked up much later, or never)
DISCIPLINE = {"gm_01": (0.92, 0.06), "gm_02": (0.80, 0.12), "gm_03": (0.62, 0.18), "gm_04": (0.86, 0.09), "gm_05": (0.50, 0.20)}
OUTCOMES = [("not_picked", 38), ("busy", 7), ("switched_off", 5), ("interested", 18), ("connected", 10),
            ("callback_requested", 10), ("not_interested", 6), ("converted", 4), ("wrong_number", 2)]


ACTIVITY = [("ORDER_PLACED_MOB", 1, 4), ("CALL_ACCEPTED_MOB", 1, 6), ("RECOMMENDATION_LISTING_OPENED_MOB · fando_research", 1, 8),
            ("RECOMMENDATION_LISTING_OPENED_MOB · equity_research", 1, 8), ("RECOMMENDATION_LISTING_OPENED_MOB · commodity_research", 1, 5),
            ("DASHBOARD_OPENED_MOB", 2, 15)]


def sprinkle_activity(ctx, d) -> None:
    """Simulated app activity overnight and before 10:00, as a fresh WebEngage export would bring in."""
    open_leads = rows(ctx.conn, "SELECT lead_id FROM leads WHERE status='open'")
    start = ctx.day_start(d) - timedelta(hours=15)  # 19:00 the evening before
    for lead in random.sample(open_leads, min(45, len(open_leads))):
        for name, lo, hi in random.sample(ACTIVITY, random.randint(1, 4)):
            t = start + timedelta(minutes=random.randint(0, 14 * 60 + 45))
            ctx.conn.execute("INSERT INTO events (user_id, event_name, event_time, attrs, source_file) VALUES (?,?,?,?,?) "
                             "ON CONFLICT (user_id, event_name, event_time) DO NOTHING",
                             (lead["lead_id"], name, t.replace(minute=0, second=0).isoformat(), f'{{"count": {random.randint(lo, hi)}}}', "demo"))
    ctx.conn.commit()


def pick_outcome(tier: str) -> str:
    weights = [w * (1.6 if tier == "hot" and o in {"interested", "converted", "callback_requested"} else 1) for o, w in OUTCOMES]
    return random.choices([o for o, _ in OUTCOMES], weights)[0]


def main(keep: bool = False) -> None:
    src = ROOT / "data" / "growth.db"
    tmp = Path(tempfile.mkdtemp(prefix="demo_outcomes_"))
    # a consistent copy even while the dashboard has the database open
    with sqlite3.connect(src) as live, sqlite3.connect(tmp / "demo.db") as copy:
        live.backup(copy)
    cfg = load_config()
    cfg.update(database=str(tmp / "demo.db"), database_url="", reports_dir=str(tmp / "reports"))
    cfg["notifications"].update(outbox_dir=str(tmp / "outbox"), webhook_url="")
    cfg["notifications"]["email"]["enabled"] = False
    ctx = open_ctx(cfg=cfg)

    day = ctx.next_working_day(ctx.today())
    days = []
    while len(days) < 5:
        days.append(day)
        day = ctx.next_working_day(day)

    stop = datetime.combine(days[-1], datetime.min.time().replace(hour=15, minute=30), ctx.tz) if keep else None
    fate = {}  # followup id -> planned completion time (None = never called)
    for d in days:
        ctx._now = ctx.day_start(d)
        sprinkle_activity(ctx, d)
        refresh_heat(ctx)
        assign_new_leads(ctx)
        create_system_followups(ctx)
        send_morning_notes(ctx, d)
        slot = ctx.day_start(d)
        while slot < ctx.day_end(d) and not (stop and slot >= stop):
            ctx._now = slot
            send_routine_nudges(ctx)
            slot_end = slot + timedelta(minutes=30)
            for gm in ctx.callers():
                on_time, late = DISCIPLINE.get(gm["id"], (0.8, 0.1))
                for f in queue_for(ctx, gm["id"]):
                    due = parse_dt(f["due_at"], ctx.tz)
                    if f["id"] not in fate and due < slot_end:
                        r = random.random()
                        if r < on_time:
                            fate[f["id"]] = max(due, slot) + timedelta(minutes=random.randint(0, 10))
                        elif r < on_time + late:
                            fate[f["id"]] = due + timedelta(minutes=random.randint(20, 55))
                        else:
                            fate[f["id"]] = due + timedelta(minutes=random.randint(70, 240)) if random.random() < 0.6 else None
            # make the calls planned for this half hour, in time order
            todo = sorted((t, fid) for fid, t in fate.items() if t and slot <= t < slot_end and t < ctx.day_end(slot.date()))
            for t, fid in todo:
                f = rows(ctx.conn, "SELECT f.*, l.heat_tier FROM followups f JOIN leads l ON l.lead_id=f.lead_id WHERE f.id=?", (fid,))[0]
                if f["status"] != "pending":
                    continue
                ctx._now = t
                outcome = pick_outcome(f["heat_tier"])
                nxt = ctx.roll_to_working_hours(t + timedelta(hours=random.choice([2, 3, 5, 20, 26]))) if outcome == "callback_requested" else None
                notes = {"callback_requested": "Asked to call back later", "interested": "Will add funds today",
                         "not_interested": "Not interested right now", "converted": "Added funds on the call"}.get(outcome, "")
                log_call(ctx, f["lead_id"], f["agent_id"], outcome, notes, nxt)
            ctx._now = slot_end
            check_overdue(ctx)
            slot = slot_end
        if not (stop and d == days[-1]):
            ctx._now = ctx.day_end(d)
            send_eod_reports(ctx, d)

    ctx._now = stop or ctx.day_end(days[-1])
    out = ROOT / "data" / "reports" / f"DEMO_followup_outcomes_{days[0]:%Y%m%d}_{days[-1]:%Y%m%d}.xlsx"
    path = write_report(ctx, days[0], days[-1], out,
                        title_note="DEMO: simulated calls on dummy GMs, not real activity")
    n = rows(ctx.conn, "SELECT COUNT(*) n FROM calls")[0]["n"]
    print(f"{n} simulated calls over {days[0]:%d %b} to {days[-1]:%d %b}. Report: {path}")
    if keep:
        demo = ROOT / "data" / "demo"
        demo.mkdir(parents=True, exist_ok=True)
        ctx.conn.close()
        with sqlite3.connect(tmp / "demo.db") as sim, sqlite3.connect(demo / "demo.db") as out:
            sim.backup(out)
        print(f"Demo database: {demo / 'demo.db'}  ·  frozen clock: {ctx._now:%Y-%m-%dT%H:%M}")
    shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main(keep="--keep" in sys.argv)
