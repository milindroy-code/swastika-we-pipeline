"""Storage: SQLite locally, Postgres when DATABASE_URL is set (hosted deployments).

Timestamps are ISO-8601 strings in the configured timezone, so they sort as text in both."""
from __future__ import annotations

import os
import re
import sqlite3

from .context import Ctx, load_config

SCHEMA = """
CREATE TABLE IF NOT EXISTS leads (
    lead_id         TEXT PRIMARY KEY,
    we_user_id      TEXT,
    name            TEXT,
    phone           TEXT,
    email           TEXT,
    category        TEXT,
    source          TEXT,
    remarks         TEXT,
    extra           TEXT,               -- JSON of unmapped sheet columns
    status          TEXT NOT NULL DEFAULT 'open',  -- open | converted | not_interested | unreachable | invalid
    assigned_to     TEXT,
    assigned_at     TEXT,
    first_seen_at   TEXT,
    updated_at      TEXT,
    row_hash        TEXT,
    npc_count       INTEGER NOT NULL DEFAULT 0,
    last_contact_at TEXT,
    last_outcome    TEXT,
    heat_score      REAL NOT NULL DEFAULT 0,  -- 0-100, from app activity (see heat.py)
    heat_tier       TEXT NOT NULL DEFAULT 'cold',  -- hot | warm | cold
    heat_reasons    TEXT
);
CREATE INDEX IF NOT EXISTS ix_leads_assigned ON leads(assigned_to, status);

CREATE TABLE IF NOT EXISTS followups (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_id       TEXT NOT NULL REFERENCES leads(lead_id),
    agent_id      TEXT NOT NULL,
    due_at        TEXT NOT NULL,
    kind          TEXT NOT NULL,          -- first_call | npc_retry | promised | system
    reason        TEXT,
    status        TEXT NOT NULL DEFAULT 'pending',  -- pending | done | cancelled
    created_at    TEXT NOT NULL,
    completed_at  TEXT,
    call_id       INTEGER,
    warned_at     TEXT,
    escalated_at  TEXT,
    missed        INTEGER NOT NULL DEFAULT 0  -- 1 once escalated: counts as a miss even if done later
);
CREATE INDEX IF NOT EXISTS ix_followups_due ON followups(status, due_at);
CREATE INDEX IF NOT EXISTS ix_followups_lead ON followups(lead_id, status);

CREATE TABLE IF NOT EXISTS calls (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_id          TEXT NOT NULL,
    agent_id         TEXT NOT NULL,
    called_at        TEXT NOT NULL,
    outcome          TEXT NOT NULL,
    notes            TEXT,
    next_followup_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_calls_agent ON calls(agent_id, called_at);

CREATE TABLE IF NOT EXISTS events (
    user_id     TEXT NOT NULL,
    event_name  TEXT NOT NULL,
    event_time  TEXT NOT NULL,
    attrs       TEXT,
    source_file TEXT,
    UNIQUE(user_id, event_name, event_time)
);
CREATE INDEX IF NOT EXISTS ix_events_user ON events(user_id, event_time);

CREATE TABLE IF NOT EXISTS profiles (
    user_id    TEXT PRIMARY KEY,
    phone      TEXT,
    email      TEXT,
    attrs      TEXT,
    updated_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_profiles_phone ON profiles(phone);
CREATE INDEX IF NOT EXISTS ix_profiles_email ON profiles(email);

-- Aggregated user activity files: one row per user x activity (count + last done).
CREATE TABLE IF NOT EXISTS activity_summary (
    user_id       TEXT NOT NULL,
    activity      TEXT NOT NULL,
    count         INTEGER NOT NULL DEFAULT 1,
    last_at       TEXT,
    activity_type TEXT,                  -- core | non-core | '' (then decided by config)
    source_file   TEXT,
    PRIMARY KEY (user_id, activity)
);

CREATE TABLE IF NOT EXISTS alerts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    followup_id     INTEGER,
    lead_id         TEXT,
    agent_id        TEXT,
    manager_id      TEXT,
    level           TEXT NOT NULL,       -- warning | escalation | info
    message         TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    acknowledged_at TEXT
);

CREATE TABLE IF NOT EXISTS notifications (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    kind       TEXT NOT NULL,            -- morning_note | warning | escalation | eod_report | info
    recipient  TEXT NOT NULL,
    subject    TEXT NOT NULL,
    body       TEXT NOT NULL,
    created_at TEXT NOT NULL,
    channels   TEXT
);

CREATE TABLE IF NOT EXISTS ingested_files (
    path        TEXT PRIMARY KEY,
    mtime       REAL NOT NULL,
    kind        TEXT,
    rows        INTEGER,
    ingested_at TEXT
);

CREATE TABLE IF NOT EXISTS pitches (
    lead_id      TEXT PRIMARY KEY,
    text         TEXT NOT NULL,
    basis_hash   TEXT NOT NULL,
    generated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS job_runs (
    job    TEXT NOT NULL,
    day    TEXT NOT NULL,
    ran_at TEXT NOT NULL,
    PRIMARY KEY (job, day)
);

-- Lists uploaded by SMs for analysis (Customer list page).
CREATE TABLE IF NOT EXISTS uploads (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    filename    TEXT NOT NULL,
    uploaded_by TEXT,
    uploaded_at TEXT NOT NULL,
    client_ids  TEXT NOT NULL,           -- JSON list of client codes in the file
    summary     TEXT                     -- JSON of the analysis result
);

-- Small editable values shared by the dashboard and the scheduler (e.g. today's market notes).
CREATE TABLE IF NOT EXISTS settings (
    key        TEXT PRIMARY KEY,
    value      TEXT,
    updated_at TEXT
);
"""

# Columns added after the first release, so existing databases are upgraded in place.
LATER_COLUMNS = {
    "leads": {"heat_score": "REAL NOT NULL DEFAULT 0", "heat_tier": "TEXT NOT NULL DEFAULT 'cold'", "heat_reasons": "TEXT",
              "campaign": "TEXT NOT NULL DEFAULT ''"},  # list the lead belongs to, e.g. 'reactivation'
    "calls": {"sarthi_activated": "INTEGER NOT NULL DEFAULT 0"},
}


# ---------------------------------------------------------------- SQLite (local / tests)
def connect_sqlite(path) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")  # dashboard and scheduler run side by side
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    for table, cols in LATER_COLUMNS.items():
        have = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        for name, decl in cols.items():
            if name not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
    conn.commit()
    return conn


# ---------------------------------------------------------------- Postgres (hosted, e.g. Neon)
_NAMED = re.compile(r"(?<![:\w]):([A-Za-z_]\w*)")


def _to_pg(sql: str, params) -> str:
    """Translate the SQLite placeholder style used across the code (? and :name) to psycopg's."""
    if not params:
        return sql
    sql = sql.replace("%", "%%")
    return _NAMED.sub(r"%(\1)s", sql) if isinstance(params, dict) else sql.replace("?", "%s")


class PgConnection:
    """Just enough of the sqlite3.Connection interface for this app, backed by psycopg.

    Runs in autocommit mode and reconnects once if the server dropped the connection
    (free-tier databases suspend when idle)."""

    def __init__(self, url: str):
        self.url = url
        self._connect()

    def _connect(self) -> None:
        import psycopg
        from psycopg.rows import dict_row

        self._conn = psycopg.connect(self.url, autocommit=True, row_factory=dict_row, prepare_threshold=None)

    def execute(self, sql: str, params=()):
        import psycopg

        query = _to_pg(sql, params)
        for attempt in (1, 2):
            try:
                cur = self._conn.cursor()
                cur.execute(query, params or None)
                return cur
            except (psycopg.OperationalError, psycopg.InterfaceError):
                if attempt == 2:
                    raise
                self._connect()

    def commit(self) -> None:  # autocommit: every statement is already saved
        pass

    def close(self) -> None:
        self._conn.close()


def connect_postgres(url: str) -> PgConnection:
    conn = PgConnection(url)
    schema = SCHEMA.replace("INTEGER PRIMARY KEY AUTOINCREMENT", "BIGSERIAL PRIMARY KEY").replace(" REAL ", " DOUBLE PRECISION ")
    conn.execute(schema)
    for table, cols in LATER_COLUMNS.items():
        for name, decl in cols.items():
            conn.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {name} {decl.replace('REAL', 'DOUBLE PRECISION')}")
    return conn


def database_url(cfg: dict) -> str:
    """cfg['database_url'] wins when present (tests set it, even to ''); otherwise the DATABASE_URL env var."""
    return cfg["database_url"] if "database_url" in cfg else os.environ.get("DATABASE_URL", "")


def open_ctx(config_path=None, cfg: dict | None = None) -> Ctx:
    ctx = Ctx(cfg or load_config(config_path))
    if os.environ.get("WEP_DATABASE"):  # demo dashboard: point at a separate SQLite file
        ctx.cfg["database"], ctx.cfg["database_url"] = os.environ["WEP_DATABASE"], ""
    if os.environ.get("WEP_NOW"):  # demo dashboard: frozen clock
        from datetime import datetime

        ctx._now = datetime.fromisoformat(os.environ["WEP_NOW"]).replace(tzinfo=ctx.tz)
    url = database_url(ctx.cfg)
    if url:
        ctx.conn = connect_postgres(url)
    else:
        db_path = ctx.path(ctx.cfg.get("database", "data/growth.db"))
        db_path.parent.mkdir(parents=True, exist_ok=True)
        ctx.conn = connect_sqlite(db_path)
    return ctx


def rows(conn, sql: str, params=()) -> list[dict]:
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def one(conn, sql: str, params=()) -> dict | None:
    r = conn.execute(sql, params).fetchone()
    return dict(r) if r else None


def get_setting(conn, key: str) -> str | None:
    r = one(conn, "SELECT value FROM settings WHERE key=?", (key,))
    return r["value"] if r else None


def set_setting(conn, key: str, value: str, now: str) -> None:
    conn.execute("INSERT INTO settings (key, value, updated_at) VALUES (?,?,?) "
                 "ON CONFLICT (key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at", (key, value, now))
    conn.commit()
