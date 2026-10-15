from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from wepipeline import jobs
from wepipeline.assign import assign_new_leads
from wepipeline.context import load_config, parse_dt
from tests.helpers import open_test_ctx
from wepipeline.db import one, rows
from wepipeline.followups import check_overdue, create_system_followups, log_call, queue_for
from wepipeline.ingest import sync_leads
from wepipeline.reports import compliance, misses

TZ = ZoneInfo("Asia/Kolkata")
MON_11 = datetime(2026, 10, 5, 11, 0, tzinfo=TZ)  # a Monday


def sheet(*leads):
    base = {"Lead ID": "", "WebEngage User ID": "", "Name": "", "Phone": "", "Email": "", "Category": "",
            "Source": "", "Remarks": "", "Last Call Status": "", "Last Call Time": "", "Next Follow-up": ""}
    return pd.DataFrame([{**base, **lead} for lead in leads])


@pytest.fixture
def ctx(tmp_path):
    cfg = load_config()
    cfg["database"] = str(tmp_path / "test.db")
    cfg["notifications"]["outbox_dir"] = str(tmp_path / "outbox")
    cfg["notifications"]["email"]["enabled"] = False
    cfg["notifications"]["webhook_url"] = ""
    cfg["market_notes_file"] = str(tmp_path / "none.md")
    cfg["reports_dir"] = str(tmp_path / "reports")
    cfg["sources"]["webengage"]["folder"] = str(tmp_path / "we")
    c = open_test_ctx(cfg)
    c._now = MON_11
    return c


def load(ctx, *leads):
    sync_leads(ctx, sheet(*leads))
    assign_new_leads(ctx)


def test_new_lead_is_assigned_with_first_call_sla(ctx):
    specialist = ctx.callers()[-1]
    specialist["categories"] = ["Funded - No trade"]
    load(ctx, {"Lead ID": "L1", "Name": "Asha Rao", "Phone": "+91 98765 43210", "Category": "Funded - No trade"})
    lead = one(ctx.conn, "SELECT * FROM leads WHERE lead_id='L1'")
    assert lead["phone"] == "9876543210"
    assert lead["assigned_to"] == specialist["id"]  # category specialist
    (f,) = queue_for(ctx, specialist["id"])
    assert f["kind"] == "first_call"
    cold_sla = ctx.cfg["heat"]["first_call_sla_minutes"]["cold"]  # no app activity -> cold
    assert parse_dt(f["due_at"], TZ) == MON_11 + timedelta(minutes=cold_sla)


def test_npc_resurfaces_after_four_hours(ctx):
    load(ctx, {"Lead ID": "L1", "Phone": "9000000001"})
    agent = one(ctx.conn, "SELECT assigned_to FROM leads")["assigned_to"]
    res = log_call(ctx, "L1", agent, "not_picked")
    assert res["npc_count"] == 1
    (f,) = queue_for(ctx, agent)
    assert (f["kind"], parse_dt(f["due_at"], TZ)) == ("npc_retry", MON_11 + timedelta(hours=4))


def test_late_npc_rolls_to_next_working_morning(ctx):
    load(ctx, {"Lead ID": "L1", "Phone": "9000000001"})
    agent = one(ctx.conn, "SELECT assigned_to FROM leads")["assigned_to"]
    ctx._now = datetime(2026, 10, 9, 17, 0, tzinfo=TZ)  # Friday 5pm + 4h is after hours
    log_call(ctx, "L1", agent, "busy")
    (f,) = queue_for(ctx, agent)
    assert parse_dt(f["due_at"], TZ) == ctx.day_start(datetime(2026, 10, 12).date())  # Monday, start of day


def test_max_npc_marks_unreachable_and_tells_manager(ctx):
    load(ctx, {"Lead ID": "L1", "Phone": "9000000001"})
    agent = one(ctx.conn, "SELECT assigned_to FROM leads")["assigned_to"]
    for i in range(2 * ctx.rules["max_npc_attempts"]):  # a first round pauses for a week; a second round closes
        ctx._now = MON_11 + timedelta(hours=2 * i)
        log_call(ctx, "L1", agent, "not_picked")
    assert one(ctx.conn, "SELECT status FROM leads")["status"] == "unreachable"
    assert queue_for(ctx, agent) == []
    assert one(ctx.conn, "SELECT COUNT(*) n FROM alerts WHERE level='info'")["n"] >= 1


def test_callback_needs_time_and_creates_promised_followup(ctx):
    load(ctx, {"Lead ID": "L1", "Phone": "9000000001"})
    agent = one(ctx.conn, "SELECT assigned_to FROM leads")["assigned_to"]
    with pytest.raises(ValueError):
        log_call(ctx, "L1", agent, "callback_requested")
    when = MON_11 + timedelta(hours=2, minutes=30)
    log_call(ctx, "L1", agent, "callback_requested", "Call after market close", when)
    (f,) = queue_for(ctx, agent)
    assert (f["kind"], parse_dt(f["due_at"], TZ)) == ("promised", when)


def test_overdue_warns_then_escalates_and_counts_as_missed(ctx):
    load(ctx, {"Lead ID": "L1", "Name": "Asha Rao", "Phone": "9000000001"})
    agent = one(ctx.conn, "SELECT assigned_to FROM leads")["assigned_to"]
    due = parse_dt(queue_for(ctx, agent)[0]["due_at"], TZ)

    ctx._now = due + timedelta(minutes=20)
    assert check_overdue(ctx) == {"warned": 1, "escalated": 0}
    ctx._now = due + timedelta(minutes=61)
    assert check_overdue(ctx) == {"warned": 0, "escalated": 1}
    assert check_overdue(ctx) == {"warned": 0, "escalated": 0}  # no repeat alerts

    manager = ctx.sm_of(ctx.member(agent))
    assert one(ctx.conn, "SELECT manager_id FROM alerts WHERE level='escalation'")["manager_id"] == manager
    log_call(ctx, "L1", agent, "interested")  # done late still counts as a miss
    row = next(r for r in compliance(ctx, due.date()) if r["caller_id"] == agent)
    assert (row["due"], row["missed"], row["on_time"]) == (1, 1, 0)
    assert misses(ctx, due.date())[0]["state"] == "done late"


def test_on_time_call_counts_towards_compliance(ctx):
    load(ctx, {"Lead ID": "L1", "Phone": "9000000001"})
    agent = one(ctx.conn, "SELECT assigned_to FROM leads")["assigned_to"]
    ctx._now = MON_11 + timedelta(minutes=30)
    log_call(ctx, "L1", agent, "interested")
    ctx._now = MON_11 + timedelta(hours=5)
    row = next(r for r in compliance(ctx, MON_11.date()) if r["caller_id"] == agent)
    assert (row["due"], row["on_time"], row["compliance_pct"]) == (1, 1, 100.0)


def test_sheet_hints_seed_followups(ctx):
    load(ctx,
         {"Lead ID": "L1", "Phone": "9000000001", "Last Call Status": "NPC", "Last Call Time": "2026-10-05 09:45"},
         {"Lead ID": "L2", "Phone": "9000000002", "Next Follow-up": "2026-10-05 15:00"})
    fu = {f["lead_id"]: f for f in rows(ctx.conn, "SELECT * FROM followups")}
    assert (fu["L1"]["kind"], parse_dt(fu["L1"]["due_at"], TZ)) == ("npc_retry", datetime(2026, 10, 5, 13, 45, tzinfo=TZ))
    assert (fu["L2"]["kind"], parse_dt(fu["L2"]["due_at"], TZ)) == ("promised", datetime(2026, 10, 5, 15, 0, tzinfo=TZ))


def test_sheet_updates_keep_workflow_state_and_add_new_leads(ctx):
    load(ctx, {"Lead ID": "L1", "Phone": "9000000001", "Category": "Dormant trader"})
    agent = one(ctx.conn, "SELECT assigned_to FROM leads")["assigned_to"]
    log_call(ctx, "L1", agent, "not_picked")
    result = sync_leads(ctx, sheet({"Lead ID": "L1", "Phone": "9000000001", "Category": "Funded - No trade"},
                                   {"Lead ID": "L2", "Phone": "9000000002"}))
    assert (result["new"], result["updated"]) == (1, 1)
    lead = one(ctx.conn, "SELECT * FROM leads WHERE lead_id='L1'")
    assert (lead["category"], lead["npc_count"], lead["assigned_to"]) == ("Funded - No trade", 1, agent)
    assert assign_new_leads(ctx)["assigned"] == 1


def test_app_activity_creates_system_followup(ctx):
    load(ctx, {"Lead ID": "L1", "WebEngage User ID": "WE1", "Phone": "9000000001"})
    agent = one(ctx.conn, "SELECT assigned_to FROM leads")["assigned_to"]
    log_call(ctx, "L1", agent, "interested")  # connected, no follow-up pending
    ctx._now = MON_11 + timedelta(hours=1)
    ctx.conn.execute("INSERT INTO events (user_id, event_name, event_time) VALUES ('WE1','Deposit Failed',?)",
                     ((MON_11 + timedelta(minutes=30)).isoformat(),))
    assert create_system_followups(ctx) == 1
    assert create_system_followups(ctx) == 0  # not duplicated
    (f,) = queue_for(ctx, agent)
    assert f["kind"] == "system" and "Deposit failed" in f["reason"]


def test_tick_sends_morning_note_once_and_eod_after_hours(ctx, tmp_path):
    leads = tmp_path / "leads.csv"
    sheet({"Lead ID": "L1", "Phone": "9000000001"}).to_csv(leads, index=False)
    ctx.cfg["sources"]["leads"]["path"] = str(leads)
    jobs.tick(ctx)
    jobs.tick(ctx)
    n_team = len(ctx.team())
    assert one(ctx.conn, "SELECT COUNT(*) n FROM notifications WHERE kind='morning_note'")["n"] == n_team
    ctx._now = datetime(2026, 10, 5, 18, 31, tzinfo=TZ)
    jobs.tick(ctx)
    assert one(ctx.conn, "SELECT COUNT(*) n FROM notifications WHERE kind='eod_report'")["n"] == n_team


def test_gm_cannot_override_npc_retry_time(ctx):
    load(ctx, {"Lead ID": "L1", "Phone": "9000000001"})
    agent = one(ctx.conn, "SELECT assigned_to FROM leads")["assigned_to"]
    log_call(ctx, "L1", agent, "not_picked", next_at=MON_11 + timedelta(days=2))  # GM tries to push it out
    (f,) = queue_for(ctx, agent)
    assert (f["kind"], parse_dt(f["due_at"], TZ)) == ("npc_retry", MON_11 + timedelta(hours=4))


def test_admin_rules_override_config(ctx):
    from wepipeline.admin import current_sla, save_rules

    rules = {"npc_retry_hours": 2, "max_npc_attempts": 3, "warning_after_minutes": 10,
             "escalate_after_minutes": 45, "stale_after_days": 2}
    save_rules(ctx, rules, {**current_sla(ctx), "cold": 60})
    load(ctx, {"Lead ID": "L1", "Phone": "9000000001"})
    agent = one(ctx.conn, "SELECT assigned_to FROM leads")["assigned_to"]
    assert parse_dt(queue_for(ctx, agent)[0]["due_at"], TZ) == MON_11 + timedelta(minutes=60)
    log_call(ctx, "L1", agent, "busy")
    assert parse_dt(queue_for(ctx, agent)[0]["due_at"], TZ) == MON_11 + timedelta(hours=2)

    with pytest.raises(ValueError):
        save_rules(ctx, {**rules, "escalate_after_minutes": 5}, current_sla(ctx))  # escalation before warning
    with pytest.raises(ValueError):
        save_rules(ctx, {**rules, "npc_retry_hours": 100}, current_sla(ctx))


def test_hourly_routine_nudges_each_gm_once_per_block(ctx):
    from wepipeline.jobs import send_routine_nudges
    from wepipeline.reports import current_block

    load(ctx, {"Lead ID": "L1", "Phone": "9000000001"})
    ctx._now = datetime(2026, 10, 5, 10, 5, tzinfo=TZ)
    assert current_block(ctx, ctx.now())[0]["time"] == "10:00"
    assert send_routine_nudges(ctx) == 0  # first block: the morning note covers it
    ctx._now = datetime(2026, 10, 5, 11, 2, tzinfo=TZ)
    assert send_routine_nudges(ctx) == len(ctx.callers())
    assert send_routine_nudges(ctx) == 0  # once per block
    ctx._now = datetime(2026, 10, 5, 13, 10, tzinfo=TZ)
    assert send_routine_nudges(ctx) == 0  # lunch: no nudge
    ctx._now = datetime(2026, 10, 5, 15, 40, tzinfo=TZ)  # scheduler was down 14:00-15:40: only the 15:00 block is sent
    assert send_routine_nudges(ctx) == len(ctx.callers())
    subjects = [r["subject"] for r in rows(ctx.conn, "SELECT subject FROM notifications WHERE kind='routine' ORDER BY id")]
    assert {s[:5] for s in subjects} == {"11:00", "15:00"}
    body = one(ctx.conn, "SELECT body FROM notifications WHERE kind='routine' ORDER BY id LIMIT 1")["body"]
    assert "overdue" in body and "due before 12:00" in body
    ctx._now = datetime(2026, 10, 5, 19, 0, tzinfo=TZ)
    assert current_block(ctx, ctx.now()) is None


def test_followups_move_when_working_hours_change(ctx):
    from wepipeline.followups import add_followup, reroll_outside_hours

    load(ctx, {"Lead ID": "L1", "Phone": "9000000001"})
    agent = one(ctx.conn, "SELECT assigned_to FROM leads")["assigned_to"]
    add_followup(ctx, "L1", agent, datetime(2026, 10, 6, 9, 30, tzinfo=TZ), "promised", "set under the old 09:30 start")
    assert reroll_outside_hours(ctx) == 1
    dues = [parse_dt(r["due_at"], TZ) for r in rows(ctx.conn, "SELECT due_at FROM followups WHERE kind='promised'")]
    assert dues == [ctx.day_start(datetime(2026, 10, 6).date())]
    assert reroll_outside_hours(ctx) == 0
