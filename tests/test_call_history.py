from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from wepipeline.call_history import call_adjustment, call_history
from wepipeline.context import iso, load_config
from tests.helpers import open_test_ctx

TZ = ZoneInfo("Asia/Kolkata")
NOW = datetime(2026, 10, 30, 15, 0, tzinfo=TZ)  # Friday afternoon


@pytest.fixture
def ctx(tmp_path):
    cfg = load_config()
    cfg["database"] = str(tmp_path / "test.db")
    cfg["notifications"]["outbox_dir"] = str(tmp_path / "outbox")
    cfg["notifications"]["email"]["enabled"] = False
    cfg["notifications"]["webhook_url"] = ""
    cfg["market_notes_file"] = str(tmp_path / "none.md")
    c = open_test_ctx(cfg)
    c._now = NOW
    return c


def call(ctx, lead, hours_ago, outcome):
    ctx.conn.execute("INSERT INTO calls (lead_id, agent_id, called_at, outcome) VALUES (?,?,?,?)",
                     (lead, "gm", iso(NOW - timedelta(hours=hours_ago)), outcome))


def test_recent_calls_decide_whether_to_call_now(ctx):
    for h in (30, 20, 5):
        call(ctx, "UNANSWERED", h, "not_picked")         # 3 NPCs in 48 h: rest 24 h after the last
    call(ctx, "TALKED", 26, "connected")                 # a connect yesterday, no callback: rest until 48 h after it
    call(ctx, "TWICE", 4, "not_picked"); call(ctx, "TWICE", 2, "busy")   # 2 attempts today: done for today
    call(ctx, "BACK", 26, "connected")
    ctx.conn.execute("INSERT INTO events (user_id, event_name, event_time, attrs) VALUES ('BACK','ORDER_PLACED_MOB',?,'{}')",
                     (iso(NOW - timedelta(hours=1)),))   # in the app after the call: call now
    call(ctx, "PROMISED", 3, "callback_requested")
    ctx.conn.execute("INSERT INTO leads (lead_id, we_user_id, extra) VALUES ('PROMISED','PROMISED','{}')")
    ctx.conn.execute("INSERT INTO followups (lead_id, agent_id, due_at, kind, created_at) VALUES ('PROMISED','gm',?,'promised',?)",
                     (iso(NOW + timedelta(minutes=10)), iso(NOW)))
    call(ctx, "OLD", 24 * 5, "not_picked")
    ctx.conn.commit()
    h = call_history(ctx, ["UNANSWERED", "TALKED", "TWICE", "BACK", "PROMISED", "OLD", "FRESH"])
    assert h["UNANSWERED"]["calls_48h"] == "3 attempts · 0 connects"
    assert h["UNANSWERED"]["call_now"] == "Resting until tomorrow 10:00"
    assert h["TALKED"]["call_now"] == "Resting until tomorrow 13:00" and h["TALKED"]["connects_48h"] == 1
    assert h["TWICE"]["call_now"] == "Resting until tomorrow 09:00" and h["TWICE"]["attempts_today"] == 2
    assert h["BACK"]["call_now"] == "Call now (back in the app)"
    assert h["PROMISED"]["call_now"] == "Callback due"
    assert h["OLD"]["call_now"] == "Call now" and h["OLD"]["calls_48h"] == "0 attempts · 0 connects" and h["OLD"]["attempts_7d"] == 1
    assert h["FRESH"]["call_now"] == "Call now (never called)" and h["FRESH"]["last_call"] == ""


def jcall(ctx, cid, hours_ago, seconds, incoming=0, n=[1000]):
    n[0] += 1
    ctx.conn.execute("INSERT INTO jarvis_calls (id, client_id, started_at, duration, caller, incoming) VALUES (?,?,?,?,?,?)",
                     (n[0], cid, iso(NOW - timedelta(hours=hours_ago)), seconds, "Asha", incoming))


def test_jarvis_calls_count_and_move_the_score(ctx):
    for d in range(7):                                    # 7 connects of 2 min across the week: called enough
        jcall(ctx, "TALKED", 20 + 24 * d, 120)
    for d in range(5):                                    # 5 calls under 30 s: attempts, not connects
        jcall(ctx, "NOREPLY", 20 + 24 * d, 12)
    jcall(ctx, "NOREPLY", 3, 300, incoming=1)             # incoming calls don't count as attempts
    jcall(ctx, "OLD", 24 * 9, 200)                        # older than a week
    ctx.conn.commit()
    h = call_history(ctx, ["talked", "NOREPLY", "OLD"])   # the client code's case doesn't matter
    assert h["talked"]["calls_7d"] == "7 calls · 7 connects · 14 min" and h["talked"]["last_call"].endswith("connected 2:00 · Asha")
    assert h["NOREPLY"]["calls_7d"] == "5 calls · 0 connects · 1 min · 1 incoming" and h["NOREPLY"]["connects_7d"] == 0
    assert h["OLD"]["attempts_7d"] == 0
    assert call_adjustment(ctx, h["talked"], "new")[0] == -40
    assert call_adjustment(ctx, h["NOREPLY"], "low_margin") == (-10.0, "5 calls this week, none connected")
    assert call_adjustment(ctx, h["OLD"], "new") == (10.0, "not called this week")
    assert call_adjustment(ctx, h["talked"], "payout") == (0.0, "")  # payouts are called whatever the history
