"""Per-customer insight for the bulk list: engagement levers, conversation opener, pitch ideas and next action.

Everything here is derived from data already in the database (app activity, registration date, calls),
so it can be recomputed for every customer on every scheduled run.
"""
from __future__ import annotations

import json
import re
from collections import Counter
from datetime import datetime, timedelta

from .activity import base_event, lead_user_ids, segment_of, summarize
from .clientdb import REGISTERED
from .context import Ctx, iso, parse_dt
from .db import rows
from .heat import TIER_BADGE, contributions, score
from . import call_script
from . import ideas as ideas_mod
from . import sarthi
from .pitch import PLAYBOOKS, market_notes, segment_label

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
    "payout": "\"I need the money\": respect it; the payout is processed as normal. Ask if a small balance can stay for Sarthi's next idea.",
    "sarthi_activation": "\"I trade on my own\": that's fine; Sarthi only sends ideas, they decide, and switching it on doesn't place any trade.",
    "referral": "\"I don't know anyone\": no problem; ask them to keep us in mind and share the app link if someone asks about trading.",
    "low_margin": "\"I've already deposited\": yes, and the account is live; topping up to the funded-account target lets them take more Sarthi ideas at once.",
}


def objection_lines(stage: str, sname: str, pricing: str, rm_line: str) -> str:
    """The two or three objections most likely on this call, each with a one-line answer."""
    if stage == "payout":
        lines = [OBJECTIONS["payout"].replace("\": ", "\" → ", 1)]
        if pricing:
            lines.append(f"\"I'm moving to Zerodha / Groww\" → {pricing}" + (f"; and {rm_line}" if rm_line else "") + ".")
        lines.append(f"\"I lost money\" → {sname} ideas come with a stop-loss; start small, returns aren't guaranteed.")
        return " · ".join(lines)
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
    label = segment_label(seg)
    return f"{name}: {label}" if label and label != "Research home" else name


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


def referral_reward(ctx: Ctx) -> str:
    from .referral import settings as referral_settings

    return referral_settings(ctx)["reward_hi"]


def _engaged(ctx: Ctx, lead: dict) -> str:
    from .drives import engagement

    value, why = engagement(ctx, lead.get("campaign") or "referral", lead["lead_id"])
    return why.replace("campaigns (", "", 1).replace("):", ":", 1) if value > 0 else ""


def _referral_context(ctx: Ctx, lead: dict, stage: str) -> dict:
    """Cohort, tags and past referrals for a referral call (the cohort decides the opening; tags only personalise)."""
    if stage != "referral":
        return {}
    from .referral import cohort, tags

    x = json.loads(lead.get("extra") or "{}")
    return {"referral_cohort": lead.get("segment") or cohort(ctx, lead), "referral_tags": tags(ctx, lead),
            "referral_history": x.get("referral_history") or {}, "referral_profile": x.get("referral_profile") or {}}


def _sarthi_interest(ctx: Ctx, lead: dict) -> str:
    from .activation import interest

    return interest(ctx, lead["lead_id"])[1]


def _event_code(label: str) -> bool:
    """'RECOMMENDATION_INTERNAL_SCREEN', 'DASHBOARD_OPENED_MOB': pivot sub-sheets named after app events, not segments."""
    return "_" in label and re.fullmatch(r"[A-Z0-9_]+", label) is not None


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
    segments = list(dict.fromkeys(l for l in (segment_label(s) for s in summary["segments"])
                                  if l and l != "Research home" and not _event_code(l)))
    preferred = sarthi.segment_from_preference(extra.get("preferred_segment") or (extra.get("referral_profile") or {}).get("preferred_segment")
                                               or (extra.get("sarthi") or {}).get("segment"))
    if preferred and preferred not in segments:
        segments.append(preferred)
    seg = segments[0] if segments else None
    sname = sarthi.name(ctx)
    sarthi_active = extra.get("sarthi_active")  # True / False from the client database, None if unknown
    sarthi_calls = sig.get("research_call_accepted", {}).get("count", 0)
    payout_amt = extra.get("payout_amount")
    payout_txt = f"₹{payout_amt:,.0f} " if payout_amt else ""
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
    ref = extra.get("referral") or {}
    act = extra.get("sarthi") or {}
    if stage == "sarthi_activation":
        from .activation import interest

        keen = interest(ctx, lead["lead_id"])[1]
        levers.insert(0, f"{sname} is off" + (f": {keen}" if keen else ""))
        if act.get("total_trades"):
            levers.append(f"Active trader: {act['total_trades']} trades on {act.get('active_days', 0)} days (last "
                          f"{act.get('window_days', 60)} days); {sname} ideas fit how they already trade")
    if stage == "referral":
        levers.insert(0, f"Top trader: {ref.get('total_trades', 0)} trades on {ref.get('active_days', 0)} trading days "
                         f"in the last {ref.get('window_days', 60)} days ({ref.get('bucket', 'high')} activity)")
    if stage == "payout":
        when = parse_dt(extra.get("payout_date"), ctx.tz)
        levers.insert(0, f"Payout requested{(' of ' + payout_txt.strip()) if payout_txt else ''}"
                         f"{(' on ' + when.strftime('%d %b')) if when else ''}: check in early (the payout proceeds as normal)")
    if lead.get("campaign") == "reactivation" and lead.get("heat_tier") in {"hot", "warm"}:
        levers.append("Re-activation list: engaged in the app again, so this is the moment to call")
    if sarthi_calls:
        levers.append(f"{sname} user: accepted {sarthi_calls} {sname} idea(s): needs funds to act on the next one"
                      if unfunded else f"{sname} user: accepted {sarthi_calls} {sname} idea(s)")
    if segments:
        levers.append(f"{sname}-ready: interested in {', '.join(segments)} ideas")
    needs_sarthi = stage in {"kyc_pending", "kyc_no_deposit", "funded_no_trade", "dormant_trader", "active_trader", "low_margin"}
    if sarthi_active is False:
        levers.append(f"{sname} not activated: activate it on the call (needed to receive the ideas)")
    elif sarthi_active is None and needs_sarthi:
        levers.append(f"{sname} status unknown: check it's activated")
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
    if stage == "sarthi_activation":
        opener = (f"I noticed {sname} isn't switched on for your account yet. It sends ready {seg_ideas} with entry, target and "
                  "stop-loss straight to the app, and you decide which to take. Can I switch it on with you now? It takes a minute.")
    elif stage == "referral":
        opener = (f"Thank you for trading so actively with us: {ref.get('total_trades', 0)} trades in the last "
                  f"{ref.get('window_days', 60)} days. How has your experience been? I also wanted to ask: is there a friend or "
                  "family member who trades, or wants to start, who might like what you get here?")
    elif stage == "payout":
        opener = (f"I'm calling about the {payout_txt}withdrawal you requested. It's being processed as normal; I just wanted "
                  "to check everything is okay with your account, and whether there's anything we could do better?")
    elif stage == "dormant_trader":
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
    if sarthi_active is False and needs_sarthi and stage != "payout":
        opener += f" The first step is switching on {sname} so its ideas start reaching you."

    # ---- pitch ideas: Sarthi story, its complete track record, activation, then margin
    ideas = []
    record = sarthi.track_line(ctx, seg)
    if stage == "sarthi_activation":
        ideas.append(f"Show one {sname} idea (entry, target, stop-loss) and switch it on together in the app on the call")
        ideas.append(f"{record}. Say it as it is: past results don't guarantee future ones" if record else
                     f"{sname} gives ready ideas; the client chooses what to take. No promises on returns")
        ideas.append("Switching it on doesn't place any trade; it only sends ideas. On charges, quote only the official terms")
    if stage == "referral":
        ideas.append("Thank them and ask for feedback first; if something bothers them, note it for the SM before asking")
        ideas.append("Ask for 1-2 friends or family who trade or want to start; take a number only with the friend's consent")
        ideas.append("What the friend gets: " + (pricing or "discount brokerage") + f", {sname} trade ideas, and help setting up")
        from .referral import settings as referral_settings

        r = referral_settings(ctx)
        if r["reward_referrer"] or r["reward_friend"]:
            ideas.append(f"Referral reward: ₹{r['reward_referrer']:,} to them and ₹{r['reward_friend']:,} to the friend, once the "
                         "friend makes their first trade on the app (programme T&C apply). Promise nothing beyond it")
        else:
            ideas.append("Referral reward: only mention the official programme; don't quote or promise an amount")
    if stage == "payout":
        ideas.append("Never delay or discourage the withdrawal: confirm it's being processed, then ask why they're withdrawing")
        if pricing:
            ideas.append(f"If it's charges: {pricing}")
        ideas.append(f"If they're unsure what to trade or had losses: {sname} ideas come with a stop-loss"
                     + (f" ({record})" if record else ""))
        if rm_line:
            ideas.append(f"If managing trades is hard: offer {rm_line}")
        ideas.append(f"Ask if they'd like to keep part of the balance for {sname}'s next idea (entirely their choice)")
    if stage == "payout":
        pass
    elif record:
        ideas.append(f"{record}. Say it as it is, then: past results don't guarantee future ones")
    else:
        ideas.append(f"{sname} explained simply: ready ideas with entry, target and stop-loss in {seg or 'equity, F&O and commodities'}; "
                     "they choose what to take and can start small")
    if sarthi_active is False:
        ideas.append(f"Activate {sname}{' for ' + seg if seg else ''} with them now: it's needed to receive the ideas and is "
                     "included with their account")
    elif sarthi_active is None and needs_sarthi:
        ideas.append(f"Check {sname} is activated (it's needed to receive the ideas); if not, activate it with them now")
    elif sarthi_active:
        ideas.append(f"{sname} is active: open today's {seg or ''} ideas together".replace("  ", " "))
    example = sarthi.highlight_for(ctx, seg) if stage != "payout" else None
    if example:
        ideas.append(f"Recent {sname} example: {example}")
    if (unfunded or stage in {"dormant_trader", "low_margin"}) and stage != "payout":
        ideas.append(f"Ask them to add funds now ({sarthi.starter_amount(ctx, seg)}) so they're ready for {sname}'s next idea")
    price_point = f"Pricing: {pricing}" if pricing and stage in {"kyc_no_deposit", "kyc_pending", "funded_no_trade", "dormant_trader"} else None
    rm_point = f"If managing trades feels like too much, offer {rm_line}" if offer_rm and stage != "payout" else None
    for point in ([rm_point, price_point] if stage == "dormant_trader" or high_value else [price_point, rm_point]):
        if point:
            ideas.append(point)
    if "trade_placed" in sig and unfunded:
        ideas.append("Check whether their orders were rejected for low balance, then help them add funds by UPI on the call")
    if stage == "dormant_trader":
        ideas.append("Ask what stopped them trading (charges, app, losses, time) and note it for the SM")
    if "F&O" in segments:
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

    # ---- call ideas: data point -> angle, in Hinglish (the GM uses their own words; no script)
    def days_since(key):
        last = parse_dt(sig.get(key, {}).get("last"), ctx.tz)
        return (now - last).total_seconds() / 86400 if last else 0
    offers_hi = ctx.cfg.get("offers") or {}
    call_ideas = ideas_mod.build({
        "sname": sname, "seg": seg, "segments": segments, "stage": stage, "unfunded": unfunded,
        "orders_unfunded": sig.get("trade_placed", {}).get("count", 0) if unfunded else 0,
        "orders_funded": sig.get("trade_placed", {}).get("count", 0) if not unfunded else 0,
        "orders_days": days_since("trade_placed"), "sarthi_calls": sarthi_calls, "sarthi_days": days_since("research_call_accepted"),
        "research_views": sig.get("research_viewed", {}).get("count", 0),
        "record": record, "record_hi": sarthi.track_line_hi(ctx, seg), "example": example,
        "sarthi_active": sarthi_active if needs_sarthi else True,
        "reg_days": reg_days, "ltd_days": ltd_days, "first_deposit": first_dep, "holdings": holdings, "high_value": high_value,
        "offer_rm": offer_rm, "starter_hi": sarthi.starter_amount(ctx, seg).replace("an amount they're comfortable with", "jitna comfortable ho"),
        "last24": last24, "best_time": best_time, "npc_count": lead.get("npc_count") or 0,
        "pricing_hi": offers_hi.get("pricing_line_hi", ""), "rm_hi": offers_hi.get("rm_line_hi", ""),
        "market": context, "payout_amount": payout_amt,
        "payout_date": (parse_dt(extra.get("payout_date"), ctx.tz).strftime("%d %b") if extra.get("payout_date") else None),
        "target": sarthi.target(ctx), "margin": sarthi.margin_facts(ctx, seg),
        "current_margin": extra.get("current_margin") or (first_dep if stage == "low_margin" else None),
        "reactivation_engaged": lead.get("campaign") == "reactivation" and lead.get("heat_tier") in {"hot", "warm"},
        "funding_push": lead.get("campaign") == "reactivation",  # churned list: deposit + trades, whatever the trade history
        "referral": ref, "reward_hi": referral_reward(ctx), **_referral_context(ctx, lead, stage),
        "campaign_engaged": _engaged(ctx, lead) if stage in {"referral", "sarthi_activation"} else "",
        "activation": act, "sarthi_interest": _sarthi_interest(ctx, lead) if stage == "sarthi_activation" else "",
    })

    best = f" around {best_time}" if best_time else ""
    action = {"kyc_pending": f"Complete KYC, then introduce {sname}",
              "kyc_no_deposit": f"{sname} activated + {sarthi.starter_amount(ctx, seg)} deposit on the call",
              "funded_no_trade": f"First trade, using a {sname} idea",
              "dormant_trader": f"Funds topped up to {sarthi.starter_amount(ctx, seg)} + first trade back with a {sname} idea",
              "active_trader": f"{sname} in a new segment + more margin",
              "payout": "Find and fix the reason; they stay invested and trade more (cancelling the payout is their choice)",
              "low_margin": f"Top-up to {sarthi.starter_amount(ctx, seg)} on the call + first trade with a {sname} idea",
              "referral": "Thank them, then 1-2 referrals (friend's name + number, with consent)",
              "sarthi_activation": f"{sname} switched on during the call"}[stage]
    if sarthi_active is not True and needs_sarthi and not action.startswith(sname) and stage != "payout":
        action = f"{sname} activated + " + action[0].lower() + action[1:]
    next_action = f"{action}; call{best}" if best else action

    script = call_script.build({
        "sname": sname, "stage": stage, "seg": seg, "target": sarthi.target(ctx), "sarthi_calls": sarthi_calls,
        "research_views": sig.get("research_viewed", {}).get("count", 0), "last24": last24, "best_time": best_time,
        "orders_unfunded": sig.get("trade_placed", {}).get("count", 0) if unfunded else 0, "ltd_days": ltd_days,
        "sarthi_active": sarthi_active if needs_sarthi else True, "reward_hi": referral_reward(ctx),
        "current_margin": extra.get("current_margin") or (first_dep if stage == "low_margin" else None),
        "reg_days": reg_days, "deposit_failed": "deposit_failed" in sig, "payout_amount": payout_amt,
        "holdings": holdings, "high_value": high_value,
    })

    return {
        **script,
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
        "ideas": "\n".join(f"• {i}" for i in ideas[:6]),  # the call card shows the first 3; sheets show all
        "next_action": next_action,
        "objection": objection_lines(stage, sname, pricing, rm_line),
        "call_ideas": call_ideas,
        "propositions": ideas_mod.propositions(ctx.cfg, stage, seg, " ".join(i["angle"] for i in call_ideas)),
        "closing": ideas_mod.closing({"sname": sname, "stage": stage, "target": sarthi.target(ctx), "unfunded": unfunded,
                                      "starter_hi": sarthi.starter_amount(ctx, seg),
                                      "current_margin": extra.get("current_margin") or (first_dep if stage == "low_margin" else None),
                                      "sarthi_active": sarthi_active if needs_sarthi else True,
                                      "funding_push": lead.get("campaign") == "reactivation", "reward_hi": referral_reward(ctx),
                                      **_referral_context(ctx, lead, stage)}, ctx.cfg),
        "call_ideas_text": "\n".join(f"• {i['fact']} → {i['angle']}" for i in call_ideas),
        "closing_text": "\n".join(f"• {c}" for c in ideas_mod.closing({"sname": sname, "stage": stage, "target": sarthi.target(ctx),
                                                                    "unfunded": unfunded, "starter_hi": sarthi.starter_amount(ctx, seg),
                                                                    "current_margin": extra.get("current_margin") or (first_dep if stage == "low_margin" else None),
                                                                    "sarthi_active": sarthi_active if needs_sarthi else True,
                                      "funding_push": lead.get("campaign") == "reactivation", "reward_hi": referral_reward(ctx),
                                      **_referral_context(ctx, lead, stage)}, ctx.cfg)),
        "objections_hi": " · ".join(ideas_mod.objections(stage, sname, offers_hi.get("pricing_line_hi", ""),
                                                         offers_hi.get("rm_line_hi", ""), sarthi.target(ctx), referral_reward(ctx))),
        "sarthi_status": ("active" if sarthi_active else "not activated" if sarthi_active is False else "unknown"),
        "priority": TIER_BADGE.get(lead.get("heat_tier") or "cold"),
    }
