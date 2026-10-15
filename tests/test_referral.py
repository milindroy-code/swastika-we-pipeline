"""Referral drives (own dashboard): pool rules, scoring (trading + app + referral history), drives, outcomes, pitch.
Nothing here may touch the funding queue, its SLAs or its reports."""
import json
from datetime import datetime, timedelta

import pandas as pd

from tests.test_followup_rules import TUE_1100, TZ, client, ctx, due  # noqa: F401  (ctx is a fixture)
from wepipeline.assign import assign_new_leads
from wepipeline.db import one, rows
from wepipeline import drives
from wepipeline.followups import check_overdue, queue_for
from wepipeline.referral import (end_drive, load_candidates, load_engagement, load_history, log_call, my_calls, record_referrals,
                                 start_drive, summary)


def query_rows(*recs, last="2026-10-05"):
    """Rows shaped like the most-active-clients query."""
    return pd.DataFrame([{"CLIENT_ID": cid, "TotalTrades": n, "ActiveTradingDays": d, "FirstTradeInPeriod": "2026-08-10",
                          "LastTradeDate": last, "ActivityBucket": b} for cid, n, d, b in recs])


def history_rows(*recs):
    """Rows shaped like the referral-history query: one per referred client (refree = the referrer)."""
    return pd.DataFrame([{"refree": ref, "LeadId": i, "Client_Id": friend, "AuthorizeDate": "2026-07-01", "WebEngageUniqueId": "",
                          "FTD": ftd, "FirstDeposit": dep, "Holdings": None} for i, (ref, friend, ftd, dep) in enumerate(recs)])


def nxt(ctx, cid):
    return drives.member(ctx, "referral", cid)["next_at"]


def members(ctx, key="referral"):
    return {r["lead_id"]: r for r in rows(ctx.conn, "SELECT * FROM drive_members WHERE program=?", (key,))}


def card(ctx, cid, key="referral"):
    return drives.card(ctx, key, drives.member(ctx, key, cid))


def test_medium_and_up_join_and_busy_or_recent_clients_are_left_alone(ctx):
    client(ctx, "PAY", campaign="payout")                                      # being worked for a payout
    client(ctx, "DONE"); ctx.conn.execute("UPDATE leads SET status='converted' WHERE lead_id='DONE'")
    load_candidates(ctx, query_rows(("ASKED", 90, 30, "High")))
    ctx.conn.execute("UPDATE drive_members SET status='referred', asked_at=? WHERE lead_id='ASKED'",
                     ((TUE_1100 - timedelta(days=10)).isoformat(),))          # gave a referral 10 days ago
    ctx.conn.commit()
    res = load_candidates(ctx, query_rows(("vh1", 180, 40, "Very High"), ("H1", 60, 22, "High"), ("MED", 25, 10, "Medium"),
                                          ("LOW", 8, 4, "Low"), ("PAY", 150, 30, "Very High"), ("DONE", 120, 35, "Very High"),
                                          ("ASKED", 90, 30, "High")))
    assert (res["eligible"], res["added"], res["reopened"], res["busy"], res["asked_recently"]) == (6, 4, 0, 1, 1)
    m = {k: v["status"] for k, v in members(ctx).items()}
    assert m == {"VH1": "open", "H1": "open", "MED": "open", "DONE": "open", "ASKED": "referred"}  # PAY: funding first; LOW: not asked
    leads = {r["lead_id"]: r["status"] for r in rows(ctx.conn, "SELECT lead_id, status FROM leads")}
    assert leads["PAY"] == "open" and leads["DONE"] == "converted" and leads["VH1"] == "drive"  # funding rows untouched


def test_score_maps_trading_app_activity_and_referral_history_to_tiers(ctx):
    load_candidates(ctx, query_rows(("TOP", 160, 40, "Very High"), ("HIGH", 60, 20, "High"), ("MED", 25, 10, "Medium"),
                                    ("MEDREF", 25, 10, "Medium")))
    load_history(ctx, history_rows(("MEDREF", "F1", "2026-07-03", 15000), ("MEDREF", "F2", "2026-07-09", 30000),
                                   ("MEDREF", "F3", None, 0), ("OLDREF", "F4", "2026-06-01", 5000)))
    t = {k: (v["heat_tier"], v["heat_score"]) for k, v in members(ctx).items()}
    assert t["TOP"][0] == "hot" and t["HIGH"][0] == "warm" and t["MED"][0] == "cold"
    assert t["MEDREF"][0] == "warm" and t["MEDREF"][1] > t["MED"][1] + 15      # a proven referrer moves up a tier
    assert t["OLDREF"][0] == "cold" and "OLDREF" in t                          # past referrer joins even without trades
    h = json.loads(one(ctx.conn, "SELECT extra FROM leads WHERE lead_id='MEDREF'")["extra"])["referral_history"]
    assert (h["referred"], h["active"], h["funded"]) == (3, 2, 2)
    assert "referred 3 before (1 high, 1 medium, 1 low quality, last Jul 2026)" in members(ctx)["MEDREF"]["heat_reasons"]
    load_candidates(ctx, query_rows(("TOP", 160, 40, "Very High")))            # next pull: MED drops out, referrers stay
    status = {k: v["status"] for k, v in members(ctx).items()}
    assert status["MED"] == "out_of_window" and status["MEDREF"] == status["OLDREF"] == "open"


def test_a_drive_hands_out_asks_hottest_first_after_the_close_and_never_touches_the_funding_queue(ctx):
    ctx.cfg["team"][1]["capacity"] = 1
    load_candidates(ctx, query_rows(*[(f"T{i}", 200 - 20 * i, 40 - 3 * i, "Very High") for i in range(8)]))
    res = start_drive(ctx, per_gm=5)
    assert res["assigned"] == 5
    assert [c["lead_id"] for c in my_calls(ctx, "gm")] == ["T0", "T1", "T2", "T3", "T4"]
    assert nxt(ctx, "T0") == datetime(2026, 10, 6, 15, 45, tzinfo=TZ).isoformat()   # after the market close
    assert queue_for(ctx, "gm") == [] and rows(ctx.conn, "SELECT * FROM followups") == []
    client(ctx, "NEW", "hot")
    assert assign_new_leads(ctx)["assigned"] == 1                              # doesn't use funding capacity
    ctx._now = datetime(2026, 10, 7, 12, 0, tzinfo=TZ)                         # a day late: still no warnings
    check_overdue(ctx)                                                         # NEW (funding) is overdue; referral asks never are
    assert {a["lead_id"] for a in rows(ctx.conn, "SELECT lead_id FROM alerts")} == {"NEW"}
    assert end_drive(ctx) == 5 and start_drive(ctx, per_gm=10)["assigned"] == 8


def test_outcomes_referral_given_saves_friends_and_no_is_final(ctx):
    load_candidates(ctx, query_rows(("A", 150, 30, "Very High"), ("B", 140, 30, "Very High"), ("C", 130, 30, "Very High")))
    start_drive(ctx, per_gm=5, start_now=True)
    log_call(ctx, "A", "gm", "referral_given", "2 friends")
    assert record_referrals(ctx, "A", "gm", "Rahul Sharma, 98765 43210\n+91-9123456789 Priya\n\nAmit (will call)") == 3
    friends = rows(ctx.conn, "SELECT name, phone FROM referrals ORDER BY id")
    assert [(f["name"], f["phone"]) for f in friends] == [("Rahul Sharma", "9876543210"), ("Priya", "9123456789"),
                                                          ("Amit (will call", None)]
    a = members(ctx)["A"]
    assert a["status"] == "referred" and a["asked_at"]
    log_call(ctx, "B", "gm", "not_interested")                                 # no revisit for a referral ask
    assert members(ctx)["B"]["status"] == "not_interested" and nxt(ctx, "B") is None
    log_call(ctx, "C", "gm", "not_picked")
    assert nxt(ctx, "C") == (TUE_1100 + timedelta(hours=4)).isoformat()        # retry time, no follow-up
    for i in range(1, 3):
        ctx._now = TUE_1100 + timedelta(hours=5 * i)
        log_call(ctx, "C", "gm", "not_picked")
    assert members(ctx)["C"]["status"] == "unreachable"
    s = summary(ctx, TUE_1100 - timedelta(days=1))
    assert (s["clients_called"], s["reached"], s["gave"], s["friends"]) == (3, 2, 1, 3)
    from wepipeline.reports import compliance

    assert compliance(ctx, TUE_1100.date())[0]["calls"] == 0                   # funding compliance is untouched


def test_referral_pitch_thanks_and_asks_without_a_funding_push_or_promises(ctx):
    from wepipeline.insights import customer_insight

    ctx.cfg["referral"].update(reward_referrer=750, reward_friend=750)         # set by the admin (750 is an example)
    load_candidates(ctx, query_rows(("A", 150, 30, "Very High")))
    ins = customer_insight(ctx, card(ctx, "A"))
    assert ins["stage"] == "referral" and "150 trades" in ins["levers"]
    text = " ".join([ins["call_ideas_text"], ins["closing_text"], ins["objections_hi"], ins["next_action"]])
    assert "shukriya" in text.lower() and "permission" in text.lower()
    assert "aapko ₹750 aur aapke dost ko ₹750" in text.lower() and "pehla trade" in text and "T&C" in text
    assert "30,000" not in text and "UPI" not in text and "guarantee" not in text.lower().replace("guaranteed nahi", "")


def test_no_reward_amount_is_quoted_until_the_admin_sets_one(ctx):
    from wepipeline.insights import customer_insight

    load_candidates(ctx, query_rows(("A", 150, 30, "Very High")))
    ins = customer_insight(ctx, card(ctx, "A"))
    assert "₹" not in ins["closing_text"] + ins["objections_hi"] and "don't quote or promise an amount" in ins["ideas"]


def test_app_heat_refresh_keeps_the_referral_score(ctx):
    from wepipeline.heat import refresh_heat

    load_candidates(ctx, query_rows(("TOP", 160, 40, "Very High")))
    before = members(ctx)["TOP"]["heat_score"]
    refresh_heat(ctx)
    assert members(ctx)["TOP"]["heat_score"] == before


def test_campaign_engagement_scores_clients_on_its_own_and_reuploads_dont_double_count(ctx):
    from wepipeline.referral import campaign_report, engagement
    from wepipeline.insights import customer_insight

    long = pd.DataFrame([
        {"ClientCode": "SHARER", "Event": "Referral link shared", "Count": 1, "Date": "2026-10-06"},
        {"ClientCode": "SHARER", "Event": "Clicked", "Count": 2, "Date": "2026-10-06"},
        {"ClientCode": "REPLIER", "Event": "Replied", "Count": 1, "Date": "2026-10-06"},
        {"ClientCode": "REPLIER", "Event": "Clicked", "Count": 1, "Date": "2026-10-06"},
        {"ClientCode": "OPENER", "Event": "Opened", "Count": 1, "Date": "2026-09-20"},
        {"ClientCode": "QUIT", "Event": "Clicked", "Count": 1, "Date": "2026-10-05"},
        {"ClientCode": "QUIT", "Event": "Unsubscribed", "Count": 1, "Date": "2026-10-06"},
    ])
    res = load_engagement(ctx, long, campaign="Diwali refer & earn", channel="WhatsApp", source="wa.xlsx")
    assert (res["engaged"], res["added"], res["opted_out"]) == (4, 4, 1)
    t = {k: v["heat_tier"] for k, v in members(ctx).items()}
    assert t == {"SHARER": "hot", "REPLIER": "warm", "OPENER": "cold", "QUIT": "cold"}  # engagement alone sets the tier
    before = engagement(ctx, "SHARER")[0]
    load_engagement(ctx, long, campaign="Diwali refer & earn", channel="WhatsApp", source="wa_again.xlsx")
    assert engagement(ctx, "SHARER")[0] == before                              # same file twice: no double count
    wide = pd.DataFrame([{"Client Code": "OPENER", "Opened": 3, "Clicked": 2, "Replied": 1}])
    load_engagement(ctx, wide, campaign="Refer SMS", channel="SMS")             # wide form, another channel
    assert members(ctx)["OPENER"]["heat_tier"] in {"warm", "hot"}
    rep = {(c["campaign"], c["channel"]): c for c in campaign_report(ctx)}
    assert rep[("Diwali refer & earn", "WhatsApp")]["users"] == 4 and rep[("Refer SMS", "SMS")]["users"] == 1
    ins = customer_insight(ctx, card(ctx, "SHARER"))
    assert "Referral campaign pe response" in ins["call_ideas_text"] and "WhatsApp" in ins["call_ideas_text"]
    load_candidates(ctx, query_rows(("TOP", 160, 40, "Very High")))            # a trader pull doesn't drop engaged clients
    assert members(ctx)["REPLIER"]["status"] == "open"


def test_engagement_adds_to_a_trader_score(ctx):
    load_candidates(ctx, query_rows(("MID", 60, 20, "High")))
    base = members(ctx)["MID"]["heat_score"]
    load_engagement(ctx, pd.DataFrame([{"ClientCode": "MID", "Event": "Clicked", "Date": "2026-10-06"}]), channel="Email")
    assert members(ctx)["MID"]["heat_score"] > base


def test_periodic_refresh_pulls_history_then_traders_only_when_stale(ctx, monkeypatch):
    from wepipeline import referral

    pulls = []
    monkeypatch.setattr(referral, "connection_status", lambda c: {"ok": True, "detail": ""})
    monkeypatch.setattr(referral, "pull_history", lambda c: pulls.append("history") or history_rows(("REF", "F1", "2026-07-03", 9000)))
    monkeypatch.setattr(referral, "pull", lambda c: pulls.append("traders") or query_rows(("TOP", 160, 40, "Very High")))
    monkeypatch.setattr(referral, "pull_base", lambda c: pulls.append("base") or pd.DataFrame(columns=["CLIENT_ID"]))
    monkeypatch.setattr(referral, "pull_profiles", lambda c, ids: pd.DataFrame(columns=["CLIENT_ID"]))
    assert referral.refresh(ctx)["status"] == "ok" and pulls == ["history", "base", "traders"]
    assert referral.refresh(ctx)["status"] == "fresh" and len(pulls) == 3       # within refresh_hours: nothing pulled
    ctx._now = TUE_1100 + timedelta(hours=25)
    assert referral.refresh(ctx)["status"] == "ok" and len(pulls) == 6
    assert set(members(ctx)) == {"REF", "TOP"}
    monkeypatch.setattr(referral, "connection_status", lambda c: {"ok": False, "detail": "off the office network"})
    assert referral.refresh(ctx, force=True) == {"status": "unreachable", "detail": "off the office network"}


def test_more_referrals_and_more_trading_referrals_mean_a_higher_score(ctx):
    load_history(ctx, history_rows(("ONE", "A1", None, 0),
                                   *[("FIVE", f"B{i}", None, 0) for i in range(5)],
                                   *[("FIVETRADED", f"C{i}", "2026-07-05", 10000) for i in range(5)]))
    sc = {k: v["heat_score"] for k, v in members(ctx).items()}
    assert sc["FIVETRADED"] > sc["FIVE"] > sc["ONE"] > 0


def test_a_proven_referrer_is_worth_calling_even_without_trading_and_old_referrals_count_less(ctx):
    recent = [("STAR", f"S{i}", "2026-09-10", 10000) for i in range(6)]                 # 6 referred, all traded, last month
    old = [("PAST", f"P{i}", "2023-05-01", 10000) for i in range(6)]                    # same, but 3+ years ago
    one_ref = [("ONCE", "O1", None, 0)]                                                 # one referral that never traded
    h = history_rows(*recent, *old, *one_ref)
    h.loc[h["refree"] == "PAST", "AuthorizeDate"] = "2023-04-20"
    h.loc[h["refree"] == "STAR", "AuthorizeDate"] = "2026-09-05"
    load_history(ctx, h)
    t = {k: (v["heat_tier"], v["heat_score"]) for k, v in members(ctx).items()}
    assert t["STAR"][0] == "hot" and t["PAST"][0] == "warm" and t["ONCE"][0] == "cold"
    assert t["STAR"][1] > t["PAST"][1] > t["ONCE"][1]


def test_campaign_report_counts_asks_calls_and_credits_successful_referrals_from_the_database(ctx):
    from wepipeline import referral

    h0 = history_rows(("A", "OLD1", "2026-07-03", 9000))
    h0["AuthorizeDate"] = "2025-11-01"                                              # referred a year ago: not rested
    load_history(ctx, h0)                                                           # history before the campaign
    load_candidates(ctx, query_rows(("A", 150, 30, "Very High"), ("B", 140, 30, "Very High"), ("C", 130, 30, "Very High")))
    camp = referral.start_campaign(ctx, "Refer & earn Oct", by="sm")
    start_drive(ctx, per_gm=5, start_now=True)
    log_call(ctx, "A", "gm", "referral_given", "friend Ravi")
    record_referrals(ctx, "A", "gm", "Ravi, 9876500001")
    log_call(ctx, "B", "gm", "not_picked")
    ctx._now = TUE_1100 + timedelta(days=3)
    later = history_rows(("A", "OLD1", "2026-07-03", 9000), ("A", "RAVI1", "2026-10-09", 30000),   # Ravi joined and traded
                         ("C", "NEWC", None, 5000), ("Z", "ZZ", None, 0))                       # C wasn't called; Z not in drive
    later.loc[later["Client_Id"] == "OLD1", "AuthorizeDate"] = "2025-11-01"
    later.loc[later["Client_Id"] == "RAVI1", "AuthorizeDate"] = "2026-10-08 12:00"
    later.loc[later["Client_Id"] == "NEWC", "AuthorizeDate"] = "2026-10-08 13:00"
    later.loc[later["Client_Id"] == "ZZ", "AuthorizeDate"] = "2026-10-07 09:00"
    load_history(ctx, later)
    r = referral.report(ctx, camp["id"])
    t = r["totals"]
    assert (t["assigned"], t["called"], t["reached"], t["gave"], t["friends"]) == (3, 2, 1, 1, 1)
    assert (t["wins"], t["wins_credited"], t["funded"], t["traded"]) == (3, 1, 2, 1)   # OLD1 is before the campaign
    assert r["by_gm"]["gm"]["wins"] == 1 and r["by_gm"]["gm"]["traded"] == 1
    credit = {w["referred_id"]: w["gm"] for w in r["wins"]}
    assert credit == {"RAVI1": "gm", "NEWC": None, "ZZ": None}                      # C was never called: no credit
    # starting fresh: uncalled C goes back to the pool; the old campaign keeps its numbers
    new = referral.start_campaign(ctx, "Diwali drive")
    assert new["returned_to_pool"] == 1 and members(ctx)["C"]["assigned_to"] is None
    assert referral.report(ctx, camp["id"])["totals"]["returned"] == 1
    assert referral.report(ctx, new["id"])["totals"]["assigned"] == 0
    assert [c["name"] for c in referral.campaigns(ctx)] == ["Diwali drive", "Refer & earn Oct"]


def test_a_client_can_be_on_the_referral_and_sarthi_lists_at_once_but_is_asked_in_one_drive_at_a_time(ctx):
    from wepipeline import activation as act
    from tests.test_activation import pool_rows

    load_candidates(ctx, query_rows(("BOTH", 160, 40, "Very High")))
    act.load_traders(ctx, pool_rows(("BOTH", 160, 40, "Very High", None, 0)))
    assert drives.member(ctx, "referral", "BOTH")["status"] == drives.member(ctx, "sarthi", "BOTH")["status"] == "open"
    res = start_drive(ctx, per_gm=5, start_now=True)
    assert res["assigned"] == 0 and res["held"] == {"waiting for Sarthi activation": 1}   # Sarthi first
    assert act.start_drive(ctx, per_gm=5, start_now=True)["assigned"] == 1
    act.log_call(ctx, "BOTH", "gm", "activated")
    assert start_drive(ctx, per_gm=5, start_now=True)["held"] == {"Sarthi just switched on: ask a week later": 1}
    ctx._now = TUE_1100 + timedelta(days=8)
    assert start_drive(ctx, per_gm=5, start_now=True)["assigned"] == 1          # a week later: the referral ask


def test_pools_kept_on_the_client_row_move_to_memberships(ctx):
    client(ctx, "OLDPOOL", "warm", campaign="referral", extra={"referral": {"total_trades": 80, "next_at": "2026-10-06T15:45:00+05:30"}})
    ctx.conn.execute("UPDATE leads SET assigned_to='gm', source='Referral drive' WHERE lead_id='OLDPOOL'")
    ctx.conn.commit()
    assert drives.migrate_legacy(ctx) == 1 and drives.migrate_legacy(ctx) == 0
    m = drives.member(ctx, "referral", "OLDPOOL")
    assert (m["status"], m["assigned_to"], m["heat_tier"], m["next_at"]) == ("open", "gm", "warm", "2026-10-06T15:45:00+05:30")
    lead = one(ctx.conn, "SELECT status, campaign, assigned_to FROM leads WHERE lead_id='OLDPOOL'")
    assert (lead["status"], lead["campaign"], lead["assigned_to"]) == ("drive", "", None)


def profile_rows(*recs):
    """Rows shaped like the client-profile query: (id, registered, ftd, ltd, sarthi flag, first deposit, holdings)."""
    return pd.DataFrame([{"CLIENT_ID": c, "REGISTRATION_DATE": reg, "IsSarthiTIOn": sar, "IsAccountClosed": 0, "FirstDeposit": dep,
                          "FTD": ftd, "LTD": ltd, "Holdings": hold} for c, reg, ftd, ltd, sar, dep, hold in recs])


def test_one_primary_cohort_per_client_with_the_rest_as_tags(ctx):
    from wepipeline import referral

    # proven referrers: one active, one dormant; a bulk referrer whose referrals never traded; a recent referrer
    h = history_rows(("PROVEN", "F1", "2026-01-05", 20000), ("SLEEPY", "F2", "2025-03-01", 10000),
                     *[("BULK", f"B{i}", None, 0) for i in range(6)], ("RECENT", "F3", "2026-09-01", 5000),
                     ("FRESHREF", "F4", None, 0))
    h.loc[h["refree"].isin(["PROVEN", "SLEEPY", "BULK"]), "AuthorizeDate"] = "2025-06-01"
    h.loc[h["refree"] == "RECENT", "AuthorizeDate"] = "2026-09-20"       # 16 days ago: rested (30-day rest)
    h.loc[h["refree"] == "FRESHREF", "AuthorizeDate"] = "2026-08-01"     # 66 days ago, friend not trading yet: C1c
    load_history(ctx, h)
    load_candidates(ctx, query_rows(("HEAVY", 60, 20, "High")))                     # 60 trades, Sarthi off
    referral.load_profiles(ctx, profile_rows(
        ("PROVEN", "2023-01-01", "2023-02-01", "2026-10-01", "1", 50000, 200000),     # active, Sarthi on, 30k+
        ("SLEEPY", "2022-01-01", "2022-02-01", "2025-01-01", None, 5000, 0),          # last trade 2025: dormant
        ("NEWBIE", "2026-07-01", "2026-07-10", "2026-10-02", "1", 10000, 0),          # joined 3 months ago, Sarthi on
        ("SARTHI", "2024-01-01", "2024-02-01", "2026-09-20", "1", 8000, 0),
        ("HEAVY", "2023-05-01", "2023-06-01", "2026-10-05", None, 40000, 0),
        ("LIGHT", "2023-05-01", "2023-06-01", "2026-08-01", None, 3000, 0),
        ("DORMANT", "2022-01-01", "2022-02-01", "2025-01-01", None, 3000, 0),         # never referred, dormant: not added
        ("BULK", "2022-01-01", "2022-02-01", "2026-10-01", None, 3000, 0),
        ("RECENT", "2022-01-01", "2022-02-01", "2026-10-01", None, 3000, 0),
        ("FRESHREF", "2022-01-01", "2022-02-01", "2026-10-01", None, 3000, 0)))
    m = {k: v["segment"] for k, v in members(ctx).items()}
    assert m == {"PROVEN": "C1a", "SLEEPY": "C1b", "FRESHREF": "C1c", "NEWBIE": "C2", "SARTHI": "C3", "HEAVY": "C4", "LIGHT": "C5",
                 "BULK": "excluded", "RECENT": "rested"}
    assert set(referral.tags(ctx, card(ctx, "PROVEN"))) == {"proven", "sarthi", "funded30"}
    assert "dormant" in referral.tags(ctx, card(ctx, "SLEEPY"))
    res = start_drive(ctx, per_gm=10, start_now=True)
    order = [c["lead_id"] for c in sorted(my_calls(ctx, "gm"), key=lambda c: c["next_at"])]
    assert order == ["PROVEN", "SLEEPY", "FRESHREF", "NEWBIE", "SARTHI", "HEAVY", "LIGHT"]      # cohort order; rested / excluded never
    end_drive(ctx)
    assert start_drive(ctx, per_gm=10, start_now=True, cohorts=["C1a", "C3"])["assigned"] == 2
    summary_ = {r["cohort"]: r for r in referral.cohort_summary(ctx)}
    assert summary_["C1a"]["with_gms"] == 1 and summary_["C2"]["in_pool"] == 1 and summary_["rested"]["in_pool"] == 1


def test_the_call_is_personalised_by_cohort_and_tags(ctx):
    from wepipeline import referral
    from wepipeline.insights import customer_insight

    h = history_rows(("SLEEPY", "F2", "2025-03-01", 10000))
    h["AuthorizeDate"] = "2025-06-01"
    load_history(ctx, h)
    referral.load_profiles(ctx, profile_rows(("SLEEPY", "2022-01-01", "2022-02-01", "2025-01-01", "1", 5000, 0),
                                             ("NEWBIE", "2026-07-01", "2026-07-10", "2026-10-02", None, 10000, 0)))
    sleepy = customer_insight(ctx, card(ctx, "SLEEPY"))
    text = sleepy["call_ideas_text"] + " " + sleepy["closing_text"]
    assert "refer kiye" in text and "trade" in text.lower() and "wapas" in text.lower()      # thank, reconnect first
    newbie = customer_insight(ctx, card(ctx, "NEWBIE"))
    assert "experience" in newbie["call_ideas_text"].lower()                                 # onboarding check-in first


def test_clients_with_an_rm_go_to_the_business_team_list_and_never_to_gm_drives(ctx):
    from wepipeline import referral

    df = profile_rows(("WITHRM", "2023-01-01", "2023-02-01", "2026-10-01", "1", 50000, 0),
                      ("NORM", "2023-01-01", "2023-02-01", "2026-10-01", "1", 50000, 0))
    df["Dealer_Id"] = [4410, 0]
    df["RM_Name"] = ["Anil K.", None]
    df["RM_Branch"] = ["Indore", None]
    referral.load_profiles(ctx, df)
    assert referral.owner(card(ctx, "WITHRM")) == "business" and referral.owner(card(ctx, "NORM")) == "growth"
    res = start_drive(ctx, per_gm=10, start_now=True)
    assert res["assigned"] == 1 and res["held"] == {"has an RM: on the Business team list": 1}
    assert [c["lead_id"] for c in my_calls(ctx, "gm")] == ["NORM"]
    biz = referral.business_list(ctx)
    assert [c["lead_id"] for c in biz] == ["WITHRM"] and biz[0]["snapshot"]["rm_name"] == "Anil K."
    s_ = {r["cohort"]: r for r in referral.cohort_summary(ctx)}
    assert (s_["C3"]["business"], s_["C3"]["growth"]) == (1, 1)


def test_referrers_are_scored_on_the_quality_of_the_clients_they_referred(ctx):
    from wepipeline import referral

    h = history_rows(*[("TWOHIGH", f"H{i}", "2026-07-01", 40000) for i in range(2)],           # 2 friends deposited 40k
                     *[("TENLOW", f"L{i}", None, 0) for i in range(4)],                         # 4 friends never traded
                     *[("THREEMED", f"M{i}", "2026-05-01", 5000) for i in range(3)])            # 3 traded, small deposits
    h["AuthorizeDate"] = "2026-06-01"
    h["LTD"] = None
    load_history(ctx, h)
    hist = {k: json.loads(one(ctx.conn, "SELECT extra FROM leads WHERE lead_id=?", (k,))["extra"])["referral_history"]
            for k in ("TWOHIGH", "TENLOW", "THREEMED")}
    assert (hist["TWOHIGH"]["high"], hist["THREEMED"]["medium"], hist["TENLOW"]["low"]) == (2, 3, 4)
    q = {k: referral.referral_quality_score(ctx, v) for k, v in hist.items()}
    assert q["TWOHIGH"] > q["THREEMED"] > q["TENLOW"] and q["TENLOW"] < 10                      # quality beats volume
    assert referral.referred_quality(ctx, 0, 0, "2025-01-01", TUE_1100 - timedelta(days=5)) == "high"   # still trading


def test_centralised_filter_narrows_the_cohort_table_and_business_list(ctx):
    from wepipeline import referral

    df = profile_rows(("CENT", "2023-01-01", "2023-02-01", "2026-10-01", "1", 50000, 0),
                      ("BRANCH", "2023-01-01", "2023-02-01", "2026-10-01", "1", 50000, 0))
    df["Dealer_Id"] = [4410, 4410]
    df["IsCentralized"] = [1, 0]
    referral.load_profiles(ctx, df)
    assert referral.centralized(card(ctx, "CENT")) and not referral.centralized(card(ctx, "BRANCH"))
    count = lambda scope: sum(r["business"] for r in referral.cohort_summary(ctx, scope))  # noqa: E731
    assert (count("all"), count("central"), count("non_central")) == (2, 1, 1)
    assert [c["lead_id"] for c in referral.business_list(ctx, scope="central")] == ["CENT"]
    assert referral.snapshot(ctx, card(ctx, "CENT"))["centralized"] is True


def test_a_centralised_scope_keeps_other_clients_out_of_gm_drives(ctx):
    from wepipeline import referral

    ctx.cfg["referral"]["scope"] = "central"
    df = profile_rows(("CENT", "2023-01-01", "2023-02-01", "2026-10-01", "1", 50000, 0),
                      ("BRANCH", "2023-01-01", "2023-02-01", "2026-10-01", "1", 50000, 0))
    df["Dealer_Id"] = [0, 0]
    df["IsCentralized"] = [1, 0]
    referral.load_profiles(ctx, df)
    res = start_drive(ctx, per_gm=10, start_now=True)
    assert res["assigned"] == 1 and res["held"] == {"outside the drive's scope (centralised only)": 1}
    assert [c["lead_id"] for c in my_calls(ctx, "gm")] == ["CENT"]
