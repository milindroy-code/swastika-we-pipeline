from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from tests.helpers import open_test_ctx
from wepipeline.context import load_config
from wepipeline.dialer import DUE_NOW, LATER_TODAY, OVERDUE, UPCOMING, dialer_queue, next_call, urgency_counts
from wepipeline.followups import add_followup

TZ = ZoneInfo("Asia/Kolkata")
NOW = datetime(2026, 10, 5, 12, 0, tzinfo=TZ)  # Monday noon


@pytest.fixture
def ctx(tmp_path):
    cfg = load_config()
    cfg["database"] = str(tmp_path / "test.db")
    cfg["notifications"]["outbox_dir"] = str(tmp_path / "outbox")
    c = open_test_ctx(cfg)
    c._now = NOW
    return c


def lead(ctx, lid, tier, due_min, missed=0):
    ctx.conn.execute("INSERT INTO leads (lead_id, we_user_id, status, assigned_to, heat_tier, heat_score, extra) VALUES (?,?,?,?,?,?,?)",
                     (lid, lid, "open", "gm_01", tier, {"hot": 90, "warm": 50, "cold": 10}[tier], "{}"))
    fid = add_followup(ctx, lid, "gm_01", NOW + timedelta(minutes=due_min), "first_call", f"{lid} reason")
    if missed:
        ctx.conn.execute("UPDATE followups SET missed=1, escalated_at=? WHERE id=?", (NOW.isoformat(), fid))
    ctx.conn.commit()


def test_order_is_overdue_by_escalation_risk_then_hot_then_due(ctx):
    lead(ctx, "ESCALATED", "hot", -120, missed=1)   # already escalated: still overdue, but nothing left to save
    lead(ctx, "OVERDUE_50", "cold", -50)            # escalates in 10 min
    lead(ctx, "OVERDUE_20", "hot", -20)             # escalates in 40 min
    lead(ctx, "DUE_COLD", "cold", 0)
    lead(ctx, "DUE_HOT", "hot", 5)
    lead(ctx, "LATER_HOT", "hot", 180)
    lead(ctx, "TOMORROW", "warm", 24 * 60)
    q = dialer_queue(ctx, "gm_01")
    assert [x["lead_id"] for x in q] == ["OVERDUE_50", "OVERDUE_20", "ESCALATED", "DUE_HOT", "DUE_COLD", "LATER_HOT", "TOMORROW"]
    assert [x["group"] for x in q] == [OVERDUE, OVERDUE, OVERDUE, DUE_NOW, DUE_NOW, LATER_TODAY, UPCOMING]
    assert q[0]["urgency"] == "Overdue 50 min · escalates to your SM in 10 min"
    assert q[2]["urgency"].endswith("already escalated to your SM")
    n = urgency_counts(q)
    assert (n["overdue"], n["escalating_soon"], n["due_now"], n["hot_waiting"]) == (3, 1, 2, 4)


def test_next_call_skips_and_call_ahead(ctx):
    lead(ctx, "A", "hot", 0)
    lead(ctx, "B", "cold", 0)
    lead(ctx, "LATER", "hot", 200)
    q = dialer_queue(ctx, "gm_01")
    assert next_call(q, set())["lead_id"] == "A"
    assert next_call(q, {"A"})["lead_id"] == "B"           # skipped goes to the back
    assert next_call(q, {"A", "B"})["lead_id"] == "A"      # everything skipped: start again rather than show nothing
    q_due_only = [x for x in q if x["lead_id"] != "A" and x["lead_id"] != "B"]
    assert next_call(q_due_only, set()) is None             # nothing due: no automatic call
    assert next_call(q_due_only, set(), call_ahead=True)["lead_id"] == "LATER"


def test_escalated_backlog_puts_hot_first_and_reads_well(ctx):
    lead(ctx, "OLD_COLD", "cold", -6 * 24 * 60, missed=1)
    lead(ctx, "NEWER_HOT", "hot", -3 * 60, missed=1)
    q = dialer_queue(ctx, "gm_01")
    assert [x["lead_id"] for x in q] == ["NEWER_HOT", "OLD_COLD"]
    assert q[0]["urgency"] == "Overdue 3 h · already escalated to your SM"
    assert q[1]["urgency"] == "Overdue 6 d 0 h · already escalated to your SM"


def test_reminders_one_per_customer_and_clickable(ctx):
    from wepipeline.dialer import dismiss, reminders
    from wepipeline.followups import check_overdue
    from wepipeline.db import one

    lead(ctx, "LATE", "cold", -30)          # overdue: gets a warning alert
    lead(ctx, "SOON", "warm", 6)            # callback due in 6 minutes
    lead(ctx, "HOT", "hot", 120)
    add_followup(ctx, "HOT", "gm_01", NOW, "system", "🔥 Turned hot (score 91): Accepted a research call ×4")
    lead(ctx, "QUIET", "cold", 300)         # nothing to remind about
    check_overdue(ctx)
    items = reminders(ctx, "gm_01")
    assert [(r["icon"], r["lead_id"]) for r in items] == [("⚠️", "LATE"), ("🔥", "HOT"), ("⏰", "SOON")]
    assert items[0]["text"].startswith("LATE · overdue 30 min")
    assert items[2]["text"] == "First call with SOON at 12:06"
    dismiss(ctx, items[0])
    assert one(ctx.conn, "SELECT acknowledged_at FROM alerts WHERE lead_id='LATE'")["acknowledged_at"]
    assert [r["lead_id"] for r in reminders(ctx, "gm_01")] == ["HOT", "SOON"]
