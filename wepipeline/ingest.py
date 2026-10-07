"""Load the live lead sheet and WebEngage CSV exports into the database."""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

from .context import Ctx, iso, parse_dt
from .db import one

LEAD_FIELDS = ["lead_id", "we_user_id", "name", "phone", "email", "category", "source", "remarks"]
HINT_FIELDS = ["last_call_status", "last_call_at", "next_followup_at"]

# Header names WebEngage commonly uses in dashboard exports (matched case-insensitively).
USER_ID_COLS = ["user_id", "user id", "userid", "cuid", "clientcode", "client code", "client_code", "client id",
                "customer id", "customer_id", "we_user_id", "id"]
EVENT_NAME_COLS = ["event_name", "event name", "eventname", "event", "events"]
EVENT_TIME_COLS = ["event_time", "event time", "eventtime", "event timestamp", "timestamp", "time", "date"]
PHONE_COLS = ["phone", "phone number", "mobile", "mobile number", "we_phone"]
EMAIL_COLS = ["email", "email id", "we_email"]
NAME_COLS = ["name", "full name", "user name", "first name"]
CATEGORY_COLS = ["category", "lead category", "segment"]
# Aggregated activity files: one row per user x activity, with a count and/or last-done date.
ACTIVITY_COLS = ["activity", "activity name", "action", "event name", "event_name", "event", "events"]
COUNT_COLS = ["count", "no of times", "no. of times", "number of times", "times", "frequency", "activity count",
              "event count", "total count", "times done"]
LAST_COLS = ["last done", "last done on", "last activity", "last activity date", "last activity time", "last performed",
             "last event time", "last seen", "date of activity", "activity date", "day of activity", "date",
             "event time", "event_time"]
DAYS_COLS = ["days since last activity", "days since", "recency", "recency (days)", "recency days", "days ago"]
TYPE_COLS = ["activity type", "type", "core/non-core", "core / non-core", "core/non core", "core or non-core",
             "activity category"]


def norm_phone(value) -> str:
    digits = re.sub(r"\D", "", str(value or ""))
    return digits[-10:] if len(digits) >= 10 else digits


def norm_email(value) -> str:
    return str(value or "").strip().lower()


def _read_sheets(path: Path) -> dict[str, pd.DataFrame]:
    """Every sheet of a workbook (WebEngage exports put one event per sheet); a single table otherwise."""
    if path.suffix.lower() in {".xlsx", ".xls"}:
        return {name: df.fillna("") for name, df in pd.read_excel(path, sheet_name=None, dtype=str).items()}
    return {"": _read_table(path)}


def _read_table(path: Path) -> pd.DataFrame:
    suffix = path.suffix.lower()
    if suffix in {".xlsx", ".xls"}:
        df = pd.read_excel(path, dtype=str)
    elif suffix in {".json", ".ndjson", ".jsonl"}:
        df = pd.read_json(path, lines=True, dtype=False).astype(str)
    else:
        df = pd.read_csv(path, dtype=str, keep_default_na=False, encoding="utf-8-sig")
    return df.fillna("")


# ---------------------------------------------------------------- leads sheet
def read_leads_frame(ctx: Ctx) -> pd.DataFrame:
    src = ctx.cfg["sources"]["leads"]
    kind = src.get("type", "csv")
    if kind == "csv":
        return _read_table(ctx.path(src["path"]))
    if kind == "gsheet_csv_url":
        return pd.read_csv(src["url"], dtype=str, keep_default_na=False).fillna("")
    if kind == "gsheet":
        import gspread  # only needed for private sheets

        client = gspread.service_account(filename=str(ctx.path(src["service_account_json"])))
        book = client.open_by_key(src["spreadsheet_key"])
        sheet = book.worksheet(src["worksheet"]) if src.get("worksheet") else book.sheet1
        return pd.DataFrame(sheet.get_all_records()).astype(str).fillna("")
    raise ValueError(f"Unknown leads source type: {kind}")


def _map_row(row: dict, columns: dict) -> tuple[dict, dict, dict]:
    """Split a sheet row into mapped lead fields, follow-up hints and leftover columns."""
    used = set()
    fields, hints = {}, {}
    for key, header in columns.items():
        if header and header in row:
            used.add(header)
            value = str(row[header]).strip()
            (hints if key in HINT_FIELDS else fields)[key] = value
    extra = {k: str(v) for k, v in row.items() if k not in used and str(v).strip()}
    return fields, hints, extra


def sync_leads(ctx: Ctx, df: pd.DataFrame | None = None) -> dict:
    """Upsert every sheet row. New rows become unassigned leads; existing leads keep their workflow state."""
    from .followups import add_followup  # local import avoids a cycle

    if df is None:
        src = ctx.cfg["sources"]["leads"]
        if src.get("type", "csv") == "csv" and not ctx.path(src["path"]).exists():
            return {"missing": src["path"]}  # no sheet configured yet; leads can still come from activity files
        df = read_leads_frame(ctx)
    columns = ctx.cfg["sources"]["leads"].get("columns", {})
    now = ctx.now()
    new = updated = skipped = 0

    for row in df.to_dict("records"):
        fields, hints, extra = _map_row(row, columns)
        fields["phone"] = norm_phone(fields.get("phone"))
        fields["email"] = norm_email(fields.get("email"))
        lead_id = fields.get("lead_id") or fields["phone"]
        if not lead_id:
            skipped += 1
            continue
        fields["lead_id"] = lead_id
        payload = {k: fields.get(k, "") for k in LEAD_FIELDS}
        row_hash = hashlib.sha1(json.dumps([payload, hints, extra], sort_keys=True).encode()).hexdigest()
        extra["_hints"] = hints

        existing = one(ctx.conn, "SELECT * FROM leads WHERE lead_id=?", (lead_id,))
        if existing is None:
            ctx.conn.execute(
                """INSERT INTO leads (lead_id, we_user_id, name, phone, email, category, source, remarks,
                                      extra, first_seen_at, updated_at, row_hash)
                   VALUES (:lead_id, :we_user_id, :name, :phone, :email, :category, :source, :remarks,
                           :extra, :now, :now, :row_hash)""",
                {**payload, "extra": json.dumps(extra), "now": iso(now), "row_hash": row_hash},
            )
            new += 1
            continue
        if existing["row_hash"] == row_hash:
            continue

        ctx.conn.execute(
            """UPDATE leads SET we_user_id=:we_user_id, name=:name, phone=:phone, email=:email,
                      category=:category, source=:source, remarks=:remarks, extra=:extra,
                      updated_at=:now, row_hash=:row_hash
               WHERE lead_id=:lead_id""",
            {**payload, "extra": json.dumps(extra), "now": iso(now), "row_hash": row_hash},
        )
        updated += 1

        # Someone edited the follow-up time in the sheet: honour it as a promised follow-up.
        old_hints = json.loads(existing["extra"] or "{}").get("_hints", {})
        new_due = parse_dt(hints.get("next_followup_at"), ctx.tz)
        if (
            existing["assigned_to"]
            and existing["status"] == "open"
            and hints.get("next_followup_at") != old_hints.get("next_followup_at")
            and new_due
            and new_due > now
        ):
            add_followup(ctx, lead_id, existing["assigned_to"], new_due, "promised", "Follow-up time updated in sheet")

    ctx.conn.commit()
    return {"new": new, "updated": updated, "skipped": skipped, "rows": len(df)}


# ---------------------------------------------------------------- WebEngage exports
# Column headers of WebEngage "Events" pivot exports: "4 Oct, 12AM" (hourly) or "4 Oct" (daily).
BUCKET_RE = re.compile(r"^\s*(\d{1,2}) ([A-Za-z]{3})(?:,?\s*(\d{1,2})(?::(\d{2}))?\s*([AP]M))?\s*$", re.I)
MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


def _find(df: pd.DataFrame, override: str, candidates: list[str]) -> str | None:
    if override and override in df.columns:
        return override
    lower = {str(c).lower().strip(): c for c in df.columns}
    return next((lower[c] for c in candidates if c in lower), None)


def _bucket_time(ctx: Ctx, header) -> datetime | None:
    m = BUCKET_RE.match(str(header))
    if not m or m.group(2).lower() not in MONTHS:
        return None
    day, month = int(m.group(1)), MONTHS[m.group(2).lower()]
    hour = int(m.group(3) or 0) % 12 + (12 if (m.group(5) or "").upper() == "PM" else 0)
    now = ctx.now()
    try:
        dt = datetime(now.year, month, day, hour, int(m.group(4) or 0), tzinfo=ctx.tz)
    except ValueError:
        return None
    return dt.replace(year=now.year - 1) if dt > now + timedelta(days=1) else dt  # headers carry no year


def _activity_type(value) -> str:
    v = str(value or "").strip().lower()
    if "non" in v:
        return "non-core"
    return "core" if "core" in v else ""


def _file_category(ctx: Ctx, filename: str) -> str:
    """Category for leads created from an activity file, chosen by a substring of the file name."""
    for needle, category in (ctx.cfg["sources"]["webengage"].get("file_categories") or {}).items():
        if needle.lower() in filename.lower():
            return category
    return ctx.cfg.get("heat", {}).get("new_lead_category", "New user - app activity")


def create_leads_from_users(ctx: Ctx, users: dict[str, dict], source_file: str) -> int:
    """Add users seen in an activity file as new leads when they are not in the lead sheet yet."""
    heat = ctx.cfg.get("heat", {})
    if not heat.get("create_leads_from_activity", True):
        return 0
    now = iso(ctx.now())
    default_category = _file_category(ctx, source_file)
    created = 0
    for uid, u in users.items():
        phone = norm_phone(u.get("phone"))
        if heat.get("require_phone_for_new_leads", False) and not phone:
            continue
        if one(ctx.conn, "SELECT 1 FROM leads WHERE lead_id=? OR we_user_id=?", (uid, uid)) or (
            phone and one(ctx.conn, "SELECT 1 FROM leads WHERE phone=?", (phone,))
        ):
            continue
        ctx.conn.execute(
            """INSERT INTO leads (lead_id, we_user_id, name, phone, email, category, source, extra, first_seen_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (uid, uid, u.get("name", ""), phone, norm_email(u.get("email")), u.get("category") or default_category,
             f"WebEngage activity ({source_file})", "{}", now, now),
        )
        created += 1
    return created


def _upsert_profile(ctx: Ctx, uid: str, phone, email, attrs: dict) -> None:
    ctx.conn.execute(
        """INSERT INTO profiles (user_id, phone, email, attrs, updated_at) VALUES (?,?,?,?,?)
           ON CONFLICT(user_id) DO UPDATE SET
               phone=CASE WHEN excluded.phone!='' THEN excluded.phone ELSE profiles.phone END,
               email=CASE WHEN excluded.email!='' THEN excluded.email ELSE profiles.email END,
               attrs=excluded.attrs, updated_at=excluded.updated_at""",
        (uid, norm_phone(phone), norm_email(email), json.dumps(attrs), iso(ctx.now())),
    )


def _upsert_activity(ctx: Ctx, uid: str, activity: str, times: int, last: datetime | None, kind: str, source: str) -> None:
    ctx.conn.execute(
        """INSERT INTO activity_summary (user_id, activity, count, last_at, activity_type, source_file) VALUES (?,?,?,?,?,?)
           ON CONFLICT(user_id, activity) DO UPDATE SET count=excluded.count, last_at=excluded.last_at,
               activity_type=excluded.activity_type, source_file=excluded.source_file""",
        (uid, activity, times, iso(last), kind, source),
    )


def _as_int(value) -> int:
    try:
        return int(float(str(value).strip() or 0))
    except ValueError:
        return 0


def _ingest_frame(ctx: Ctx, df: pd.DataFrame, sheet: str, path: Path) -> tuple[str, int, dict]:
    """Load one table. Returns (kind, rows loaded, users seen with contact details)."""
    cfg = ctx.cfg["sources"]["webengage"]
    user_col = _find(df, cfg.get("user_id_column", ""), USER_ID_COLS)
    if not user_col:
        return "unrecognised", 0, {}
    event_col = _find(df, cfg.get("event_name_column", ""), EVENT_NAME_COLS)
    time_col = _find(df, cfg.get("event_time_column", ""), EVENT_TIME_COLS)
    activity_col = _find(df, "", ACTIVITY_COLS)
    count_col = _find(df, "", COUNT_COLS + ["sum", "total"])
    days_col = _find(df, "", DAYS_COLS)
    type_col = _find(df, "", TYPE_COLS)
    last_col = _find(df, "", LAST_COLS)
    phone_col = _find(df, "", PHONE_COLS)
    email_col = _find(df, "", EMAIL_COLS)
    name_col = _find(df, "", NAME_COLS)
    category_col = _find(df, "", CATEGORY_COLS)
    buckets = {c: t for c in df.columns if (t := _bucket_time(ctx, c))}
    now = ctx.now()
    count = 0
    users: dict[str, dict] = {}

    def remember(r: dict, uid: str) -> None:
        if uid not in users:
            users[uid] = {key: (str(r.get(col, "")) if col else "") for key, col in
                          [("phone", phone_col), ("email", email_col), ("name", name_col), ("category", category_col)]}

    def qualified(event: str) -> str:
        # Several sheets can share one event (equity / F&O / commodity research); keep them apart by sheet name.
        generic = not sheet or sheet.lower() == event.lower() or sheet.lower().startswith("sheet")
        return event if generic else f"{event} · {sheet}"

    if activity_col and buckets:
        kind = "activity"  # WebEngage pivot: one row per user, one column per hour/day with the count
        for r in df.to_dict("records"):
            uid, event = str(r[user_col]).strip(), str(r[activity_col]).strip()
            if not uid or not event:
                continue
            activity = qualified(event)
            hits = {t: n for c, t in buckets.items() if (n := _as_int(r[c])) > 0}
            times = _as_int(r[count_col]) if count_col else 0
            times = times or sum(hits.values())
            if not times:
                continue
            for t, n in hits.items():
                ctx.conn.execute(
                    "INSERT INTO events (user_id, event_name, event_time, attrs, source_file) VALUES (?,?,?,?,?) "
                    "ON CONFLICT (user_id, event_name, event_time) DO UPDATE SET attrs=excluded.attrs, source_file=excluded.source_file",
                    (uid, activity, iso(t), json.dumps({"count": n, "sheet": sheet}), path.name),
                )
            _upsert_activity(ctx, uid, activity, times, max(hits) if hits else None,
                             _activity_type(r[type_col]) if type_col else "", path.name)
            remember(r, uid)
            count += 1
    elif activity_col and (count_col or days_col or type_col):
        kind = "activity"  # one row per user x activity with a count and/or last-done date
        for r in df.to_dict("records"):
            uid, activity = str(r[user_col]).strip(), str(r[activity_col]).strip()
            if not uid or not activity:
                continue
            last = parse_dt(r[last_col], ctx.tz) if last_col else None
            if not last and days_col and str(r[days_col]).strip():
                try:
                    last = now - timedelta(days=float(r[days_col]))
                except ValueError:
                    last = None
            _upsert_activity(ctx, uid, qualified(activity), max(_as_int(r[count_col]), 1) if count_col else 1, last,
                             _activity_type(r[type_col]) if type_col else "", path.name)
            remember(r, uid)
            count += 1
    elif event_col and time_col:
        kind = "events"
        skip = {user_col, event_col, time_col}
        for r in df.to_dict("records"):
            uid = str(r[user_col]).strip()
            ts = parse_dt(r[time_col], ctx.tz)
            if not uid or not r[event_col] or not ts:
                continue
            attrs = {k: v for k, v in r.items() if k not in skip and str(v).strip()}
            cur = ctx.conn.execute(
                "INSERT INTO events (user_id, event_name, event_time, attrs, source_file) VALUES (?,?,?,?,?) "
                "ON CONFLICT (user_id, event_name, event_time) DO NOTHING",
                (uid, str(r[event_col]).strip(), iso(ts), json.dumps(attrs), path.name),
            )
            remember(r, uid)
            count += cur.rowcount
    else:
        kind = "profiles"
        for r in df.to_dict("records"):
            uid = str(r[user_col]).strip()
            if not uid:
                continue
            attrs = {k: v for k, v in r.items() if k != user_col and str(v).strip()}
            _upsert_profile(ctx, uid, r.get(phone_col) if phone_col else "", r.get(email_col) if email_col else "", attrs)
            count += 1
    return kind, count, users


def ingest_webengage_file(ctx: Ctx, path: Path) -> dict:
    """Load one export (every sheet of a workbook). Recognises WebEngage event pivots, aggregated
    activity lists, event-level exports and user profile exports."""
    sheets = _read_sheets(path)
    kinds, total, users = set(), 0, {}
    for sheet, df in sheets.items():
        kind, n, seen = _ingest_frame(ctx, df, sheet, path)
        kinds.add(kind)
        total += n
        for uid, u in seen.items():
            users.setdefault(uid, u)
    new_leads = 0
    if users and kinds & {"activity", "events"}:
        for uid, u in users.items():
            if u["phone"] or u["email"]:
                _upsert_profile(ctx, uid, u["phone"], u["email"], {k: v for k, v in u.items() if v})
        new_leads = create_leads_from_users(ctx, users, path.name)
    kind = ",".join(sorted(kinds))
    ctx.conn.execute(
        """INSERT INTO ingested_files (path, mtime, kind, rows, ingested_at) VALUES (?,?,?,?,?)
           ON CONFLICT(path) DO UPDATE SET mtime=excluded.mtime, kind=excluded.kind,
               rows=excluded.rows, ingested_at=excluded.ingested_at""",
        (str(path), path.stat().st_mtime, kind, total, iso(ctx.now())),
    )
    ctx.conn.commit()
    return {"file": path.name, "kind": kind, "rows": total, "users": len(users), "new_leads": new_leads}


def sync_webengage(ctx: Ctx) -> list[dict]:
    """Load every new or modified export in the WebEngage folder."""
    folder = ctx.path(ctx.cfg["sources"]["webengage"]["folder"])
    folder.mkdir(parents=True, exist_ok=True)
    results = []
    for path in sorted(folder.iterdir()):
        if path.name.startswith("~$") or path.suffix.lower() not in {".csv", ".xlsx", ".xls", ".json", ".ndjson", ".jsonl"}:
            continue
        seen = one(ctx.conn, "SELECT mtime FROM ingested_files WHERE path=?", (str(path),))
        if seen and seen["mtime"] >= path.stat().st_mtime:
            continue
        results.append(ingest_webengage_file(ctx, path))
    return results
