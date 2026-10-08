from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from tests.helpers import open_test_ctx
from wepipeline import jobs
from wepipeline.clientdb import bucket, connection_string, sync_new_clients
from wepipeline.context import load_config
from wepipeline.db import one, rows

TZ = ZoneInfo("Asia/Kolkata")
NOW = datetime(2026, 10, 7, 11, 0, tzinfo=TZ)


@pytest.fixture
def ctx(tmp_path):
    cfg = load_config()
    cfg["notifications"]["outbox_dir"] = str(tmp_path / "outbox")
    cfg["notifications"]["email"]["enabled"] = False
    cfg["notifications"]["webhook_url"] = ""
    cfg["database"] = str(tmp_path / "test.db")
    cfg["market_notes_file"] = str(tmp_path / "none.md")
    cfg["reports_dir"] = str(tmp_path / "reports")
    cfg["sources"]["leads"]["path"] = str(tmp_path / "no_sheet.csv")
    cfg["sources"]["webengage"]["folder"] = str(tmp_path / "we")
    c = open_test_ctx(cfg)
    c._now = NOW
    return c


def clients(*pairs):
    """(client_id, days since registration) -> rows shaped like the SQL Server query."""
    return pd.DataFrame([{"CLIENT_ID": cid, "REGISTRATION_DATE": (NOW - timedelta(days=d)).replace(tzinfo=None),
                          "DaysSinceRegistration": d} for cid, d in pairs])


def test_buckets():
    assert [bucket(d) for d in (0, 7, 8, 15, 16, 30)] == [
        "New - no deposit (0-7 days)", "New - no deposit (0-7 days)", "New - no deposit (8-15 days)",
        "New - no deposit (8-15 days)", "New - no deposit (16-30 days)", "New - no deposit (16-30 days)"]


def test_new_clients_become_bucketed_leads_and_duplicates_collapse(ctx):
    res = sync_new_clients(ctx, clients(("C1", 1), ("C2", 10), ("C3", 25), ("C1", 1)), pd.DataFrame())
    assert (res["clients"], res["new_leads"]) == (3, 3)
    assert res["buckets"] == {"New - no deposit (0-7 days)": 1, "New - no deposit (8-15 days)": 1, "New - no deposit (16-30 days)": 1}
    assert one(ctx.conn, "SELECT category FROM leads WHERE lead_id='C2'")["category"] == "New - no deposit (8-15 days)"


def test_fresher_registration_scores_higher_and_is_called_first(ctx):
    sync_new_clients(ctx, clients(("FRESH", 0), ("WEEK", 7), ("OLD", 28)), pd.DataFrame())
    jobs.sync(ctx)
    s = {r["lead_id"]: r["heat_score"] for r in rows(ctx.conn, "SELECT lead_id, heat_score FROM leads")}
    assert s["FRESH"] > s["WEEK"] > s["OLD"]
    first = rows(ctx.conn, "SELECT lead_id FROM leads ORDER BY assigned_at, heat_score DESC")
    assert all(r["lead_id"] for r in first)  # all assigned


def test_registration_plus_app_activity_beats_registration_alone(ctx):
    sync_new_clients(ctx, clients(("ACTIVE", 3), ("QUIET", 3)), pd.DataFrame())
    ctx.conn.execute("INSERT INTO activity_summary (user_id, activity, count, last_at, activity_type) VALUES (?,?,?,?,?)",
                     ("ACTIVE", "ORDER_PLACED_MOB", 4, NOW.isoformat(), ""))
    jobs.sync(ctx)
    s = {r["lead_id"]: r for r in rows(ctx.conn, "SELECT lead_id, heat_score, heat_tier FROM leads")}
    assert s["ACTIVE"]["heat_score"] > s["QUIET"]["heat_score"]


def test_bucket_moves_as_client_ages_and_deposit_closes_lead(ctx):
    sync_new_clients(ctx, clients(("C1", 6)), pd.DataFrame())
    jobs.sync(ctx)
    assert one(ctx.conn, "SELECT COUNT(*) n FROM followups WHERE lead_id='C1' AND status='pending'")["n"] == 1
    ctx._now = NOW + timedelta(days=3)
    sync_new_clients(ctx, clients(("C1", 6)).assign(DaysSinceRegistration=9), pd.DataFrame())
    assert one(ctx.conn, "SELECT category FROM leads WHERE lead_id='C1'")["category"] == "New - no deposit (8-15 days)"

    res = sync_new_clients(ctx, pd.DataFrame(columns=["CLIENT_ID", "REGISTRATION_DATE"]),
                           pd.DataFrame([{"CLIENT_ID": "C1", "FirstDepositDate": NOW}]))
    assert res["deposited_closed"] == 1
    lead = one(ctx.conn, "SELECT status, last_outcome FROM leads WHERE lead_id='C1'")
    assert (lead["status"], lead["last_outcome"]) == ("converted", "deposited")
    assert one(ctx.conn, "SELECT COUNT(*) n FROM followups WHERE lead_id='C1' AND status='pending'")["n"] == 0


def test_activity_file_lead_takes_the_registration_bucket(ctx):
    ctx.conn.execute("INSERT INTO leads (lead_id, we_user_id, category, source, extra) VALUES ('C9','C9','Authenticated - no funds added',"
                     "'WebEngage activity (x.xlsx)','{}')")
    sync_new_clients(ctx, clients(("C9", 2)), pd.DataFrame())
    assert one(ctx.conn, "SELECT category FROM leads WHERE lead_id='C9'")["category"] == "New - no deposit (0-7 days)"


def test_connection_string_uses_windows_login_by_default(ctx, monkeypatch):
    import wepipeline.clientdb as clientdb

    monkeypatch.setattr(clientdb, "_load_windows_user_env", lambda: None)  # never read the real profile in tests
    for var in ("CLIENT_DB_CONN", "CLIENT_DB_USER", "CLIENT_DB_PASSWORD"):
        monkeypatch.delenv(var, raising=False)
    ctx.cfg["sources"]["client_db"]["server"] = "SQL01"
    cs = connection_string(ctx)
    assert "Server=SQL01" in cs and "Trusted_Connection=yes" in cs and "ApplicationIntent=ReadOnly" in cs
    monkeypatch.setenv("CLIENT_DB_USER", "growth_ro")
    monkeypatch.setenv("CLIENT_DB_PASSWORD", "x")
    assert "UID=growth_ro" in connection_string(ctx)


def lookup(*recs):
    """Rows shaped like the client-ageing lookup query."""
    base = {"CLIENT_ID": "", "REGISTRATION_DATE": None, "AccountStatus": "Active", "IsAccountClosed": 0,
            "FirstDeposit": None, "FirstDepositDate": None, "FTD": None, "LTD": None, "Holdings": None}
    return pd.DataFrame([{**base, **r} for r in recs])


def test_every_lead_gets_an_ageing_category(ctx):
    for cid in ("NEWBIE", "OLDNODEP", "FUNDED", "DORMANT", "ACTIVE", "CLOSED", "GHOST"):
        ctx.conn.execute("INSERT INTO leads (lead_id, we_user_id, category, source, extra) VALUES (?,?,?,?,?)",
                         (cid, cid, "Authenticated - no funds added", "WebEngage activity (x.xlsx)", "{}"))
    d = lambda days: (NOW - timedelta(days=days)).replace(tzinfo=None)
    res = __import__("wepipeline.clientdb", fromlist=["enrich_leads"]).enrich_leads(ctx, lookup(
        {"CLIENT_ID": "NEWBIE", "REGISTRATION_DATE": d(3)},
        {"CLIENT_ID": "OLDNODEP", "REGISTRATION_DATE": d(400)},
        {"CLIENT_ID": "FUNDED", "REGISTRATION_DATE": d(900), "FirstDeposit": 5000, "FirstDepositDate": d(800)},
        {"CLIENT_ID": "DORMANT", "REGISTRATION_DATE": d(2000), "FirstDeposit": 1000, "FTD": d(1900).date(), "LTD": d(240).date(),
         "Holdings": 250000},
        {"CLIENT_ID": "ACTIVE", "REGISTRATION_DATE": d(700), "FTD": d(600).date(), "LTD": d(5).date()},
        {"CLIENT_ID": "CLOSED", "REGISTRATION_DATE": d(50), "IsAccountClosed": 1},
    ))
    cats = {r["lead_id"]: r["category"] for r in rows(ctx.conn, "SELECT lead_id, category FROM leads WHERE status='open'")}
    assert cats == {"NEWBIE": "New - no deposit (0-7 days)", "OLDNODEP": "No deposit (90+ days)",
                    "FUNDED": "Deposited - no trade yet", "DORMANT": "Traded - dormant (30+ days)",
                    "ACTIVE": "Traded - active (last 30 days)", "GHOST": "Not found in client DB"}
    assert res["closed_accounts"] == 1
    assert one(ctx.conn, "SELECT status FROM leads WHERE lead_id='CLOSED'")["status"] == "invalid"

    from wepipeline.insights import customer_insight
    dormant = one(ctx.conn, "SELECT * FROM leads WHERE lead_id='DORMANT'")
    ins = customer_insight(ctx, dormant)
    assert ins["stage"] == "dormant trader" and ins["days_since_last_trade"] == 240
    assert "Dormant: last trade 8 months ago (240 days)" in ins["levers"] and "High-value: holdings of ₹250,000" in ins["levers"]
    assert "8 months since your last trade" in ins["opener"]


def test_deposit_closes_only_no_deposit_leads(ctx):
    sync_new_clients(ctx, clients(("NEW1", 3)), pd.DataFrame())
    ctx.conn.execute("INSERT INTO leads (lead_id, we_user_id, category, source, extra) VALUES "
                     "('OLD1','OLD1','Traded - dormant (30+ days)','WebEngage activity (x.xlsx)','{}')")
    res = sync_new_clients(ctx, pd.DataFrame(columns=["CLIENT_ID", "REGISTRATION_DATE"]),
                           pd.DataFrame([{"CLIENT_ID": "NEW1"}, {"CLIENT_ID": "OLD1"}]))
    assert res["deposited_closed"] == 1
    status = {r["lead_id"]: r["status"] for r in rows(ctx.conn, "SELECT lead_id, status FROM leads")}
    assert status == {"NEW1": "converted", "OLD1": "open"}


def test_placeholder_rows_are_ignored(ctx, tmp_path):
    from wepipeline.ingest import purge_ignored

    ctx.conn.execute("INSERT INTO leads (lead_id, we_user_id, category, source, extra) VALUES ('Others','Others','x','WebEngage activity (x)','{}')")
    ctx.conn.execute("INSERT INTO activity_summary (user_id, activity, count) VALUES ('Others','DASHBOARD_OPENED_MOB',999)")
    assert purge_ignored(ctx) == 1
    assert one(ctx.conn, "SELECT COUNT(*) n FROM leads")["n"] == 0
    assert one(ctx.conn, "SELECT COUNT(*) n FROM activity_summary")["n"] == 0
