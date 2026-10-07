"""Per-customer insight for the bulk list: engagement levers, conversation opener, pitch ideas and next action.

Everything here is derived from data already in the database (app activity, registration date, calls),
so it can be recomputed for every customer on every scheduled run.
"""
from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timedelta

from .activity import base_event, lead_user_ids, segment_of, summarize
from .clientdb import REGISTERED
from .context import Ctx, iso, parse_dt
from .db import rows
from .heat import TIER_BADGE, contributions, score
from .pitch import PLAYBOOKS, SEGMENT_LABELS, market_notes

DEFAULT_LABELS = {
    "ORDER_PLACED_MOB": "Placed an order",
    "CALL_ACCEPTED_MOB": "Accepted a research call",
    "RECOMMENDATION_LISTING_OPENED_MOB": "Opened research",
    "RECOMMENDATION_LANDING_SCREEN_MOB": "Visited research home",
    "DASHBOARD_OPENED_MOB": "Opened the app dashboard",
    REGISTERED: "Registered",
}

OBJECTIONS = {
    "kyc_pending": "\"I'll do it later\": offer to finish KYC together on this call; it takes about five minutes.",
    "kyc_no_deposit": "\"I'll add money later\": suggest a small first amount by UPI on the call; unused funds can be withdrawn any time.",
    "funded_no_trade": "\"I don't know what to buy\": show the research section and how to place a limit order; the choice stays theirs.",
    "dormant_trader": "\"Charges / app issues\": ask what stopped them, note it for the SM, and show what has changed.",
    "active_trader": "\"I'm fine for now\": ask for feedback and mention segments they haven't activated, with risk disclosures.",
}


def activity_label(ctx: Ctx, activity: str) -> str:
    labels = {**DEFAULT_LABELS, **(ctx.cfg.get("activity_labels") or {})}
    base = activity.split(" · ")[0].strip()
    name = labels.get(base) or labels.get(base.upper()) or base.replace("_MOB", "").replace("_", " ").title()
    seg = segment_of(activity)
    return f"{name}: {SEGMENT_LABELS.get(seg, seg)}" if seg and seg != "master research" else name


def _when(days: float) -> str:
    if days < 1 / 24:
        return "just now"
    if days < 1:
        return f"{int(days * 24)} h ago"
    return "yesterday" if days < 2 else f"{int(days)} d ago"


def _gap(days: int) -> str:
    """Spoken form of a long gap: '45 days', '8 months', 'over 3 years'."""
    if days < 60:
        return f"{days} days"
    if days < 365:
        return f"{days // 30} months"
    years = days // 365
    return f"over {years} year{'s' if years > 1 else ''}"


def _hour_window(h: int) -> str:
    fmt = lambda x: datetime(2000, 1, 1, x % 24).strftime("%I %p").lstrip("0")
    return f"{fmt(h)}–{fmt(h + 1)}"


def registration(ctx: Ctx, lead: dict) -> datetime | None:
    extra = json.loads(lead.get("extra") or "{}")
    return parse_dt(extra.get("registration_date"), ctx.tz)


def customer_insight(ctx: Ctx, lead: dict) -> dict:
    now = ctx.now()
    ids = lead_user_ids(ctx, lead)
    _, parts = score(ctx, contributions(ctx, ids))
    summary = summarize(ctx, lead)
    sig, stage = summary["signals"], summary["stage"]

    # hour-of-day and momentum from timestamped events (WebEngage pivots carry hourly counts)
    hours, last24, prev48 = Counter(), 0, 0
    if ids:
        marks = ",".join("?" * len(ids))
        for ev in rows(ctx.conn, f"SELECT event_time, attrs FROM events WHERE user_id IN ({marks}) AND event_time >= ?",
                       (*ids, iso(now - timedelta(days=30)))):
            n = json.loads(ev["attrs"] or "{}").get("count", 1)
            n = int(n) if str(n).isdigit() else 1
            t = parse_dt(ev["event_time"], ctx.tz)
            hours[t.hour] += n
            age = now - t
            last24 += n if age <= timedelta(hours=24) else 0
            prev48 += n if timedelta(hours=24) < age <= timedelta(hours=72) else 0
    best_time = _hour_window(hours.most_common(1)[0][0]) if sum(hours.values()) >= 3 else ""
    momentum = ("Inactive" if not (last24 or prev48) else "Cooling" if not last24
                else "Rising" if last24 >= max(prev48 / 2, 1) else "Steady")

    core = [p for p in parts if p["core"] and p["activity"] != REGISTERED]
    non_core = [p for p in parts if not p["core"] and p["activity"] != REGISTERED]
    detail = lambda ps: "; ".join(f"{activity_label(ctx, p['activity'])} ×{p['count']} ({_when(p['days'])})" for p in ps)

    reg = registration(ctx, lead)
    reg_days = (now.date() - reg.date()).days if reg else None
    extra = json.loads(lead.get("extra") or "{}")
    ltd_days = extra.get("days_since_last_trade")
    holdings = extra.get("holdings")
    first_dep = extra.get("first_deposit")
    segments = [SEGMENT_LABELS.get(s, s) for s in summary["segments"] if s != "master research"]

    # ---- engagement levers: strongest first
    levers = []
    unfunded = stage in {"kyc_pending", "kyc_no_deposit"} and "deposit_success" not in sig
    if "trade_placed" in sig and unfunded:
        levers.append(f"Funding blocker: tried to place {sig['trade_placed']['count']} order(s) with no funds in the account")
    if "research_call_accepted" in sig:
        levers.append(f"Research-led: accepted {sig['research_call_accepted']['count']} research call(s)")
    if segments:
        levers.append(f"Segment interest: {', '.join(segments)} research")
    if "trade_placed" in sig and not unfunded:
        levers.append(f"Order intent: placed {sig['trade_placed']['count']} order(s) recently; check they went through (margin / balance)")
    if stage == "dormant_trader" and ltd_days is not None:
        levers.append(f"Dormant: last trade {_gap(ltd_days)} ago ({ltd_days:,} days), now back in the app")
    if stage == "funded_no_trade":
        dep = f" ₹{first_dep:,.0f}" if first_dep else ""
        levers.append(f"Funded, never traded: deposited{dep} but no trade yet")
    if holdings and holdings >= ctx.cfg.get("heat", {}).get("high_holdings", 100000):
        levers.append(f"High-value: holdings of ₹{holdings:,.0f}")
    if "deposit_failed" in sig:
        levers.append("Payment friction: a deposit attempt failed")
    if last24 >= 3:
        levers.append(f"Active right now: {last24} app actions in the last 24 h")
    browse = sum(p["count"] for p in non_core)
    if browse >= 5 and not core:
        levers.append(f"Browsing only: {browse} app visits with no core action yet")
    if reg_days is not None and stage == "kyc_no_deposit":
        if reg_days <= 2:
            levers.append(f"Fresh sign-up: registered {('today' if reg_days == 0 else f'{reg_days} d ago')}")
        elif reg_days > 15 and not core:
            levers.append(f"At risk: {reg_days} days since registration with no deposit")
        elif reg_days > 7 and not core:
            levers.append(f"Going cold: {reg_days} days since registration")
    if not parts or all(p["activity"] == REGISTERED for p in parts):
        levers.append("No app activity seen: check whether they could log in")
    if lead.get("npc_count", 0) >= 3:
        levers.append(f"Hard to reach: {lead['npc_count']} NPCs in a row" + (f"; try {best_time}" if best_time else ""))

    # ---- conversation opener: from the strongest lever
    play = PLAYBOOKS[stage]
    if stage == "dormant_trader" and ("research_call_accepted" in sig or segments):
        gap = _gap(ltd_days) if ltd_days else "a while"
        opener = (f"It's been {gap} since your last trade, and I saw you've been checking our "
                  f"{segments[0] + ' ' if segments else ''}research again. Can I help you get set up to act on it?")
    elif stage == "dormant_trader":
        opener = f"It's been {_gap(ltd_days)} since your last trade. I wanted to check in and show you what's new in the app." if ltd_days else play["opener"]
    elif stage == "funded_no_trade":
        opener = "Your account is funded but you haven't placed a trade yet. Can I walk you through your first order?"
    elif stage == "active_trader":
        opener = play["opener"]
    elif "trade_placed" in sig and unfunded:
        opener = "I noticed you tried to place an order in the app. It may not have gone through because the account isn't funded yet. Shall I help you add funds so your next order goes through?"
    elif "research_call_accepted" in sig:
        opener = "I saw you've been following our research calls. Your account just needs funding before you can act on them. I can help you do that in a couple of minutes."
    elif segments:
        opener = f"I saw you've been looking at our {segments[0]} research. Can I walk you through how to get started in {segments[0]}?"
    elif reg_days is not None and reg_days <= 2:
        opener = "Welcome aboard! I'm your Growth Manager. I'm calling to help you get set up and answer any questions about the app."
    elif reg_days is not None and reg_days > 15:
        opener = "You opened your account a few weeks ago and I wanted to check in. Was anything stopping you from getting started?"
    else:
        opener = play["opener"]

    # ---- pitch ideas
    ideas = []
    if "trade_placed" in sig and unfunded:
        ideas.append("Check whether their orders were rejected for low balance, then help them add funds by UPI on the call")
    if "research_call_accepted" in sig or segments:
        ideas.append("Show where research calls appear in the app, and that acting on them needs a funded account (no return promises)"
                     if unfunded else "Show how to act on a research call from the app, and check their available margin (no return promises)")
    if stage == "dormant_trader":
        ideas.append("Ask what stopped them trading (charges, app, losses, time) and note it for the SM")
    if "fando_research" in summary["segments"]:
        ideas.append("If they want F&O: explain margin and the mandatory risk disclosure before anything else")
    if momentum == "Rising":
        ideas.append("They're active right now, so call today rather than tomorrow")
    if not core and browse:
        ideas.append("Guide them to one first core step (research → add funds) while they're on the call")
    ideas += [p for p in play["points"] if p not in ideas][: max(0, 3 - len(ideas))]
    notes = market_notes(ctx, 1)
    if notes:
        ideas.append(f"Market context: {notes[0]}")

    best = f" around {best_time}" if best_time else ""
    action = {"kyc_pending": "Complete KYC on the call", "kyc_no_deposit": "Get the first deposit on the call",
              "funded_no_trade": "Help place the first order", "dormant_trader": "Re-activate: first trade back",
              "active_trader": "Add a segment / collect feedback"}[stage]
    next_action = f"{action}; call{best}" if lead.get("heat_tier") != "cold" or best else action

    return {
        "deposit_status": extra.get("deposit_status", ""),
        "first_deposit_date": (parse_dt(extra.get("first_deposit_date"), ctx.tz) or now).strftime("%d %b %Y") if extra.get("first_deposit_date") else "",
        "last_trade_date": (parse_dt(extra.get("last_trade_date"), ctx.tz) or now).strftime("%d %b %Y") if extra.get("last_trade_date") else "",
        "days_since_last_trade": ltd_days if ltd_days is not None else "",
        "holdings": round(holdings) if holdings else "",
        "account_status": extra.get("account_status", ""),
        "registered_on": reg.strftime("%d %b %Y") if reg else "",
        "days_since_registration": reg_days if reg_days is not None else "",
        "stage": stage.replace("_", " "),
        "last_active": summary["last_seen"],
        "events_30d": summary["events_30d"],
        "core_actions": sum(p["count"] for p in core),
        "non_core_actions": browse,
        "core_detail": detail(core),
        "non_core_detail": detail(non_core),
        "momentum": f"{momentum} ({last24} in last 24 h)" if last24 or prev48 else momentum,
        "best_time": best_time,
        "segments": ", ".join(segments),
        "levers": "\n".join(f"• {l}" for l in levers[:4]),
        "opener": opener,
        "ideas": "\n".join(f"• {i}" for i in ideas[:4]),
        "next_action": next_action,
        "objection": OBJECTIONS[stage],
        "priority": TIER_BADGE.get(lead.get("heat_tier") or "cold"),
    }
