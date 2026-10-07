"""Scheduled jobs: sync sources, run follow-up rules, send the morning note and EOD report."""
from __future__ import annotations

import logging
import os
import time as _time
from datetime import date

from .assign import assign_new_leads
from .context import Ctx, iso
from .db import one
from .followups import check_overdue, create_system_followups
from .heat import refresh_heat
from .ingest import sync_leads, sync_webengage
from .notify import notify
from .reports import eod_report, morning_note

log = logging.getLogger(__name__)


def sync(ctx: Ctx) -> dict:
    """One full pass: sheet + WebEngage -> heat scores -> assignment -> system follow-ups -> overdue checks."""
    result = {}
    try:
        result["leads"] = sync_leads(ctx)
    except Exception as e:  # a sheet outage must not stop follow-up checks
        log.exception("Lead sync failed")
        result["leads"] = {"error": str(e)}
    try:
        result["webengage"] = sync_webengage(ctx)
    except Exception as e:
        log.exception("WebEngage sync failed")
        result["webengage"] = {"error": str(e)}
    result["heat"] = refresh_heat(ctx)
    result["assignment"] = assign_new_leads(ctx)
    result["system_followups"] = create_system_followups(ctx)
    result["overdue"] = check_overdue(ctx)
    return result


def _ran(ctx: Ctx, job: str, day: date | str) -> bool:
    key = day if isinstance(day, str) else day.isoformat()
    return one(ctx.conn, "SELECT 1 FROM job_runs WHERE job=? AND day=?", (job, key)) is not None


def _mark(ctx: Ctx, job: str, day: date | str) -> None:
    key = day if isinstance(day, str) else day.isoformat()
    ctx.conn.execute("INSERT INTO job_runs (job, day, ran_at) VALUES (?,?,?) ON CONFLICT (job, day) DO UPDATE SET ran_at=excluded.ran_at",
                     (job, key, iso(ctx.now())))
    ctx.conn.commit()


def send_morning_notes(ctx: Ctx, day: date | None = None) -> int:
    day = day or ctx.today()
    for member in ctx.team():
        subject, body = morning_note(ctx, member, day)
        notify(ctx, member["id"], "morning_note", subject, body)
    _mark(ctx, "morning_note", day)
    return len(ctx.team())


def send_eod_reports(ctx: Ctx, day: date | None = None) -> int:
    day = day or ctx.today()
    check_overdue(ctx)
    for member in ctx.team():
        subject, body = eod_report(ctx, day, member)
        notify(ctx, member["id"], "eod_report", subject, body)
    reports_dir = ctx.path(ctx.cfg.get("reports_dir", "data/reports"))
    reports_dir.mkdir(parents=True, exist_ok=True)
    for m in ctx.managers():
        subject, body = eod_report(ctx, day, m)
        (reports_dir / f"eod_{day.isoformat()}_{m['id']}.md").write_text(f"# {subject}\n\n{body}\n", encoding="utf-8")
    _mark(ctx, "eod_report", day)
    return len(ctx.team())


def tick(ctx: Ctx) -> dict:
    """What the scheduler does every few minutes."""
    now, today = ctx.now(), ctx.today()
    result = {}
    # Inside the office network only (CLIENT_DB_PULL=1): refresh no-deposit clients once an hour.
    if os.environ.get("CLIENT_DB_PULL") == "1" and not _ran(ctx, "client_db", now.strftime("%Y-%m-%dT%H")):
        from .clientdb import sync_new_clients

        try:
            result["client_db"] = sync_new_clients(ctx)
            _mark(ctx, "client_db", now.strftime("%Y-%m-%dT%H"))
        except Exception as e:  # server unreachable must not stop follow-up checks
            log.exception("Client DB pull failed")
            result["client_db"] = {"error": str(e)}
    result["sync"] = sync(ctx)
    # Refresh the customer priority sheet / CSV (hourly by default).
    every = int(ctx.cfg.get("outputs", {}).get("export_every_minutes", 60))
    slot = now.replace(minute=(now.minute // every) * every if every < 60 else 0).strftime("%Y-%m-%dT%H:%M")
    if ctx.cfg.get("outputs") and not _ran(ctx, "export", slot):
        from .sheet_export import export

        try:
            result["export"] = export(ctx)
            _mark(ctx, "export", slot)
        except Exception as e:
            log.exception("Customer list export failed")
            result["export"] = {"error": str(e)}
    if ctx.is_working_day(today):
        if now >= ctx.day_start(today) and not _ran(ctx, "morning_note", today):
            result["morning_notes"] = send_morning_notes(ctx, today)
        if now >= ctx.day_end(today) and not _ran(ctx, "eod_report", today):
            result["eod_reports"] = send_eod_reports(ctx, today)
    return result


def loop(ctx: Ctx) -> None:
    interval = 60 * ctx.rules.get("sync_interval_minutes", 5)
    log.info("Scheduler started; running every %s s", interval)
    while True:
        try:
            log.info("tick: %s", tick(ctx))
        except Exception:
            log.exception("tick failed")
        _time.sleep(interval)
