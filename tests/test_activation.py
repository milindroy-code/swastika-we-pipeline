"""Sarthi activation drives (own dashboard): Sarthi-off target list, scoring from trades / app interest / campaigns,
drives, outcomes, results from the client database, pitch. Nothing may touch the funding queue or its reports."""
import json
from datetime import datetime, timedelta

import pandas as pd

from tests.test_followup_rules import TUE_1100, TZ, client, ctx  # noqa: F401  (ctx is a fixture)
from wepipeline import activation as act
from wepipeline import drives
from wepipeline.db import one, rows
from wepipeline.followups import check_overdue, queue_for


def members(ctx, key="sarthi"):
    return {r["lead_id"]: r for r in rows(ctx.conn, "SELECT * FROM drive_members WHERE program=?", (key,))}


def pool_rows(*recs, last="2026-10-05"):
    """Rows shaped like the Sarthi pool query: trades + the Sarthi flag."""
    return pd.DataFrame([{"CLIENT_ID": cid, "TotalTrades": n, "ActiveTradingDays": d, "FirstTradeInPeriod": "2026-08-10",
                          "LastTradeDate": last, "ActivityBucket": b, "IsSarthiTIOn": on, "SarthiActivationDate": None,
                          "PreferredSegment": "Intraday,FNO", "IsAccountClosed": closed} for cid, n, d, b, on, closed in recs])


def event(ctx, uid, name, count, hours_ago=2):
    ctx.conn.execute("INSERT INTO events (user_id, event_name, event_time, attrs) VALUES (?,?,?,?)",
                     (uid, name, (TUE_1100 - timedelta(hours=hours_ago)).isoformat(), json.dumps({"count": count})))
    ctx.conn.commit()


def test_only_sarthi_off_open_accounts_join_and_switched_on_clients_leave(ctx):
    client(ctx, "FUND", campaign="new")                                         # being worked for funding: left alone
    res = act.load_traders(ctx, pool_rows(("OFF1", 160, 40, "Very High", None, 0), ("OFF2", 8, 4, "Low", "", 0),
                                          ("ON1", 150, 40, "Very High", "1", 0), ("SHUT", 90, 30, "High", None, 1),
                                          ("TINY", 2, 1, "Very Low", None, 0), ("FUND", 70, 25, "High", None, 0)))
    assert (res["sarthi_off"], res["eligible"], res["added"], res["busy"]) == (4, 3, 2, 1)
    assert {k for k, v in members(ctx).items() if v["status"] == "open"} == {"OFF1", "OFF2"}
    assert json.loads(one(ctx.conn, "SELECT extra FROM leads WHERE lead_id='FUND'")["extra"])["sarthi_active"] is False
    act.load_traders(ctx, pool_rows(("OFF1", 160, 40, "Very High", "1", 0), ("OFF2", 8, 4, "Low", None, 0)))  # OFF1 switched on
    assert members(ctx)["OFF1"]["status"] == "activated"


def test_app_users_with_sarthi_off_join_when_engaged_and_sarthi_interest_drives_the_tier(ctx):
    for uid in ("KEEN", "BROWSER", "QUIET", "ALREADY", "UNKNOWN"):
        event(ctx, uid, "DASHBOARD_OPENED_MOB", 1, hours_ago=200)
    event(ctx, "KEEN", "CALL_ACCEPTED_MOB", 3)                                  # accepted ideas but can't receive them
    event(ctx, "KEEN", "RECOMMENDATION_LISTING_OPENED_MOB · F&O", 4)
    event(ctx, "BROWSER", "RECOMMENDATION_LISTING_OPENED_MOB · Equity", 1, hours_ago=120)
    status = pd.DataFrame([{"CLIENT_ID": c, "IsSarthiTIOn": on, "SarthiActivationDate": None, "PreferredSegment": "", "IsAccountClosed": 0}
                           for c, on in [("KEEN", None), ("BROWSER", ""), ("QUIET", None), ("ALREADY", "1")]])
    res = act.load_app_users(ctx, status)
    assert (res["eligible"], res["unknown"]) == (2, 1)                          # QUIET: barely active, no Sarthi interest
    t = {k: (v["heat_tier"], v["heat_score"]) for k, v in members(ctx).items()}
    assert set(t) == {"KEEN", "BROWSER"} and t["KEEN"][0] == "hot" and t["BROWSER"][0] == "cold"
    reason = members(ctx)["KEEN"]["heat_reasons"]
    assert "accepted 3 Sarthi idea(s)" in reason and "with Sarthi off" in reason


def test_drive_outcomes_and_results_from_the_database_without_touching_the_funding_queue(ctx):
    act.load_traders(ctx, pool_rows(*[(f"T{i}", 200 - 20 * i, 40 - 3 * i, "Very High", None, 0) for i in range(4)]))
    camp = act.start_campaign(ctx, "Sarthi switch-on Oct", by="sm")
    assert act.start_drive(ctx, per_gm=3, start_now=True)["assigned"] == 3
    assert [c["lead_id"] for c in act.my_calls(ctx, "gm")] == ["T0", "T1", "T2"]  # hottest first
    act.log_call(ctx, "T0", "gm", "activated")                                  # switched on during the call
    assert members(ctx)["T0"]["status"] == "activated"
    assert json.loads(one(ctx.conn, "SELECT extra FROM leads WHERE lead_id='T0'")["extra"])["sarthi_active"] is True
    act.log_call(ctx, "T1", "gm", "interested", next_at=TUE_1100 + timedelta(days=1))
    assert queue_for(ctx, "gm") == [] and rows(ctx.conn, "SELECT * FROM followups") == []
    ctx._now = TUE_1100 + timedelta(days=2)
    check_overdue(ctx)
    assert rows(ctx.conn, "SELECT * FROM alerts") == []
    # the client DB later shows T1 switched on after the call, and X (never asked) on their own
    act.load_activations(ctx, pd.DataFrame([{"CLIENT_ID": "T1", "IsSarthiTIOn": "1", "SarthiActivationDate": "2026-10-07 10:00"},
                                            {"CLIENT_ID": "X", "IsSarthiTIOn": "1", "SarthiActivationDate": "2026-10-07 12:00"},
                                            {"CLIENT_ID": "OLD", "IsSarthiTIOn": "1", "SarthiActivationDate": "2026-09-01 12:00"}]))
    assert members(ctx)["T1"]["status"] == "activated"
    r = act.report(ctx, camp["id"])
    t = r["totals"]
    assert (t["assigned"], t["called"], t["reached"], t["activated_on_call"]) == (3, 2, 2, 1)
    credit = {w["client_id"]: (w["gm"], w.get("pending_db", False)) for w in r["wins"]}
    assert credit == {"T1": ("gm", False), "X": (None, False), "T0": ("gm", True)}  # T0: GM-marked, DB not updated yet
    assert (t["wins"], t["wins_credited"], r["by_gm"]["gm"]["wins"]) == (3, 2, 2)
    from wepipeline.reports import compliance

    assert compliance(ctx, TUE_1100.date())[0]["calls"] == 0                    # funding compliance untouched
    new = act.start_campaign(ctx, "Next push")
    assert new["returned_to_pool"] == 1                                         # T2 was never called


def test_sarthi_pitch_shows_and_switches_on_without_funding_push_promises_or_charge_claims(ctx):
    from wepipeline.insights import customer_insight

    event(ctx, "K", "CALL_ACCEPTED_MOB", 2)
    act.load_traders(ctx, pool_rows(("K", 120, 30, "Very High", None, 0)))
    ins = customer_insight(ctx, drives.card(ctx, "sarthi", drives.member(ctx, "sarthi", "K")))
    assert ins["stage"] == "sarthi activation" and "is off" in ins["levers"]
    text = " ".join([ins["call_ideas_text"], ins["closing_text"], ins["objections_hi"], ins["next_action"], ins["ideas"]])
    assert "on kar dete hain" in text and "koi trade place nahi" in text.lower() and "switched on during the call" in text
    assert "30,000" not in text and "UPI" not in text and "extra charge nahi" not in text
    assert "guarantee nahi" in text or "guaranteed nahi" in text


def test_sarthi_campaign_engagement_marks_clients_hot(ctx):
    long = pd.DataFrame([{"ClientCode": "A1", "Event": "Activated Sarthi from link", "Date": "2026-10-06"},
                         {"ClientCode": "A2", "Event": "Clicked", "Date": "2026-10-06"},
                         {"ClientCode": "A2", "Event": "Replied", "Date": "2026-10-06"}])
    res = act.load_engagement(ctx, long, campaign="Sarthi push", channel="WhatsApp")
    assert res["engaged"] == 2
    t = {k: v["heat_tier"] for k, v in members(ctx).items()}
    assert t["A1"] in {"hot", "warm"} and t["A2"] == "warm"
    rep = act.channel_report(ctx)
    assert rep[0]["users"] == 2 and rep[0]["channel"] == "WhatsApp"
    # referral engagement is a separate program: no cross-talk
    from wepipeline.referral import campaign_report

    assert campaign_report(ctx) == []


def test_client_types_new_first_and_filters(ctx):
    df = pd.DataFrame([
        {"CLIENT_ID": "NEWBIE", "TotalTrades": 12, "ActiveTradingDays": 5, "FirstTradeInPeriod": "2026-09-28", "LastTradeDate": "2026-10-05",
         "ActivityBucket": "Low", "IsSarthiTIOn": None, "IsAccountClosed": 0, "REGISTRATION_DATE": "2026-09-25"},          # joined 11 days ago
        {"CLIENT_ID": "BACK", "TotalTrades": 40, "ActiveTradingDays": 12, "FirstTradeInPeriod": "2026-09-26", "LastTradeDate": "2026-10-05",
         "ActivityBucket": "Medium", "IsSarthiTIOn": None, "IsAccountClosed": 0, "REGISTRATION_DATE": "2022-01-10"},       # old, trading again
        {"CLIENT_ID": "REGULAR", "TotalTrades": 160, "ActiveTradingDays": 40, "FirstTradeInPeriod": "2026-08-10", "LastTradeDate": "2026-10-05",
         "ActivityBucket": "Very High", "IsSarthiTIOn": None, "IsAccountClosed": 0, "REGISTRATION_DATE": "2021-05-01"}])
    act.load_traders(ctx, df)
    m = members(ctx)
    assert {k: v["segment"] for k, v in m.items()} == {"NEWBIE": "new", "BACK": "resurrected", "REGULAR": "active"}
    assert "new client (joined 25 Sep)" in m["NEWBIE"]["heat_reasons"]
    assert m["REGULAR"]["heat_score"] > m["NEWBIE"]["heat_score"]               # the heavy trader still scores higher...
    act.start_drive(ctx, per_gm=3, start_now=True)
    assert [c["lead_id"] for c in act.my_calls(ctx, "gm")][0] == "NEWBIE"       # ...but new clients are handed out first
    act.end_drive(ctx)
    assert act.start_drive(ctx, per_gm=3, start_now=True, segments=["resurrected"])["assigned"] == 1
    assert [c["lead_id"] for c in act.my_calls(ctx, "gm")] == ["BACK"]
    assert act.summary(ctx)["by_type"] == {"new": 1, "active": 1, "resurrected": 0}


def test_every_new_client_with_sarthi_off_joins_unless_on_a_funding_list(ctx):
    client(ctx, "INFUNDING", campaign="new")                                   # its funding call asks for Sarthi already
    df = pd.DataFrame([{"CLIENT_ID": c, "REGISTRATION_DATE": d, "IsSarthiTIOn": on, "IsAccountClosed": 0, "FirstDeposit": dep, "FTD": None}
                       for c, d, on, dep in [("FUNDEDNEW", "2026-09-30", None, 30000), ("FRESH", "2026-10-04", "0", None),
                                             ("ON", "2026-10-01", "1", 5000), ("INFUNDING", "2026-10-02", None, None)]])
    res = act.load_new(ctx, df)
    assert (res["sarthi_off"], res["added"], res["busy"]) == (3, 2, 1)
    m = members(ctx)
    assert set(m) == {"FUNDEDNEW", "FRESH"} and m["FUNDEDNEW"]["segment"] == m["FRESH"]["segment"] == "new"
    assert m["FUNDEDNEW"]["heat_score"] > m["FRESH"]["heat_score"]               # funded new client ranks higher
    assert "deposited ₹30,000" in m["FUNDEDNEW"]["heat_reasons"]
    act.load_traders(ctx, pool_rows(("TOP", 160, 40, "Very High", None, 0)))    # a trader pull doesn't drop new clients
    assert members(ctx)["FRESH"]["status"] == "open"
