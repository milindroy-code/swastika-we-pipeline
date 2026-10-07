from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from tests.helpers import open_test_ctx
from wepipeline import market
from wepipeline.context import load_config

TZ = ZoneInfo("Asia/Kolkata")
NOW = datetime(2026, 10, 7, 16, 0, tzinfo=TZ)


@pytest.fixture
def ctx(tmp_path):
    cfg = load_config()
    cfg["database"] = str(tmp_path / "test.db")
    cfg["market_notes_file"] = str(tmp_path / "none.md")
    c = open_test_ctx(cfg)
    c._now = NOW
    return c


class Resp:
    def __init__(self, payload=None, content=b""):
        self.payload, self.content = payload, content

    def raise_for_status(self):
        pass

    def json(self):
        return self.payload


def yahoo_payload(prices, last):
    ts = [int(datetime(2026, 10, d, 9, 15).timestamp()) for d in (1, 2, 5, 6, 7)]
    return {"chart": {"result": [{"meta": {"regularMarketPrice": last, "regularMarketTime": ts[-1]},
                                  "timestamp": ts, "indicators": {"quote": [{"close": prices}]}}]}}


def test_yahoo_change_is_vs_previous_session_not_range_start(monkeypatch):
    # 2 Oct was a holiday (empty bar); yesterday closed at 73,068
    monkeypatch.setattr(market.requests, "get", lambda *a, **k: Resp(yahoo_payload([71910, None, 72382, 73068, 72639], 72639)))
    q = market.fetch_yahoo("^BSESN")
    assert q["prev"] == 73068 and round(q["pct"], 2) == -0.59


def test_headline_filter_keeps_macro_and_drops_tips(ctx, monkeypatch):
    rss = b"""<rss><channel>
      <item><title>Jefferies picks 3 bank stocks to gain the most from RBI rate hikes</title><pubDate>Wed, 07 Oct 2026 12:00:00 +0530</pubDate></item>
      <item><title>5 commodity stocks that rallied up to 48% in a month</title><pubDate>Wed, 07 Oct 2026 12:00:00 +0530</pubDate></item>
      <item><title>India bonds tumble after RBI's first rate hike in nearly four years</title><pubDate>Wed, 07 Oct 2026 12:30:00 +0530</pubDate></item>
      <item><title>Dow Jones| Nasdaq | Live: US futures slip as yields rebound; Fed minutes in focus</title><pubDate>Wed, 07 Oct 2026 13:00:00 +0530</pubDate></item>
      <item><title>Celebrity wedding photos</title><pubDate>Wed, 07 Oct 2026 13:00:00 +0530</pubDate></item>
      <item><title>Old: Sensex hits record</title><pubDate>Mon, 05 Oct 2026 10:00:00 +0530</pubDate></item>
    </channel></rss>"""
    monkeypatch.setattr(market, "FEEDS", [("ET Markets", "x")])
    monkeypatch.setattr(market.requests, "get", lambda *a, **k: Resp(content=rss))
    titles = [h["text"] for h in market.fetch_headlines(ctx, limit=5)]
    assert titles == ["India bonds tumble after RBI's first rate hike in nearly four years",
                      "US futures slip as yields rebound; Fed minutes in focus"]


def q(last, pct):
    return {"last": last, "pct": pct, "prev": last / (1 + pct / 100), "time": datetime.now()}


def test_sanity_checks_drop_a_source_that_disagrees(ctx):
    idx = {"NIFTY 50": {"last": 22603, "pct": -0.76, "adv": 9, "dec": 41},
           "INDIA VIX": {"last": 13.93, "pct": 2.35, "adv": None, "dec": None}}
    yahoo = {"^NSEI": q(22603, -0.76), "^INDIAVIX": q(13.88, 2.02), "^BSESN": q(72639, 1.0),  # Sensex disagrees
             "INR=X": q(96.8, 0.4), "GC=F": q(4114, -1.7), "CL=F": q(500, 0.4)}  # crude out of range
    checks = {c["check"]: c for c in market.sanity_check(ctx, idx, "07-Oct-2026 15:30", yahoo)}
    assert checks["NIFTY 50: NSE vs Yahoo"]["ok"]
    assert checks["INDIA VIX: NSE vs Yahoo"]["ok"]  # VIX snapshots differ a little; within its tolerance
    assert not checks["Sensex moves with Nifty"]["ok"] and checks["Sensex moves with Nifty"]["drop"] == "SENSEX"
    assert not checks["Crude range ($/bbl)"]["ok"]
    assert checks["Nifty breadth matches its direction"]["ok"]


def test_build_notes_leaves_out_failed_sources(ctx, monkeypatch):
    monkeypatch.setattr(market, "fetch_nse", lambda: {"as_of": "07-Oct-2026 15:30", "rows": {
        "NIFTY 50": {"last": 22603.0, "pct": -0.76, "adv": 9, "dec": 41}, "NIFTY BANK": {"last": 55056.0, "pct": -0.13, "adv": 5, "dec": 7},
        "NIFTY IT": {"last": 27758.0, "pct": -1.34, "adv": 2, "dec": 8}, "NIFTY METAL": {"last": 12307.0, "pct": -2.33, "adv": 1, "dec": 14}}})
    quotes = {"^BSESN": q(72639, 1.4), "^NSEI": q(22603, -0.76), "^NSEBANK": q(55056, -0.13), "^CNXIT": q(27758, -1.34),
              "^INDIAVIX": None, "INR=X": q(96.8, 0.44), "GC=F": q(4114, -1.75), "CL=F": q(89.8, 0.45)}
    monkeypatch.setattr(market, "fetch_yahoo", lambda sym: quotes[sym])
    monkeypatch.setattr(market, "fetch_headlines", lambda c, limit=3: [])
    texts = [n["text"] for n in market.build_notes(ctx)["notes"]]
    assert texts[0].startswith("Nifty 50 closed at 22,603 (−0.8%)") and "Sensex" not in texts[0]  # Sensex failed its check
    assert any(t.startswith("Sectors: IT led (−1.3%), Metal lagged (−2.3%)") for t in texts)
    assert any("gold $4,114/oz (−1.8%)" in t for t in texts)


def test_market_notes_put_own_notes_first_and_skip_samples(ctx):
    from wepipeline.db import set_setting
    from wepipeline.pitch import market_notes
    import json

    set_setting(ctx.conn, "market_notes", "[Sample] old placeholder\n- RBI policy decision today at 10:00", "x")
    set_setting(ctx.conn, "market_auto", json.dumps({"notes": [{"text": "Nifty 50 closed at 22,603", "tags": ["equity"]}]}), "x")
    assert market_notes(ctx, 5) == ["RBI policy decision today at 10:00", "Nifty 50 closed at 22,603"]
    assert market.note_for(ctx, ["F&O"]) == "Nifty 50 closed at 22,603"  # no F&O note: falls back to the index line
