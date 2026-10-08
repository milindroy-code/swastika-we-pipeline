"""Follow-up rules, one test each, with the live settings (NPC 4 h, warning 15 min, escalation 60 min, 6 NPCs,
first call hot 30 min / warm 2 h / cold 4 h, payout 30 min, 5 min per call, 10:00-18:30 Mon-Fri)."""
import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from tests.helpers import open_test_ctx
from wepipeline import jobs
from wepipeline.assign import assign_new_leads
from wepipeline.clientdb import sync_new_clients
from wepipeline.context import load_config, parse_dt
from wepipeline.db import one, rows
from wepipeline.dialer import dialer_queue
from wepipeline.followups import check_overdue, create_system_followups, log_call
from wepipeline.heat import refresh_heat
from wepipeline.reports import compliance

TZ = ZoneInfo("Asia/Kolkata")
TUE_1100 = datetime(2026, 10, 6, 11, 0, tzinfo=TZ)


@pytest.fixture
def ctx(tmp_path):
    cfg = load_config()
    cfg["database"] = str(tmp_path / "test.db")
    cfg["notifications"]["outbox_dir"] = str(tmp_path / "outbox")
    cfg["reports_dir"] = str(tmp_path / "reports")
    cfg["sources"]["leads"]["path"] = str(tmp_path / "none.csv")
    cfg["sources"]["webengage"]["folder"] = str(tmp_path / "we")
    cfg["team"] = [{"id": "sm", "name": "SM One", "role": "sm"},
                   {"id": "gm", "name": "GM One", "role": "gm", "sm": "sm", "capacity": 100}]
    c = open_test_ctx(cfg)
    c._now = TUE_1100
    return c


def client(ctx, cid, tier="cold", campaign="", extra=None):
    ctx.conn.execute("INSERT INTO leads (lead_id, we_user_id, status, category, source, extra, heat_tier, heat_score, campaign, first_seen_at) "
                     "VALUES (?,?,?,?,?,?,?,?,?,?)", (cid, cid, "open", "x", "WebEngage activity (t)", json.dumps(extra or {}),
                                                     tier, {"hot": 90, "warm": 50, "cold": 10}[tier], campaign, ctx.now().isoformat()))
    ctx.conn.commit()


def due(ctx, cid, kind=None):
    q = "SELECT due_at FROM followups WHERE lead_id=? AND status='pending'" + (" AND kind=?" if kind else "") + " ORDER BY due_at"
    r = one(ctx.conn, q, (cid, kind) if kind else (cid,))
    return parse_dt(r["due_at"], TZ) if r else None


def assign(ctx):
    assign_new_leads(ctx)


# ---------------------------------------------------------------- first call
def test_01_first_call_window_follows_priority(ctx):
    client(ctx, "HOT", "hot"); client(ctx, "WARM", "warm"); client(ctx, "COLD", "cold")
    assign(ctx)
    assert due(ctx, "HOT") == TUE_1100 + timedelta(minutes=30)
    assert due(ctx, "WARM") == TUE_1100 + timedelta(hours=2)
    assert due(ctx, "COLD") == TUE_1100 + timedelta(hours=4)


def test_02_late_assignment_rolls_into_next_working_morning(ctx):
    ctx._now = datetime(2026, 10, 9, 17, 0, tzinfo=TZ)  # Friday 17:00; cold = 4 h -> after 18:30
    client(ctx, "COLD", "cold")
    assign(ctx)
    assert due(ctx, "COLD") == datetime(2026, 10, 12, 10, 0, tzinfo=TZ)  # Monday 10:00


def test_03_bulk_assignment_is_spaced_5_minutes_hottest_first(ctx):
    for i in range(4):
        client(ctx, f"H{i}", "hot")
    assign(ctx)
    times = sorted(due(ctx, f"H{i}") for i in range(4))
    assert [(b - a).total_seconds() / 60 for a, b in zip(times, times[1:])] == [5, 5, 5]


def test_04_payout_clients_are_called_first_within_30_minutes(ctx):
    client(ctx, "HOT", "hot"); client(ctx, "PAY", "cold", campaign="payout")
    assign(ctx)
    assert due(ctx, "PAY") == TUE_1100 + timedelta(minutes=30)
    assert dialer_queue(ctx, "gm")[0]["lead_id"] == "PAY"


# ---------------------------------------------------------------- not picked up
def test_05_not_picked_up_comes_back_in_4_hours(ctx):
    client(ctx, "C"); assign(ctx)
    log_call(ctx, "C", "gm", "not_picked")
    assert due(ctx, "C", "npc_retry") == TUE_1100 + timedelta(hours=4)


def test_06_npc_after_hours_rolls_to_next_working_day(ctx):
    client(ctx, "C"); assign(ctx)
    ctx._now = datetime(2026, 10, 9, 16, 0, tzinfo=TZ)  # Friday 16:00 + 4 h = 20:00
    log_call(ctx, "C", "gm", "busy")
    assert due(ctx, "C", "npc_retry") == datetime(2026, 10, 12, 10, 0, tzinfo=TZ)


def test_07_gm_cannot_pick_their_own_npc_retry_time(ctx):
    client(ctx, "C"); assign(ctx)
    log_call(ctx, "C", "gm", "switched_off", next_at=TUE_1100 + timedelta(days=3))
    assert due(ctx, "C", "npc_retry") == TUE_1100 + timedelta(hours=4)


def test_08_six_npcs_in_a_row_mark_the_client_unreachable_and_tell_the_sm(ctx):
    client(ctx, "C"); assign(ctx)
    for _ in range(6):
        log_call(ctx, "C", "gm", "not_picked")
    assert one(ctx.conn, "SELECT status FROM leads WHERE lead_id='C'")["status"] == "unreachable"
    assert due(ctx, "C") is None
    assert one(ctx.conn, "SELECT COUNT(*) n FROM alerts WHERE lead_id='C' AND manager_id='sm'")["n"] == 1


# ---------------------------------------------------------------- spoke to them
def test_09_promised_callback_is_due_at_the_promised_time(ctx):
    client(ctx, "C"); assign(ctx)
    with pytest.raises(ValueError):
        log_call(ctx, "C", "gm", "callback_requested")  # a time is required
    when = TUE_1100.replace(hour=16, minute=30)
    log_call(ctx, "C", "gm", "callback_requested", "after market", when)
    assert due(ctx, "C", "promised") == when


def test_10_interested_without_a_time_gets_a_system_follow_up_after_3_quiet_days(ctx):
    client(ctx, "C"); assign(ctx)
    log_call(ctx, "C", "gm", "interested")
    assert due(ctx, "C") is None
    ctx._now = TUE_1100 + timedelta(days=3, hours=1)
    assert create_system_followups(ctx) == 1 and due(ctx, "C", "system") is not None


def test_11_converted_closes_the_client_and_records_the_deposit(ctx):
    client(ctx, "C"); assign(ctx)
    log_call(ctx, "C", "gm", "converted", "added by UPI", deposit_amount=30000)
    assert one(ctx.conn, "SELECT status FROM leads WHERE lead_id='C'")["status"] == "converted"
    assert due(ctx, "C") is None
    row = next(r for r in compliance(ctx, TUE_1100.date()) if r["caller_id"] == "gm")
    assert (row["converted"], row["funded_target"], row["deposits"]) == (1, 1, 30000)


def test_12_not_interested_and_wrong_number_close_without_follow_ups(ctx):
    client(ctx, "A"); client(ctx, "B"); assign(ctx)
    log_call(ctx, "A", "gm", "not_interested"); log_call(ctx, "B", "gm", "wrong_number")
    status = {r["lead_id"]: r["status"] for r in rows(ctx.conn, "SELECT lead_id, status FROM leads")}
    assert status == {"A": "not_interested", "B": "invalid"} and due(ctx, "A") is None and due(ctx, "B") is None


# ---------------------------------------------------------------- deadlines
def test_13_overdue_15_min_warns_the_gm_once_60_min_escalates_to_the_sm_once(ctx):
    client(ctx, "C", "hot"); assign(ctx)
    d = due(ctx, "C")
    ctx._now = d + timedelta(minutes=16)
    assert check_overdue(ctx) == {"warned": 1, "escalated": 0}
    assert check_overdue(ctx) == {"warned": 0, "escalated": 0}
    ctx._now = d + timedelta(minutes=61)
    assert check_overdue(ctx) == {"warned": 0, "escalated": 1}
    assert check_overdue(ctx) == {"warned": 0, "escalated": 0}
    assert one(ctx.conn, "SELECT manager_id FROM alerts WHERE level='escalation'")["manager_id"] == "sm"


def test_14_calling_after_the_escalation_still_counts_as_missed(ctx):
    client(ctx, "C", "hot"); assign(ctx)
    d = due(ctx, "C")
    ctx._now = d + timedelta(minutes=61); check_overdue(ctx)
    log_call(ctx, "C", "gm", "interested")
    row = next(r for r in compliance(ctx, d.date()) if r["caller_id"] == "gm")
    assert (row["missed"], row["on_time"]) == (1, 0)


def test_15_calling_early_or_within_15_minutes_is_on_time(ctx):
    client(ctx, "A", "hot"); client(ctx, "B", "hot"); assign(ctx)
    log_call(ctx, "A", "gm", "interested")                                  # before it was due
    ctx._now = due(ctx, "B") + timedelta(minutes=10); log_call(ctx, "B", "gm", "interested")  # 10 min after
    ctx._now = TUE_1100.replace(hour=18)
    row = next(r for r in compliance(ctx, TUE_1100.date()) if r["caller_id"] == "gm")
    assert (row["due"], row["on_time"], row["missed"]) == (2, 2, 0)


# ---------------------------------------------------------------- the system creates follow-ups too
def test_16_app_activity_after_the_last_call_creates_a_follow_up(ctx):
    client(ctx, "C"); assign(ctx)
    log_call(ctx, "C", "gm", "interested")
    ctx._now = TUE_1100 + timedelta(hours=2)
    ctx.conn.execute("INSERT INTO events (user_id, event_name, event_time) VALUES ('C','Deposit Failed',?)",
                     ((TUE_1100 + timedelta(hours=1)).isoformat(),))
    assert create_system_followups(ctx) == 1
    assert create_system_followups(ctx) == 0  # not twice for the same activity


def test_17_a_client_who_turns_hot_is_pulled_forward_to_now(ctx):
    client(ctx, "C", "cold"); assign(ctx)
    assert due(ctx, "C") == TUE_1100 + timedelta(hours=4)
    ctx._now = TUE_1100 + timedelta(minutes=20)
    for name, n in [("CALL_ACCEPTED_MOB", 6), ("ORDER_PLACED_MOB", 4)]:
        ctx.conn.execute("INSERT INTO events (user_id, event_name, event_time, attrs) VALUES (?,?,?,?)",
                         ("C", name, ctx.now().isoformat(), json.dumps({"count": n})))
    refresh_heat(ctx)
    assert due(ctx, "C") == ctx.now()


def test_18_a_deposit_in_the_client_database_closes_the_client_and_its_follow_ups(ctx):
    sync_new_clients(ctx, pd.DataFrame([{"CLIENT_ID": "D1", "REGISTRATION_DATE": TUE_1100.replace(tzinfo=None)}]), pd.DataFrame())
    assign(ctx)
    assert due(ctx, "D1") is not None
    sync_new_clients(ctx, pd.DataFrame(columns=["CLIENT_ID", "REGISTRATION_DATE"]), pd.DataFrame([{"CLIENT_ID": "D1"}]))
    assert one(ctx.conn, "SELECT status FROM leads WHERE lead_id='D1'")["status"] == "converted" and due(ctx, "D1") is None


# ---------------------------------------------------------------- the day
def test_19_morning_note_hourly_nudges_and_eod_report_go_out_once(ctx):
    client(ctx, "C"); assign(ctx)
    for hhmm in ["10:01", "10:20", "11:02", "11:40", "13:05", "14:01", "18:31", "18:45"]:
        h, m = map(int, hhmm.split(":"))
        ctx._now = TUE_1100.replace(hour=h, minute=m)
        jobs.tick(ctx)
    kinds = {r["kind"]: r["n"] for r in rows(ctx.conn, "SELECT kind, COUNT(*) n FROM notifications GROUP BY kind")}
    assert kinds["morning_note"] == 2   # the GM and the SM, once each
    assert kinds["eod_report"] == 2
    assert kinds["routine"] == 2        # 11:00 and 14:00 nudges for the one GM (13:00 lunch has none)


def test_20_nothing_falls_due_on_a_weekend(ctx):
    ctx._now = datetime(2026, 10, 10, 12, 0, tzinfo=TZ)  # Saturday
    client(ctx, "C", "hot"); assign(ctx)
    assert due(ctx, "C").weekday() == 0 and due(ctx, "C").hour == 10  # Monday morning
