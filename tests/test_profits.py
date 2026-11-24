from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from wepipeline import clientdb, profits
from wepipeline.context import load_config
from tests.helpers import open_test_ctx

TZ = ZoneInfo("Asia/Kolkata")
NOW = datetime(2026, 11, 3, 15, 0, tzinfo=TZ)


@pytest.fixture
def ctx(tmp_path, monkeypatch):
    cfg = load_config()
    cfg["database"] = str(tmp_path / "test.db")
    cfg["notifications"]["outbox_dir"] = str(tmp_path / "outbox")
    cfg["notifications"]["email"]["enabled"] = False
    cfg["notifications"]["webhook_url"] = ""
    cfg["market_notes_file"] = str(tmp_path / "none.md")
    cfg.setdefault("referral", {})["profit_booking"] = {"bulk_clients": 3}
    c = open_test_ctx(cfg)
    c._now = NOW
    closes = pd.DataFrame([
        {"CLIENT_ID": "winner ", "PL_AMT": 9000, "ClosedOn": "2026-11-02", "Scrip": "EF HDFCBANK 23Nov2026"},
        {"CLIENT_ID": "WINNER", "PL_AMT": -1000, "ClosedOn": "2026-10-30", "Scrip": "TCS"},
        {"CLIENT_ID": "SMALL", "PL_AMT": 400, "ClosedOn": "2026-11-02", "Scrip": "TCS"},       # under the ₹1,000 floor
        {"CLIENT_ID": "LOSER", "PL_AMT": -5000, "ClosedOn": "2026-11-02", "Scrip": "TCS"},
        {"CLIENT_ID": "OLDWIN", "PL_AMT": 30000, "ClosedOn": "2026-10-28", "Scrip": "LT"},
        {"CLIENT_ID": "CLOSED", "PL_AMT": 8000, "ClosedOn": "2026-11-02", "Scrip": "LT"},       # account closed
    ] + [{"CLIENT_ID": f"IPO{i}", "PL_AMT": 5000, "ClosedOn": "2026-10-28", "Scrip": "JPL"} for i in range(3)])  # an IPO listing

    def query(_ctx, sql):
        if "FIFO_Process" in sql:
            return closes
        return pd.DataFrame({"CLIENT_ID": ["WINNER", "SMALL", "LOSER", "OLDWIN", "IPO0", "IPO1", "IPO2"],
                             "IsCentralized": [1, 1, 1, 0, 1, 1, 1]})

    monkeypatch.setattr(clientdb, "_query", query)
    return c


def test_profit_bookers_are_found_and_lift_the_referral_score(ctx):
    res = profits.pull(ctx)
    assert res["clients"] == 2 and res["set_aside"] == {"JPL 28 Oct": 3}  # WINNER and OLDWIN; the IPO listing is left out
    b = profits.booking(ctx, "winner")
    assert (b["net_profit"], b["profit_closes"], b["closes"], b["best_scrip"], b["last_profit_date"]) == (8000, 1, 2, "HDFCBANK future", "2026-11-02")
    pts, why = profits.boost(ctx, "WINNER")
    assert pts == round(25 * (8000 / 25000) ** 0.5, 1) and why.startswith("booked ₹8,000 profit in 7 d (best: HDFCBANK future +₹9,000 on 02 Nov)")
    assert profits.boost(ctx, "OLDWIN")[0] == 12.5  # full profit, but 6 days ago: half
    assert profits.talking_point(ctx, "WINNER").startswith("Badhai ho! Aapne kal HDFCBANK future mein ₹9,000 ka profit book kiya.")
    assert [t["client_id"] for t in profits.targets(ctx, "central")] == ["WINNER"]
    assert [t["client_id"] for t in profits.targets(ctx, "all", days=3)] == ["WINNER"]
