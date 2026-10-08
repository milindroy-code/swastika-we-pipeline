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
        sarthi.save_track(ctx, "last 30 days", {"F&O": {"closed": 10, "profit": 8, "loss": 4}}, [], "", "")
    sarthi.save_track(ctx, "last 30 days", {"F&O": {"closed": 20, "profit": 14, "loss": 6},
                                            "Equity": {"closed": 10, "profit": 6, "loss": 4}},
                      ["F&O: Bank Nifty idea of 6 Oct closed at target", "General highlight"], "Research desk", "")
    assert sarthi.track_line(ctx, "F&O") == "Sarthi F&O, last 30 days: 14 of 20 closed ideas ended in profit, 6 in loss"
    assert sarthi.track_line(ctx, None) == "Sarthi, last 30 days: 20 of 30 closed ideas ended in profit, 10 in loss"
    assert sarthi.highlight_for(ctx, "F&O") == "Bank Nifty idea of 6 Oct closed at target"
    assert sarthi.highlight_for(ctx, "Commodity") == "General highlight"
    assert "does not guarantee" in sarthi.track(ctx)["disclaimer"]


def test_no_track_record_means_no_numbers(ctx):
    lead = add_lead(ctx, "N1", {"stage": "kyc_no_deposit", "registration_date": (NOW - timedelta(days=1)).isoformat()})
    ins = customer_insight(ctx, lead)
    assert "closed ideas ended in profit" not in ins["ideas"]
    assert ins["ideas"].startswith("• Sarthi explained simply")


def test_unfunded_research_user_gets_sarthi_and_margin_pitch(ctx):
    sarthi.save_track(ctx, "last 30 days", {"F&O": {"closed": 20, "profit": 14, "loss": 6}}, [], "Research desk", "")
    ctx.cfg["sarthi"]["starter_amount"] = {"F&O": 25000}
    lead = add_lead(ctx, "U1", {"stage": "kyc_no_deposit", "sarthi_active": False, "preferred_segment": "FNO"},
                    [("CALL_ACCEPTED_MOB", 4), ("RECOMMENDATION_LISTING_OPENED_MOB · fando_research", 6)])
    ins = customer_insight(ctx, lead)
    assert ins["opener"].startswith("I saw you've been following Sarthi's F&O trade ideas")
    ideas = ins["ideas"].split("\n")
    assert ideas[0] == "• Sarthi F&O, last 30 days: 14 of 20 closed ideas ended in profit, 6 in loss. Say it as it is, then: past results don't guarantee future ones"
    assert ideas[1].startswith("• Activate Sarthi for F&O with them now: it's needed to receive the ideas")
    assert ins["opener"].endswith("The first step is switching on Sarthi so its ideas start reaching you.")
    assert any("add funds now (₹25,000)" in i for i in ideas)
    assert "Sarthi user: accepted 4 Sarthi idea(s)" in ins["levers"] and "Sarthi not activated: activate it on the call" in ins["levers"]
    assert ins["next_action"].startswith("Sarthi activated + funds added on the call")
    assert ins["sarthi_status"] == "not activated"


def test_dormant_and_funded_openers_lead_with_sarthi(ctx):
    dormant = add_lead(ctx, "D1", {"stage": "dormant_trader", "days_since_last_trade": 400, "sarthi_active": True})
    funded = add_lead(ctx, "F1", {"stage": "funded_no_trade"})
    d, f = customer_insight(ctx, dormant), customer_insight(ctx, funded)
    assert d["opener"].startswith("It's been over 1 year since your last trade. Sarthi now sends ready trade ideas")
    assert "Sarthi is active: open today's ideas together" in d["ideas"]
    assert "Sarthi is an easy way to start" in f["opener"]


def test_pitches_never_promise_earnings(ctx):
    sarthi.save_track(ctx, "last 30 days", {"Equity": {"closed": 10, "profit": 6, "loss": 4}}, [], "", "")
    for i, extra in enumerate([{"stage": s} for s in ("kyc_pending", "kyc_no_deposit", "funded_no_trade", "dormant_trader", "active_trader")]):
        ins = customer_insight(ctx, add_lead(ctx, f"P{i}", extra, [("ORDER_PLACED_MOB", 2), ("CALL_ACCEPTED_MOB", 1)]))
        text = " ".join([ins["opener"], ins["ideas"], ins["objection"], ins["next_action"], ins["levers"]])
        assert not PROMISES.search(text), text


def test_sarthi_is_not_repeated_in_the_points(ctx):
    lead = add_lead(ctx, "R1", {"stage": "kyc_no_deposit"}, [("RECOMMENDATION_LISTING_OPENED_MOB · commodity_research", 3)])
    ideas = customer_insight(ctx, lead)["ideas"].split("\n")
    assert sum(("ready ideas" in i or "ready trade ideas" in i) and "Sarthi" in i for i in ideas) == 1


def test_pricing_and_rm_offers(ctx):
    nodep = customer_insight(ctx, add_lead(ctx, "O1", {"stage": "kyc_no_deposit"}))
    assert any(i.startswith("• Pricing: discount brokerage pricing, comparable with Groww and Zerodha") for i in nodep["ideas"].split("\n"))
    assert "\"I already use Zerodha or Groww\" →" in nodep["objection"]
    assert "Relationship Manager" not in nodep["ideas"]           # RM is not pushed on a brand-new no-deposit client
    dormant = customer_insight(ctx, add_lead(ctx, "O2", {"stage": "dormant_trader", "days_since_last_trade": 90}))
    points = dormant["ideas"].split("\n")
    rm = next(i for i, p in enumerate(points) if "dedicated Relationship Manager" in p)
    price = next(i for i, p in enumerate(points) if p.startswith("• Pricing:"))
    assert rm < price                                              # dormant: RM before price
    assert "\"No time to manage trades\" → we offer a dedicated Relationship Manager" in dormant["objection"]
    rich = customer_insight(ctx, add_lead(ctx, "O3", {"stage": "kyc_no_deposit", "holdings": 500000}))
    assert "offer a dedicated RM" in rich["levers"]


def test_unknown_status_asks_to_check_and_logged_activation_sticks(ctx):
    from wepipeline.followups import log_call
    from wepipeline.clientdb import enrich_leads
    import pandas as pd

    lead = add_lead(ctx, "S1", {"stage": "kyc_no_deposit"})
    ins = customer_insight(ctx, lead)
    assert ins["sarthi_status"] == "unknown"
    assert "• Check Sarthi is activated (it's needed to receive the ideas); if not, activate it with them now" in ins["ideas"].split("\n")
    assert ins["next_action"].startswith("Sarthi activated + funds added")

    log_call(ctx, "S1", "gm_01", "interested", "Switched on Sarthi F&O", sarthi_activated=True)
    after = customer_insight(ctx, one(ctx.conn, "SELECT * FROM leads WHERE lead_id='S1'"))
    assert after["sarthi_status"] == "active" and "Check Sarthi" not in after["ideas"]
    assert one(ctx.conn, "SELECT sarthi_activated FROM calls WHERE lead_id='S1'")["sarthi_activated"] == 1
    # the client database replica still says "off": the GM's activation is kept
    enrich_leads(ctx, pd.DataFrame([{"CLIENT_ID": "S1", "REGISTRATION_DATE": NOW.replace(tzinfo=None), "IsSarthiTIOn": 0}]))
    assert customer_insight(ctx, one(ctx.conn, "SELECT * FROM leads WHERE lead_id='S1'"))["sarthi_status"] == "active"


def test_preferred_segment_lists():
    assert sarthi.segment_from_preference("Intraday,Delivery,FNO") == "F&O"
    assert sarthi.segment_from_preference("Delivery,Intraday,FNO,Commodity") == "F&O"
    assert sarthi.segment_from_preference("Commodity") == "Commodity"
    assert sarthi.segment_from_preference("Intraday,Delivery") == "Equity"
    assert sarthi.segment_from_preference("Segment Disabled") is None
    assert sarthi.segment_from_preference(None) is None


def test_pull_track_from_research_db(ctx, monkeypatch):
    import pandas as pd
    import wepipeline.clientdb as clientdb

    seen = {}

    def fake_query(c, sql):
        seen["sql"] = sql
        return pd.DataFrame([{"Segment": "Commodity", "closed": 159, "profit": 103, "loss": 56, "target_hit": 16, "stoploss_hit": 32},
                             {"Segment": "F&O", "closed": 44, "profit": 26, "loss": 18, "target_hit": 7, "stoploss_hit": 11}])
    monkeypatch.setattr(clientdb, "_query", fake_query)
    sarthi.save_track(ctx, "x", {}, ["F&O: approved example"], "manual", "Custom disclaimer")
    sarthi.pull_track(ctx)
    assert "tblScripOrder" in seen["sql"] and "Avoid call is not executed" in seen["sql"] and "DAY, -30" in seen["sql"]
    assert sarthi.track_line(ctx, "Commodity") == "Sarthi Commodity, last 30 days: 103 of 159 closed ideas ended in profit, 56 in loss"
    t = sarthi.track(ctx)
    assert t["highlights"] == ["F&O: approved example"] and t["disclaimer"] == "Custom disclaimer"  # admin's text kept
    assert t["source"].endswith("(auto)")


def test_blank_sarthi_flag_in_client_db_means_not_activated(ctx):
    from wepipeline.clientdb import classify
    info = classify(ctx, {"CLIENT_ID": "X", "REGISTRATION_DATE": NOW.replace(tzinfo=None), "IsSarthiTIOn": None})
    assert info["extra"]["sarthi_active"] is False
    assert classify(ctx, {"CLIENT_ID": "Y", "IsSarthiTIOn": True})["extra"]["sarthi_active"] is True
