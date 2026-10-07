import csv
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from tests.helpers import open_test_ctx
from wepipeline import jobs, sheet_export
from wepipeline.clientdb import sync_new_clients
from wepipeline.context import load_config

TZ = ZoneInfo("Asia/Kolkata")
NOW = datetime(2026, 10, 7, 16, 0, tzinfo=TZ)


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
    clients = pd.DataFrame([{"CLIENT_ID": cid, "REGISTRATION_DATE": (NOW - timedelta(days=d)).replace(tzinfo=None)}
                            for cid, d in [("BUYER", 1), ("BROWSER", 5), ("OLD", 20)]])
    sync_new_clients(c, clients, pd.DataFrame())
    for hour, n in [(9, 2), (13, 1), (15, 3)]:  # BUYER tried to order today, mostly around 3 PM
        c.conn.execute("INSERT INTO events (user_id, event_name, event_time, attrs) VALUES (?,?,?,?)",
                       ("BUYER", "ORDER_PLACED_MOB", NOW.replace(hour=hour).isoformat(), f'{{"count": {n}}}'))
    c.conn.execute("INSERT INTO events (user_id, event_name, event_time, attrs) VALUES (?,?,?,?)",
                   ("BUYER", "CALL_ACCEPTED_MOB", NOW.replace(hour=15).isoformat(), '{"count": 2}'))
    c.conn.execute("INSERT INTO events (user_id, event_name, event_time, attrs) VALUES (?,?,?,?)",
                   ("BROWSER", "DASHBOARD_OPENED_MOB", (NOW - timedelta(days=1)).isoformat(), '{"count": 7}'))
    jobs.sync(c)
    return c


def test_rows_carry_priority_levers_and_pitch(ctx):
    data = {r["client"]: r for r in sheet_export.build_rows(ctx)}
    buyer, browser, old = data["BUYER"], data["BROWSER"], data["OLD"]
    assert buyer["rank"] == 1 and buyer["priority"] == "🔥 Hot"
    assert "Funding blocker: tried to place 6 order(s)" in buyer["levers"]
    assert "Research-led" in buyer["levers"]
    assert buyer["opener"].startswith("I noticed you tried to place an order")
    assert buyer["best_time"] == "3 PM–4 PM"
    assert buyer["core_detail"].startswith("Placed an order ×6")
    assert buyer["days_since_registration"] == 1 and buyer["category"] == "New - no deposit (0-7 days)"
    assert "Browsing only: 7 app visits" in browser["levers"]
    assert "At risk: 20 days since registration" in old["levers"]
    assert old["days_since_registration"] == 20
    for r in data.values():
        assert "guarantee" not in (r["opener"] + r["ideas"]).lower()


def test_csv_has_every_column(ctx, tmp_path):
    res = sheet_export.export(ctx)
    with open(res["csv"], encoding="utf-8-sig") as f:
        rows = list(csv.reader(f))
    assert rows[0] == [h for h, _ in sheet_export.COLUMNS]
    assert len(rows) == 4 and rows[1][3] == "BUYER"


class FakeWS:
    def __init__(self, title):
        self.title, self.id, self.values, self.calls = title, hash(title) % 1000, None, []

    def clear(self): self.calls.append("clear")
    def resize(self, rows, cols): self.calls.append(("resize", rows, cols))
    def update(self, values, range_name, value_input_option=None):
        self.values, self.input = values, value_input_option
    def freeze(self, rows, cols): self.calls.append("freeze")
    def format(self, rng, fmt): self.calls.append("format")
    def set_basic_filter(self): self.calls.append("filter")


class FakeBook:
    url = "https://docs.google.com/spreadsheets/d/KEY"

    def __init__(self):
        self.tabs, self.requests = {}, []

    def worksheet(self, title):
        import gspread
        if title not in self.tabs:
            raise gspread.WorksheetNotFound(title)
        return self.tabs[title]

    def add_worksheet(self, title, rows, cols):
        self.tabs[title] = FakeWS(title)
        return self.tabs[title]

    def batch_update(self, body): self.requests += body["requests"]


def test_google_sheet_push_writes_three_tabs_as_raw_values(ctx, monkeypatch):
    book = FakeBook()
    monkeypatch.setattr(sheet_export, "_client", lambda c: type("C", (), {"open_by_key": lambda self, k: book})())
    ctx.cfg["outputs"]["google_sheet"].update(enabled=True, spreadsheet_key="KEY")
    res = sheet_export.export(ctx)
    assert res["google_sheet"] == {"sheet": book.url, "rows": 3}
    customers = book.tabs["Customers"]
    assert customers.input == "RAW" and customers.values[0][0] == "Rank" and len(customers.values) == 4
    assert {"Summary", "How scoring works"} <= set(book.tabs)
    assert len(book.requests) == 3  # hot / warm / cold row colours, added once
    sheet_export.export(ctx)
    assert len(book.requests) == 3  # not duplicated on the next run


def test_sheet_failure_does_not_break_export(ctx, monkeypatch):
    def boom(c):
        raise RuntimeError("no key")
    monkeypatch.setattr(sheet_export, "_client", boom)
    ctx.cfg["outputs"]["google_sheet"].update(enabled=True, spreadsheet_key="KEY")
    res = sheet_export.export(ctx)
    assert res["rows"] == 3 and "error" in res["google_sheet"]
