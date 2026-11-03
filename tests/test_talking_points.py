from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from wepipeline import talking_points as tp
from wepipeline.context import iso, load_config
from tests.helpers import open_test_ctx

TZ = ZoneInfo("Asia/Kolkata")
NOW = datetime(2026, 10, 30, 15, 0, tzinfo=TZ)


@pytest.fixture
def ctx(tmp_path, monkeypatch):
    cfg = load_config()
    cfg["database"] = str(tmp_path / "test.db")
    cfg["notifications"]["outbox_dir"] = str(tmp_path / "outbox")
    cfg["notifications"]["email"]["enabled"] = False
    cfg["notifications"]["webhook_url"] = ""
    cfg["market_notes_file"] = str(tmp_path / "none.md")
    c = open_test_ctx(cfg)
    c._now = NOW
    monkeypatch.setattr(tp, "last_trades", lambda ctx, ids: {"TRADER": {"scrip": "EF HDFCBANK 23Nov2026", "date": NOW - timedelta(days=2)}})
    return c


def ev(ctx, user, name, hours_ago):
    ctx.conn.execute("INSERT INTO events (user_id, event_name, event_time, attrs) VALUES (?,?,?,'{}')",
                     (user, name, iso(NOW - timedelta(hours=hours_ago))))


def test_one_or_two_talking_points_from_friction_or_the_last_trade(ctx):
    ev(ctx, "FUNDER", "FUNDS_PROCEED_TO_ADD_MOB", 20); ev(ctx, "FUNDER", "RECOMMENDATION_LISTING_OPENED_MOB", 2)
    ev(ctx, "ORDERER", "ORDER_PLACED_MOB", 50); ev(ctx, "ORDERER", "PLACE_ORDER_OPENED_MOB", 3); ev(ctx, "ORDERER", "PLACE_ORDER_OPENED_MOB", 1)
    ev(ctx, "TRADER", "SEARCH_SCREEN_OPENED_MOB", 4)
    ctx.conn.commit()
    leads = [{"lead_id": i, "extra": '{"current_margin": 12000}'} for i in ("FUNDER", "ORDERER", "TRADER", "QUIET")]
    out = tp.talking_points(ctx, leads, "low_margin")
    assert out["FUNDER"]["lever"] == "Friction: tried to add funds yesterday 19:00"
    assert out["FUNDER"]["talking_points"].startswith("1. Aapne kal funds add karne") and "\n2. Aap Sarthi ke ideas" in out["FUNDER"]["talking_points"]
    assert out["FUNDER"]["app_login"] == "Last login today 13:00 · active 2 of last 7 days"
    assert out["FUNDER"]["ask"] == "Top up ₹18,000 to reach ₹30,000 (has ₹12,000)"
    assert out["ORDERER"]["lever"].startswith("Friction: opened the order window 2x without placing since their last order")
    assert out["TRADER"]["lever"] == "Last trade: HDFCBANK future (28 Oct)" and out["TRADER"]["app_surfaces"] == "Search (1x)"
    assert out["QUIET"]["app_login"] == "No app login in the last 7 days" and out["QUIET"]["talking_points"].startswith("1. Account khul gaya")
    assert tp.talking_points(ctx, leads[:1], "new")["FUNDER"]["lever"].endswith(", money not received")
