"""New clients who have not made their first deposit, pulled from the back-office SQL Server.

Runs on a machine inside the office network (the SQL Server is not reachable from the cloud).
It writes into the same database the dashboard uses, so it can feed the hosted app too.
"""
from __future__ import annotations

import json
import logging
import os

import pandas as pd

from .context import Ctx, iso, norm_client_id, parse_dt
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


# ---------------------------------------------------------------- pull a list's clients (for the WebEngage team)
NOT_CLOSED = "ISNULL(CAST(C.IsAccountClosed AS VARCHAR(10)), '0') NOT IN ('1', 'True', 'true')"
LIST_SQL = {
    # funded, but the first deposit is below the funding target
    "low_margin": f"""
SELECT C.CLIENT_ID, C.REGISTRATION_DATE, F.FirstDeposit, F.FirstDepositDate,
       DATEDIFF(DAY, C.REGISTRATION_DATE, GETDATE()) AS DaysSinceRegistration
FROM [Repl_TargetReview].[dbo].[tbl_ClientNew] C
JOIN [Repl_TargetReview].[dbo].[tbl_ClientFtdLtd] F ON C.CLIENT_ID = F.CLIENT_ID
WHERE C.REGISTRATION_DATE >= DATEADD(DAY, -{{window_days}}, CAST(GETDATE() AS DATE))
  AND F.FirstDeposit > 0 AND F.FirstDeposit < {{target}} AND {NOT_CLOSED}
ORDER BY DaysSinceRegistration ASC""",
    # a payout of more than payout_share of the balance just before it, in the last payout_days. Balance = pay-ins (TRANS_TYPE
    # 'r', CR_AMT) minus payouts ('p', DR_AMT) over balance_days, from the back-office ledger (FA_TRANSACTIONS), as the
    # payout team's own query does. It counts payouts that went through, not requests that were rejected.
    "payout": f"""
SELECT P.ACCOUNTCODE AS CLIENT_ID, C.REGISTRATION_DATE, P.LastPayoutDate, P.PayoutAmount, P.BalanceBeforePayout,
       CAST(ROUND(100 * P.LargestShareOfBalance, 0) AS INT) AS PayoutPctOfBalance
FROM (
    SELECT R.ACCOUNTCODE, MAX(R.VOUCHERDATE) AS LastPayoutDate, SUM(R.DR) AS PayoutAmount, MAX(R.BalanceBefore) AS BalanceBeforePayout,
           MAX(R.DR / NULLIF(R.BalanceBefore, 0)) AS LargestShareOfBalance
    FROM (
        SELECT T.*, SUM(T.CR - T.DR) OVER (PARTITION BY T.ACCOUNTCODE ORDER BY T.VOUCHERDATE, T.TRANS_TYPE DESC
                                           ROWS UNBOUNDED PRECEDING) - (T.CR - T.DR) AS BalanceBefore
        FROM (
            SELECT ACCOUNTCODE, VOUCHERDATE, TRANS_TYPE, ISNULL(CR_AMT, 0) AS CR, ISNULL(DR_AMT, 0) AS DR
            FROM [192.168.0.18].Capsfo.dbo.FA_TRANSACTIONS
            WHERE TRANS_TYPE IN ('p', 'r') AND VOUCHERDATE >= DATEADD(DAY, -{{balance_days}}, CAST(GETDATE() AS DATE))
        ) T
    ) R
    WHERE R.TRANS_TYPE = 'p' AND R.DR > 0 AND R.BalanceBefore > 0 AND R.DR > {{payout_share}} * R.BalanceBefore
      AND R.VOUCHERDATE >= DATEADD(DAY, -{{payout_days}}, CAST(GETDATE() AS DATE))
    GROUP BY R.ACCOUNTCODE
) P
JOIN [Repl_TargetReview].[dbo].[tbl_ClientNew] C ON C.CLIENT_ID = P.ACCOUNTCODE
WHERE {NOT_CLOSED}
ORDER BY P.LastPayoutDate DESC""",
    # switched Sarthi off within sarthi_off_days (the activation history's latest "off") and still off
    "sarthi_disabled": f"""
SELECT C.CLIENT_ID, C.REGISTRATION_DATE, D.SarthiOffOn, F.FirstDeposit, F.LTD AS LastTradeDate
FROM (
    SELECT ClientCode, MAX(CreatedOn) AS SarthiOffOn FROM [Repl_TargetReview].[dbo].[tbl_SarthiActivationHistory]
    WHERE SarthiActivationFlag = 0 AND CreatedOn >= DATEADD(DAY, -{{sarthi_off_days}}, GETDATE()) GROUP BY ClientCode
) D
JOIN [Repl_TargetReview].[dbo].[tbl_ClientNew] C ON C.CLIENT_ID = D.ClientCode
LEFT JOIN [Repl_TargetReview].[dbo].[tbl_ClientFtdLtd] F ON C.CLIENT_ID = F.CLIENT_ID
WHERE ISNULL(CAST(C.IsSarthiTIOn AS INT), 0) = 0 AND {NOT_CLOSED}
ORDER BY D.SarthiOffOn DESC""",
    # Sarthi not switched on, registered within sarthi_new_days; a recent switch-off is on the list above instead
    "sarthi_off": f"""
SELECT C.CLIENT_ID, C.REGISTRATION_DATE, F.FirstDeposit, F.LTD AS LastTradeDate
FROM [Repl_TargetReview].[dbo].[tbl_ClientNew] C
LEFT JOIN [Repl_TargetReview].[dbo].[tbl_ClientFtdLtd] F ON C.CLIENT_ID = F.CLIENT_ID
WHERE ISNULL(CAST(C.IsSarthiTIOn AS INT), 0) = 0 AND {NOT_CLOSED}
  AND C.REGISTRATION_DATE >= DATEADD(DAY, -{{sarthi_new_days}}, CAST(GETDATE() AS DATE))
  AND NOT EXISTS (SELECT 1 FROM [Repl_TargetReview].[dbo].[tbl_SarthiActivationHistory] H WHERE H.ClientCode = C.CLIENT_ID
                  AND H.SarthiActivationFlag = 0 AND H.CreatedOn >= DATEADD(DAY, -{{sarthi_off_days}}, GETDATE()))
ORDER BY C.REGISTRATION_DATE DESC""",
    # traded before, no trade in the last churn_min_days but one within churn_max_days
    "reactivation": f"""
SELECT C.CLIENT_ID, C.REGISTRATION_DATE, F.LTD AS LastTradeDate, DATEDIFF(DAY, F.LTD, GETDATE()) AS DaysSinceLastTrade
FROM [Repl_TargetReview].[dbo].[tbl_ClientNew] C
JOIN [Repl_TargetReview].[dbo].[tbl_ClientFtdLtd] F ON C.CLIENT_ID = F.CLIENT_ID
WHERE F.LTD < DATEADD(DAY, -{{churn_min_days}}, CAST(GETDATE() AS DATE))
  AND F.LTD >= DATEADD(DAY, -{{churn_max_days}}, CAST(GETDATE() AS DATE)) AND {NOT_CLOSED}
ORDER BY F.LTD DESC""",
}


def list_query(ctx: Ctx, list_type: str) -> str | None:
    """The read-only query behind a list type, with its parameters filled in. None when there is no query yet (payouts)."""
    from .sarthi import target, window_days

    c = _cfg(ctx)
    r = ctx.cfg.get("reactivation") or {}
    p = ctx.cfg.get("payout") or {}
    sl = ctx.cfg.get("sarthi_lists") or {}
    sql = {"no_deposit": c.get("new_clients_query"), "low_margin": c.get("low_margin_query") or LIST_SQL["low_margin"],
           "reactivation": c.get("reactivation_query") or LIST_SQL["reactivation"],
           "payout": c.get("payout_query") or LIST_SQL["payout"],
           "sarthi_disabled": LIST_SQL["sarthi_disabled"], "sarthi_off": LIST_SQL["sarthi_off"]}.get(list_type)
    if not sql:
        return None
    for k, v in {"window_days": window_days(ctx), "target": int(target(ctx)), "churn_min_days": int(r.get("churn_min_days", 90)),
                 "churn_max_days": int(r.get("churn_max_days", 365)), "payout_days": int(p.get("pull_days", 7)),
                 "payout_share": float(p.get("share_of_balance", 0.25)), "balance_days": int(p.get("balance_days", 365)),
                 "sarthi_off_days": int(sl.get("off_days", 15)), "sarthi_new_days": int(sl.get("new_days", 30))}.items():
        sql = sql.replace("{" + k + "}", str(v))
    return sql


CENTRAL_DEFAULTS = {"direct_only": True, "prefixes": ["DT"],
                    "sub_branches": ["BPL", "SHRSHP", "GO", "PUSS", "HRN", "HOBR", "HORET", "G-ZSM2", "G-ZSM1", "G-ASM1", "G-ASM2",
                                     "PUDS", "PUVT", "TB", "TELE", "GOVPD"]}


def centralised_sql(ctx: Ctx, alias: str = "C") -> str:
    """1 / 0: is the client centralised? The business "Vertical" rule from the payout and funding reports: a direct
    client (IsDirect = 1; partnership clients never are) whose sub-branch is one of the centralised ones (config
    `centralised`). One definition for activation lists, payouts and the referral drive."""
    c = {**CENTRAL_DEFAULTS, **(ctx.cfg.get("centralised") or {})}
    safe = lambda v: str(v).replace("'", "''")  # noqa: E731
    names = ", ".join(f"'{safe(b)}'" for b in c["sub_branches"])
    branch = [f"{alias}.Sub_Branch_Code IN ({names})"] if names else []
    branch += [f"{alias}.Sub_Branch_Code LIKE '{safe(p)}%'" for p in c["prefixes"]]
    cond = "(" + " OR ".join(branch or ["1 = 0"]) + ")"
    if c["direct_only"]:
        cond = f"ISNULL(CAST({alias}.IsDirect AS INT), 0) = 1 AND {cond}"
    return f"CASE WHEN {cond} THEN 1 ELSE 0 END"


def with_centralised_flag(ctx: Ctx, sql: str) -> str:
    """Wrap a list query so every row also says whether the client is centralised (centralised_sql)."""
    inner = sql.strip().rstrip(";")
    cut = inner.upper().rfind("ORDER BY")
    if cut > inner.rfind(")"):  # a derived table can't keep a trailing ORDER BY
        inner = inner[:cut]
    return f"""SELECT L.*, ISNULL((SELECT TOP 1 {centralised_sql(ctx, "C2")} FROM [Repl_TargetReview].[dbo].[tbl_ClientNew] C2
    WHERE C2.CLIENT_ID = L.CLIENT_ID), 0) AS IsCentralized
FROM ({inner}) L"""


def pull_list(ctx: Ctx, list_type: str) -> pd.DataFrame:
    """The clients a list type covers, straight from the client database: one row per client, CLIENT_ID first,
    with an IsCentralized flag (1 / 0) so the page can show all clients or centralised ones only."""
    sql = list_query(ctx, list_type)
    if not sql:
        raise ValueError("There's no client-database query for this list yet. Upload the list as a file instead.")
    df = _query(ctx, with_centralised_flag(ctx, sql))
    cols = {k.lower(): k for k in df.columns}
    if "client_id" not in cols:
        raise ValueError(f"The query must return CLIENT_ID (got {list(df.columns)})")
    df = df.rename(columns={cols["client_id"]: "CLIENT_ID"})
    df["CLIENT_ID"] = df["CLIENT_ID"].map(norm_client_id)
    df = df[df["CLIENT_ID"] != ""].drop_duplicates("CLIENT_ID")
    return df[["CLIENT_ID"] + [c for c in df.columns if c != "CLIENT_ID"]].reset_index(drop=True)


ID_COLUMNS = ("CLIENT_ID", "Client code", "ClientCode", "Client ID", "UCC", "Code", "Customer ID", "User ID", "CUID")


def read_client_ids(df: pd.DataFrame) -> list[str]:
    """Client IDs from an uploaded list: a client-code column if one is named so, else the first column. Deduplicated."""
    from .drives import col

    c = col(df, *ID_COLUMNS) or (df.columns[0] if len(df.columns) else None)
    if c is None:
        raise ValueError("The file is empty.")
    ids = [norm_client_id(v) for v in df[c].tolist()]
    return list(dict.fromkeys(i for i in ids if i and i.upper() not in {"NAN", "NONE", "NULL"}))


def check_custom_list(ctx: Ctx, client_ids: list[str]) -> pd.DataFrame:
    """Your own list of client IDs, matched in the client database (read-only): registration, deposit, last trade and
    whether each client is centralised. IDs the database doesn't know stay in, marked FoundInClientDB = 0."""
    import pyodbc

    sql = with_centralised_flag(ctx, LOOKUP_SQL)
    frames = []
    with pyodbc.connect(connection_string(ctx), timeout=20, readonly=True) as conn:
        cur = conn.cursor()
        for i in range(0, len(client_ids), 1000):
            chunk = client_ids[i:i + 1000]
            cur.execute(sql.format(ids=",".join("?" * len(chunk))), chunk)
            frames.append(pd.DataFrame.from_records(cur.fetchall(), columns=[c[0] for c in cur.description]))
    found = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=["CLIENT_ID", "IsCentralized"])
    found["CLIENT_ID"] = found["CLIENT_ID"].map(norm_client_id)
    found = found.drop_duplicates("CLIENT_ID")
    out = pd.DataFrame({"CLIENT_ID": client_ids}).merge(found, on="CLIENT_ID", how="left")
    out["FoundInClientDB"] = out["CLIENT_ID"].isin(set(found["CLIENT_ID"])).astype(int)
    out["IsCentralized"] = out["IsCentralized"].fillna(0).astype(int)
    keep = ["CLIENT_ID", "FoundInClientDB", "IsCentralized", "REGISTRATION_DATE", "FirstDeposit", "FirstDepositDate", "LTD", "IsSarthiTIOn"]
    return out[[c for c in keep if c in out.columns]]


def sync_new_clients(ctx: Ctx, clients: pd.DataFrame | None = None, deposits: pd.DataFrame | None = None) -> dict:
    """Add/refresh no-deposit clients as leads and close leads who have since deposited."""
    c = _cfg(ctx)
    if clients is None:
        from .sarthi import window_days

        clients = _query(ctx, c["new_clients_query"].replace("{window_days}", str(window_days(ctx))))
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
        cid = norm_client_id(r[id_col])
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
    amt_col = cols.get("firstdeposit")
    deposited = {norm_client_id(r[id_col]): (_num(r.get(amt_col)) if amt_col else None) for r in deposits.to_dict("records")}
    from .sarthi import target

    goal = target(ctx)
    now = iso(ctx.now())
    closed = 0
    for lead in rows(ctx.conn, "SELECT lead_id, we_user_id, assigned_to, category FROM leads WHERE status='open'"):
        cid = lead["we_user_id"] or lead["lead_id"]
        if cid not in deposited or lead["category"] not in NO_DEPOSIT_CATEGORIES:
            continue
        amount = deposited[cid]
        if amount and goal and amount < goal:  # funded, but below the target: keep working them as low margin
            extra = json.loads(one(ctx.conn, "SELECT extra FROM leads WHERE lead_id=?", (lead["lead_id"],))["extra"] or "{}")
            extra.update(current_margin=amount, deposit_status="deposited")
            ctx.conn.execute("UPDATE leads SET campaign='low_margin', category='Deposited - no trade yet', extra=?, updated_at=? "
                             "WHERE lead_id=?", (json.dumps(extra), now, lead["lead_id"]))
            closed += 1
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


def campaign_for(category: str) -> str:
    """List type for a client that arrived without one (older uploads, the client-DB pull), from its DB category."""
    if category.startswith("Traded - dormant"):
        return "reactivation"
    if category.startswith(("Traded - active", "Deposited")):
        return "low_margin"  # already funded: the goal is topping up to the target
    return "new"


def fill_missing_campaigns(ctx: Ctx) -> int:
    """Give open clients without a list type one from the category they already carry (no client-DB call needed)."""
    filled = 0
    for lead in rows(ctx.conn, "SELECT lead_id, category FROM leads WHERE status='open' AND COALESCE(campaign,'')=''"):
        ctx.conn.execute("UPDATE leads SET campaign=? WHERE lead_id=?", (campaign_for(lead["category"] or ""), lead["lead_id"]))
        filled += 1
    ctx.conn.commit()
    return filled


def enrich_leads(ctx: Ctx, lookup: pd.DataFrame | None = None) -> dict:
    """Give every open lead its ageing category from the client database, whatever source it came from."""
    ageing = NO_DEPOSIT_CATEGORIES | {"Deposited - no trade yet", "Traded - dormant (30+ days)", "Traded - active (last 30 days)",
                                     "Not found in client DB"}
    marks = ",".join("?" * len(ageing))
    # drive-only clients (referral / Sarthi pools, status 'drive') keep their own profiles: never aged as funding leads
    leads = rows(ctx.conn, f"SELECT lead_id, we_user_id, category, source, extra, status, campaign FROM leads "
                           f"WHERE status<>'drive' AND (status='open' OR category NOT IN ({marks}))", tuple(sorted(ageing)))
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
            found.setdefault(norm_client_id(rec["CLIENT_ID"]), rec)
    from .sarthi import target

    now = iso(ctx.now())
    counts, closed = {}, 0
    for lead in leads:
        cid = norm_client_id(lead["we_user_id"] or lead["lead_id"])
        extra = json.loads(lead["extra"] or "{}")
        if cid not in found:
            category = "Not found in client DB"
            if lead["status"] != "open":
                ctx.conn.execute("UPDATE leads SET category=? WHERE lead_id=?", (category, lead["lead_id"]))
                continue
        else:
            info = classify(ctx, found[cid])
            if lead["status"] != "open":  # closed lead with an old label: just give it its ageing category
                ctx.conn.execute("UPDATE leads SET category=? WHERE lead_id=?", (info["category"], lead["lead_id"]))
                continue
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
            campaign = lead["campaign"] or campaign_for(category)
            first = info["extra"].get("first_deposit") or 0
            if campaign in {"new", "low_margin"} and info["extra"]["deposit_status"] == "deposited":
                goal = target(ctx)
                margin = max(float(extra.get("current_margin") or 0), first)
                if goal and margin >= goal:  # funded at the target: done
                    ctx.conn.execute("UPDATE leads SET status='converted', last_outcome='deposited', category=?, extra=?, "
                                     "updated_at=? WHERE lead_id=?", (category, json.dumps(extra), now, lead["lead_id"]))
                    ctx.conn.execute("UPDATE followups SET status='cancelled', completed_at=?, reason=COALESCE(reason,'') || "
                                     "' (closed: funded at the target)' WHERE lead_id=? AND status='pending'", (now, lead["lead_id"]))
                    counts[category] = counts.get(category, 0) + 1
                    continue
                extra["current_margin"] = margin  # funded below the target: top up
                lead["campaign"] = "low_margin"
            if info["registration"]:
                ctx.conn.execute(
                    """INSERT INTO activity_summary (user_id, activity, count, last_at, activity_type, source_file) VALUES (?,?,?,?,?,?)
                       ON CONFLICT (user_id, activity) DO UPDATE SET last_at=excluded.last_at, source_file=excluded.source_file""",
                    (cid, REGISTERED, 1, iso(info["registration"]), "", "client_db"),
                )
        automated = lead["source"] == SOURCE or (lead["source"] or "").startswith("WebEngage activity")  # uploads included
        ctx.conn.execute("UPDATE leads SET category=?, campaign=?, extra=?, updated_at=? WHERE lead_id=?",
                         (category if automated else lead["category"], lead["campaign"] or campaign_for(category),
                          json.dumps(extra), now, lead["lead_id"]))
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
