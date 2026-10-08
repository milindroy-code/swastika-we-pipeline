"""The real team (GMs and SMs) and the hourly routine, edited by the admin on the Admin page.

Both are stored in the database (settings) and override config.yaml, so changes apply without a redeploy.
"""
from __future__ import annotations

import json
import re
from datetime import time

from .context import Ctx, iso
from .db import get_setting, rows, set_setting


def _slug(name: str, role: str, taken: set[str]) -> str:
    base = f"{role}_" + (re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_") or "member")
    slug, n = base, 2
    while slug in taken:
        slug, n = f"{base}_{n}", n + 1
    return slug


def validate_team(members: list[dict]) -> list[dict]:
    """Clean and check the edited table. Raises ValueError with a message the admin can act on."""
    clean, taken = [], set()
    for m in members:
        name = str(m.get("name") or "").strip()
        if not name:
            continue  # blank row in the editor
        role = str(m.get("role") or "").strip().lower()
        role = {"growth manager": "gm", "gm": "gm", "service manager": "sm", "sm": "sm"}.get(role)
        if not role:
            raise ValueError(f"{name}: role must be GM or SM")
        mid = str(m.get("id") or "").strip() or _slug(name, role, taken)
        if mid in taken:
            raise ValueError(f"Two people share the ID '{mid}'")
        taken.add(mid)
        entry = {"id": mid, "name": name, "role": role,
                 "title": "Growth Manager" if role == "gm" else "Service Manager",
                 "email": str(m.get("email") or "").strip(), "active": bool(m.get("active", True))}
        if role == "gm":
            try:
                raw = m.get("capacity")
                cap = 50 if raw is None or str(raw).strip() in {"", "nan", "None"} else int(float(raw))  # 0 is not "blank"
            except ValueError:
                raise ValueError(f"{name}: capacity must be a number") from None
            if not 1 <= cap <= 1000:
                raise ValueError(f"{name}: capacity must be between 1 and 1000 open clients")
            entry.update(capacity=cap, sm=str(m.get("sm") or "").strip())
            cats = str(m.get("categories") or "").strip()
            if cats:
                entry["categories"] = [c.strip() for c in cats.split(",") if c.strip()]
        clean.append(entry)
    if not any(m["role"] == "sm" for m in clean):
        raise ValueError("Add at least one SM")
    sms = {m["id"] for m in clean if m["role"] == "sm"}
    sm_names = {m["name"].lower(): m["id"] for m in clean if m["role"] == "sm"}
    for m in clean:
        if m["role"] == "gm":
            m["sm"] = sm_names.get(m["sm"].lower(), m["sm"])  # the editor may hold the SM's name
            if m["sm"] not in sms:
                raise ValueError(f"{m['name']}: reports to an SM who isn't in the team")
    if not any(m["role"] == "sm" for m in clean):
        raise ValueError("Add at least one SM")
    if not any(m["role"] == "gm" and m["active"] for m in clean):
        raise ValueError("Add at least one active GM")
    return clean


def save_team(ctx: Ctx, members: list[dict]) -> dict:
    """Store the team and move the leads of GMs who were removed or deactivated to active GMs
    (pending follow-ups and promised callbacks move with them)."""
    from .assign import open_load, pick_caller
    from .followups import reassign_lead

    clean = validate_team(members)
    set_setting(ctx.conn, "team", json.dumps(clean), iso(ctx.now()))
    ctx.clear_rules_cache()
    active = {m["id"] for m in clean if m["role"] == "gm" and m["active"]}
    load = open_load(ctx)
    moved, released = 0, 0
    for lead in rows(ctx.conn, "SELECT lead_id, assigned_to, category, heat_score FROM leads WHERE status='open' "
                               "AND assigned_to IS NOT NULL ORDER BY heat_score DESC"):
        if lead["assigned_to"] in active:
            continue
        new = pick_caller(ctx, lead["category"], load)
        if new:
            reassign_lead(ctx, lead["lead_id"], new)
            load[new] = load.get(new, 0) + 1
            moved += 1
        else:  # nobody has room: back to the pool, assigned (hottest first) as capacity frees up
            ctx.conn.execute("UPDATE leads SET assigned_to=NULL, assigned_at=NULL WHERE lead_id=?", (lead["lead_id"],))
            ctx.conn.execute("UPDATE followups SET status='cancelled', reason=COALESCE(reason,'') || ' (released: GM left the team)' "
                             "WHERE lead_id=? AND status='pending'", (lead["lead_id"],))
            released += 1
    ctx.conn.commit()
    return {"members": len(clean), "gms": len(active), "leads_moved": moved, "leads_back_to_pool": released}


def validate_routine(blocks: list[dict]) -> list[dict]:
    clean = []
    for b in blocks:
        text = str(b.get("block") or "").strip()
        t = str(b.get("time") or "").strip()
        if not text and not t:
            continue
        try:
            t = time.fromisoformat(t if len(t) > 4 else f"0{t}").strftime("%H:%M")
        except ValueError:
            raise ValueError(f"'{b.get('time')}' is not a time (use HH:MM, e.g. 14:00)") from None
        if not text:
            raise ValueError(f"{t}: say what this block is for")
        clean.append({"time": t, "block": text, "nudge": bool(b.get("nudge", True))})
    if not clean:
        raise ValueError("Add at least one block")
    clean.sort(key=lambda b: b["time"])
    if len({b["time"] for b in clean}) != len(clean):
        raise ValueError("Two blocks start at the same time")
    return clean


def save_routine(ctx: Ctx, blocks: list[dict]) -> list[dict]:
    clean = validate_routine(blocks)
    set_setting(ctx.conn, "day_structure", json.dumps(clean, ensure_ascii=False), iso(ctx.now()))
    ctx.clear_rules_cache()
    return clean


def stored(ctx: Ctx, key: str):
    raw = get_setting(ctx.conn, key)
    return json.loads(raw) if raw else None
