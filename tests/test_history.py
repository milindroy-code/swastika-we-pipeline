"""Uploads on consecutive days with overlapping dates consolidate: no double counting, recency-weighted priority."""
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from tests.helpers import open_test_ctx
from wepipeline.context import load_config
from wepipeline.db import one, rows
from wepipeline.heat import contributions
from wepipeline.upload import analyse_upload, batch_rows, list_uploads

TZ = ZoneInfo("Asia/Kolkata")
WED = datetime(2026, 10, 7, 17, 0, tzinfo=TZ)
THU = datetime(2026, 10, 8, 11, 0, tzinfo=TZ)


@pytest.fixture
def ctx(tmp_path):
    cfg = load_config()
    cfg["database"] = str(tmp_path / "test.db")
    cfg["notifications"]["outbox_dir"] = str(tmp_path / "outbox")
    cfg["sources"]["webengage"]["folder"] = str(tmp_path / "we")
    c = open_test_ctx(cfg)
    return c


def workbook(path, hours, data):
    """data: {event: {client: {hour_header: count}}} -> a WebEngage pivot workbook, one sheet per event."""
    with pd.ExcelWriter(path) as xl:
        for event, clients in data.items():
            pd.DataFrame([{"Events": event, "ClientCode": cid, "Sum": sum(h.values()), **{x: h.get(x, 0) for x in hours}}
                          for cid, h in clients.items()]).to_excel(xl, sheet_name=event[:31], index=False)


def test_overlapping_uploads_consolidate_and_reprioritise(ctx, tmp_path):
    # Wednesday's export covers 5-7 Oct
    wed_hours = ["5 Oct, 11AM", "6 Oct, 11AM", "7 Oct, 11AM"]
    workbook(tmp_path / "wed.xlsx", wed_hours, {
        "CALL_ACCEPTED_MOB": {"X": {"5 Oct, 11AM": 3, "6 Oct, 11AM": 3, "7 Oct, 11AM": 3}, "Y": {"6 Oct, 11AM": 1}},
        "ORDER_PLACED_MOB": {"X": {"7 Oct, 11AM": 2}},
    })
    ctx._now = WED
    analyse_upload(ctx, tmp_path / "wed.xlsx", uploaded_by="sm_01", list_type="no_deposit")
    x_wed = one(ctx.conn, "SELECT heat_score, assigned_to FROM leads WHERE lead_id='X'")
    y_wed = one(ctx.conn, "SELECT heat_score FROM leads WHERE lead_id='Y'")["heat_score"]
    assert x_wed["heat_score"] > y_wed  # Wednesday: X is the hot one

    # Thursday's export covers 6-8 Oct: 6-7 Oct overlap (same numbers), Y very active today, Z is new
    thu_hours = ["6 Oct, 11AM", "7 Oct, 11AM", "8 Oct, 9AM", "8 Oct, 10AM"]
    workbook(tmp_path / "thu.xlsx", thu_hours, {
        "CALL_ACCEPTED_MOB": {"X": {"6 Oct, 11AM": 3, "7 Oct, 11AM": 3}, "Y": {"6 Oct, 11AM": 1, "8 Oct, 9AM": 4, "8 Oct, 10AM": 3}},
        "ORDER_PLACED_MOB": {"X": {"7 Oct, 11AM": 2}, "Y": {"8 Oct, 10AM": 3}, "Z": {"8 Oct, 10AM": 1}},
    })
    ctx._now = THU
    s = analyse_upload(ctx, tmp_path / "thu.xlsx", uploaded_by="sm_01", list_type="no_deposit")
    assert (s["clients"], s["new_leads"], s["already_known"]) == (3, 1, 2)

    # 1. no double counting: X's 6-7 Oct hours were in both files, but count once; 5 Oct (only Wednesday) is kept
    x_calls = contributions(ctx, ["X"])["CALL_ACCEPTED_MOB"]["count"]
    assert x_calls == 9  # 3 + 3 + 3, not 3 + 3 + 3 + 3 + 3
    assert one(ctx.conn, "SELECT COUNT(*) n FROM events WHERE user_id='X' AND event_name='CALL_ACCEPTED_MOB'")["n"] == 3
    # Y's history is the union of both files
    assert contributions(ctx, ["Y"])["CALL_ACCEPTED_MOB"]["count"] == 8  # 1 (6 Oct) + 4 + 3 (today)

    # 2. priority follows recency: Y (busy today) now outranks X (nothing new since yesterday)
    heat = {r["lead_id"]: r["heat_score"] for r in rows(ctx.conn, "SELECT lead_id, heat_score FROM leads")}
    assert heat["Y"] > heat["X"]
    assert heat["X"] < x_wed["heat_score"]  # X has cooled a little overnight (recency weighting)

    # 3. one open follow-up per client: re-uploading doesn't stack duplicates, and X keeps its GM
    for cid in ("X", "Y", "Z"):
        pending = rows(ctx.conn, "SELECT kind FROM followups WHERE lead_id=? AND status='pending'", (cid,))
        kinds = [p["kind"] for p in pending]
        assert kinds.count("first_call") <= 1 and len(kinds) <= 2, (cid, kinds)
    assert one(ctx.conn, "SELECT assigned_to FROM leads WHERE lead_id='X'")["assigned_to"] == x_wed["assigned_to"]

    # 4. both uploads are kept; Wednesday's results now show the consolidated picture
    ups = list_uploads(ctx)
    assert [u["filename"].split("/")[-1] for u in ups][:2] == ["thu.xlsx", "wed.xlsx"]
    wed_view = {r["client"]: r for r in batch_rows(ctx, ups[1]["client_ids"])}
    assert [r["client"] for r in batch_rows(ctx, ups[1]["client_ids"])][0] == "Y"  # re-ranked with Thursday's data
    assert "8 ideas accept kiye" in wed_view["Y"]["call_ideas_text"]


def test_corrected_numbers_in_a_later_file_replace_the_old_ones(ctx, tmp_path):
    hours = ["7 Oct, 11AM"]
    workbook(tmp_path / "a.xlsx", hours, {"CALL_ACCEPTED_MOB": {"X": {"7 Oct, 11AM": 5}}})
    ctx._now = WED
    analyse_upload(ctx, tmp_path / "a.xlsx", list_type="no_deposit")
    workbook(tmp_path / "b.xlsx", hours, {"CALL_ACCEPTED_MOB": {"X": {"7 Oct, 11AM": 2}}})  # WebEngage re-counted that hour
    analyse_upload(ctx, tmp_path / "b.xlsx", list_type="no_deposit")
    events = rows(ctx.conn, "SELECT attrs FROM events WHERE user_id='X'")
    assert len(events) == 1 and '"count": 2' in events[0]["attrs"]
