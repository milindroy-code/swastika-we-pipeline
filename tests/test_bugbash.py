"""Regressions from the 8 Oct bug bash: each test is a bug that was found and fixed."""
import json
from datetime import datetime, timedelta

import pandas as pd

from tests.test_followup_rules import TUE_1100, TZ, assign, client, ctx, due  # noqa: F401  (ctx is a fixture)
from wepipeline.assign import assign_new_leads
from wepipeline.context import norm_client_id
from wepipeline.db import one, rows
from wepipeline.followups import check_overdue, create_system_followups, expire_out_of_window, log_call, queue_for


def test_partial_deposit_keeps_reactivation_and_payout_lists(ctx):
    client(ctx, "R", campaign="reactivation", extra={"registration_date": "2025-01-01T10:00:00+05:30"})
    client(ctx, "P", campaign="payout", extra={"registration_date": "2025-01-01T10:00:00+05:30"})
    assign(ctx)
    log_call(ctx, "R", "gm", "converted", deposit_amount=5000)
    log_call(ctx, "P", "gm", "converted", deposit_amount=5000)
    expire_out_of_window(ctx)
    got = {r["lead_id"]: (r["status"], r["campaign"]) for r in rows(ctx.conn, "SELECT lead_id, status, campaign FROM leads")}
    assert got == {"R": ("open", "reactivation"), "P": ("open", "payout")}


def test_converted_without_an_amount_does_not_close_the_client(ctx):
    client(ctx, "C"); assign(ctx)
    log_call(ctx, "C", "gm", "converted", deposit_amount=0)
    assert one(ctx.conn, "SELECT status FROM leads")["status"] == "open" and due(ctx, "C") is not None


def test_weekend_app_activity_creates_one_follow_up_not_three(ctx):
    client(ctx, "C"); assign(ctx)
    log_call(ctx, "C", "gm", "interested", next_at=TUE_1100 + timedelta(days=10))
    sat = datetime(2026, 10, 10, 13, 0, tzinfo=TZ)
    for h in (0, 3, 7):
        ctx._now = sat + timedelta(hours=h)
        ctx.conn.execute("INSERT INTO events (user_id, event_name, event_time) VALUES ('C','Deposit Failed',?)",
                         ((ctx._now - timedelta(minutes=5)).isoformat(),))
        create_system_followups(ctx)
    assert one(ctx.conn, "SELECT COUNT(*) n FROM followups WHERE lead_id='C' AND status='pending' AND kind='system'")["n"] == 1
    assert len(queue_for(ctx, "gm")) == 1  # one card for the client, the promised call listed on it
    assert len(queue_for(ctx, "gm")[0]["also_pending"]) == 1


def test_no_escalation_after_hours_the_clock_resumes_next_morning(ctx):
    ctx._now = datetime(2026, 10, 9, 18, 0, tzinfo=TZ)  # Friday
    client(ctx, "C"); assign(ctx)
    ctx.conn.execute("UPDATE followups SET due_at=? WHERE lead_id='C'", (datetime(2026, 10, 9, 18, 20, tzinfo=TZ).isoformat(),))
    ctx._now = datetime(2026, 10, 9, 19, 30, tzinfo=TZ)
    assert check_overdue(ctx) == {"warned": 0, "escalated": 0}                  # 10 working minutes late
    ctx._now = datetime(2026, 10, 12, 10, 10, tzinfo=TZ)                        # Monday: 20 working minutes
    assert check_overdue(ctx) == {"warned": 1, "escalated": 0}
    ctx._now = datetime(2026, 10, 12, 10, 51, tzinfo=TZ)
    assert check_overdue(ctx)["escalated"] == 1


def test_holidays_are_not_working_days(ctx):
    ctx.cfg["working_hours"]["holidays"] = ["2026-10-07"]
    assert ctx.roll_to_working_hours(datetime(2026, 10, 6, 19, 0, tzinfo=TZ)) == datetime(2026, 10, 8, 10, 0, tzinfo=TZ)


def test_payout_is_assigned_even_when_every_gm_is_full(ctx):
    ctx.cfg["team"][1]["capacity"] = 1
    client(ctx, "W", "warm"); assign(ctx)
    log_call(ctx, "W", "gm", "interested")
    client(ctx, "P", campaign="payout")
    assign_new_leads(ctx)
    assert one(ctx.conn, "SELECT assigned_to FROM leads WHERE lead_id='P'")["assigned_to"] == "gm"


def test_a_double_tap_on_not_picked_is_one_attempt(ctx):
    client(ctx, "C"); assign(ctx)
    for _ in range(3):
        log_call(ctx, "C", "gm", "not_picked")
    assert one(ctx.conn, "SELECT npc_count FROM leads")["npc_count"] == 1


def test_client_codes_have_one_spelling():
    assert [norm_client_id(v) for v in (" ab12 ", 12345.0, "12345.0", None, "nan")] == ["AB12", "12345", "12345", "", ""]


def test_segment_names_from_different_exports_are_one_activity(ctx):
    from wepipeline.ingest import event_key, merge_event_aliases

    assert event_key("RECOMMENDATION_LISTING_OPENED_MOB", "fando_research") == event_key("RECOMMENDATION_LISTING_OPENED_MOB", "fandoresearch")
    assert event_key("RECOMMENDATION_LANDING_SCREEN_MOB", "master research") == "RECOMMENDATION_LANDING_SCREEN_MOB"
    for name in ("RECOMMENDATION_LISTING_OPENED_MOB · fando_research", "RECOMMENDATION_LISTING_OPENED_MOB · fandoresearch"):
        ctx.conn.execute("INSERT INTO events (user_id, event_name, event_time, attrs) VALUES ('X',?,?,?)",
                         (name, "2026-10-05T11:00:00+05:30", '{"count": 3}'))
    merge_event_aliases(ctx)
    assert [r["event_name"] for r in rows(ctx.conn, "SELECT event_name FROM events")] == ["RECOMMENDATION_LISTING_OPENED_MOB · F&O"]


def test_compliance_ignores_follow_ups_done_before_they_were_due(ctx):
    from wepipeline.reports import compliance

    client(ctx, "A"); client(ctx, "B"); assign(ctx)
    ctx.conn.execute("UPDATE followups SET due_at=? WHERE lead_id='A'", ((TUE_1100 - timedelta(hours=1)).isoformat(),))
    ctx.conn.execute("UPDATE followups SET due_at=? WHERE lead_id='B'", ((TUE_1100 + timedelta(hours=6)).isoformat(),))
    log_call(ctx, "B", "gm", "interested")                                      # B done early
    row = compliance(ctx, ctx.today())[0]
    assert (row["due"], row["on_time"], row["compliance_pct"]) == (1, 0, 0.0)


def test_excel_report_tables_do_not_overwrite_the_headline_numbers(ctx):
    from wepipeline.excel_report import build_workbook

    client(ctx, "C"); assign(ctx)
    log_call(ctx, "C", "gm", "converted", deposit_amount=30000)
    ws = build_workbook(ctx, ctx.today(), ctx.today())["Summary"]
    labels = [ws.cell(row=r, column=1).value for r in range(5, 20)]
    assert labels[-1] == "Deposits on converted calls (₹)" and str(labels[-2]).startswith("Funded")
    assert "Calls!$M$" in ws["B18"].value  # funded counts the Funded column (same rule as the EOD email)


def test_upload_never_downgrades_a_payout_and_reopens_funded_clients(ctx, tmp_path):
    from wepipeline.upload import analyse_upload

    client(ctx, "P1", campaign="payout")
    ctx.conn.execute("UPDATE leads SET status='converted' WHERE lead_id='P1'")
    ctx.conn.commit()
    f = tmp_path / "payout.xlsx"
    pd.DataFrame([{"ClientCode": "p1", "Payout amount": 5000}]).to_excel(f, index=False)
    analyse_upload(ctx, f, list_type="payout", use_client_db=False)
    assert (lambda r: (r["status"], r["campaign"]))(one(ctx.conn, "SELECT * FROM leads WHERE lead_id='P1'")) == ("open", "payout")
    g = tmp_path / "new.xlsx"
    pd.DataFrame([{"ClientCode": "P1"}]).to_excel(g, index=False)
    analyse_upload(ctx, g, list_type="no_deposit", use_client_db=False)
    assert one(ctx.conn, "SELECT campaign FROM leads WHERE lead_id='P1'")["campaign"] == "payout"


def test_enrich_applies_the_funding_target(ctx):
    from wepipeline.clientdb import enrich_leads

    for cid in ("BIG", "SMALL"):
        client(ctx, cid, campaign="new")
    d = (TUE_1100 - timedelta(days=5)).replace(tzinfo=None)
    base = {"AccountStatus": "", "IsAccountClosed": 0, "IsSarthiTIOn": None, "SarthiActivationDate": None, "PreferredSegment": "",
            "FTD": None, "LTD": None, "Holdings": None, "REGISTRATION_DATE": d, "FirstDepositDate": d}
    enrich_leads(ctx, pd.DataFrame([{**base, "CLIENT_ID": "BIG", "FirstDeposit": 50000},
                                    {**base, "CLIENT_ID": "SMALL", "FirstDeposit": 8000}]))
    got = {r["lead_id"]: (r["status"], r["campaign"], json.loads(r["extra"]).get("current_margin"))
           for r in rows(ctx.conn, "SELECT lead_id, status, campaign, extra FROM leads")}
    assert got["BIG"][0] == "converted" and got["SMALL"] == ("open", "low_margin", 8000)


def test_pitch_never_asks_a_payout_client_to_cancel_or_a_kyc_client_to_deposit(ctx):
    from wepipeline.ideas import closing

    base = {"sname": "Sarthi", "target": 30000, "unfunded": True, "sarthi_active": False, "starter_hi": "₹30,000"}
    payout = " ".join(closing({**base, "stage": "payout", "payout_amount": 5000}, {}))
    assert "cancel" not in payout.lower() and "30,000" not in payout
    kyc = " ".join(closing({**base, "stage": "kyc_pending"}, {}))
    assert "KYC" in kyc and "30,000" not in kyc


def test_admin_pin_with_unusual_characters_is_just_wrong(monkeypatch):
    from wepipeline.admin import check_pin

    monkeypatch.setenv("ADMIN_PIN", "589854")
    assert check_pin("₹589854") is False and check_pin(" 589854 ") is True
