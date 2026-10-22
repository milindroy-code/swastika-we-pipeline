"""Daily cohort lists, ready before the calling day starts.

For each cohort (new clients without deposit, new clients with low margin, payouts, churned clients):
  1. pull the clients from the client database (the same read-only queries as "Get the client list"),
  2. find each client's WebEngage ID (CRM lead -> eKYC application),
  3. fetch their app activity from the WebEngage API, slowly (one call at a time, a pause between calls, back-off on errors),
  4. score and publish the list through the normal analysis (analyse_upload), held until an SM presses Assign.

The API is per client, so ~6,000 clients take hours. The worker (`python run.py cohorts`) fetches overnight from
`cohorts.fetch_at` and publishes each list at `cohorts.publish_at`, topping up anyone not fetched recently. A list can
also be run on demand from the dashboard, which queues it for the worker.
"""
from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

from .context import Ctx, iso, norm_client_id, parse_dt
from .db import one, rows

log = logging.getLogger(__name__)

COHORTS = ["no_deposit", "low_margin", "payout", "reactivation"]  # fetch / publish order: smallest and most urgent first
DEFAULTS = {"enabled": True, "cohorts": COHORTS, "centralised_only": True, "lookback_days": 15, "fresh_hours": 16, "refetch_after_hours": 4,
            "fetch_at": "20:00", "publish_at": "08:30", "publish_on_holidays": False,
            "night_only": ["reactivation"]}  # fetched only by the overnight run; daytime runs score what is already fetched
API_DEFAULTS = {"host": "https://dashboard.in.webengage.com", "account": "in~~991992ab", "delay_seconds": 4,
                "page_size": 2000, "env_file": "secrets/webengage.env"}
WE_ID_SQL = """
SELECT l.Client_Id AS CLIENT_ID, COALESCE(e.WebengageUniqueId, ea.WebengageUniqueId) AS WE_ID
FROM [Repl_CRMLive].[dbo].[tbl_Lead] l WITH (NOLOCK)
LEFT JOIN [Repl-eKyc].[dbo].[ekycapplication] e WITH (NOLOCK) ON e.id = l.eKYCId
LEFT JOIN [Repl-eKyc].[dbo].[ekycapplication_Auth] ea WITH (NOLOCK) ON ea.id = l.eKYCId
WHERE l.Client_Id IN ({ids})"""


def settings(ctx: Ctx) -> dict:
    return {**DEFAULTS, **(ctx.cfg.get("cohorts") or {})}


def api_settings(ctx: Ctx) -> dict:
    return {**API_DEFAULTS, **((ctx.cfg.get("sources") or {}).get("webengage_api") or {})}


# ---------------------------------------------------------------- WebEngage IDs
def lookup_we_ids(ctx: Ctx, client_ids: list[str]) -> dict[str, str]:
    """client code -> WebEngage unique ID (the ID the app logs the client in with), read-only, in chunks."""
    import pyodbc

    from .clientdb import connection_string

    sql = ((ctx.cfg.get("sources") or {}).get("client_db") or {}).get("we_id_query") or WE_ID_SQL
    found: dict[str, str] = {}
    with pyodbc.connect(connection_string(ctx), timeout=20, readonly=True) as conn:
        cur = conn.cursor()
        for i in range(0, len(client_ids), 1000):
            chunk = client_ids[i:i + 1000]
            cur.execute(sql.format(ids=",".join("?" * len(chunk))), chunk)
            for cid, we_id in cur.fetchall():
                if cid and we_id and str(we_id).strip():
                    found.setdefault(norm_client_id(cid), str(we_id).strip())
    return found


# ---------------------------------------------------------------- WebEngage API
def _load_env_file(ctx: Ctx) -> None:
    """WE_API_KEY from the environment, or from secrets/webengage.env (git-ignored) when it isn't set."""
    path = ctx.path(api_settings(ctx)["env_file"])
    if os.environ.get("WE_API_KEY") or not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        k, sep, v = line.strip().partition("=")
        if sep and not k.startswith("#") and v.strip():
            os.environ.setdefault(k.strip(), v.strip())


class Api:
    """The WebEngage user-activity API, one call at a time with a pause between calls, so the account is never flooded."""

    def __init__(self, ctx: Ctx, sleep=time.sleep):
        _load_env_file(ctx)
        self.s = api_settings(ctx)
        key = os.environ.get("WE_API_KEY", "")
        if not key:
            raise RuntimeError("WE_API_KEY is not set (System environment variable, or secrets/webengage.env).")
        self.headers = {"Accept": "application/json", "User-Agent": "growth-dashboards/1.0", "Authorization": f"Bearer {key}"}
        self.sleep, self._last, self.calls = sleep, 0.0, 0

    def _get(self, url: str) -> dict:
        for attempt in range(6):
            gap = float(self.s["delay_seconds"]) - (time.monotonic() - self._last)
            if gap > 0:
                self.sleep(gap)
            try:
                with urllib.request.urlopen(urllib.request.Request(url, headers=self.headers), timeout=60) as r:
                    return json.loads(r.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                if e.code not in (429, 500, 502, 503, 504):
                    raise RuntimeError(f"WebEngage HTTP {e.code}: {e.read()[:200]!r}") from None
                wait = int(e.headers.get("Retry-After") or 30 * 2 ** attempt)
                log.warning("WebEngage HTTP %s, waiting %ss", e.code, wait)
            except (urllib.error.URLError, TimeoutError, ConnectionError) as e:  # network drop: wait, then retry
                wait = 30 * 2 ** attempt
                log.warning("WebEngage unreachable (%s), waiting %ss", e, wait)
            finally:
                self._last, self.calls = time.monotonic(), self.calls + 1
            self.sleep(wait)
        raise RuntimeError("WebEngage did not answer after 6 tries")

    def activities(self, we_id: str, start: datetime, end: datetime) -> list[dict]:
        """Every activity between start and end (newest first from the API). A window that comes back full is split in two."""
        stamp = lambda d: d.strftime("%Y-%m-%dT%H:%M:%S.") + f"{d.microsecond // 1000:03d}" + d.strftime("%z")[:3] + ":" + d.strftime("%z")[3:]  # noqa: E731
        q = {"from": stamp(end), "to": stamp(start), "size": self.s["page_size"], "cuid": we_id}
        url = f"{self.s['host']}/api/v3/accounts/{self.s['account']}/users/{urllib.parse.quote(we_id)}/activities?{urllib.parse.urlencode(q)}"
        events = ((self._get(url).get("response") or {}).get("data") or {}).get("eventStream") or []
        if len(events) >= int(self.s["page_size"]) and end - start > timedelta(hours=1):
            mid = start + (end - start) / 2
            return self.activities(we_id, mid, end) + self.activities(we_id, start, mid)
        return events


def store_activity(ctx: Ctx, client_id: str, events: list[dict]) -> int:
    """App events (not WebEngage's own push / journey / email events) into the events table the heat score reads."""
    n = 0
    for e in events:
        if e.get("category") != "application" or not e.get("name") or not e.get("time"):
            continue
        ts = parse_dt(e["time"], ctx.tz)
        if not ts:
            continue
        cur = ctx.conn.execute(
            "INSERT INTO events (user_id, event_name, event_time, attrs, source_file) VALUES (?,?,?,?,?) "
            "ON CONFLICT (user_id, event_name, event_time) DO NOTHING",
            (client_id, e["name"], iso(ts), json.dumps({"we_event_id": e.get("id")}), "webengage_api"))
        n += cur.rowcount
    return n


def _retry_locked(ctx: Ctx, fn, *args, tries: int = 10):
    """SQLite allows one writer: the scheduler's re-score can hold the database for longer than the 30 s busy timeout.
    Roll back (a failed write leaves the transaction open, and every later write fails until it is closed) and retry."""
    import sqlite3

    for attempt in range(tries):
        try:
            return fn(*args)
        except sqlite3.OperationalError as e:
            ctx.conn.rollback()
            if "locked" not in str(e) or attempt == tries - 1:
                raise
            log.warning("Database busy, retrying in 15 s (%s)", e)
            time.sleep(15)


def fetch_client(ctx: Ctx, api: Api, client_id: str, we_id: str) -> int:
    """Bring one client's activity up to now: from where the last fetch stopped (an hour of overlap), or lookback_days."""
    events, error = [], None
    start = _fetch_start(ctx, client_id, we_id)
    try:
        events = api.activities(we_id, start, ctx.now())
    except RuntimeError as e:
        error = str(e)[:300]
    added = _retry_locked(ctx, _save_fetch, ctx, client_id, we_id, events, error)
    if error:
        raise RuntimeError(error)
    return added


def _fetch_start(ctx: Ctx, client_id: str, we_id: str) -> datetime:
    s = settings(ctx)
    now = ctx.now()
    state = one(ctx.conn, "SELECT * FROM we_fetch WHERE client_id=?", (client_id,))
    start = now - timedelta(days=int(s["lookback_days"]))
    if state and state["we_id"] == we_id and state["fetched_to"]:
        start = max(start, parse_dt(state["fetched_to"], ctx.tz) - timedelta(hours=1))
    return start


def _save_fetch(ctx: Ctx, client_id: str, we_id: str, events: list[dict], error: str | None) -> int:
    now = ctx.now()
    added = store_activity(ctx, client_id, events)
    ctx.conn.execute(
        """INSERT INTO we_fetch (client_id, we_id, fetched_at, fetched_to, events, error) VALUES (?,?,?,?,?,?)
           ON CONFLICT (client_id) DO UPDATE SET we_id=excluded.we_id, fetched_at=excluded.fetched_at,
               fetched_to=CASE WHEN excluded.error IS NULL THEN excluded.fetched_to ELSE we_fetch.fetched_to END,
               events=we_fetch.events + excluded.events, error=excluded.error""",
        (client_id, we_id, iso(now), None if error else iso(now), added, error))
    ctx.conn.commit()
    if error:
        raise RuntimeError(error)
    return added


def needs_fetch(ctx: Ctx, client_ids: list[str], since: datetime | None = None) -> list[str]:
    """Clients not fetched since `since` (default: within fresh_hours), or never fetched."""
    cutoff = iso(since or ctx.now() - timedelta(hours=float(settings(ctx)["fresh_hours"])))
    fresh = set()
    for i in range(0, len(client_ids), 900):
        chunk = client_ids[i:i + 900]
        fresh |= {r["client_id"] for r in rows(ctx.conn, f"SELECT client_id FROM we_fetch WHERE fetched_to>=? AND error IS NULL "
                                                         f"AND client_id IN ({','.join('?' * len(chunk))})", (cutoff, *chunk))}
    return [c for c in client_ids if c not in fresh]


# ---------------------------------------------------------------- one cohort run
def pull_cohort(ctx: Ctx, cohort: str) -> pd.DataFrame:
    from .clientdb import pull_list

    df = pull_list(ctx, cohort)
    if settings(ctx)["centralised_only"] and "IsCentralized" in df:
        df = df[df["IsCentralized"].fillna(0).astype(int) == 1]
    return df.reset_index(drop=True)


def _list_file(ctx: Ctx, cohort: str, df: pd.DataFrame) -> Path:
    """The cohort as an uploadable list: client codes plus the amounts the pitch uses (payout, first deposit)."""
    out = pd.DataFrame({"Client code": df["CLIENT_ID"]})
    if cohort == "payout":
        out["Payout amount"] = df.get("PayoutAmount")
        out["Payout date"] = df.get("LastPayoutDate")
    if cohort == "low_margin":
        out["Current margin"] = df.get("FirstDeposit")
    folder = ctx.path("data/uploads")
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{ctx.now():%Y%m%d_%H%M%S}_morning_{cohort}.csv"
    out.to_csv(path, index=False, encoding="utf-8-sig")
    return path


def _update(ctx: Ctx, run_id: int, **fields) -> None:
    def write():
        ctx.conn.execute(f"UPDATE cohort_runs SET {', '.join(f'{k}=?' for k in fields)} WHERE id=?", (*fields.values(), run_id))
        ctx.conn.commit()

    _retry_locked(ctx, write)


def run_cohort(ctx: Ctx, run: dict, api: Api | None = None) -> dict:
    """Pull -> WebEngage IDs -> fetch stale activity -> (publish) score the list. Progress is written to cohort_runs."""
    from .upload import analyse_upload

    rid, cohort = run["id"], run["cohort"]
    _update(ctx, rid, status="running", step="Getting the client list", started_at=iso(ctx.now()), message=None)
    df = pull_cohort(ctx, cohort)
    ids = df["CLIENT_ID"].tolist()
    _update(ctx, rid, step="Finding WebEngage IDs", total=len(ids))
    we = lookup_we_ids(ctx, ids) if ids else {}
    # the overnight fetch refreshes everyone not fetched in the hours before it was queued; the morning publish then only
    # tops up clients new to the list (or not fetched within fresh_hours)
    since = None if run["publish"] else parse_dt(run["requested_at"], ctx.tz) - timedelta(hours=float(settings(ctx)["refetch_after_hours"]))
    todo = needs_fetch(ctx, [c for c in ids if c in we], since)
    if run["publish"] and cohort in settings(ctx)["night_only"]:
        todo = []  # too many clients to fetch in the day: tonight's run fetches them
    _update(ctx, rid, step="Fetching app activity", no_we_id=len(ids) - len(we), to_fetch=len(todo), fetched=0)
    api = api or Api(ctx)
    failed = 0
    for n, cid in enumerate(todo, 1):
        try:
            fetch_client(ctx, api, cid, we[cid])
        except RuntimeError as e:
            failed += 1
            log.warning("Activity fetch failed for %s: %s", cid, e)
        if n % 5 == 0 or n == len(todo):
            _update(ctx, rid, fetched=n)
        if failed >= 25 and failed > n // 2:  # WebEngage is refusing us: stop rather than hammer it
            raise RuntimeError(f"Stopped: {failed} of {n} activity fetches failed (last: see the scheduler log)")
    result = {"clients": len(ids), "we_ids": len(we), "fetched": len(todo), "failed": failed}
    if run["publish"] and ids:
        _update(ctx, rid, step="Scoring and ranking")
        path = _list_file(ctx, cohort, df)
        summary = _retry_locked(ctx, lambda: analyse_upload(ctx, path, uploaded_by="system", use_client_db=True, list_type=cohort),
                                tries=4)
        if summary.get("error"):
            raise RuntimeError(summary["error"])
        result["upload_id"] = summary["upload_id"]
        result["tiers"] = summary.get("tiers")
    _update(ctx, rid, status="ready", step="Ready" if run["publish"] else "Activity fetched", finished_at=iso(ctx.now()),
            upload_id=result.get("upload_id"),
            message=(f"{failed} fetch(es) failed; " if failed else "") + f"{len(ids) - len(we)} without a WebEngage ID")
    return result


# ---------------------------------------------------------------- queue + schedule
def queue(ctx: Ctx, cohorts: list[str], publish: bool = True, requested_by: str | None = None) -> int:
    """Ask the worker to run these cohorts (skips one already queued or running)."""
    added = 0
    for c in cohorts:
        if one(ctx.conn, "SELECT 1 FROM cohort_runs WHERE cohort=? AND publish>=? AND status IN ('queued','running')", (c, int(publish))):
            continue
        ctx.conn.execute("INSERT INTO cohort_runs (cohort, publish, status, step, requested_by, requested_at) VALUES (?,?,?,?,?,?)",
                         (c, int(publish), "queued", "Waiting to start", requested_by, iso(ctx.now())))
        added += 1
    ctx.conn.commit()
    return added


def latest_runs(ctx: Ctx) -> dict[str, dict]:
    """The newest run per cohort (what the dashboard shows), and the newest published one."""
    out = {}
    for c in settings(ctx)["cohorts"]:
        last = one(ctx.conn, "SELECT * FROM cohort_runs WHERE cohort=? ORDER BY id DESC LIMIT 1", (c,))
        ready = one(ctx.conn, "SELECT * FROM cohort_runs WHERE cohort=? AND status='ready' AND upload_id IS NOT NULL "
                              "ORDER BY id DESC LIMIT 1", (c,))
        out[c] = {"last": last, "ready": ready}
    return out


def _at(ctx: Ctx, hhmm: str) -> datetime:
    h, m = (int(x) for x in hhmm.split(":"))
    return ctx.now().replace(hour=h, minute=m, second=0, microsecond=0)


def schedule(ctx: Ctx) -> list[str]:
    """Queue the overnight fetch and the morning publish once a day each."""
    from .jobs import _mark, _ran

    s, now, today = settings(ctx), ctx.now(), ctx.today()
    done = []
    if not s["enabled"]:
        return done
    if now >= _at(ctx, s["publish_at"]) and not _ran(ctx, "cohort_publish", today):
        if ctx.is_working_day(today) or s["publish_on_holidays"]:
            queue(ctx, s["cohorts"], publish=True, requested_by="schedule")
            done.append("publish")
        _mark(ctx, "cohort_publish", today)
    if now >= _at(ctx, s["fetch_at"]) and not _ran(ctx, "cohort_fetch", today):
        queue(ctx, s["cohorts"], publish=False, requested_by="schedule")
        _mark(ctx, "cohort_fetch", today)
        done.append("fetch")
    return done


def work_once(ctx: Ctx, api: Api | None = None) -> dict | None:
    """Run the oldest queued cohort (publish runs before overnight fetches)."""
    run = one(ctx.conn, "SELECT * FROM cohort_runs WHERE status='queued' ORDER BY publish DESC, id LIMIT 1")
    if not run:
        return None
    try:
        res = run_cohort(ctx, run, api)
        log.info("cohort %s: %s", run["cohort"], res)
        return res
    except Exception as e:
        log.exception("Cohort run %s (%s) failed", run["id"], run["cohort"])
        ctx.conn.rollback()  # never leave a failed write's transaction open: it would block every later write
        _update(ctx, run["id"], status="failed", finished_at=iso(ctx.now()), message=f"{type(e).__name__}: {str(e)[:300]}")
        return {"error": str(e)}


def worker(ctx: Ctx) -> None:
    """`python run.py cohorts`: runs beside the scheduler, so a long fetch never holds up follow-up checks."""
    ctx.conn.execute("UPDATE cohort_runs SET status='queued', step='Restarted' WHERE status='running'")  # a run cut off by a restart
    ctx.conn.commit()
    log.info("Cohort worker started")
    while True:
        try:
            schedule(ctx)
            if work_once(ctx) is None:
                time.sleep(60)
        except Exception:
            log.exception("cohort worker tick failed")
            ctx.conn.rollback()
            time.sleep(60)
