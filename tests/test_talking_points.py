from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from wepipeline import orders, talking_points as tp
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
    ideas = [{"at": NOW - timedelta(hours=3), "idea": "Buy NIFTY 23300 CE", "segment": "F&O"},
             {"at": NOW - timedelta(hours=30), "idea": "Buy TCS", "segment": "Equity"}]
    monkeypatch.setattr(orders, "sarthi_ideas", lambda ctx, days=9: sorted(ideas, key=lambda x: x["at"]))
    return c


def ev(ctx, user, name, hours_ago):
    ctx.conn.execute("INSERT INTO events (user_id, event_name, event_time, attrs) VALUES (?,?,?,'{}')",
                     (user, name, iso(NOW - timedelta(hours=hours_ago))))


def order(ctx, cid, hours_ago, symbol, side="B", n=[0]):
    n[0] += 1
    ctx.conn.execute("INSERT INTO app_orders (id, client_id, symbol, side, qty, fill_at) VALUES (?,?,?,?,?,?)",
                     (n[0], cid, symbol, side, 50, iso(NOW - timedelta(hours=hours_ago))))


def test_one_talking_point_from_the_most_important_recent_action(ctx):
    ev(ctx, "FUNDER", "FUNDS_PROCEED_TO_ADD_MOB", 20); ev(ctx, "FUNDER", "CALL_ACCEPTED_MOB", 2)  # deposit attempt beats Sarthi
    ev(ctx, "SARTHI", "CALL_ACCEPTED_MOB", 2); ev(ctx, "SARTHI", "DASHBOARD_OPENED_MOB", 1)       # login never beats an action
    ev(ctx, "TRADER", "PLACE_ORDER_OPENED_MOB", 30); order(ctx, "TRADER", 6, "NIFTY03NOV26C23200")  # traded after trying
    ev(ctx, "LEFT", "ORDER_PLACED_MOB", 50); ev(ctx, "LEFT", "PLACE_ORDER_OPENED_MOB", 4); ev(ctx, "LEFT", "PLACE_ORDER_OPENED_MOB", 3)
    ev(ctx, "VIEWER", "RECOMMENDATION_LISTING_OPENED_MOB", 28)
    ev(ctx, "OLDDEP", "FUNDS_PROCEED_TO_ADD_MOB", 24 * 5); ev(ctx, "OLDDEP", "SEARCH_SCREEN_OPENED_MOB", 5)  # fresh 72 h wins
    ev(ctx, "LOGIN", "DASHBOARD_OPENED_MOB", 26)
    ctx.conn.commit()
    leads = [{"lead_id": i, "extra": "{}"} for i in ("FUNDER", "SARTHI", "TRADER", "LEFT", "VIEWER", "OLDDEP", "LOGIN", "QUIET")]
    out = tp.talking_points(ctx, leads, "new")
    assert out["FUNDER"]["key_activity"] == "Tried to add funds yesterday 19:00 (no money received yet)"
    assert out["FUNDER"]["talking_point"].startswith("Aapne kal 19:00 baje funds add karne ki koshish ki thi.")
    assert out["SARTHI"]["key_activity"] == "Accepted a Sarthi idea today 13:00; idea live then: Buy NIFTY 23300 CE (F&O)"
    assert "Sarthi ka idea accept kiya tha (Buy NIFTY 23300 CE, F&O)" in out["SARTHI"]["talking_point"]
    assert out["SARTHI"]["talking_point"].endswith("Account mein funds add karke aise ideas turant le sakte hain. Abhi UPI se karwa doon?")
    assert out["TRADER"]["key_activity"] == "Bought 50 NIFTY 23200 CE option today 09:00"
    assert out["TRADER"]["talking_point"] == ("Aapne aaj 09:00 baje NIFTY 23200 CE option kharida tha. "
                                               "Agle trade ke liye account mein funds add kar lijiye. Abhi UPI se karwa doon?")
    assert out["LEFT"]["key_activity"] == "Opened the order window 2x without placing an order (last today 12:00)"
    assert out["VIEWER"]["key_activity"] == "Checked Sarthi recommendations yesterday 11:00; idea live then: Buy TCS (Equity)"
    assert out["OLDDEP"]["key_activity"] == "Searched / looked at stocks today 10:00"
    assert out["LOGIN"]["key_activity"] == "Opened the app yesterday 13:00"
    assert out["QUIET"]["key_activity"] == "No app activity in the last 7 days" and out["QUIET"]["talking_point"].startswith("Account khul gaya hai")
    assert tp.talking_points(ctx, leads[-1:], "low_margin")["QUIET"]["talking_point"].startswith("Aapke account mein funds hain")
    assert "\n" not in out["FUNDER"]["talking_point"]  # one point, not a list


def test_trading_symbols_are_readable():
    w = orders.symbol_words
    assert [w("LAURUSLABS-EQ"), w("NIFTY03NOV26C23200"), w("BANKNIFTY25NOV2652000PE"), w("NATGASMINI24NOV26")] == \
        ["LAURUSLABS", "NIFTY 23200 CE option", "BANKNIFTY 52000 PE option", "NATGASMINI future"]
