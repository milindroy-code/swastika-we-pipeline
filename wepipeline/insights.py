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
from . import sarthi
from .pitch import PLAYBOOKS, SEGMENT_LABELS, market_notes

DEFAULT_LABELS = {
    "ORDER_PLACED_MOB": "Placed an order",
    "CALL_ACCEPTED_MOB": "Accepted a Sarthi idea",
    "RECOMMENDATION_LISTING_OPENED_MOB": "Opened Sarthi ideas",
    "RECOMMENDATION_LANDING_SCREEN_MOB": "Visited Sarthi home",
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


def objection_lines(stage: str, sname: str, pricing: str, rm_line: str) -> str:
    """The two or three objections most likely on this call, each with a one-line answer."""
    lines = []
    if stage in {"kyc_no_deposit", "funded_no_trade", "dormant_trader", "kyc_pending"}:
        lines.append(f"\"I don't know what to trade\" → that's what {sname} is for: ready ideas with a stop-loss; start small, "
                     "returns aren't guaranteed.")
    if pricing and stage != "active_trader":
        lines.append(f"\"Charges are high\" / \"I already use Zerodha or Groww\" → {pricing}.")
    if rm_line and stage in {"dormant_trader", "funded_no_trade", "active_trader"}:
        lines.append(f"\"No time to manage trades\" → we offer {rm_line}.")
    lines.append(OBJECTIONS[stage].replace("\": ", "\" → ", 1))
    return " · ".join(lines[:3])


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
    preferred = sarthi.segment_from_preference(extra.get("preferred_segment"))
    if preferred and preferred not in segments:
        segments.append(preferred)
    seg = segments[0] if segments else None
    sname = sarthi.name(ctx)
    sarthi_active = extra.get("sarthi_active")  # True / False from the client database, None if unknown
    sarthi_calls = sig.get("research_call_accepted", {}).get("count", 0)
    offers = ctx.cfg.get("offers") or {}
    pricing = offers.get("pricing_line", "")
    rm_line = offers.get("rm_line", "")
    high_value = bool(holdings and holdings >= ctx.cfg.get("heat", {}).get("high_holdings", 100000))
    offer_rm = bool(rm_line) and (stage in set(offers.get("rm_for_stages", [])) or (high_value and "high_value" in offers.get("rm_for_stages", [])))

    # ---- engagement levers: strongest first
    levers = []
    unfunded = stage in {"kyc_pending", "kyc_no_deposit"} and "deposit_success" not in sig
    if "trade_placed" in sig and unfunded:
        levers.append(f"Funding blocker: tried to place {sig['trade_placed']['count']} order(s) with no funds in the account")
    if sarthi_calls:
        levers.append(f"{sname} user: accepted {sarthi_calls} {sname} idea(s): needs funds to act on the next one"
                      if unfunded else f"{sname} user: accepted {sarthi_calls} {sname} idea(s)")
    if segments:
        levers.append(f"{sname}-ready: interested in {', '.join(segments)} ideas")
    if sarthi_active is False:
        levers.append(f"{sname} not activated yet: activate it on the call")
    if "trade_placed" in sig and not unfunded:
        levers.append(f"Order intent: placed {sig['trade_placed']['count']} order(s) recently; check they went through (margin / balance)")
    if stage == "dormant_trader" and ltd_days is not None:
        levers.append(f"Dormant: last trade {_gap(ltd_days)} ago ({ltd_days:,} days), now back in the app")
    if stage == "funded_no_trade":
        dep = f" ₹{first_dep:,.0f}" if first_dep else ""
        levers.append(f"Funded, never traded: deposited{dep} but no trade yet")
    if holdings and holdings >= ctx.cfg.get("heat", {}).get("high_holdings", 100000):
        levers.append(f"High-value: holdings of ₹{holdings:,.0f}" + ("; offer a dedicated RM" if offer_rm else ""))
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

    # ---- conversation opener: Sarthi is the hero feature; the goal is funds in the account (margin)
    play = PLAYBOOKS[stage]
    seg_ideas = f"{seg} trade ideas" if seg else "trade ideas in equity, F&O and commodities"
    if stage == "dormant_trader":
        gap = _gap(ltd_days) if ltd_days else "a while"
        opener = (f"It's been {gap} since your last trade. {sname} now sends ready {seg_ideas}, each with entry, target and "
                  f"stop-loss, so you don't have to research trades yourself. Can I show you today's ideas?")
    elif stage == "funded_no_trade":
        opener = (f"Your account is funded but you haven't traded yet. {sname} is an easy way to start: it gives ready "
                  f"{seg_ideas} with entry, target and stop-loss, and you decide which to take. Shall we look at today's?")
    elif stage == "active_trader":
        opener = (f"Thanks for trading with us. Have you tried {sname}? It sends ready {seg_ideas} you can review and "
                  f"take in a tap.")
    elif "trade_placed" in sig and unfunded:
        opener = ("I noticed you tried to place an order in the app; it couldn't go through because the account isn't funded yet. "
                  f"Once you add funds you can also take {sname}'s ready {seg_ideas}. Shall I help you add funds now?")
    elif sarthi_calls and unfunded:
        opener = (f"I saw you've been following {sname}'s {seg_ideas}. To take the next one, your account just needs funds. "
                  "I can help you add them in a couple of minutes.")
    elif segments and unfunded:
        opener = (f"I saw you've been looking at {seg} ideas in the app. {sname} sends ready {seg} trade ideas with entry, "
                  "target and stop-loss, so you don't have to pick trades yourself. Shall I help you get set up?")
    elif reg_days is not None and reg_days <= 2:
        opener = (f"Welcome aboard! I'm your Growth Manager. If picking what to trade feels like a lot, {sname} does the homework: "
                  "ready trade ideas in equity, F&O and commodities, and you choose which to take. Shall I help you get started?")
    elif unfunded:
        opener = (f"You opened your account a little while ago. Many people find choosing trades overwhelming, which is why "
                  f"{sname} sends ready trade ideas with entry, target and stop-loss. Shall I help you add funds and try the next one?")
    else:
        opener = play["opener"]

    # ---- pitch ideas: Sarthi story, its complete track record, activation, then margin
    ideas = []
    record = sarthi.track_line(ctx, seg)
    if record:
        ideas.append(f"{record}. Say it as it is, then: past results don't guarantee future ones")
    else:
        ideas.append(f"{sname} explained simply: ready ideas with entry, target and stop-loss in {seg or 'equity, F&O and commodities'}; "
                     "they choose what to take and can start small")
    example = sarthi.highlight_for(ctx, seg)
    if example:
        ideas.append(f"Recent {sname} example: {example}")
    if sarthi_active is False:
        ideas.append(f"Activate {sname}{' for ' + seg if seg else ''} with them on the call")
    elif sarthi_active:
        ideas.append(f"{sname} is active: open today's {seg or ''} ideas together".replace("  ", " "))
    if unfunded or stage == "dormant_trader":
        ideas.append(f"Ask them to add funds now ({sarthi.starter_amount(ctx, seg)}) so they're ready for {sname}'s next idea")
    price_point = f"Pricing: {pricing}" if pricing and stage in {"kyc_no_deposit", "kyc_pending", "funded_no_trade", "dormant_trader"} else None
    rm_point = f"If managing trades feels like too much, offer {rm_line}" if offer_rm else None
    for point in ([rm_point, price_point] if stage == "dormant_trader" or high_value else [price_point, rm_point]):
        if point:
            ideas.append(point)
    if "trade_placed" in sig and unfunded:
        ideas.append("Check whether their orders were rejected for low balance, then help them add funds by UPI on the call")
    if stage == "dormant_trader":
        ideas.append("Ask what stopped them trading (charges, app, losses, time) and note it for the SM")
    if "fando_research" in summary["segments"]:
        ideas.append("If they want F&O: explain margin and the mandatory risk disclosure before anything else")
    if momentum == "Rising":
        ideas.append("They're active right now, so call today rather than tomorrow")
    if not core and browse:
        ideas.append("Guide them to one first core step (research → add funds) while they're on the call")
    ideas += [p for p in play["points"] if p not in ideas and sname not in p][: max(0, 3 - len(ideas))]  # Sarthi already covered
    from .market import note_for

    context = note_for(ctx, segments) or next(iter(market_notes(ctx, 1)), None)
    if context:
        ideas.append(f"Market context (facts only, no tips): {context}")

    best = f" around {best_time}" if best_time else ""
    action = {"kyc_pending": f"Complete KYC, then introduce {sname}",
              "kyc_no_deposit": f"Funds added on the call + {sname} activated",
              "funded_no_trade": f"First trade, using a {sname} idea",
              "dormant_trader": f"Funds topped up + first trade back with a {sname} idea",
              "active_trader": f"{sname} in a new segment + more margin"}[stage]
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
        "objection": objection_lines(stage, sname, pricing, rm_line),
        "sarthi_status": ("active" if sarthi_active else "not activated" if sarthi_active is False else ""),
        "priority": TIER_BADGE.get(lead.get("heat_tier") or "cold"),
    }
