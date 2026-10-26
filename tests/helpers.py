import os
from pathlib import Path

from wepipeline.db import open_ctx

TABLES = ["followups", "calls", "events", "profiles", "activity_summary", "alerts", "notifications",
          "ingested_files", "pitches", "job_runs", "settings", "uploads", "referrals", "referral_outcomes", "drive_campaigns", "drive_asks", "drive_members",
          "campaign_engagement", "sarthi_activations", "client_scores", "we_fetch", "we_ids", "cohort_runs", "leads"]


def open_test_ctx(cfg: dict):
    """SQLite by default. Set TEST_DATABASE_URL to run the same tests against a throwaway Postgres
    (never your real DATABASE_URL: every table is emptied)."""
    cfg["database_url"] = os.environ.get("TEST_DATABASE_URL", "")
    # exports go next to the test database, never into the real data/ folder or a real Google Sheet
    out = cfg.setdefault("outputs", {})
    out["csv_path"] = str(Path(cfg["database"]).parent / "customer_priority.csv")
    out["google_sheet"] = {**out.get("google_sheet", {}), "enabled": False}
    cfg["market"] = {"enabled": False}  # tests never call live market feeds
    cfg["sources"].setdefault("client_db", {})["server"] = ""  # ...nor the real client database
    cfg.setdefault("referral", {})["scope"] = "all"  # tests drive every client; the scope has its own test
    cfg.setdefault("assignment", {})["require_sm_plan"] = False  # most tests assign freely; the rule has its own test
    ctx = open_ctx(cfg=cfg)
    if cfg["database_url"]:
        ctx.conn.execute(f"TRUNCATE {', '.join(TABLES)} RESTART IDENTITY CASCADE")
    return ctx
