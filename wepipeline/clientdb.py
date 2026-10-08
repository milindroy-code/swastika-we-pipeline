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


def _load_windows_user_env() -> None:
    """On Windows, `setx` stores credentials in the user profile, but programs started earlier (like the
    dashboard) don't see them. Read them from the profile when they're missing from the environment."""
    if os.name != "nt" or os.environ.get("CLIENT_DB_USER"):
        return
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            for name in ("CLIENT_DB_USER", "CLIENT_DB_PASSWORD", "CLIENT_DB_CONN"):
                try:
                    os.environ.setdefault(name, winreg.QueryValueEx(key, name)[0])
                except FileNotFoundError:
                    pass
    except OSError:
        pass


def available(ctx: Ctx) -> bool:
    """True when a client database is configured (it may still be unreachable from this machine)."""
    _load_windows_user_env()
    return bool(os.environ.get("CLIENT_DB_CONN") or (_cfg(ctx).get("enabled", True) and _cfg(ctx).get("server")))


def reachable(ctx: Ctx, timeout: float = 3.0) -> bool:
    """Quick network check (a few seconds) before an ODBC login that would otherwise hang for ~20 s per try."""
    import socket

    server = _cfg(ctx).get("server") or ""
    if not server or os.environ.get("CLIENT_DB_CONN"):
        return bool(os.environ.get("CLIENT_DB_CONN"))
    host, _, port = server.replace("tcp:", "").partition(",")
    host = host.split("\\")[0]  # named instance: HOST\INSTANCE
    try:
        with socket.create_connection((host, int(port or 1433)), timeout=timeout):
            return True
    except OSError:
        return False


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
    _load_windows_user_env()
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
    return [(7, "New - no deposit (0-7 days)"), (15, "New - no deposit (8-15 days)"), (30, "New - no deposit (16-30 days)"),
            (90, "No deposit (31-90 days)"), (10**6, "No deposit (90+ days)")]


NO_DEPOSIT_CATEGORIES = {label for _, label in _bucket_spec()}


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
    ages = [bucket(max((now.date() - d.date()).days, 0)) for d in seen.values()]
    buckets = {label: ages.count(label) for _, label in _bucket_spec() if label in ages}
    return {"clients": len(seen), "new_leads": new, "refreshed": refreshed, "deposited_closed": closed, "buckets": buckets}


def close_deposited(ctx: Ctx, deposits: pd.DataFrame) -> int:
    """No-deposit leads that have now made their first deposit are converted: no more calls for a first deposit.
    Leads in other categories (existing clients being re-activated) are not touched."""
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
    for lead in rows(ctx.conn, "SELECT lead_id, we_user_id, assigned_to, category FROM leads WHERE status='open'"):
        cid = lead["we_user_id"] or lead["lead_id"]
        if cid not in deposited or lead["category"] not in NO_DEPOSIT_CATEGORIES:
            continue
        ctx.conn.execute("UPDATE leads SET status='converted', last_outcome='deposited', updated_at=? WHERE lead_id=?",
                         (now, lead["lead_id"]))
        ctx.conn.execute("UPDATE followups SET status='cancelled', completed_at=?, reason=COALESCE(reason,'') || ' (closed: first deposit made)' "
                         "WHERE lead_id=? AND status='pending'", (now, lead["lead_id"]))
        closed += 1
    ctx.conn.commit()
    return closed


# ---------------------------------------------------------------- ageing for every known client
LOOKUP_SQL = """
SELECT C.CLIENT_ID, C.REGISTRATION_DATE, C.AccountStatus, C.IsAccountClosed,
       C.IsSarthiTIOn, C.SarthiActivationDate, C.PreferredSegment,
       F.FirstDeposit, F.FirstDepositDate, F.FTD, F.LTD, F.Holdings
FROM [Repl_TargetReview].[dbo].[tbl_ClientNew] C
LEFT JOIN [Repl_TargetReview].[dbo].[tbl_ClientFtdLtd] F ON C.CLIENT_ID = F.CLIENT_ID
WHERE C.CLIENT_ID IN ({ids})
"""


def lookup_clients(ctx: Ctx, client_ids: list[str]) -> pd.DataFrame:
    """Registration, deposit and trade dates for specific clients (read-only, in chunks under SQL Server's 2,100-parameter limit)."""
    import pyodbc

    sql = _cfg(ctx).get("lookup_query") or LOOKUP_SQL
    frames = []
    with pyodbc.connect(connection_string(ctx), timeout=20, readonly=True) as conn:
        cur = conn.cursor()
        for i in range(0, len(client_ids), 1000):
            chunk = client_ids[i:i + 1000]
            cur.execute(sql.format(ids=",".join("?" * len(chunk))), chunk)
            cols = [c[0] for c in cur.description]
            frames.append(pd.DataFrame.from_records(cur.fetchall(), columns=cols))
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def _num(v) -> float:
    try:
        return float(v) if v is not None and str(v).strip() not in {"", "nan", "None", "NaT"} else 0.0
    except (TypeError, ValueError):
        return 0.0


def classify(ctx: Ctx, rec: dict) -> dict:
    """Ageing category, pitch stage and facts for one client row from the lookup query."""
    now = ctx.now()
    reg = parse_dt(rec.get("REGISTRATION_DATE"), ctx.tz)
    ftd, ltd = parse_dt(rec.get("FTD"), ctx.tz), parse_dt(rec.get("LTD"), ctx.tz)
    fdd = parse_dt(rec.get("FirstDepositDate"), ctx.tz)
    deposited = _num(rec.get("FirstDeposit")) > 0 or fdd is not None
    reg_days = max((now.date() - reg.date()).days, 0) if reg else None
    ltd_days = (now.date() - ltd.date()).days if ltd else None
    if ftd or ltd:
        if ltd_days is not None and ltd_days <= 30:
            category, stage = "Traded - active (last 30 days)", "active_trader"
        else:
            category, stage = "Traded - dormant (30+ days)", "dormant_trader"
    elif deposited:
        category, stage = "Deposited - no trade yet", "funded_no_trade"
    else:
        category, stage = bucket(reg_days if reg_days is not None else 10**6), "kyc_no_deposit"
    return {
        "category": category,
        "stage": stage,
        "closed": str(rec.get("IsAccountClosed") or "").strip().lower() in {"1", "true", "y", "yes"},
        "registration": reg,
        "extra": {
            "registration_date": iso(reg), "days_since_registration": reg_days, "stage": stage,
            "deposit_status": "deposited" if deposited else "no deposit",
            "first_deposit": _num(rec.get("FirstDeposit")) or None, "first_deposit_date": iso(fdd),
            "first_trade_date": iso(ftd), "last_trade_date": iso(ltd), "days_since_last_trade": ltd_days,
            "holdings": _num(rec.get("Holdings")) or None, "account_status": rec.get("AccountStatus") or "",
            # client found: a blank flag means Sarthi was never switched on
            "sarthi_active": str(rec.get("IsSarthiTIOn")).strip().lower() in {"1", "true", "y", "yes"},
            "sarthi_activated_on": iso(parse_dt(rec.get("SarthiActivationDate"), ctx.tz)),
            "preferred_segment": str(rec.get("PreferredSegment") or "").strip(),
        },
    }


LOOKUP_FIELDS = ["CLIENT_ID", "REGISTRATION_DATE", "AccountStatus", "IsAccountClosed", "FirstDeposit",
                 "FirstDepositDate", "FTD", "LTD", "Holdings", "IsSarthiTIOn", "SarthiActivationDate", "PreferredSegment"]


def enrich_leads(ctx: Ctx, lookup: pd.DataFrame | None = None) -> dict:
    """Give every open lead its ageing category from the client database, whatever source it came from."""
    leads = rows(ctx.conn, "SELECT lead_id, we_user_id, category, source, extra FROM leads WHERE status='open'")
    if not leads:
        return {"looked_up": 0}
    ids = sorted({(l["we_user_id"] or l["lead_id"]).strip() for l in leads})
    if lookup is None:
        lookup = lookup_clients(ctx, ids)
    found = {}
    if not lookup.empty:
        cols = {k.lower(): k for k in lookup.columns}
        for r in lookup.to_dict("records"):
            rec = {name: r.get(cols.get(name.lower())) for name in LOOKUP_FIELDS}
            found.setdefault(str(rec["CLIENT_ID"]).strip(), rec)
    now = iso(ctx.now())
    counts, closed = {}, 0
    for lead in leads:
        cid = (lead["we_user_id"] or lead["lead_id"]).strip()
        extra = json.loads(lead["extra"] or "{}")
        if cid not in found:
            category = "Not found in client DB"
        else:
            info = classify(ctx, found[cid])
            if info["closed"]:
                ctx.conn.execute("UPDATE leads SET status='invalid', last_outcome='account_closed', updated_at=? WHERE lead_id=?",
                                 (now, lead["lead_id"]))
                ctx.conn.execute("UPDATE followups SET status='cancelled', completed_at=? WHERE lead_id=? AND status='pending'",
                                 (now, lead["lead_id"]))
                closed += 1
                continue
            category = info["category"]
            activated_here = extra.get("sarthi_activated_by")  # a GM switched it on; the replica may not show it yet
            extra.update(info["extra"])
            if activated_here and not extra.get("sarthi_active"):
                extra["sarthi_active"] = True
            if info["registration"]:
                ctx.conn.execute(
                    """INSERT INTO activity_summary (user_id, activity, count, last_at, activity_type, source_file) VALUES (?,?,?,?,?,?)
                       ON CONFLICT (user_id, activity) DO UPDATE SET last_at=excluded.last_at, source_file=excluded.source_file""",
                    (cid, REGISTERED, 1, iso(info["registration"]), "", "client_db"),
                )
        automated = lead["source"] == SOURCE or (lead["source"] or "").startswith("WebEngage activity")  # uploads included
        ctx.conn.execute("UPDATE leads SET category=?, extra=?, updated_at=? WHERE lead_id=?",
                         (category if automated else lead["category"], json.dumps(extra), now, lead["lead_id"]))
        counts[category] = counts.get(category, 0) + 1
    ctx.conn.commit()
    return {"looked_up": len(ids), "found": len(found), "closed_accounts": closed, "categories": dict(sorted(counts.items()))}


def refresh_from_client_db(ctx: Ctx) -> dict:
    """Pull new no-deposit clients, age every lead, and refresh Sarthi's track record from the research database."""
    from .sarthi import pull_track

    result = {"new_clients": sync_new_clients(ctx), "ageing": enrich_leads(ctx)}
    try:
        result["sarthi_track"] = {k: v for k, v in pull_track(ctx).items() if k in ("period", "segments")}
    except Exception as e:  # research database unavailable: keep the last good record
        log.warning("Sarthi track record not refreshed: %s", e)
        result["sarthi_track"] = {"error": str(e)}
    return result
