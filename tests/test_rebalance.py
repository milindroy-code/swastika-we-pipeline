from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from tests.helpers import open_test_ctx
from wepipeline.assign import (assign_new_leads, clear_overdue, day_plan, go_live_reset, overdue_counts, rebalance,
                               take_back_unworked, unworked_counts,
                               set_day_plans)
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
                     "assigned_at, campaign, first_seen_at, released_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (lid, lid, "open", "x", "WebEngage activity (t)", "{}", tier, score, assigned,
                      NOW.isoformat() if assigned else None, campaign, NOW.isoformat(), NOW.isoformat()))  # released by an SM
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


def test_admin_clear_overdue_clears_and_reassigns_nobody(ctx):
    ctx.cfg["team"][1]["capacity"] = 10
    for lid, tier, score in [("A_COLD", "cold", 5), ("B_HOT", "hot", 95)]:
        lead(ctx, lid, tier, score, "gm_a")  # never called
    lead(ctx, "WORKED", "warm", 50, "gm_a")
    log_call(ctx, "WORKED", "gm_a", "not_picked")  # called once: stays with the GM
    lead(ctx, "LATER", "warm", 40, "gm_a")
    ctx.conn.execute("UPDATE followups SET due_at=? WHERE lead_id='LATER'", ((NOW + timedelta(days=1)).isoformat(),))
    ctx._now = NOW + timedelta(hours=6)
    check_overdue(ctx)
    assert overdue_counts(ctx) == {"gm_a": 3}
    res = clear_overdue(ctx)
    assert res == {"cleared": 3, "back_on_hold": 2, "kept": 1, "alerts_cleared": res["alerts_cleared"]} and res["alerts_cleared"] > 0
    assert overdue_counts(ctx) == {} and one(ctx.conn, "SELECT COUNT(*) n FROM alerts")["n"] == 0
    leads = {r["lead_id"]: r for r in rows(ctx.conn, "SELECT lead_id, assigned_to, on_hold FROM leads")}
    assert leads["WORKED"]["assigned_to"] == "gm_a" and leads["LATER"]["assigned_to"] == "gm_a"
    assert all(leads[l]["assigned_to"] is None and leads[l]["on_hold"] == 1 for l in ("A_COLD", "B_HOT"))
    assign_new_leads(ctx)  # held: the scheduler doesn't hand them out again
    assert one(ctx.conn, "SELECT COUNT(*) n FROM leads WHERE assigned_to IS NULL")["n"] == 2
    later = one(ctx.conn, "SELECT due_at FROM followups WHERE lead_id='LATER' AND status='pending'")
    assert parse_dt(later["due_at"], TZ) == NOW + timedelta(days=1)  # not overdue: untouched


def test_sm_plan_sets_how_many_which_lists_and_priority_per_gm(ctx):
    ctx.cfg["team"][1]["capacity"] = 50
    ctx.cfg["assignment"]["require_sm_plan"] = True
    for lid, tier, score, camp in [("P1", "cold", 5, "payout"), ("H_NEW", "hot", 90, "new"), ("W_NEW", "warm", 60, "new"),
                                   ("H_LOW", "hot", 85, "low_margin"), ("C_NEW", "cold", 3, "new"), ("H_NEW2", "hot", 80, "new")]:
        lead(ctx, lid, tier, score, campaign=camp)
    assert day_plan(ctx, "gm_a")["saved"] is False
    res = set_day_plans(ctx, {"gm_a": {"new_clients": 2, "lists": ["no_deposit"], "tiers": ["hot", "warm"]}})
    mine = {r["lead_id"] for r in rows(ctx.conn, "SELECT lead_id FROM leads WHERE assigned_to='gm_a'")}
    assert "P1" in mine  # a payout always goes to someone, whatever the plan, and doesn't count
    assert mine - {"P1"} == {"H_NEW", "H_NEW2"}  # 2 new clients, hottest first, only new / hot-warm
    assert res["by_caller"]["gm_a"] == 3
    plan = day_plan(ctx, "gm_a")
    assert plan["saved_today"] and (plan["new_clients"], plan["lists"], plan["tiers"]) == (2, ["no_deposit"], ["hot", "warm"])
    ctx._now = NOW + timedelta(days=1)  # next day: the plan carries over, the count starts again
    assert day_plan(ctx, "gm_a")["saved_today"] is False and day_plan(ctx, "gm_a")["new_clients"] == 2
    assign_new_leads(ctx)
    assert one(ctx.conn, "SELECT assigned_to FROM leads WHERE lead_id='W_NEW'")["assigned_to"] == "gm_a"
    assert one(ctx.conn, "SELECT assigned_to FROM leads WHERE lead_id='C_NEW'")["assigned_to"] is None  # cold: not in the plan
    assert one(ctx.conn, "SELECT assigned_to FROM leads WHERE lead_id='H_LOW'")["assigned_to"] is None  # low margin: not ticked
    set_day_plans(ctx, {"gm_a": {"new_clients": 5, "lists": [], "tiers": ["hot"]}})  # no list ticked: nothing new
    assert one(ctx.conn, "SELECT assigned_to FROM leads WHERE lead_id='H_LOW'")["assigned_to"] is None


def test_with_require_sm_plan_a_gm_gets_nothing_until_the_sm_plans(ctx):
    ctx.cfg["assignment"]["require_sm_plan"] = True
    ctx.cfg["team"][1]["capacity"] = 50
    for lid, tier, score, camp in [("P1", "cold", 5, "payout"), ("H_NEW", "hot", 90, "new")]:
        lead(ctx, lid, tier, score, campaign=camp)
    assign_new_leads(ctx)
    owner = {r["lead_id"]: r["assigned_to"] for r in rows(ctx.conn, "SELECT lead_id, assigned_to FROM leads")}
    assert owner == {"P1": "gm_a", "H_NEW": None}  # only the payout goes out
    set_day_plans(ctx, {"gm_a": {"new_clients": None, "lists": ["no_deposit"], "tiers": ["hot"]}})
    assert one(ctx.conn, "SELECT assigned_to FROM leads WHERE lead_id='H_NEW'")["assigned_to"] == "gm_a"


def test_admin_can_switch_to_automatic_and_back(ctx):
    from wepipeline.assign import assignment_mode, set_assignment_mode

    ctx.cfg["assignment"]["require_sm_plan"] = True
    ctx.cfg["team"][1]["capacity"] = 50
    set_day_plans(ctx, {"gm_a": {"new_clients": 1, "lists": ["no_deposit"], "tiers": ["hot"]}})
    for lid, tier, score in [("H1", "hot", 90), ("H2", "hot", 80), ("C1", "cold", 5)]:
        lead(ctx, lid, tier, score, campaign="new")
    assert assignment_mode(ctx) == "plan"
    assign_new_leads(ctx)
    assert one(ctx.conn, "SELECT COUNT(*) n FROM leads WHERE assigned_to='gm_a'")["n"] == 1  # the SM's number
    res = set_assignment_mode(ctx, "auto")  # automatic: up to capacity, the plan is ignored
    assert assignment_mode(ctx) == "auto" and res["assigned"] == 2
    assert day_plan(ctx, "gm_a")["new_clients"] == 1  # ...but kept
    set_assignment_mode(ctx, "plan")
    assert assignment_mode(ctx) == "plan"


def test_sm_cohort_order_decides_who_goes_out_first_and_the_calling_order(ctx):
    from wepipeline.dialer import dialer_queue

    ctx.cfg["assignment"]["require_sm_plan"] = True
    ctx.cfg["team"][1]["capacity"] = 50
    for lid, tier, score, camp in [("PAY_HOT", "hot", 99, "payout"), ("LOW_COLD", "cold", 5, "low_margin"),
                                   ("NEW_WARM", "warm", 50, "new"), ("NEW_HOT", "hot", 90, "new"), ("LOW_HOT", "hot", 95, "low_margin")]:
        lead(ctx, lid, tier, score, campaign=camp)
    # new clients first, then low margin, then payouts; every priority; room for 4 (payouts don't count)
    set_day_plans(ctx, {"gm_a": {"new_clients": 3, "lists": ["no_deposit", "low_margin", "payout"], "tiers": ["hot", "warm", "cold"]}})
    assert day_plan(ctx, "gm_a")["lists"] == ["no_deposit", "low_margin", "payout"]
    mine = {r["lead_id"] for r in rows(ctx.conn, "SELECT lead_id FROM leads WHERE assigned_to='gm_a'")}
    assert mine == {"NEW_HOT", "NEW_WARM", "LOW_HOT", "PAY_HOT"}  # 3 by the order (new x2, then the hottest low margin) + payout
    queue = [q["lead_id"] for q in dialer_queue(ctx, "gm_a")]
    assert queue.index("NEW_HOT") < queue.index("NEW_WARM") < queue.index("LOW_HOT") < queue.index("PAY_HOT")


def test_sm_plans_assign_from_todays_lists_not_the_old_pool(ctx):
    import json

    from wepipeline.assign import waiting_for_plan

    ctx.cfg["assignment"]["require_sm_plan"] = True
    ctx.cfg["team"][1]["capacity"] = 50
    for lid, tier, score in [("LIST_HOT", "hot", 90), ("LIST_COLD", "cold", 5), ("OLD_PULL", "warm", 60)]:
        lead(ctx, lid, tier, score, campaign="new")
    ctx.conn.execute("UPDATE leads SET on_hold=1, released_at=NULL WHERE lead_id LIKE 'LIST_%'")  # today's list arrives held
    ctx.conn.execute("UPDATE leads SET released_at=NULL WHERE lead_id='OLD_PULL'")  # the old hourly pull: never released
    up = ctx.conn.execute("INSERT INTO uploads (filename, uploaded_at, client_ids, summary) VALUES (?,?,?,?) RETURNING id",
                          ("x_morning_no_deposit.csv", NOW.isoformat(), json.dumps(["LIST_HOT", "LIST_COLD"]), "{}")).fetchone()["id"]
    ctx.conn.execute("INSERT INTO cohort_runs (cohort, status, requested_at, upload_id) VALUES ('no_deposit','ready',?,?)",
                     (NOW.isoformat(), up))
    ctx.conn.commit()
    assert waiting_for_plan(ctx) == {("no_deposit", "hot"): 1, ("no_deposit", "cold"): 1}  # not the old pull
    set_day_plans(ctx, {"gm_a": {"new_clients": None, "lists": ["no_deposit"], "tiers": ["hot", "warm", "cold"]}})
    owner = {r["lead_id"]: (r["assigned_to"], r["on_hold"]) for r in rows(ctx.conn, "SELECT lead_id, assigned_to, on_hold FROM leads")}
    assert owner == {"LIST_HOT": ("gm_a", 0), "LIST_COLD": ("gm_a", 0), "OLD_PULL": (None, 0)}


def test_held_payouts_on_todays_list_wait_for_the_sm_plan(ctx):
    import json

    ctx.cfg["assignment"]["require_sm_plan"] = True
    ctx.cfg["team"][1]["capacity"] = 50
    lead(ctx, "PAY", "hot", 99, campaign="payout")
    ctx.conn.execute("UPDATE leads SET on_hold=1, released_at=NULL WHERE lead_id='PAY'")
    up = ctx.conn.execute("INSERT INTO uploads (filename, uploaded_at, client_ids, summary) VALUES (?,?,?,?) RETURNING id",
                          ("x_morning_payout.csv", NOW.isoformat(), json.dumps(["PAY"]), "{}")).fetchone()["id"]
    ctx.conn.execute("INSERT INTO cohort_runs (cohort, status, requested_at, upload_id) VALUES ('payout','ready',?,?)", (NOW.isoformat(), up))
    ctx.conn.commit()
    assign_new_leads(ctx)  # the scheduler: no plan includes payouts yet
    assert one(ctx.conn, "SELECT assigned_to FROM leads WHERE lead_id='PAY'")["assigned_to"] is None
    set_day_plans(ctx, {"gm_a": {"new_clients": 5, "lists": ["payout"], "tiers": ["hot"]}})
    assert one(ctx.conn, "SELECT assigned_to FROM leads WHERE lead_id='PAY'")["assigned_to"] == "gm_a"


def test_admin_takes_back_clients_not_called_yet_due_or_not(ctx):
    ctx.cfg["team"][1]["capacity"] = 10
    lead(ctx, "FUTURE", "cold", 5, "gm_a")
    ctx.conn.execute("UPDATE followups SET due_at=? WHERE lead_id='FUTURE'", ((NOW + timedelta(days=3)).isoformat(),))  # not overdue
    lead(ctx, "CALLED", "warm", 50, "gm_a")
    log_call(ctx, "CALLED", "gm_a", "not_picked")
    assert overdue_counts(ctx).get("gm_a", 0) == 0 and unworked_counts(ctx) == {"gm_a": 1}
    assert take_back_unworked(ctx, ["gm_a"]) == {"taken_back": 1}
    leads = {r["lead_id"]: r for r in rows(ctx.conn, "SELECT lead_id, assigned_to, on_hold FROM leads")}
    assert (leads["FUTURE"]["assigned_to"], leads["FUTURE"]["on_hold"]) == (None, 1) and leads["CALLED"]["assigned_to"] == "gm_a"
    assert one(ctx.conn, "SELECT COUNT(*) n FROM followups WHERE lead_id='FUTURE' AND status='pending'")["n"] == 0
    assign_new_leads(ctx)
    assert one(ctx.conn, "SELECT assigned_to FROM leads WHERE lead_id='FUTURE'")["assigned_to"] is None  # held: not handed out
