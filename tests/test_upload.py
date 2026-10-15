from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from tests.helpers import open_test_ctx
from wepipeline.context import load_config
from wepipeline.db import one, rows
from wepipeline.upload import analyse_upload, assign_upload, batch_rows, batch_workbook, held_count, list_uploads

TZ = ZoneInfo("Asia/Kolkata")
NOW = datetime(2026, 10, 8, 11, 0, tzinfo=TZ)  # Thursday


@pytest.fixture
def ctx(tmp_path):
    cfg = load_config()
    cfg["database"] = str(tmp_path / "test.db")
    cfg["notifications"]["outbox_dir"] = str(tmp_path / "outbox")
    cfg["reports_dir"] = str(tmp_path / "reports")
    cfg["sources"]["leads"]["path"] = str(tmp_path / "no_sheet.csv")
    cfg["sources"]["webengage"]["folder"] = str(tmp_path / "we")
    c = open_test_ctx(cfg)
    c._now = NOW
    return c


def pivot(event, counts):
    hours = ["7 Oct, 6PM", "8 Oct, 9AM", "8 Oct, 10AM"]
    return pd.DataFrame([{"Events": event, "ClientCode": cid, "Sum": sum(h.values()), **{x: h.get(x, 0) for x in hours}}
                         for cid, h in counts.items()])


def test_uploaded_pivot_is_scored_ranked_assigned_and_pitched(ctx, tmp_path):
    # one client is already known before the upload
    ctx.conn.execute("INSERT INTO leads (lead_id, we_user_id, status, category, source, extra) VALUES "
                     "('OLD1','OLD1','open','x','WebEngage activity (earlier.xlsx)','{}')")
    path = tmp_path / "SM_upload_list.xlsx"
    with pd.ExcelWriter(path) as xl:
        pivot("ORDER_PLACED_MOB", {"HOTC": {"8 Oct, 10AM": 4}}).to_excel(xl, sheet_name="ORDER_PLACED_MOB", index=False)
        pivot("CALL_ACCEPTED_MOB", {"HOTC": {"8 Oct, 9AM": 5}, "OLD1": {"7 Oct, 6PM": 1}}).to_excel(xl, sheet_name="CALL_ACCEPTED_MOB", index=False)
        pivot("DASHBOARD_OPENED_MOB", {"COLDC": {"7 Oct, 6PM": 1}, "Others": {"8 Oct, 9AM": 900}}).to_excel(
            xl, sheet_name="DASHBOARD_OPENED_MOB", index=False)
    s = analyse_upload(ctx, path, uploaded_by="sm_01", list_type="no_deposit")
    assert (s["clients"], s["new_leads"], s["already_known"]) == (3, 2, 1)  # "Others" ignored
    assert one(ctx.conn, "SELECT category FROM leads WHERE lead_id='HOTC'")["category"] == "Not yet matched in client DB"
    assert s["ageing"]["status"] == "skipped"
    assert s["assigned"] == 0 and s["on_hold"] == 3 and s["waiting"] == 0  # nobody goes to a GM until the SM presses Assign
    assert one(ctx.conn, "SELECT COUNT(*) n FROM leads WHERE lead_id='Others'")["n"] == 0
    up = list_uploads(ctx)[0]
    assert batch_rows(ctx, up["client_ids"])[0]["gm"] == "Waiting for Assign (SM)"
    from wepipeline import jobs
    assert jobs.sync(ctx)["assignment"]["assigned"] == 0  # the hourly sync leaves a held list alone
    assert held_count(ctx, up["client_ids"]) == 3

    res = assign_upload(ctx, up, assigned_by="sm_01")
    assert (res["released"], res["assigned"]) == (3, 3) and held_count(ctx, up["client_ids"]) == 0
    assert list_uploads(ctx)[0]["summary"]["assigned_by"] == "sm_01"
    data = batch_rows(ctx, up["client_ids"])
    assert [r["client"] for r in data][0] == "HOTC" and data[0]["batch_rank"] == 1
    assert data[0]["priority"] == "🔥 Hot" and data[0]["gm"].startswith("GM")
    assert data[0]["opener"].startswith("I noticed you tried to place an order")
    assert "Funding blocker" in data[0]["levers"]
    first_call = one(ctx.conn, "SELECT due_at FROM followups WHERE lead_id='HOTC'")["due_at"]
    assert first_call.startswith("2026-10-08T11:30")  # hot: first call within 30 minutes


def test_plain_list_of_client_codes_becomes_leads(ctx, tmp_path):
    path = tmp_path / "codes.csv"
    pd.DataFrame({"ClientCode": ["A1", "A2", "Others", ""], "Remarks": ["x", "", "", ""]}).to_csv(path, index=False)
    s = analyse_upload(ctx, path, uploaded_by="sm_02")
    assert (s["clients"], s["new_leads"]) == (2, 2)
    assert {r["lead_id"] for r in rows(ctx.conn, "SELECT lead_id FROM leads")} == {"A1", "A2"}


def test_file_without_client_codes_is_rejected(ctx, tmp_path):
    path = tmp_path / "bad.csv"
    pd.DataFrame({"Name": ["x"], "City": ["Indore"]}).to_csv(path, index=False)
    assert "No client codes found" in analyse_upload(ctx, path)["error"]


def test_analysed_workbook_has_list_and_summary(ctx, tmp_path):
    path = tmp_path / "codes.csv"
    pd.DataFrame({"ClientCode": ["A1", "A2"]}).to_csv(path, index=False)
    analyse_upload(ctx, path, uploaded_by="sm_01")
    wb = batch_workbook(ctx, list_uploads(ctx)[0])
    assert wb.sheetnames == ["Summary", "Analysed list", "Activity timeline", "Details"]
    ws = wb["Analysed list"]
    assert [c.value for c in ws[1]] == ["Rank", "Priority", "Client", "Client profile", "App activity", "Engagement levers",
                                        "Insight", "Call script", "Ask (call to action)", "Agar customer bole",
                                        "When to call", "GM", "Status"]
    assert ws.max_row == 3 and wb["Details"]["A1"].value == "Rank in this list"
    row = {c.value: v.value for c, v in zip(ws[1], ws[2])}
    assert row["GM"] is None and row["Status"] == "open: waiting for Assign (SM)"  # GM stays empty until assigned
    assert wb["Summary"]["B5"].value.startswith("=COUNTIFS(")


def test_reactivation_list_only_engaged_customers_go_to_gms(ctx, tmp_path):
    from wepipeline import jobs
    from wepipeline.sheet_export import build_rows

    # an existing customer (already assigned) who also appears on the re-activation list
    ctx.conn.execute("INSERT INTO leads (lead_id, we_user_id, status, category, source, extra, assigned_to) VALUES "
                     "('KNOWN','KNOWN','open','Traded - dormant (30+ days)','Client DB: registered, no deposit','{}','gm_02')")
    path = tmp_path / "reactivation_oct.xlsx"
    with pd.ExcelWriter(path) as xl:
        pivot("CALL_ACCEPTED_MOB", {"ENGAGED": {"8 Oct, 10AM": 5}}).to_excel(xl, sheet_name="CALL_ACCEPTED_MOB", index=False)
        pivot("ORDER_PLACED_MOB", {"ENGAGED": {"8 Oct, 9AM": 3}}).to_excel(xl, sheet_name="ORDER_PLACED_MOB", index=False)
        pivot("DASHBOARD_OPENED_MOB", {"QUIET": {"7 Oct, 6PM": 1}, "KNOWN": {"7 Oct, 6PM": 1}}).to_excel(
            xl, sheet_name="DASHBOARD_OPENED_MOB", index=False)
    s = analyse_upload(ctx, path, uploaded_by="sm_01", list_type="reactivation")
    assert one(ctx.conn, "SELECT assigned_to FROM leads WHERE lead_id='KNOWN'")["assigned_to"] is None  # released before Assign
    res = assign_upload(ctx, list_uploads(ctx)[0])
    tags = {r["lead_id"]: (r["campaign"], r["assigned_to"], r["heat_tier"])
            for r in rows(ctx.conn, "SELECT lead_id, campaign, assigned_to, heat_tier FROM leads")}
    assert {k: v[0] for k, v in tags.items()} == {"ENGAGED": "reactivation", "QUIET": "reactivation", "KNOWN": "reactivation"}
    assert tags["ENGAGED"][1] is not None and tags["ENGAGED"][2] in ("hot", "warm")   # engaged: straight to a GM
    assert tags["QUIET"][1] is None                                                    # not engaged: watching
    assert tags["KNOWN"][1] is None             # cold and never worked by its GM: released back to watching
    assert (res["watching_reactivation"], res["unassigned"], s["released_to_watching"]) == (2, 0, 1)
    rows_ = {r["client"]: r for r in build_rows(ctx)}
    assert rows_["QUIET"]["gm"] == "Waiting for engagement (re-activation)" and rows_["QUIET"]["list"] == "Churned, active again"
    assert "Re-activation list: engaged in the app again" in rows_["ENGAGED"]["levers"]

    # QUIET starts using the app: on the next sync it is scored up and handed to a GM
    for name, n in [("CALL_ACCEPTED_MOB", 6), ("ORDER_PLACED_MOB", 4)]:
        ctx.conn.execute("INSERT INTO events (user_id, event_name, event_time, attrs) VALUES (?,?,?,?)",
                         ("QUIET", name, NOW.isoformat(), f'{{"count": {n}}}'))
    ctx.conn.commit()
    res = jobs.sync(ctx)
    assert res["assignment"]["watching_reactivation"] == 1  # KNOWN is still not engaged
    assert one(ctx.conn, "SELECT assigned_to FROM leads WHERE lead_id='QUIET'")["assigned_to"] is not None


def test_reactivation_threshold_is_configurable(ctx, tmp_path):
    ctx.cfg["reactivation"] = {"assign_tiers": ["hot"]}
    path = tmp_path / "r.xlsx"
    with pd.ExcelWriter(path) as xl:
        pivot("DASHBOARD_OPENED_MOB", {"WARMISH": {"8 Oct, 10AM": 12}}).to_excel(xl, sheet_name="D", index=False)
        pivot("CALL_ACCEPTED_MOB", {"WARMISH": {"8 Oct, 9AM": 1}}).to_excel(xl, sheet_name="CALL_ACCEPTED_MOB", index=False)
    analyse_upload(ctx, path, list_type="reactivation")
    lead = one(ctx.conn, "SELECT heat_tier, assigned_to FROM leads WHERE lead_id='WARMISH'")
    assert lead["heat_tier"] != "hot" and lead["assigned_to"] is None


def test_reactivation_upload_releases_unworked_cold_customers_only(ctx, tmp_path):
    from wepipeline.followups import add_followup, log_call

    for lid in ("COLD_UNWORKED", "COLD_CALLED", "COLD_PROMISED"):
        ctx.conn.execute("INSERT INTO leads (lead_id, we_user_id, status, category, source, extra, assigned_to, assigned_at) "
                         "VALUES (?,?,?,?,?,?,?,?)", (lid, lid, "open", "x", "WebEngage activity (t)", "{}", "gm_03", NOW.isoformat()))
        add_followup(ctx, lid, "gm_03", NOW, "first_call", "New lead")
    log_call(ctx, "COLD_CALLED", "gm_03", "interested", "spoke yesterday")
    add_followup(ctx, "COLD_PROMISED", "gm_03", NOW.replace(hour=16), "promised", "call at 4")
    path = tmp_path / "react.csv"
    pd.DataFrame({"ClientCode": ["COLD_UNWORKED", "COLD_CALLED", "COLD_PROMISED"]}).to_csv(path, index=False)
    s = analyse_upload(ctx, path, list_type="reactivation")
    owner = {r["lead_id"]: r["assigned_to"] for r in rows(ctx.conn, "SELECT lead_id, assigned_to FROM leads")}
    assert owner == {"COLD_UNWORKED": None, "COLD_CALLED": "gm_03", "COLD_PROMISED": "gm_03"}
    assert s["released_to_watching"] == 1 and s["on_hold"] == 1
    assert one(ctx.conn, "SELECT COUNT(*) n FROM followups WHERE lead_id='COLD_UNWORKED' AND status='pending'")["n"] == 0


def test_payout_list_is_urgent_retention_with_amounts(ctx, tmp_path):
    from wepipeline.insights import customer_insight
    from wepipeline.dialer import dialer_queue

    path = tmp_path / "payouts_08oct.csv"
    pd.DataFrame({"ClientCode": ["PAY1", "PAY2"], "Payout Amount": ["25,000", "4000"], "Payout Date": ["08-10-2026", "07-10-2026"]}).to_csv(path, index=False)
    # a hot non-payout lead already waiting, to check payouts jump ahead
    ctx.conn.execute("INSERT INTO leads (lead_id, we_user_id, status, category, source, extra, heat_tier, heat_score) VALUES "
                     "('HOTX','HOTX','open','x','WebEngage activity (t)','{}','hot',95)")
    s = analyse_upload(ctx, path, uploaded_by="sm_01", list_type="payout")
    assert (s["clients"], s["assigned"], s["on_hold"]) == (2, 0, 2)
    assert assign_upload(ctx, list_uploads(ctx)[0])["assigned"] == 2
    lead = one(ctx.conn, "SELECT * FROM leads WHERE lead_id='PAY1'")
    assert lead["campaign"] == "payout"
    fu = one(ctx.conn, "SELECT due_at, reason FROM followups WHERE lead_id='PAY1'")
    assert fu["due_at"].startswith("2026-10-08T11:30") and fu["reason"].startswith("💸 Payout requested")
    ins = customer_insight(ctx, lead)
    assert ins["stage"] == "payout"
    assert ins["opener"].startswith("I'm calling about the ₹25,000 withdrawal you requested. It's being processed as normal")
    assert ins["ideas"].split("\n")[0].startswith("• Never delay or discourage the withdrawal")
    assert "Payout requested of ₹25,000 on 08 Oct" in ins["levers"]
    assert "\"I need the money\" → respect it" in ins["objection"]
    agent = lead["assigned_to"]
    q = dialer_queue(ctx, agent)
    assert q[0]["campaign"] == "payout"  # payouts are first in the GM's line
