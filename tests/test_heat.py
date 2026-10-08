from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from wepipeline import jobs
from wepipeline.context import load_config, parse_dt
from tests.helpers import open_test_ctx
from wepipeline.db import one, rows
from wepipeline.followups import queue_for
from wepipeline.heat import contributions, score

TZ = ZoneInfo("Asia/Kolkata")
NOW = datetime(2026, 10, 7, 11, 0, tzinfo=TZ)  # Wednesday


@pytest.fixture
def ctx(tmp_path):
    cfg = load_config()
    cfg["database"] = str(tmp_path / "test.db")
    cfg["notifications"]["outbox_dir"] = str(tmp_path / "outbox")
    cfg["notifications"]["email"]["enabled"] = False
    cfg["notifications"]["webhook_url"] = ""
    cfg["market_notes_file"] = str(tmp_path / "none.md")
    cfg["reports_dir"] = str(tmp_path / "reports")
    cfg["sources"]["leads"]["path"] = str(tmp_path / "no_sheet.csv")
    cfg["sources"]["webengage"]["folder"] = str(tmp_path / "we")
    (tmp_path / "we").mkdir()
    c = open_test_ctx(cfg)
    c._now = NOW
    return c


def pivot(event: str, counts: dict[str, dict[str, int]]) -> pd.DataFrame:
    """WebEngage Events export: Events | ClientCode | Sum | one column per hour."""
    hours = ["5 Oct, 10AM", "6 Oct, 3PM", "7 Oct, 9AM", "7 Oct, 10AM"]
    out = []
    for client, by_hour in counts.items():
        row = {"Events": event, "ClientCode": client, "Sum": sum(by_hour.values())}
        row.update({h: by_hour.get(h, 0) for h in hours})
        out.append(row)
    return pd.DataFrame(out)


def write_workbook(ctx, sheets: dict[str, pd.DataFrame], name="user_auth_after_4octandnot_added_fund.xlsx"):
    path = ctx.path(ctx.cfg["sources"]["webengage"]["folder"]) / name
    with pd.ExcelWriter(path) as xl:
        for sheet, df in sheets.items():
            df.to_excel(xl, sheet_name=sheet, index=False)
    return path


@pytest.fixture
def loaded(ctx):
    write_workbook(ctx, {
        "ORDER_PLACED_MOB": pivot("ORDER_PLACED_MOB", {"HOT1": {"7 Oct, 10AM": 3}}),
        "CALL_ACCEPTED_MOB": pivot("CALL_ACCEPTED_MOB", {"HOT1": {"7 Oct, 9AM": 4}}),
        "DASHBOARD_OPENED_MOB": pivot("DASHBOARD_OPENED_MOB", {"HOT1": {"7 Oct, 9AM": 5}, "WARM1": {"7 Oct, 9AM": 2},
                                                               "COLD1": {"5 Oct, 10AM": 1}}),
        "equity_research": pivot("RECOMMENDATION_LISTING_OPENED_MOB", {"WARM1": {"6 Oct, 3PM": 2}}),
        "fando_research": pivot("RECOMMENDATION_LISTING_OPENED_MOB", {"HOT1": {"7 Oct, 9AM": 1}}),
    })
    result = jobs.sync(ctx)
    return ctx, result


def test_pivot_workbook_creates_leads_and_activity(loaded):
    ctx, result = loaded
    (f,) = result["webengage"]
    assert (f["kind"], f["users"], f["new_leads"]) == ("activity", 3, 3)
    lead = one(ctx.conn, "SELECT * FROM leads WHERE lead_id='HOT1'")
    assert lead["category"] == "Not yet matched in client DB"  # ageing comes from the client database
    acts = contributions(ctx, ["HOT1"])
    assert acts["ORDER_PLACED_MOB"]["count"] == 3
    assert parse_dt(acts["ORDER_PLACED_MOB"]["last"], TZ) == datetime(2026, 10, 7, 10, 0, tzinfo=TZ)
    # same event on two sheets is kept apart by sheet name
    assert "RECOMMENDATION_LISTING_OPENED_MOB · fando_research" in acts


def test_core_recent_frequent_activity_scores_highest(loaded):
    ctx, _ = loaded
    scores = {r["lead_id"]: r for r in rows(ctx.conn, "SELECT lead_id, heat_score, heat_tier FROM leads")}
    assert scores["HOT1"]["heat_score"] > scores["WARM1"]["heat_score"] > scores["COLD1"]["heat_score"]
    assert scores["HOT1"]["heat_tier"] == "hot"
    assert scores["COLD1"]["heat_tier"] == "cold"


def test_recency_and_frequency_each_raise_the_score(ctx):
    base = {"ORDER_PLACED_MOB": {"count": 1, "last": NOW.isoformat(), "type": ""}}
    older = {"ORDER_PLACED_MOB": {"count": 1, "last": (NOW - timedelta(days=6)).isoformat(), "type": ""}}
    more = {"ORDER_PLACED_MOB": {"count": 8, "last": NOW.isoformat(), "type": ""}}
    noncore = {"DASHBOARD_OPENED_MOB": {"count": 1, "last": NOW.isoformat(), "type": ""}}
    declared = {"DASHBOARD_OPENED_MOB": {"count": 1, "last": NOW.isoformat(), "type": "core"}}
    s = {k: score(ctx, v)[0] for k, v in dict(base=base, older=older, more=more, noncore=noncore, declared=declared).items()}
    assert s["more"] > s["base"] > s["older"]
    assert s["base"] > s["noncore"]
    assert s["declared"] == s["base"]  # an Activity Type column overrides the config list


def test_hot_leads_get_short_sla_and_top_of_queue(loaded):
    ctx, _ = loaded
    hot_fu = one(ctx.conn, "SELECT * FROM followups WHERE lead_id='HOT1'")
    assert parse_dt(hot_fu["due_at"], TZ) == NOW + timedelta(minutes=ctx.cfg["heat"]["first_call_sla_minutes"]["hot"])
    agent = hot_fu["agent_id"]
    for lead_id in ("WARM1", "COLD1"):  # put every lead in the same caller's queue
        ctx.conn.execute("UPDATE followups SET agent_id=?, due_at=? WHERE lead_id=?", (agent, (NOW - timedelta(hours=1)).isoformat(), lead_id))
    assert queue_for(ctx, agent)[0]["lead_id"] == "HOT1"


def test_lead_turning_hot_is_pulled_forward(loaded):
    ctx, _ = loaded
    lead = one(ctx.conn, "SELECT * FROM leads WHERE lead_id='COLD1'")
    ctx._now = NOW + timedelta(minutes=30)
    write_workbook(ctx, {"ORDER_PLACED_MOB": pivot("ORDER_PLACED_MOB", {"COLD1": {"7 Oct, 10AM": 6}}),
                         "CALL_ACCEPTED_MOB": pivot("CALL_ACCEPTED_MOB", {"COLD1": {"7 Oct, 10AM": 5}})}, name="later.xlsx")
    result = jobs.sync(ctx)
    assert result["heat"]["became_hot"] == 1
    pending = rows(ctx.conn, "SELECT * FROM followups WHERE lead_id='COLD1' AND status='pending' ORDER BY due_at")
    assert pending[0]["kind"] == "system" and "Turned hot" in pending[0]["reason"]
    assert parse_dt(pending[0]["due_at"], TZ) == ctx.now()
    assert one(ctx.conn, "SELECT 1 FROM notifications WHERE recipient=? AND subject LIKE '%Hot lead%'", (lead["assigned_to"],))
