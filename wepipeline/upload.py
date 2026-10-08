"""Analyse a customer list uploaded by an SM: ingest, age from the client database, score, rank, assign, pitch."""
from __future__ import annotations

import json
import logging
from collections import Counter
from pathlib import Path

from openpyxl import Workbook
from openpyxl.formatting.rule import FormulaRule
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from .context import Ctx, iso
from .db import one, rows
from .ingest import ingest_webengage_file

log = logging.getLogger(__name__)
BATCH_COLUMNS = [
    ("Rank in this list", "batch_rank"), ("Overall rank", "rank"), ("Priority", "priority"), ("Heat score", "heat"),
    ("Client code", "client"), ("Name", "name"), ("Phone", "phone"), ("Category", "category"), ("Stage", "stage"),
    ("List", "list"), ("Assigned GM", "gm"), ("SM", "sm"), ("First call due", "next_due"), ("Registered on", "registered_on"),
    ("Days since registration", "days_since_registration"), ("Deposit status", "deposit_status"),
    ("Last trade date", "last_trade_date"), ("Core activity (type × times, recency)", "core_detail"),
    ("Non-core activity", "non_core_detail"), ("Best time to call", "best_time"), ("Research segments", "segments"), ("Sarthi status", "sarthi_status"),
    ("Why hot (score drivers)", "heat_reasons"), ("Engagement levers", "levers"), ("Conversation opener", "opener"),
    ("Pitch ideas", "ideas"), ("Objection to expect", "objection"), ("Lead status", "status"),
]
# What the SM says the list is: sets the category (and so the pitch stage) for customers new to the system.
# The client database, when reachable, replaces it with the real ageing category.
LIST_TYPES = {
    "no_deposit": ("New customers without deposit", "Not yet matched in client DB", "new"),
    "reactivation": ("Re-activation customers (only engaged ones go to GMs)", "Not yet matched in client DB", "reactivation"),
    "payout": ("Customers doing a payout (retain the funds)", "Not yet matched in client DB", "payout"),
    "unknown": ("Mixed / not sure (work it out from app activity)", None, ""),  # not offered in the dashboard
}
TIER_FILL = {"🔥 Hot": "FCDDD9", "🌤️ Warm": "FFF2CC", "❄️ Cold": "E6EFFC"}


def analyse_upload(ctx: Ctx, path: Path, uploaded_by: str | None = None, use_client_db: bool = True,
                   list_type: str = "unknown") -> dict:
    from . import jobs
    from .clientdb import available, enrich_leads, reachable

    ingest = ingest_webengage_file(ctx, path, create_from_profiles=True, category=LIST_TYPES[list_type][1])
    ids = ingest["client_ids"]
    if not ids:
        return {"error": "No client codes found. The file needs a ClientCode / User ID column.", "ingest": ingest}
    known_before = ingest["users"] - ingest["new_leads"]
    campaign = LIST_TYPES[list_type][2]
    if campaign:  # tag every customer in the file, new and already known, so the list can be filtered and handled
        marks = ",".join("?" * len(ids))
        ctx.conn.execute(f"UPDATE leads SET campaign=? WHERE lead_id IN ({marks}) AND status='open'", (campaign, *ids))
        ctx.conn.commit()
    released = 0
    fallback_stage = {"no_deposit": "kyc_no_deposit", "reactivation": "dormant_trader"}.get(list_type)
    if fallback_stage:
        for l in rows(ctx.conn, f"SELECT lead_id, extra FROM leads WHERE lead_id IN ({','.join('?' * len(ids))}) AND status='open'", ids):
            extra = json.loads(l["extra"] or "{}")
            if not extra.get("stage"):  # the client database, when reachable, replaces it with the real stage
                extra["stage"] = fallback_stage
                ctx.conn.execute("UPDATE leads SET extra=? WHERE lead_id=?", (json.dumps(extra), l["lead_id"]))
        ctx.conn.commit()
    if campaign == "payout":
        _store_payout_details(ctx, path, ids)
    ageing = {"status": "skipped", "detail": "client database not configured"}
    if use_client_db and available(ctx) and not reachable(ctx):
        ageing = {"status": "unavailable", "detail": "client database not reachable from here (office network / VPN needed)"}
    elif use_client_db and available(ctx):
        try:
            res = enrich_leads(ctx)
            ageing = {"status": "ok", "detail": f"{res.get('found', 0)} of {res.get('looked_up', 0)} open leads found in the client database"}
        except Exception as e:  # unreachable from the cloud, wrong password, VPN down...
            log.warning("Client DB ageing skipped: %s", e)
            ageing = {"status": "unavailable", "detail": f"client database not reachable from here ({type(e).__name__})"}
    if campaign == "reactivation":
        from .assign import release_unengaged_reactivation
        from .heat import refresh_heat

        refresh_heat(ctx)  # score first, so only the unengaged are released
        released = release_unengaged_reactivation(ctx, ids)
    sync = jobs.sync(ctx)  # heat score, assignment (hot first), system follow-ups, overdue checks

    marks = ",".join("?" * len(ids))
    from .assign import engaged_enough

    leads = rows(ctx.conn, f"SELECT lead_id, status, heat_tier, assigned_to, category, campaign FROM leads WHERE lead_id IN ({marks})", ids)
    summary = {
        "file": path.name, "kind": ingest["kind"], "list_type": LIST_TYPES[list_type][0], "clients": len(ids), "new_leads": ingest["new_leads"],
        "already_known": known_before, "ageing": ageing,
        "tiers": dict(Counter(l["heat_tier"] for l in leads if l["status"] == "open")),
        "assigned": sum(1 for l in leads if l["status"] == "open" and l["assigned_to"]),
        "waiting": sum(1 for l in leads if l["status"] == "open" and not l["assigned_to"] and engaged_enough(ctx, l)),
        "watching": sum(1 for l in leads if l["status"] == "open" and not l["assigned_to"] and not engaged_enough(ctx, l)),
        "released_to_watching": released,
        "closed": sum(1 for l in leads if l["status"] != "open"),
        "not_in_client_db": sum(1 for l in leads if l["category"] == "Not found in client DB"),
        "assignment_run": sync.get("assignment", {}),
    }
    cur = ctx.conn.execute(
        "INSERT INTO uploads (filename, uploaded_by, uploaded_at, client_ids, summary) VALUES (?,?,?,?,?) RETURNING id",
        (path.name, uploaded_by, iso(ctx.now()), json.dumps(ids), json.dumps(summary, default=str)),
    )
    summary["upload_id"] = cur.fetchone()["id"]
    ctx.conn.commit()
    return summary


PAYOUT_AMOUNT_COLS = ["payout amount", "withdrawal amount", "amount", "payout", "requested amount", "payout amt"]
PAYOUT_DATE_COLS = ["payout date", "withdrawal date", "request date", "requested on", "date"]


def _store_payout_details(ctx: Ctx, path: Path, ids: list[str]) -> None:
    """Pick up payout amount / date columns when the file has them, for the opener ('the ₹25,000 withdrawal')."""
    from .context import parse_dt
    from .ingest import USER_ID_COLS, _find, _read_sheets

    wanted, found = set(ids), {}
    for df in _read_sheets(path).values():
        uid = _find(df, "", USER_ID_COLS)
        amt = _find(df, "", PAYOUT_AMOUNT_COLS)
        when = _find(df, "", PAYOUT_DATE_COLS)
        if not uid or not (amt or when):
            continue
        for r in df.to_dict("records"):
            cid = str(r[uid]).strip()
            if cid not in wanted:
                continue
            d = found.setdefault(cid, {})
            try:
                if amt and str(r[amt]).strip():
                    d["payout_amount"] = float(str(r[amt]).replace(",", "").replace("₹", ""))
            except ValueError:
                pass
            dt = parse_dt(r[when], ctx.tz) if when else None
            if dt:
                d["payout_date"] = iso(dt)
    for cid, d in found.items():
        lead = one(ctx.conn, "SELECT extra FROM leads WHERE lead_id=?", (cid,))
        if lead:
            extra = json.loads(lead["extra"] or "{}")
            extra.update(d)
            ctx.conn.execute("UPDATE leads SET extra=? WHERE lead_id=?", (json.dumps(extra), cid))
    ctx.conn.commit()


def list_uploads(ctx: Ctx, limit: int = 20) -> list[dict]:
    out = []
    for u in rows(ctx.conn, "SELECT * FROM uploads ORDER BY id DESC LIMIT ?", (limit,)):
        out.append({**u, "client_ids": json.loads(u["client_ids"]), "summary": json.loads(u["summary"] or "{}")})
    return out


def batch_rows(ctx: Ctx, client_ids: list[str]) -> list[dict]:
    """Rows for just this list, in priority order, with the rank inside the list and in the whole base."""
    from .sheet_export import build_rows

    wanted = set(client_ids)
    data = [r for r in build_rows(ctx) if r["client"] in wanted]
    data.sort(key=lambda r: (r["status"] != "open", -(r["heat"] or 0)))
    for i, r in enumerate(data, 1):
        r["batch_rank"] = i if r["status"] == "open" else ""
    return data


def batch_workbook(ctx: Ctx, upload: dict) -> Workbook:
    data = batch_rows(ctx, upload["client_ids"])
    s = upload["summary"]
    wb = Workbook()
    ws = wb.active
    ws.title = "Analysed list"
    head = [h for h, _ in BATCH_COLUMNS]
    ws.append(head)
    for r in data:
        ws.append([r.get(k, "") for _, k in BATCH_COLUMNS])
    widths = [8, 8, 10, 8, 13, 16, 13, 26, 16, 14, 16, 16, 15, 13, 10, 12, 13, 46, 36, 13, 16, 12, 46, 48, 60, 70, 60, 10]
    for i, w in enumerate(widths, 1):
        ws.column_dimensions[get_column_letter(i)].width = w
    for c in ws[1]:
        c.font = Font(name="Arial", bold=True, color="FFFFFF")
        c.fill = PatternFill("solid", fgColor="1F3A5F")
        c.alignment = Alignment(wrap_text=True, vertical="center")
    for row in ws.iter_rows(min_row=2):
        for c in row:
            c.font = Font(name="Arial", size=10)
            c.alignment = Alignment(wrap_text=True, vertical="top")
    n = max(len(data) + 1, 2)
    last = get_column_letter(len(head))
    col = {k: get_column_letter(i) for i, (_, k) in enumerate(BATCH_COLUMNS, 1)}  # formulas follow the column order
    for tier, color in TIER_FILL.items():
        ws.conditional_formatting.add(f"A2:{last}{n}", FormulaRule(formula=[f'${col["priority"]}2="{tier}"'], fill=PatternFill("solid", fgColor=color)))
    ws.freeze_panes = "F2"
    ws.auto_filter.ref = f"A1:{last}{n}"

    sm = wb.create_sheet("Summary")
    P, G, S, C = col["priority"], col["gm"], col["status"], col["client"]
    lines = [
        ("Customer list analysis", ""),
        ("File", s.get("file", upload.get("filename"))),
        ("Uploaded", f"{upload.get('uploaded_at', '')[:16].replace('T', ' ')} by {upload.get('uploaded_by') or ''}"),
        ("", ""),
        ("Clients in the file", f'=COUNTIFS(\'Analysed list\'!{C}2:{C}{n},"<>")'),
        ("New to the system", s.get("new_leads", 0)),
        ("Already known", s.get("already_known", 0)),
        ("🔥 Hot", f'=COUNTIFS(\'Analysed list\'!{P}2:{P}{n},"🔥 Hot",\'Analysed list\'!{S}2:{S}{n},"open")'),
        ("🌤️ Warm", f'=COUNTIFS(\'Analysed list\'!{P}2:{P}{n},"🌤️ Warm",\'Analysed list\'!{S}2:{S}{n},"open")'),
        ("❄️ Cold", f'=COUNTIFS(\'Analysed list\'!{P}2:{P}{n},"❄️ Cold",\'Analysed list\'!{S}2:{S}{n},"open")'),
        ("Assigned to a GM", f'=COUNTIFS(\'Analysed list\'!{G}2:{G}{n},"<>Waiting*",\'Analysed list\'!{S}2:{S}{n},"open")'),
        ("Waiting for GM capacity", f'=COUNTIFS(\'Analysed list\'!{G}2:{G}{n},"Waiting for capacity")'),
        ("Re-activation: watching for engagement", f'=COUNTIFS(\'Analysed list\'!{G}2:{G}{n},"Waiting for engagement*")'),
        ("Client database ageing", s.get("ageing", {}).get("detail", "")),
        ("", ""),
        ("How the heat score works", "core activity ×10, non-core ×3; halves every 3 days since last done; more repeats count more "
                                     "(capped); registration counts as core. 0–100: 🔥 ≥ 70, 🌤️ ≥ 40, ❄️ below."),
        ("Compliance", "Pitch ideas never recommend specific securities or promise returns."),
    ]
    for r, (a, b) in enumerate(lines, 1):
        sm.cell(row=r, column=1, value=a).font = Font(name="Arial", bold=r == 1 or not b, size=14 if r == 1 else 10)
        sm.cell(row=r, column=2, value=b).font = Font(name="Arial", size=10)
    sm.column_dimensions["A"].width = 28
    sm.column_dimensions["B"].width = 90
    wb.move_sheet("Summary", offset=-1)
    wb.active = 0
    return wb
