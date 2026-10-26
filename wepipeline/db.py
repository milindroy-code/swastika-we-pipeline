"""Storage: SQLite locally, Postgres when DATABASE_URL is set (hosted deployments).

Timestamps are ISO-8601 strings in the configured timezone, so they sort as text in both."""
from __future__ import annotations

import os
import re
import sqlite3
import threading

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

-- Friends a client referred on a referral-ask call (taken with the friend's consent).
CREATE TABLE IF NOT EXISTS referrals (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    referrer_id TEXT NOT NULL,
    agent_id    TEXT,
    name        TEXT,
    phone       TEXT,
    raw         TEXT,
    created_at  TEXT NOT NULL
);




-- Drive programs (referral asks, Sarthi activation; see wepipeline/drives.py).
CREATE TABLE IF NOT EXISTS drive_campaigns (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    program     TEXT NOT NULL,
    name        TEXT NOT NULL,
    started_at  TEXT NOT NULL,
    ended_at    TEXT,
    started_by  TEXT
);

-- A client's place in a drive program. One client can be in several programs (referral pool and Sarthi list) at
-- once; each membership has its own status, GM, score and next try. Funding lists (leads.status='open') come first.
CREATE TABLE IF NOT EXISTS drive_members (
    program         TEXT NOT NULL,
    lead_id         TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'open',  -- open / referred / activated / not_interested / unreachable / invalid / out_of_window
    assigned_to     TEXT,
    assigned_at     TEXT,
    heat_score      REAL NOT NULL DEFAULT 0,
    heat_tier       TEXT NOT NULL DEFAULT 'cold',
    heat_reasons    TEXT,
    npc_count       INTEGER NOT NULL DEFAULT 0,
    last_outcome    TEXT,
    last_contact_at TEXT,
    next_at         TEXT,
    asked_at        TEXT,
    drive_at        TEXT,
    campaign_id     INTEGER,
    segment         TEXT,                          -- client type: new / active / resurrected
    joined_at       TEXT,
    updated_at      TEXT,
    PRIMARY KEY (program, lead_id)
);

-- Every ask handed to a GM in a drive (one row per client per drive).
CREATE TABLE IF NOT EXISTS drive_asks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    program     TEXT NOT NULL,
    campaign_id INTEGER NOT NULL,
    lead_id     TEXT NOT NULL,
    agent_id    TEXT NOT NULL,
    assigned_at TEXT NOT NULL,
    returned_at TEXT                     -- taken back to the pool uncalled (drive / campaign ended)
);

-- Engagement with a program's channel campaigns (WhatsApp, SMS, email, push...), one row per client, campaign,
-- channel, action and day, so overlapping uploads replace rather than add up.
-- A score from the client's previous app history, uploaded by the SM (Customer list, step 4); blended into heat.
CREATE TABLE IF NOT EXISTS client_scores (
    client_id   TEXT PRIMARY KEY,
    score       REAL NOT NULL,
    raw         TEXT,
    source_file TEXT,
    uploaded_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS campaign_engagement (
    program     TEXT NOT NULL,
    user_id     TEXT NOT NULL,
    campaign    TEXT NOT NULL,
    channel     TEXT NOT NULL,
    action      TEXT NOT NULL,
    day         TEXT NOT NULL,
    count       INTEGER NOT NULL DEFAULT 1,
    source_file TEXT,
    UNIQUE(program, user_id, campaign, channel, action, day)
);

-- Sarthi switched on, from the client database (SarthiActivationDate); how Sarthi drives count success.
CREATE TABLE IF NOT EXISTS sarthi_activations (
    client_id    TEXT PRIMARY KEY,
    activated_at TEXT,
    updated_at   TEXT
);

-- Clients who joined through a referral (CRM lead source 37), refreshed from the client database.
CREATE TABLE IF NOT EXISTS referral_outcomes (
    referred_id   TEXT PRIMARY KEY,
    referrer_id   TEXT NOT NULL,
    lead_ref      TEXT,
    authorized_at TEXT,
    first_trade   TEXT,
    first_deposit REAL,
    holdings      REAL,
    last_trade    TEXT,
    updated_at    TEXT
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

-- App activity pulled per client from the WebEngage API (cohorts.py): where each client's fetch has got to.
CREATE TABLE IF NOT EXISTS we_fetch (
    client_id  TEXT PRIMARY KEY,
    we_id      TEXT,
    fetched_at TEXT,                     -- when we last asked WebEngage
    fetched_to TEXT,                     -- activity is complete up to this time
    events     INTEGER NOT NULL DEFAULT 0,
    error      TEXT
);

-- Each GM's plan for the day (assign.py): how many new clients, from which lists, of which priority.
CREATE TABLE IF NOT EXISTS gm_plans (
    gm_id      TEXT NOT NULL,
    day        TEXT NOT NULL,
    plan       TEXT NOT NULL,            -- JSON {"new_clients": int, "lists": [...], "tiers": [...]}
    updated_at TEXT NOT NULL,
    PRIMARY KEY (gm_id, day)
);

-- Client code -> WebEngage ID (CRM lead -> eKYC). IDs don't change, so each client is looked up once; we_id is NULL
-- when the CRM has none (looked up again after a few days, in case it appears).
CREATE TABLE IF NOT EXISTS we_ids (
    client_id    TEXT PRIMARY KEY,
    we_id        TEXT,
    looked_up_at TEXT NOT NULL
);

-- Daily cohort lists: pull from the client database, fetch app activity, score, publish as a list (cohorts.py).
CREATE TABLE IF NOT EXISTS cohort_runs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    cohort       TEXT NOT NULL,          -- no_deposit | low_margin | payout | reactivation
    publish      INTEGER NOT NULL DEFAULT 1,  -- 0 = overnight fetch only, 1 = also score and publish the list
    status       TEXT NOT NULL,          -- queued | running | ready | failed | cancelled
    step         TEXT,
    requested_by TEXT,
    requested_at TEXT NOT NULL,
    started_at   TEXT,
    finished_at  TEXT,
    total        INTEGER NOT NULL DEFAULT 0,
    to_fetch     INTEGER NOT NULL DEFAULT 0,
    fetched      INTEGER NOT NULL DEFAULT 0,
    no_we_id     INTEGER NOT NULL DEFAULT 0,
    upload_id    INTEGER,
    message      TEXT
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
              "campaign": "TEXT NOT NULL DEFAULT ''",  # list the lead belongs to, e.g. 'reactivation'
              "on_hold": "INTEGER NOT NULL DEFAULT 0",  # 1 = in an uploaded list the SM has not assigned yet
              "released_at": "TEXT"},  # when an SM pressed Assign on a list holding this client
    "drive_members": {"segment": "TEXT"},
    "referral_outcomes": {"last_trade": "TEXT"},  # client type in a drive: new / active / resurrected
    "calls": {"sarthi_activated": "INTEGER NOT NULL DEFAULT 0", "deposit_amount": "REAL",
              "program": "TEXT"},  # set for drive calls (referral / sarthi); NULL = the funding work
}


# ---------------------------------------------------------------- SQLite (local / tests)
def _open_sqlite(path) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")  # dashboard and scheduler run side by side
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


class SqliteConnection:
    """One SQLite connection per thread. The dashboard serves several sessions and an auto-refresh at once; sharing a
    single connection between threads fails with 'bad parameter or other API misuse' under load."""

    def __init__(self, path):
        self.path = str(path)
        self._local = threading.local()

    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = self._local.conn = _open_sqlite(self.path)
        return conn

    def execute(self, sql: str, params=()):
        return self._conn().execute(sql, params)

    def executescript(self, script: str):
        return self._conn().executescript(script)

    def commit(self) -> None:
        self._conn().commit()

    def rollback(self) -> None:
        self._conn().rollback()

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None


def connect_sqlite(path) -> SqliteConnection:
    conn = SqliteConnection(path)
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


def write_retrying(conn, fn, tries: int = 6, wait: float = 10.0):
    """Run a write (fn does its statements and commits). SQLite allows one writer at a time, and the scheduler's or the
    cohort worker's re-score can hold the database for longer than the 30 s busy timeout: roll back (a failed write leaves
    its transaction open, blocking every later write) and try again."""
    import time

    for attempt in range(tries):
        try:
            return fn()
        except sqlite3.OperationalError as e:
            conn.rollback()
            if "locked" not in str(e) or attempt == tries - 1:
                raise
            time.sleep(wait)
