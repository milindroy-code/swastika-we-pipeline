"""New clients who have not made their first deposit, pulled from the back-office SQL Server.

Runs on a machine inside the office network (the SQL Server is not reachable from the cloud).
It writes into the same database the dashboard uses, so it can feed the hosted app too.
"""
from __future__ import annotations

import json
import logging
import os

import pandas as pd

from .context import Ctx, iso, parse_dt
from .db import one, rows

log = logging.getLogger(__name__)
SOURCE = "Client DB: registered, no deposit"
REGISTERED = "REGISTERED"  # synthetic activity: makes registration recency part of the heat score


def _cfg(ctx: Ctx) -> dict:
    return ctx.cfg["sources"].get("client_db") or {}


def connection_string(ctx: Ctx) -> str:
    """CLIENT_DB_CONN overrides everything; otherwise built from config + optional SQL login env vars."""
    if os.environ.get("CLIENT_DB_CONN"):
        return os.environ["CLIENT_DB_CONN"]
    c = _cfg(ctx)
    if not c.get("server"):
        raise RuntimeError("Set sources.client_db.server in config.yaml (or the CLIENT_DB_CONN environment variable).")
    parts = [f"Driver={{{c.get('driver', 'ODBC Driver 18 for SQL Server')}}}", f"Server={c['server']}",
             f"Database={c.get('database', 'Repl_TargetReview')}", "Encrypt=yes",
             f"TrustServerCertificate={'yes' if c.get('trust_server_certificate', True) else 'no'}",
             "ApplicationIntent=ReadOnly"]
    user = os.environ.get("CLIENT_DB_USER")
    parts += [f"UID={user}", f"PWD={os.environ.get('CLIENT_DB_PASSWORD', '')}"] if user else ["Trusted_Connection=yes"]
    return ";".join(parts) + ";"


def _query(ctx: Ctx, sql: str) -> pd.DataFrame:
    import pyodbc

    with pyodbc.connect(connection_string(ctx), timeout=20, readonly=True) as conn:
        cur = conn.cursor()
        cur.execute(sql)
        cols = [c[0] for c in cur.description]
        return pd.DataFrame.from_records(cur.fetchall(), columns=cols)


def bucket(days: int) -> str:
    """0-7, 8-15 or 16-30 days since registration (the windows the team works in)."""
    for upper, label in _bucket_spec():
        if days <= upper:
            return label
    return _bucket_spec()[-1][1]


def _bucket_spec() -> list[tuple[int, str]]:
    return [(7, "New - no deposit (0-7 days)"), (15, "New - no deposit (8-15 days)"), (30, "New - no deposit (16-30 days)")]


def sync_new_clients(ctx: Ctx, clients: pd.DataFrame | None = None, deposits: pd.DataFrame | None = None) -> dict:
    """Add/refresh no-deposit clients as leads and close leads who have since deposited."""
    c = _cfg(ctx)
    if clients is None:
        clients = _query(ctx, c["new_clients_query"])
    if deposits is None and c.get("deposits_query"):
        deposits = _query(ctx, c["deposits_query"])
    now = ctx.now()
    cols = {k.lower(): k for k in clients.columns}
    id_col, reg_col = cols.get("client_id"), cols.get("registration_date")
    if not id_col or not reg_col:
        raise ValueError(f"The new-clients query must return CLIENT_ID and REGISTRATION_DATE (got {list(clients.columns)})")

    new = refreshed = 0
    seen = {}
    for r in clients.to_dict("records"):
        cid = str(r[id_col] or "").strip()
        reg = parse_dt(r[reg_col], ctx.tz)
        if not cid or not reg or cid in seen:  # the FTD join can repeat a client; keep the first row
            continue
        seen[cid] = reg
        days = max((now.date() - reg.date()).days, 0)
        category = bucket(days)
        extra = {"registration_date": iso(reg), "days_since_registration": days}
        lead = one(ctx.conn, "SELECT * FROM leads WHERE lead_id=? OR we_user_id=?", (cid, cid))
        if lead is None:
            ctx.conn.execute(
                """INSERT INTO leads (lead_id, we_user_id, name, phone, email, category, source, extra, first_seen_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (cid, cid, "", "", "", category, SOURCE, json.dumps(extra), iso(now), iso(now)),
            )
            new += 1
        elif lead["status"] == "open" and (lead["source"] == SOURCE or (lead["source"] or "").startswith("WebEngage activity")):
            # keep the age bucket current; leads from the lead sheet keep the sheet's own category
            merged = {**json.loads(lead["extra"] or "{}"), **extra}
            ctx.conn.execute("UPDATE leads SET category=?, extra=?, updated_at=? WHERE lead_id=?",
                             (category, json.dumps(merged), iso(now), lead["lead_id"]))
            refreshed += 1
        ctx.conn.execute(
            """INSERT INTO activity_summary (user_id, activity, count, last_at, activity_type, source_file) VALUES (?,?,?,?,?,?)
               ON CONFLICT (user_id, activity) DO UPDATE SET last_at=excluded.last_at, source_file=excluded.source_file""",
            (cid, REGISTERED, 1, iso(reg), "", "client_db"),
        )
    ctx.conn.commit()
    closed = close_deposited(ctx, deposits) if deposits is not None else 0
    buckets = {label: sum(1 for d in seen.values() if bucket(max((now.date() - d.date()).days, 0)) == label)
               for _, label in _bucket_spec()}
    return {"clients": len(seen), "new_leads": new, "refreshed": refreshed, "deposited_closed": closed, "buckets": buckets}


def close_deposited(ctx: Ctx, deposits: pd.DataFrame) -> int:
    """Leads that have made their first deposit are converted: no more calls for a first deposit."""
    if deposits.empty:
        return 0
    cols = {k.lower(): k for k in deposits.columns}
    id_col = cols.get("client_id")
    if not id_col:
        raise ValueError("The deposits query must return CLIENT_ID")
    date_col = cols.get("firstdepositdate")
    deposited = {str(r[id_col]).strip(): r.get(date_col) if date_col else None for r in deposits.to_dict("records")}
    now = iso(ctx.now())
    closed = 0
    for lead in rows(ctx.conn, "SELECT lead_id, we_user_id, assigned_to FROM leads WHERE status='open'"):
        cid = lead["we_user_id"] or lead["lead_id"]
        if cid not in deposited:
            continue
        ctx.conn.execute("UPDATE leads SET status='converted', last_outcome='deposited', updated_at=? WHERE lead_id=?",
                         (now, lead["lead_id"]))
        ctx.conn.execute("UPDATE followups SET status='cancelled', completed_at=?, reason=COALESCE(reason,'') || ' (closed: first deposit made)' "
                         "WHERE lead_id=? AND status='pending'", (now, lead["lead_id"]))
        closed += 1
    ctx.conn.commit()
    return closed
