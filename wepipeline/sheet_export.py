"""Publish the prioritised customer list (with levers and pitch) to a live Google Sheet and a CSV.

The Google Sheet is written by a Google service account:
  * GOOGLE_SERVICE_ACCOUNT_JSON env var holding the key file's JSON (GitHub Actions / Streamlit secrets), or
  * outputs.google_sheet.service_account_json pointing at the key file (local runs).
Share the sheet with the service account's email as Editor.
"""
from __future__ import annotations

import csv
import json
import logging
import os
from collections import Counter

from .activity import humanize_ago
from .context import Ctx, parse_dt
from .db import rows
from .followups import KIND_LABELS, OUTCOMES
from .assign import engaged_enough
from .insights import customer_insight

log = logging.getLogger(__name__)

COLUMNS = [
    ("Rank", "rank"), ("Priority", "priority"), ("Heat score", "heat"), ("Client code", "client"), ("Name", "name"),
    ("Phone", "phone"), ("Category", "category"), ("Registered on", "registered_on"),
    ("Days since registration", "days_since_registration"), ("Deposit status", "deposit_status"),
    ("First deposit date", "first_deposit_date"), ("Last trade date", "last_trade_date"),
    ("Days since last trade", "days_since_last_trade"), ("Holdings (₹)", "holdings"), ("Account status", "account_status"),
    ("Stage", "stage"), ("Client status", "status"),
    ("List", "list"), ("GM", "gm"), ("SM", "sm"), ("Next follow-up", "next_due"), ("Follow-up type", "next_kind"), ("Overdue", "overdue"),
    ("Calls made", "calls"), ("NPC attempts", "npc"), ("Last call outcome", "last_outcome"), ("Last call", "last_call"),
    ("Last call notes", "last_notes"),
    ("Last active in app", "last_active"), ("App actions (30 d)", "events_30d"), ("Core actions", "core_actions"),
    ("Non-core actions", "non_core_actions"), ("Core activity (type × times, recency)", "core_detail"),
    ("Non-core activity", "non_core_detail"), ("Momentum", "momentum"), ("Best time to call", "best_time"),
    ("Research segments", "segments"), ("Sarthi status", "sarthi_status"), ("Why hot (score drivers)", "heat_reasons"),
    ("Engagement levers", "levers"), ("Call ideas (data → angle)", "call_ideas_text"), ("Close", "closing_text"),
    ("Next best action", "next_action"), ("Agar customer bole", "objections_hi"), ("Updated", "updated"),
]
TIER_COLORS = {"🔥 Hot": {"red": 0.99, "green": 0.87, "blue": 0.85}, "🌤️ Warm": {"red": 1.0, "green": 0.95, "blue": 0.8},
               "❄️ Cold": {"red": 0.9, "green": 0.94, "blue": 0.99}}


def build_rows(ctx: Ctx, lead_id: str | None = None) -> list[dict]:
    """Every lead (or just one), open ones first by priority, then everything else."""
    now = ctx.now()
    stamp = now.strftime("%d %b %H:%M")
    grace = ctx.rules.get("warning_after_minutes", 15)
    only = " WHERE lead_id=?" if lead_id else ""
    args = (lead_id,) if lead_id else ()
    calls = {r["lead_id"]: r for r in rows(ctx.conn, f"SELECT lead_id, COUNT(*) n, MAX(called_at) last FROM calls{only} GROUP BY lead_id", args)}
    notes = {}
    for r in rows(ctx.conn, f"SELECT lead_id, notes FROM calls{only} ORDER BY called_at", args):
        notes[r["lead_id"]] = r["notes"] or ""  # ascending, so the latest call's notes are kept
    nexts = {}
    pending = " AND lead_id=?" if lead_id else ""
    for f in rows(ctx.conn, f"SELECT lead_id, due_at, kind FROM followups WHERE status='pending'{pending} ORDER BY due_at DESC", args):
        nexts[f["lead_id"]] = f  # rows come latest-first, so the earliest due one is kept
    leads = rows(ctx.conn, f"SELECT * FROM leads{only} ORDER BY CASE WHEN status='open' THEN 0 ELSE 1 END, heat_score DESC, first_seen_at DESC", args)
    out = []
    for rank, lead in enumerate(leads, 1):
        ins = customer_insight(ctx, lead)
        nxt = nexts.get(lead["lead_id"])
        due = parse_dt(nxt["due_at"], ctx.tz) if nxt else None
        gm = ctx.member(lead["assigned_to"]) or {}
        c = calls.get(lead["lead_id"], {})
        out.append({
            **ins,
            "rank": rank if lead["status"] == "open" else "",
            "heat": round(lead["heat_score"] or 0),
            "client": lead["we_user_id"] or lead["lead_id"],
            "name": lead["name"] or "",
            "phone": lead["phone"] or "",
            "category": lead["category"] or "",
            "status": lead["status"],
            "gm": gm.get("name", ("Waiting for capacity" if engaged_enough(ctx, lead)
                                  else "Waiting for engagement (re-activation)") if lead["status"] == "open" else ""),
            "list": {"new": "New, no deposit", "low_margin": "Low margin", "reactivation": "Churned, active again",
                     "payout": "Payout"}.get(lead.get("campaign") or "", ""),
            "sm": (ctx.member(ctx.sm_of(gm)) or {}).get("name", ""),
            "next_due": due.strftime("%d %b %H:%M") if due else "",
            "next_kind": KIND_LABELS.get(nxt["kind"], "") if nxt else "",
            "overdue": "⚠️ Yes" if due and (now - due).total_seconds() > grace * 60 else "",
            "calls": c.get("n", 0),
            "npc": lead["npc_count"],
            "last_outcome": OUTCOMES.get(lead["last_outcome"] or "", {}).get("label", lead["last_outcome"] or ""),
            "last_call": parse_dt(c["last"], ctx.tz).strftime("%d %b %H:%M") if c.get("last") else "",
            "last_notes": notes.get(lead["lead_id"], ""),
            "last_active": humanize_ago(ctx, ins["last_active"]) if ins["last_active"] else "no activity seen",
            "heat_reasons": lead["heat_reasons"] or "",
            "updated": stamp,
        })
    return out


def write_csv(ctx: Ctx, data: list[dict]) -> str:
    path = ctx.path((ctx.cfg.get("outputs", {}).get("csv_path")) or "data/exports/customer_priority.csv")
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        f = open(path, "w", newline="", encoding="utf-8-sig")  # BOM so Excel shows ₹ and emoji correctly
    except PermissionError:  # the file is open in Excel: write a timestamped copy instead of failing
        path = path.with_name(f"{path.stem}_{ctx.now():%Y%m%d_%H%M}{path.suffix}")
        log.warning("%s is open in another program; writing %s instead", path.name, path)
        f = open(path, "w", newline="", encoding="utf-8-sig")
    with f:
        w = csv.writer(f)
        w.writerow([h for h, _ in COLUMNS])
        w.writerows([[r.get(k, "") for _, k in COLUMNS] for r in data])
    return str(path)


def _client(ctx: Ctx):
    import gspread

    raw = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    if raw:
        return gspread.service_account_from_dict(json.loads(raw))
    key_file = ctx.path(ctx.cfg.get("outputs", {}).get("google_sheet", {}).get("service_account_json", "secrets/service_account.json"))
    if not key_file.exists():
        raise RuntimeError(f"No Google service account key: set GOOGLE_SERVICE_ACCOUNT_JSON or put the key at {key_file}")
    return gspread.service_account(filename=str(key_file))


def _tab(book, title: str, rows_: int, cols: int):
    import gspread

    try:
        return book.worksheet(title), False
    except gspread.WorksheetNotFound:
        return book.add_worksheet(title=title, rows=max(rows_, 10), cols=max(cols, 5)), True


def summary_table(ctx: Ctx, data: list[dict]) -> list[list]:
    open_rows = [r for r in data if r["status"] == "open"]
    tiers = ["🔥 Hot", "🌤️ Warm", "❄️ Cold"]
    cats = sorted({r["category"] for r in open_rows})
    table = [["Open customers by category and priority", *tiers, "Total"]]
    for cat in cats:
        cnt = Counter(r["priority"] for r in open_rows if r["category"] == cat)
        table.append([cat, *[cnt.get(t, 0) for t in tiers], sum(cnt.values())])
    tot = Counter(r["priority"] for r in open_rows)
    table.append(["Total", *[tot.get(t, 0) for t in tiers], len(open_rows)])
    table += [[], ["By GM", *tiers, "Total", "Overdue now"]]
    for gm in sorted({r["gm"] for r in open_rows}):
        mine = [r for r in open_rows if r["gm"] == gm]
        cnt = Counter(r["priority"] for r in mine)
        table.append([gm, *[cnt.get(t, 0) for t in tiers], len(mine), sum(1 for r in mine if r["overdue"])])
    closed = Counter(r["status"] for r in data if r["status"] != "open")
    table += [[], ["Closed clients", "Count"], *[[k, v] for k, v in sorted(closed.items())]]
    table += [[], [f"Updated {data[0]['updated'] if data else ''}. Rows are rewritten on every run: add your own notes in another sheet."]]
    return table


def scoring_guide(ctx: Ctx) -> list[list]:
    from .heat import _settings

    h = _settings(ctx)
    return [
        ["How the heat score works"],
        ["Per activity", "weight × recency × frequency"],
        ["Weight", f"core = {h['core_weight']}, non-core = {h['non_core_weight']}"],
        ["Core activities", ", ".join(h["core_activities"])],
        ["Recency", f"halves every {h['recency_half_life_days']} days since the activity was last done"],
        ["Frequency", f"1 + log2(times done), capped at {h['frequency_cap']}"],
        ["Registration", "counts as a core activity on its registration date, so fresher sign-ups score higher"],
        ["Score", f"100 × (1 − e^(−sum ÷ {h['score_scale']})), 0–100"],
        ["Tiers", f"🔥 Hot ≥ {h['tiers']['hot']} · 🌤️ Warm ≥ {h['tiers']['warm']} · ❄️ Cold below"],
        ["First call due", " · ".join(f"{t}: {m} min" for t, m in h["first_call_sla_minutes"].items())],
        ["NPC retry", f"{ctx.rules.get('npc_retry_hours', 4)} h (set by admin)"],
        [],
        ["Compliance", "Pitch ideas never recommend specific securities or promise returns. F&O needs the risk disclosure first."],
    ]


def push_to_google_sheet(ctx: Ctx, data: list[dict]) -> dict:
    cfg = ctx.cfg.get("outputs", {}).get("google_sheet", {})
    key = os.environ.get("GOOGLE_SHEET_KEY") or cfg.get("spreadsheet_key")
    if not key:
        raise RuntimeError("Set outputs.google_sheet.spreadsheet_key (the long id in the sheet's URL)")
    book = _client(ctx).open_by_key(key)
    header = [h for h, _ in COLUMNS]
    values = [header] + [[r.get(k, "") for _, k in COLUMNS] for r in data]

    ws, created = _tab(book, cfg.get("customers_tab", "Customers"), len(values) + 50, len(header))
    ws.clear()
    ws.resize(rows=max(len(values), 2), cols=len(header))
    ws.update(values, "A1", value_input_option="RAW")  # RAW: customer text is never run as a formula
    if created:
        ws.freeze(rows=1, cols=4)
        ws.format("1:1", {"textFormat": {"bold": True}, "wrapStrategy": "WRAP"})
        rules = [{"addConditionalFormatRule": {"index": i, "rule": {
            "ranges": [{"sheetId": ws.id, "startRowIndex": 1, "startColumnIndex": 1, "endColumnIndex": 2}],
            "booleanRule": {"condition": {"type": "TEXT_EQ", "values": [{"userEnteredValue": tier}]},
                            "format": {"backgroundColor": color}}}}} for i, (tier, color) in enumerate(TIER_COLORS.items())]
        book.batch_update({"requests": rules})
    ws.set_basic_filter()

    summary = summary_table(ctx, data)
    sws, _ = _tab(book, "Summary", len(summary) + 5, 6)
    sws.clear()
    sws.update(summary, "A1", value_input_option="RAW")
    guide = scoring_guide(ctx)
    gws, _ = _tab(book, "How scoring works", len(guide) + 5, 3)
    gws.clear()
    gws.update(guide, "A1", value_input_option="RAW")
    return {"sheet": book.url, "rows": len(data)}


def sheet_enabled(ctx: Ctx) -> bool:
    return bool(ctx.cfg.get("outputs", {}).get("google_sheet", {}).get("enabled") or os.environ.get("GOOGLE_SHEET_KEY"))


def update_customer_row(ctx: Ctx, lead_id: str) -> dict | None:
    """Rewrite one customer's row in the live sheet right after a call (rank is kept until the hourly refresh).
    Returns None when no Google Sheet is configured."""
    if not sheet_enabled(ctx):
        return None
    cfg = ctx.cfg.get("outputs", {}).get("google_sheet", {})
    found = build_rows(ctx, lead_id)
    if not found:
        return {"error": f"unknown client {lead_id}"}
    row = found[0]
    book = _client(ctx).open_by_key(os.environ.get("GOOGLE_SHEET_KEY") or cfg["spreadsheet_key"])
    ws = book.worksheet(cfg.get("customers_tab", "Customers"))
    client_col = [k for _, k in COLUMNS].index("client") + 1
    cell = ws.find(row["client"], in_column=client_col)
    values = [row.get(k, "") for _, k in COLUMNS]
    if cell is None:  # customer not in the sheet yet: add at the bottom until the next full refresh
        ws.append_row(values, value_input_option="RAW")
        return {"appended": row["client"]}
    values[0] = ws.cell(cell.row, 1).value or ""  # keep the current rank
    ws.update([values], f"A{cell.row}", value_input_option="RAW")
    return {"updated_row": cell.row, "client": row["client"]}


def export(ctx: Ctx) -> dict:
    data = build_rows(ctx)
    result = {"rows": len(data), "csv": write_csv(ctx, data)}
    if sheet_enabled(ctx):
        try:
            result["google_sheet"] = push_to_google_sheet(ctx, data)
        except Exception as e:  # a Sheets outage must not break follow-up checks
            log.exception("Google Sheet export failed")
            result["google_sheet"] = {"error": str(e)}
    return result
