from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from wepipeline import cohorts
from wepipeline.context import load_config
from wepipeline.db import one, rows
from tests.helpers import open_test_ctx

TZ = ZoneInfo("Asia/Kolkata")
NOW = datetime(2026, 10, 22, 21, 0, tzinfo=TZ)  # Thursday evening


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


def ev(name, hours_ago, category="application", i=[0]):
    i[0] += 1
    return {"name": name, "category": category, "id": f"e{i[0]}", "time": (NOW - timedelta(hours=hours_ago)).astimezone(ZoneInfo("UTC"))
            .strftime("%Y-%m-%dT%H:%M:%S.000Z")}


class FakeApi:
    """Stands in for the WebEngage API: per WebEngage ID, the events it holds; records every window asked for."""

    def __init__(self, data, page_size=2000):
        self.data, self.calls, self.s = data, [], {"page_size": page_size}

    def activities(self, we_id, start, end):
        self.calls.append((we_id, start, end))
        return [e for e in self.data.get(we_id, [])
                if start <= datetime.fromisoformat(e["time"].replace("Z", "+00:00")) <= end]


def test_only_app_events_are_stored_once(ctx):
    events = [ev("ORDER_PLACED_MOB", 2), ev("Push Sent to FCM / APNs (Mobile)", 1, "system")]
    assert cohorts.store_activity(ctx, "C1", events) == 1
    assert cohorts.store_activity(ctx, "C1", events) == 0  # the overlap of the next fetch adds nothing
    assert [r["event_name"] for r in rows(ctx.conn, "SELECT event_name FROM events")] == ["ORDER_PLACED_MOB"]


def test_next_fetch_continues_from_the_last_one(ctx):
    api = FakeApi({"W1": [ev("ORDER_PLACED_MOB", 30), ev("DASHBOARD_OPENED_MOB", 1)]})
    cohorts.fetch_client(ctx, api, "C1", "W1")
    assert NOW - api.calls[0][1] == timedelta(days=15)  # first fetch: the whole lookback
    ctx._now = NOW + timedelta(hours=10)
    cohorts.fetch_client(ctx, api, "C1", "W1")
    assert api.calls[1][1] == NOW - timedelta(hours=1)  # then only since the last fetch, with an hour of overlap
    assert one(ctx.conn, "SELECT events FROM we_fetch WHERE client_id='C1'")["events"] == 2
    assert cohorts.needs_fetch(ctx, ["C1", "C2"]) == ["C2"]


def test_a_full_window_is_split(ctx, monkeypatch):
    api = cohorts.Api.__new__(cohorts.Api)
    api.s = {"page_size": 2, "host": "h", "account": "a"}
    seen = []

    def fake_get(url):
        seen.append(url)
        return {"response": {"data": {"eventStream": [{}, {}] if len(seen) == 1 else [{}]}}}

    api._get = fake_get
    assert len(api.activities("W1", NOW - timedelta(days=2), NOW)) == 2  # 2 halves, 1 event each
    assert len(seen) == 3


def test_run_pulls_fetches_scores_and_holds_the_list(ctx, monkeypatch):
    df = pd.DataFrame({"CLIENT_ID": ["HOT1", "COLD1", "NOID1"], "IsCentralized": [1, 1, 1]})
    monkeypatch.setattr(cohorts, "pull_cohort", lambda c, k: df)
    monkeypatch.setattr(cohorts, "lookup_we_ids", lambda c, ids: {"HOT1": "W-HOT", "COLD1": "W-COLD"})
    api = FakeApi({"W-HOT": [ev(name, h) for h in (1, 2, 3, 26, 50) for name in
                             ("ORDER_PLACED_MOB", "CALL_ACCEPTED_MOB", "RECOMMENDATION_LISTING_OPENED_MOB", "DASHBOARD_OPENED_MOB")],
                   "W-COLD": [ev("Push Received (Mobile)", 3, "system")]})
    cohorts.queue(ctx, ["no_deposit"], requested_by="sm_01")
    assert cohorts.queue(ctx, ["no_deposit"]) == 0  # already queued
    res = cohorts.work_once(ctx, api)
    run = one(ctx.conn, "SELECT * FROM cohort_runs")
    assert run["status"] == "ready" and (run["total"], run["to_fetch"], run["fetched"], run["no_we_id"]) == (3, 2, 2, 1)
    assert res["upload_id"] == run["upload_id"]
    leads = {r["lead_id"]: r for r in rows(ctx.conn, "SELECT lead_id, heat_tier, on_hold, assigned_to, campaign FROM leads")}
    assert leads["HOT1"]["heat_tier"] == "hot" and leads["COLD1"]["heat_tier"] == "cold"
    assert all(l["on_hold"] == 1 and l["assigned_to"] is None and l["campaign"] == "new" for l in leads.values())
    ready = cohorts.latest_runs(ctx)["no_deposit"]["ready"]
    assert ready["upload_id"] == run["upload_id"]
    # the next run the same evening fetches nobody again
    cohorts.queue(ctx, ["no_deposit"])
    cohorts.work_once(ctx, api)
    assert len(api.calls) == 2


def test_overnight_fetch_refreshes_the_afternoon_and_the_morning_only_tops_up(ctx, monkeypatch):
    df = pd.DataFrame({"CLIENT_ID": ["A1"], "IsCentralized": [1]})
    monkeypatch.setattr(cohorts, "pull_cohort", lambda c, k: df)
    monkeypatch.setattr(cohorts, "lookup_we_ids", lambda c, ids: {"A1": "WA", "B1": "WB"})
    api = FakeApi({})
    ctx._now = NOW - timedelta(hours=6)  # 15:00: fetched in the afternoon
    cohorts.fetch_client(ctx, api, "A1", "WA")
    ctx._now = NOW  # 21:00: the overnight run (queued at 20:00) refreshes it
    ctx.conn.execute("INSERT INTO cohort_runs (cohort, publish, status, requested_at) VALUES ('no_deposit', 0, 'queued', ?)",
                     ((NOW - timedelta(hours=1)).isoformat(),))
    cohorts.work_once(ctx, api)
    assert len(api.calls) == 2
    ctx._now = NOW + timedelta(hours=11, minutes=30)  # 08:30: fetched overnight, so the morning list fetches nobody
    df.loc[1] = ["B1", 1]  # ...except a client new to the list
    cohorts.queue(ctx, ["no_deposit"])
    cohorts.work_once(ctx, api)
    assert [c[0] for c in api.calls] == ["WA", "WA", "WB"]


def test_schedule_queues_the_fetch_and_the_publish_once_a_day(ctx):
    assert cohorts.schedule(ctx) == ["publish", "fetch"]  # 21:00: past both times today
    assert cohorts.schedule(ctx) == []
    queued = rows(ctx.conn, "SELECT cohort, publish FROM cohort_runs ORDER BY id")
    assert [(r["cohort"], r["publish"]) for r in queued][:4] == [(c, 1) for c in cohorts.COHORTS]
    assert len(queued) == 4  # the fetch is not queued again on top of the publish runs
    ctx._now = datetime(2026, 10, 24, 9, 0, tzinfo=TZ)  # Saturday: no lists to publish
    assert cohorts.schedule(ctx) == []


def test_a_failing_fetch_is_recorded_and_the_run_goes_on(ctx, monkeypatch):
    df = pd.DataFrame({"CLIENT_ID": ["A1", "B1"], "IsCentralized": [1, 1]})
    monkeypatch.setattr(cohorts, "pull_cohort", lambda c, k: df)
    monkeypatch.setattr(cohorts, "lookup_we_ids", lambda c, ids: {"A1": "WA", "B1": "WB"})

    class Flaky(FakeApi):
        def activities(self, we_id, start, end):
            if we_id == "WA":
                raise RuntimeError("WebEngage HTTP 404")
            return super().activities(we_id, start, end)

    cohorts.queue(ctx, ["no_deposit"], publish=False)
    cohorts.work_once(ctx, Flaky({"WB": [ev("ORDER_PLACED_MOB", 1)]}))
    run = one(ctx.conn, "SELECT * FROM cohort_runs")
    assert run["status"] == "ready" and run["message"].startswith("1 fetch(es) failed")
    assert one(ctx.conn, "SELECT error FROM we_fetch WHERE client_id='A1'")["error"].startswith("WebEngage HTTP 404")
    assert cohorts.needs_fetch(ctx, ["A1", "B1"]) == ["A1"]  # retried next time


def test_churned_clients_are_only_fetched_overnight(ctx, monkeypatch):
    df = pd.DataFrame({"CLIENT_ID": ["R1"], "IsCentralized": [1]})
    monkeypatch.setattr(cohorts, "pull_cohort", lambda c, k: df)
    monkeypatch.setattr(cohorts, "lookup_we_ids", lambda c, ids: {"R1": "WR"})
    api = FakeApi({"WR": [ev("ORDER_PLACED_MOB", 1)]})
    cohorts.queue(ctx, ["reactivation"], publish=True)  # "Rebuild now" in the day: scored, nothing fetched
    cohorts.work_once(ctx, api)
    assert api.calls == [] and one(ctx.conn, "SELECT status FROM cohort_runs")["status"] == "ready"
    cohorts.queue(ctx, ["reactivation"], publish=False)  # the overnight run fetches
    cohorts.work_once(ctx, api)
    assert [c[0] for c in api.calls] == ["WR"]


def test_a_locked_database_is_retried_not_fatal(ctx, monkeypatch):
    import sqlite3

    monkeypatch.setattr(cohorts.time, "sleep", lambda s: None)
    real, fails = cohorts._save_fetch, [2]

    def flaky(*a):
        if fails[0]:
            fails[0] -= 1
            raise sqlite3.OperationalError("database is locked")
        return real(*a)

    monkeypatch.setattr(cohorts, "_save_fetch", flaky)
    assert cohorts.fetch_client(ctx, FakeApi({"W1": [ev("ORDER_PLACED_MOB", 1)]}), "C1", "W1") == 1
