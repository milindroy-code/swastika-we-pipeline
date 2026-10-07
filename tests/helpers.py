import os

from wepipeline.db import open_ctx

TABLES = ["followups", "calls", "events", "profiles", "activity_summary", "alerts", "notifications",
          "ingested_files", "pitches", "job_runs", "settings", "leads"]


def open_test_ctx(cfg: dict):
    """SQLite by default. Set TEST_DATABASE_URL to run the same tests against a throwaway Postgres
    (never your real DATABASE_URL: every table is emptied)."""
    cfg["database_url"] = os.environ.get("TEST_DATABASE_URL", "")
    ctx = open_ctx(cfg=cfg)
    if cfg["database_url"]:
        ctx.conn.execute(f"TRUNCATE {', '.join(TABLES)} RESTART IDENTITY CASCADE")
    return ctx
