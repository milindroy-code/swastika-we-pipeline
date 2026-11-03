"""Calls made from Jarvis (the CRM), copied into the dashboard so the lists and the score know who was called.

Source: [Repl_CRMLive].[dbo].[tbl_CallLog] (one row per call: client code, start time, duration in seconds, who called,
incoming or outgoing), caller names from [Repl_CRMLive].[CloneHRM].[Employee]. Read-only. Only calls to the dashboard's
own clients are kept, for `keep_days`. Each sync re-reads the last couple of hours, since a call's row can be updated
after it ends.
"""
from __future__ import annotations

import logging
from datetime import timedelta

from .context import Ctx, iso, parse_dt
from .db import one, rows

log = logging.getLogger(__name__)

CALLS_SQL = """
SELECT L.Id, UPPER(LTRIM(RTRIM(L.ClientCode))) AS ClientCode, L.StartTime, ISNULL(L.CallDuration, 0) AS CallDuration,
       L.CreatedBy, E.EmployeeName, ISNULL(CAST(L.IsIncomingCall AS INT), 0) AS Incoming, L.Disposition
FROM [Repl_CRMLive].[dbo].[tbl_CallLog] L WITH (NOLOCK)
LEFT JOIN [Repl_CRMLive].[CloneHRM].[Employee] E WITH (NOLOCK) ON E.EmployeeId = L.CreatedBy
WHERE L.StartTime >= '{since}' AND L.StartTime <= GETDATE() AND ISNULL(L.ClientCode, '') <> ''"""


def settings(ctx: Ctx) -> dict:
    return {"keep_days": 30, "first_days": 8, "overlap_hours": 2, **(ctx.cfg.get("jarvis_calls") or {})}


def sync_calls(ctx: Ctx) -> dict:
    """Copy new Jarvis calls for the dashboard's clients. Needs the client database (office network)."""
    from .clientdb import _query, available, reachable

    if not (available(ctx) and reachable(ctx)):
        return {"status": "skipped", "detail": "client database not reachable"}
    s, now = settings(ctx), ctx.now()
    last = one(ctx.conn, "SELECT MAX(started_at) AS t FROM jarvis_calls")
    last_t = parse_dt(last["t"], ctx.tz) if last and last["t"] else None
    since = (last_t - timedelta(hours=s["overlap_hours"])) if last_t else now - timedelta(days=s["first_days"])
    df = _query(ctx, CALLS_SQL.format(since=since.strftime("%Y-%m-%d %H:%M:%S")))
    clients = {r["lead_id"].upper() for r in rows(ctx.conn, "SELECT lead_id FROM leads")}
    clients |= {r["lead_id"].upper() for r in rows(ctx.conn, "SELECT lead_id FROM drive_members")}
    kept = 0
    for r in df.itertuples(index=False):
        if r.ClientCode not in clients:
            continue
        t = parse_dt(str(r.StartTime), ctx.tz)
        if not t:
            continue
        ctx.conn.execute(
            "INSERT INTO jarvis_calls (id, client_id, started_at, duration, caller_id, caller, incoming, disposition) "
            "VALUES (?,?,?,?,?,?,?,?) ON CONFLICT (id) DO UPDATE SET duration=excluded.duration, disposition=excluded.disposition",
            (int(r.Id), r.ClientCode, iso(t), int(r.CallDuration or 0), int(r.CreatedBy) if r.CreatedBy == r.CreatedBy and r.CreatedBy else None,
             r.EmployeeName if isinstance(r.EmployeeName, str) else None, int(r.Incoming or 0),
             r.Disposition if isinstance(r.Disposition, str) else None))
        kept += 1
    ctx.conn.execute("DELETE FROM jarvis_calls WHERE started_at < ?", (iso(now - timedelta(days=s["keep_days"])),))
    ctx.conn.commit()
    return {"status": "ok", "read": len(df), "kept": kept, "since": iso(since)}
