"""Sarthi, the hero feature in every pitch: ready trade ideas (entry, target, stop-loss) in equity, F&O and commodities.

The track record shown to customers comes only from what the admin enters on the Admin page (official Sarthi data,
complete for the period: targets AND stop-losses). Nothing is invented here. Every use carries the disclaimer, and
pitch text never promises earnings.
"""
from __future__ import annotations

import json

from .context import Ctx, iso
from .db import get_setting, set_setting

SEGMENTS = ["Equity", "F&O", "Commodity"]
DEFAULT_DISCLAIMER = ("Past performance of Sarthi ideas does not guarantee future results. Trading involves risk; "
                      "the customer decides which ideas to take.")
PREFERRED_SEGMENT = {"eq": "Equity", "equity": "Equity", "cash": "Equity", "fno": "F&O", "f&o": "F&O", "fo": "F&O",
                     "derivative": "F&O", "derivatives": "F&O", "commodity": "Commodity", "mcx": "Commodity", "comm": "Commodity"}


def name(ctx: Ctx) -> str:
    return (ctx.cfg.get("sarthi") or {}).get("name", "Sarthi")


def segment_from_preference(value) -> str | None:
    v = str(value or "").strip().lower()
    return next((seg for key, seg in PREFERRED_SEGMENT.items() if key == v or key in v.split()), None) if v else None


def track(ctx: Ctx) -> dict:
    raw = get_setting(ctx.conn, "sarthi_track")
    data = json.loads(raw) if raw else {}
    data.setdefault("period", "last 30 days")
    data.setdefault("segments", {})
    data.setdefault("highlights", [])
    data.setdefault("disclaimer", DEFAULT_DISCLAIMER)
    return data


def save_track(ctx: Ctx, period: str, segments: dict[str, dict], highlights: list[str], source: str, disclaimer: str) -> dict:
    """Validate and store the official track record. Raises ValueError on inconsistent numbers."""
    clean = {}
    for seg, v in segments.items():
        closed, target, stop = (int(v.get(k) or 0) for k in ("closed", "target", "stoploss"))
        if closed < 0 or target < 0 or stop < 0:
            raise ValueError(f"{seg}: numbers can't be negative")
        if target + stop > closed:
            raise ValueError(f"{seg}: target hits + stop-loss hits ({target + stop}) can't exceed calls closed ({closed})")
        if closed:
            clean[seg] = {"closed": closed, "target": target, "stoploss": stop}
    data = {"period": period.strip() or "last 30 days", "segments": clean,
            "highlights": [h.strip() for h in highlights if h.strip()], "source": source.strip(),
            "disclaimer": disclaimer.strip() or DEFAULT_DISCLAIMER, "updated": iso(ctx.now())}
    set_setting(ctx.conn, "sarthi_track", json.dumps(data, ensure_ascii=False), iso(ctx.now()))
    return data


def track_line(ctx: Ctx, segment: str | None) -> str | None:
    """'Sarthi F&O, last 30 days: 14 of 20 closed ideas hit target, 6 hit stop-loss' (complete record, never cherry-picked)."""
    t = track(ctx)
    segs = t["segments"]
    if segment in segs:
        s, label = segs[segment], f"{name(ctx)} {segment}"
    elif segs:
        s = {k: sum(v[k] for v in segs.values()) for k in ("closed", "target", "stoploss")}
        label = name(ctx)
    else:
        return None
    line = f"{label}, {t['period']}: {s['target']} of {s['closed']} closed ideas hit target"
    if s["stoploss"]:
        line += f", {s['stoploss']} hit stop-loss"
    return line


def highlight_for(ctx: Ctx, segment: str | None) -> str | None:
    """An admin-approved example; lines may start with 'F&O:' / 'Equity:' / 'Commodity:' to target a segment."""
    lines = track(ctx)["highlights"]
    if segment:
        hit = next((h.split(":", 1)[1].strip() for h in lines if h.lower().startswith(segment.lower() + ":")), None)
        if hit:
            return hit
    return next((h for h in lines if not any(h.lower().startswith(s.lower() + ":") for s in SEGMENTS)), None)


def starter_amount(ctx: Ctx, segment: str | None) -> str:
    amounts = (ctx.cfg.get("sarthi") or {}).get("starter_amount") or {}
    amt = amounts.get(segment) or amounts.get("default")
    return f"₹{int(amt):,}" if amt else "an amount they're comfortable with"
