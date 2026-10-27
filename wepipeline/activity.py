"""Turn raw WebEngage events into per-lead activity signals and a lifecycle stage."""
from __future__ import annotations

import json
from collections import Counter
from datetime import timedelta

from .context import Ctx, iso, parse_dt
from .db import rows

STAGES = ["kyc_pending", "kyc_no_deposit", "funded_no_trade", "dormant_trader", "active_trader", "payout", "low_margin", "referral", "sarthi_activation"]
INSTRUMENT_KEYS = ["symbol", "scrip", "stock", "instrument", "stock name", "scrip name", "ticker"]
AMOUNT_KEYS = ["amount", "deposit amount", "value", "order value"]


def base_event(name: str) -> str:
    """'RECOMMENDATION_LISTING_OPENED_MOB · equity_research' -> 'recommendation_listing_opened_mob'."""
    return name.split(" · ")[0].strip().lower()


def segment_of(name: str) -> str | None:
    return name.split(" · ", 1)[1] if " · " in name else None


def signal_lookup(ctx: Ctx) -> dict[str, str]:
    """Lower-cased WebEngage event name -> signal."""
    return {name.lower(): signal for signal, names in ctx.cfg.get("signals", {}).items() for name in names}


def lead_user_ids(ctx: Ctx, lead: dict) -> list[str]:
    """Every WebEngage user id that belongs to this lead (explicit id, lead id, phone/email match)."""
    ids = {lead.get("we_user_id"), lead.get("lead_id")}
    if lead.get("phone"):
        ids.update(r["user_id"] for r in rows(ctx.conn, "SELECT user_id FROM profiles WHERE phone=?", (lead["phone"],)))
    if lead.get("email"):
        ids.update(r["user_id"] for r in rows(ctx.conn, "SELECT user_id FROM profiles WHERE email=?", (lead["email"],)))
    return sorted(i for i in ids if i)


def _attr(attrs: dict, keys: list[str]) -> str | None:
    lower = {k.lower(): v for k, v in attrs.items()}
    return next((lower[k] for k in keys if lower.get(k)), None)


def summarize(ctx: Ctx, lead: dict, days: int = 30) -> dict:
    ids = lead_user_ids(ctx, lead)
    now = ctx.now()
    summary = {"events_30d": 0, "app_opens_7d": 0, "last_seen": None, "signals": {}, "instruments": [], "segments": [],
               "last_amount": None, "recent": [], "stage": None}
    if not ids:
        summary["stage"] = stage_for(ctx, lead, summary)
        return summary
    marks = ",".join("?" * len(ids))
    events = rows(
        ctx.conn,
        f"SELECT event_name, event_time, attrs FROM events WHERE user_id IN ({marks}) AND event_time >= ? ORDER BY event_time DESC",
        (*ids, iso(now - timedelta(days=days))),
    )
    lookup = signal_lookup(ctx)
    week_ago = iso(now - timedelta(days=7))
    instruments, segments = Counter(), Counter()
    for ev in events:
        attrs = json.loads(ev["attrs"] or "{}")
        n = int(attrs["count"]) if str(attrs.get("count", "")).isdigit() else 1  # pivot exports carry hourly counts
        signal = lookup.get(base_event(ev["event_name"]))
        if signal:
            s = summary["signals"].setdefault(signal, {"count": 0, "last": ev["event_time"]})
            s["count"] += n
        if segment_of(ev["event_name"]):
            segments[segment_of(ev["event_name"])] += n
        if signal == "app_open" and ev["event_time"] >= week_ago:
            summary["app_opens_7d"] += n
        if signal in {"instrument_viewed", "watchlist_add", "order_abandoned", "trade_placed"}:
            name = _attr(attrs, INSTRUMENT_KEYS)
            if name:
                instruments[name] += 1
        if signal in {"deposit_initiated", "deposit_failed", "deposit_success"} and summary["last_amount"] is None:
            summary["last_amount"] = _attr(attrs, AMOUNT_KEYS)
        if len(summary["recent"]) < 8:
            summary["recent"].append({"event": ev["event_name"], "time": ev["event_time"], "count": n})
    summary["events_30d"] = sum(int(a["count"]) if str((a := json.loads(e["attrs"] or "{}")).get("count", "")).isdigit() else 1
                                for e in events)
    summary["last_seen"] = events[0]["event_time"] if events else None
    summary["instruments"] = [name for name, _ in instruments.most_common(5)]
    summary["segments"] = [name for name, _ in segments.most_common(3)]
    summary["stage"] = stage_for(ctx, lead, summary)
    return summary


def stage_for(ctx: Ctx, lead: dict, summary: dict) -> str:
    if lead.get("campaign") == "payout":  # a withdrawal request outranks everything else we know about the client
        return "payout"
    if lead.get("campaign") == "referral":  # one of our most active traders: thank them, ask for a referral
        return "referral"
    if lead.get("campaign") in ("sarthi", "sarthi_disabled", "sarthi_off"):  # Sarthi off: show it, switch it on together
        return "sarthi_activation"
    if lead.get("campaign") == "low_margin":  # funded, but below the funded-account target
        return "low_margin"
    known = json.loads(lead.get("extra") or "{}").get("stage")  # from the client database (deposit / trade dates)
    if known in STAGES:
        return known
    mapped = ctx.cfg.get("category_stage", {}).get(lead.get("category") or "")
    if mapped in STAGES:
        return mapped
    sig = summary["signals"]
    if "trade_placed" in sig:
        last = parse_dt(sig["trade_placed"]["last"], ctx.tz)
        return "active_trader" if last and ctx.now() - last < timedelta(days=14) else "dormant_trader"
    if "deposit_success" in sig:
        return "funded_no_trade"
    if "kyc_complete" in sig:
        return "kyc_no_deposit"
    return "kyc_pending"


def humanize_ago(ctx: Ctx, ts: str | None) -> str:
    dt = parse_dt(ts, ctx.tz)
    if not dt:
        return "never"
    delta = ctx.now() - dt
    if delta < timedelta(hours=1):
        return f"{max(int(delta.total_seconds() // 60), 1)} min ago"
    if delta < timedelta(days=1):
        return f"{int(delta.total_seconds() // 3600)} h ago"
    return f"{delta.days} d ago"
