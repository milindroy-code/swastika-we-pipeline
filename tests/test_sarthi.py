import json
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from tests.helpers import open_test_ctx
from wepipeline import sarthi
from wepipeline.context import load_config
from wepipeline.db import one
from wepipeline.insights import customer_insight

TZ = ZoneInfo("Asia/Kolkata")
NOW = datetime(2026, 10, 8, 11, 0, tzinfo=TZ)
PROMISES = re.compile(r"\b(earn|guaranteed? (returns?|profit)|sure[- ]shot|assured|risk[- ]free|double your)\b", re.I)


@pytest.fixture
def ctx(tmp_path):
    cfg = load_config()
    cfg["database"] = str(tmp_path / "test.db")
    c = open_test_ctx(cfg)
    c._now = NOW
    return c


def add_lead(ctx, lid, extra, events=()):
    ctx.conn.execute("INSERT INTO leads (lead_id, we_user_id, status, category, source, extra, heat_tier) VALUES (?,?,?,?,?,?,?)",
                     (lid, lid, "open", "x", "WebEngage activity (t)", json.dumps(extra), "hot"))
    for name, n in events:
        ctx.conn.execute("INSERT INTO events (user_id, event_name, event_time, attrs) VALUES (?,?,?,?)",
                         (lid, name, (NOW - timedelta(hours=2)).isoformat(), json.dumps({"count": n})))
    ctx.conn.commit()
    return one(ctx.conn, "SELECT * FROM leads WHERE lead_id=?", (lid,))


def test_track_record_is_complete_and_validated(ctx):
    with pytest.raises(ValueError):  # wins + losses can't exceed calls closed
        sarthi.save_track(ctx, "last 30 days", {"F&O": {"closed": 10, "target": 8, "stoploss": 4}}, [], "", "")
    sarthi.save_track(ctx, "last 30 days", {"F&O": {"closed": 20, "target": 14, "stoploss": 6},
                                            "Equity": {"closed": 10, "target": 6, "stoploss": 4}},
                      ["F&O: Bank Nifty idea of 6 Oct closed at target", "General highlight"], "Research desk", "")
    assert sarthi.track_line(ctx, "F&O") == "Sarthi F&O, last 30 days: 14 of 20 closed ideas hit target, 6 hit stop-loss"
    assert sarthi.track_line(ctx, None) == "Sarthi, last 30 days: 20 of 30 closed ideas hit target, 10 hit stop-loss"
    assert sarthi.highlight_for(ctx, "F&O") == "Bank Nifty idea of 6 Oct closed at target"
    assert sarthi.highlight_for(ctx, "Commodity") == "General highlight"
    assert "does not guarantee" in sarthi.track(ctx)["disclaimer"]


def test_no_track_record_means_no_numbers(ctx):
    lead = add_lead(ctx, "N1", {"stage": "kyc_no_deposit", "registration_date": (NOW - timedelta(days=1)).isoformat()})
    ins = customer_insight(ctx, lead)
    assert "closed ideas hit target" not in ins["ideas"]
    assert ins["ideas"].startswith("• Sarthi explained simply")


def test_unfunded_research_user_gets_sarthi_and_margin_pitch(ctx):
    sarthi.save_track(ctx, "last 30 days", {"F&O": {"closed": 20, "target": 14, "stoploss": 6}}, [], "Research desk", "")
    ctx.cfg["sarthi"]["starter_amount"] = {"F&O": 25000}
    lead = add_lead(ctx, "U1", {"stage": "kyc_no_deposit", "sarthi_active": False, "preferred_segment": "FNO"},
                    [("CALL_ACCEPTED_MOB", 4), ("RECOMMENDATION_LISTING_OPENED_MOB · fando_research", 6)])
    ins = customer_insight(ctx, lead)
    assert ins["opener"].startswith("I saw you've been following Sarthi's F&O trade ideas")
    ideas = ins["ideas"].split("\n")
    assert ideas[0] == "• Sarthi F&O, last 30 days: 14 of 20 closed ideas hit target, 6 hit stop-loss. Say it as it is, then: past results don't guarantee future ones"
    assert "• Activate Sarthi for F&O with them on the call" in ideas
    assert any("add funds now (₹25,000)" in i for i in ideas)
    assert "Sarthi user: accepted 4 Sarthi idea(s)" in ins["levers"] and "Sarthi not activated yet" in ins["levers"]
    assert ins["next_action"].startswith("Funds added on the call + Sarthi activated")
    assert ins["sarthi_status"] == "not activated"


def test_dormant_and_funded_openers_lead_with_sarthi(ctx):
    dormant = add_lead(ctx, "D1", {"stage": "dormant_trader", "days_since_last_trade": 400, "sarthi_active": True})
    funded = add_lead(ctx, "F1", {"stage": "funded_no_trade"})
    d, f = customer_insight(ctx, dormant), customer_insight(ctx, funded)
    assert d["opener"].startswith("It's been over 1 year since your last trade. Sarthi now sends ready trade ideas")
    assert "Sarthi is active: open today's ideas together" in d["ideas"]
    assert "Sarthi is an easy way to start" in f["opener"]


def test_pitches_never_promise_earnings(ctx):
    sarthi.save_track(ctx, "last 30 days", {"Equity": {"closed": 10, "target": 6, "stoploss": 4}}, [], "", "")
    for i, extra in enumerate([{"stage": s} for s in ("kyc_pending", "kyc_no_deposit", "funded_no_trade", "dormant_trader", "active_trader")]):
        ins = customer_insight(ctx, add_lead(ctx, f"P{i}", extra, [("ORDER_PLACED_MOB", 2), ("CALL_ACCEPTED_MOB", 1)]))
        text = " ".join([ins["opener"], ins["ideas"], ins["objection"], ins["next_action"], ins["levers"]])
        assert not PROMISES.search(text), text


def test_sarthi_is_not_repeated_in_the_points(ctx):
    lead = add_lead(ctx, "R1", {"stage": "kyc_no_deposit"}, [("RECOMMENDATION_LISTING_OPENED_MOB · commodity_research", 3)])
    ideas = customer_insight(ctx, lead)["ideas"].split("\n")
    assert sum("ready" in i and "Sarthi" in i for i in ideas) == 1
