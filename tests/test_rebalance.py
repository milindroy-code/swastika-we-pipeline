from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from tests.helpers import open_test_ctx
from wepipeline.assign import assign_new_leads, go_live_reset, rebalance
from wepipeline.context import load_config, parse_dt
from wepipeline.db import one, rows
from wepipeline.followups import add_followup, check_overdue, log_call

TZ = ZoneInfo("Asia/Kolkata")
NOW = datetime(2026, 10, 8, 12, 0, tzinfo=TZ)


@pytest.fixture
def ctx(tmp_path):
    cfg = load_config()
    cfg["database"] = str(tmp_path / "test.db")
    cfg["notifications"]["outbox_dir"] = str(tmp_path / "outbox")
    cfg["team"] = [{"id": "sm_x", "name": "SM X", "role": "sm"},
                   {"id": "gm_a", "name": "GM A", "role": "gm", "sm": "sm_x", "capacity": 2}]
    c = open_test_ctx(cfg)
    c._now = NOW
    return c


def lead(ctx, lid, tier, score, assigned=None, campaign=""):
    ctx.conn.execute("INSERT INTO leads (lead_id, we_user_id, status, category, source, extra, heat_tier, heat_score, assigned_to, "
                     "assigned_at, campaign, first_seen_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                     (lid, lid, "open", "x", "WebEngage activity (t)", "{}", tier, score, assigned,
                      NOW.isoformat() if assigned else None, campaign, NOW.isoformat()))
    if assigned:
        add_followup(ctx, lid, assigned, NOW - timedelta(hours=2), "first_call", "New lead")
    ctx.conn.commit()


def test_engaged_lead_swaps_in_for_an_unworked_cold_one(ctx):
    lead(ctx, "COLD_UNWORKED", "cold", 5, "gm_a")
    lead(ctx, "COLD_CALLED", "cold", 3, "gm_a")
    log_call(ctx, "COLD_CALLED", "gm_a", "interested", "keep me")
    lead(ctx, "HOT_WAITING", "hot", 90)
    assert rebalance(ctx) == 1
    assign_new_leads(ctx)
    owner = {r["lead_id"]: r["assigned_to"] for r in rows(ctx.conn, "SELECT lead_id, assigned_to FROM leads")}
    assert owner == {"COLD_UNWORKED": None, "COLD_CALLED": "gm_a", "HOT_WAITING": "gm_a"}
    assert rebalance(ctx) == 0  # nothing engaged left waiting


def test_bulk_first_calls_are_staggered_hottest_first(ctx):
    ctx.cfg["team"][1]["capacity"] = 10
    for lid, tier, score in [("C1", "cold", 5), ("H1", "hot", 95), ("W1", "warm", 50), ("H2", "hot", 80)]:
        lead(ctx, lid, tier, score)
    assign_new_leads(ctx)
    due = {r["lead_id"]: parse_dt(r["due_at"], TZ) for r in rows(ctx.conn, "SELECT lead_id, due_at FROM followups")}
    order = sorted(due, key=due.get)
    assert order == ["H1", "H2", "W1", "C1"]
    gaps = [(due[b] - due[a]).total_seconds() / 60 for a, b in zip(order, order[1:])]
    assert all(g >= 5 for g in gaps)


def test_go_live_reset_retimes_unworked_and_clears_false_escalations(ctx):
    ctx.cfg["team"][1]["capacity"] = 10
    lead(ctx, "A_COLD", "cold", 5, "gm_a")
    lead(ctx, "B_HOT", "hot", 95, "gm_a")
    lead(ctx, "WORKED", "warm", 50, "gm_a")
    log_call(ctx, "WORKED", "gm_a", "not_picked")       # its NPC retry is real work: untouched
    ctx._now = NOW + timedelta(hours=3)
    check_overdue(ctx)
    assert one(ctx.conn, "SELECT COUNT(*) n FROM alerts WHERE level='escalation'")["n"] >= 2
    res = go_live_reset(ctx)
    assert res["retimed"] == 2 and res["false_alerts_cleared"] >= 4
    fu = {r["lead_id"]: r for r in rows(ctx.conn, "SELECT * FROM followups WHERE status='pending'")}
    assert parse_dt(fu["B_HOT"]["due_at"], TZ) < parse_dt(fu["A_COLD"]["due_at"], TZ)   # hot first
    assert fu["B_HOT"]["missed"] == 0 and fu["B_HOT"]["escalated_at"] is None
    assert fu["WORKED"]["kind"] == "npc_retry"                                          # untouched
    assert one(ctx.conn, "SELECT COUNT(*) n FROM alerts WHERE lead_id IN ('A_COLD','B_HOT')")["n"] == 0


def test_alerts_for_cancelled_followups_are_cleared(ctx):
    lead(ctx, "GONE", "cold", 5, "gm_a")
    ctx._now = NOW + timedelta(hours=3)
    check_overdue(ctx)
    assert one(ctx.conn, "SELECT COUNT(*) n FROM alerts WHERE lead_id='GONE'")["n"] > 0
    ctx.conn.execute("UPDATE followups SET status='cancelled' WHERE lead_id='GONE'")  # e.g. the customer deposited
    check_overdue(ctx)
    assert one(ctx.conn, "SELECT COUNT(*) n FROM alerts WHERE lead_id='GONE'")["n"] == 0
