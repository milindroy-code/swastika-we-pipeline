from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from tests.helpers import open_test_ctx
from wepipeline.context import load_config
from wepipeline.db import one, rows
from wepipeline.upload import analyse_upload, batch_rows, batch_workbook, list_uploads

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
    assert one(ctx.conn, "SELECT category FROM leads WHERE lead_id='HOTC'")["category"] == "Uploaded list - no deposit yet"
    assert s["ageing"]["status"] == "skipped"
    assert s["assigned"] == 3 and s["waiting"] == 0
    assert one(ctx.conn, "SELECT COUNT(*) n FROM leads WHERE lead_id='Others'")["n"] == 0

    data = batch_rows(ctx, list_uploads(ctx)[0]["client_ids"])
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
    assert wb.sheetnames == ["Summary", "Analysed list"]
    ws = wb["Analysed list"]
    assert ws["A1"].value == "Rank in this list" and ws.max_row == 3
    assert wb["Summary"]["B5"].value.startswith("=COUNTIFS(")
