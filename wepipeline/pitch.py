"""Call pitches built from the lead's category, app activity and today's market notes."""
from __future__ import annotations

import hashlib
import json

from .activity import humanize_ago, summarize
from . import sarthi
from .heat import TIER_BADGE
from .context import Ctx, iso
from .db import get_setting, one, rows

PLAYBOOKS = {
    "kyc_pending": {
        "goal": "Get KYC completed on this call",
        "opener": "I saw you started setting up your account. I can help you finish KYC in about five minutes right now.",
        "points": [
            "Keep PAN, Aadhaar-linked mobile and a cancelled cheque or bank statement handy",
            "Offer to stay on the line while they finish the e-sign step",
            "Account opening is free and fully online",
        ],
    },
    "kyc_no_deposit": {
        "goal": "Get the first deposit and activate Sarthi",
        "opener": "Your account is active. Let's get it funded so you can take Sarthi's ready trade ideas.",
        "points": [
            "Sarthi sends ready trade ideas (entry, target, stop-loss) in equity, F&O and commodities; they choose which to take",
            "Adding funds by UPI is instant, and unused money can be withdrawn",
            "Start with a small amount to get familiar with the app",
            "Walk them through the Add Funds screen while on the call",
        ],
    },
    "funded_no_trade": {
        "goal": "Get the first trade placed, ideally from a Sarthi idea",
        "opener": "You've added funds. I'd like to help you place your first order so you're comfortable with the app.",
        "points": [
            "Open today's Sarthi ideas together and show how to take one",
            "Show how to place, modify and cancel an order (start with a limit order)",
            "Mention SIPs or ETFs for a hands-off start",
            "Point them to research and watchlist features they can use themselves",
        ],
    },
    "dormant_trader": {
        "goal": "Re-activate trading with Sarthi ideas and fresh funds",
        "opener": "It's been a while since your last trade. A few things have changed in the app that I wanted to show you.",
        "points": [
            "Ask what stopped them; listen for pricing, app or confidence issues",
            "Show Sarthi: ready trade ideas so they don't have to research trades themselves",
            "Highlight new features and any current pricing offer",
            "Offer a quick walkthrough of tools such as alerts, screeners and the research desk",
        ],
    },
    "active_trader": {
        "goal": "Deepen engagement (more segments, higher activity)",
        "opener": "Thanks for trading with us. I wanted to check whether you're getting everything you need.",
        "points": [
            "Ask about segments they don't use yet (F&O, commodities, MTF), with the risk disclosures",
            "Collect feedback on execution and app experience",
            "Mention referral benefits",
        ],
    },
}

SEGMENT_LABELS = {"equity_research": "Equity", "fando_research": "F&O", "commodity_research": "Commodity",
                  "master research": "Research home"}

COMPLIANCE = "Do not recommend specific buy/sell actions or promise returns. Share market information as context only."


def market_notes_text(ctx: Ctx) -> str:
    """Notes saved from the dashboard (stored in the database); falls back to the market notes file."""
    saved = get_setting(ctx.conn, "market_notes")
    if saved is not None:
        return saved
    path = ctx.path(ctx.cfg.get("market_notes_file", "data/market_notes.md"))
    return path.read_text(encoding="utf-8") if path.exists() else ""


def market_notes(ctx: Ctx, limit: int = 5) -> list[str]:
    """The SM's own notes first (Data & admin page), then live market notes (refreshed every 15 min)."""
    from .market import live_notes

    lines = [ln.strip().lstrip("-*• ").strip() for ln in market_notes_text(ctx).splitlines()]
    own = [ln for ln in lines if ln and not ln.startswith("#") and not ln.startswith("[Sample]")]
    live = [n["text"] for n in live_notes(ctx)["notes"]]
    return (own + live)[:limit]


def activity_hooks(ctx: Ctx, summary: dict) -> list[str]:
    """Plain-language facts the caller can use as an opener."""
    hooks = []
    sig = summary["signals"]
    if summary["last_seen"]:
        hooks.append(f"Last active in the app {humanize_ago(ctx, summary['last_seen'])}; {summary['app_opens_7d']} app opens in the last 7 days")
    else:
        hooks.append("No app activity in the last 30 days: ask whether they had trouble logging in")
    amount = f" of ₹{summary['last_amount']}" if summary["last_amount"] else ""
    if "deposit_failed" in sig:
        hooks.append(f"A deposit{amount} failed {humanize_ago(ctx, sig['deposit_failed']['last'])}: offer to help retry (UPI or net banking)")
    elif "deposit_initiated" in sig and "deposit_success" not in sig:
        hooks.append(f"Opened Add Funds{amount} {humanize_ago(ctx, sig['deposit_initiated']['last'])} but didn't complete it")
    if "order_abandoned" in sig:
        hooks.append(f"Opened an order window {humanize_ago(ctx, sig['order_abandoned']['last'])} without placing it: ask what held them back")
    if summary["instruments"]:
        hooks.append("Has been looking at " + ", ".join(summary["instruments"]) + ": use this as the conversation starter")
    unfunded = summary["stage"] in {"kyc_pending", "kyc_no_deposit"} and "deposit_success" not in sig
    if "trade_placed" in sig and unfunded:
        hooks.append(f"Tried to place {sig['trade_placed']['count']} order(s) without funds (last {humanize_ago(ctx, sig['trade_placed']['last'])}): "
                     "check whether they were rejected for low balance, then help them add funds")
    if "research_call_accepted" in sig:
        hooks.append(f"Accepted {sig['research_call_accepted']['count']} research call(s) in the app "
                     f"(last {humanize_ago(ctx, sig['research_call_accepted']['last'])}): they want to act on research")
    if summary.get("segments"):
        labels = [SEGMENT_LABELS.get(seg, seg) for seg in summary["segments"]]
        hooks.append("Browsing research for: " + ", ".join(labels) + ". Lead with that segment")
    if "kyc_started" in sig and "kyc_complete" not in sig:
        hooks.append("Started KYC but has not completed it")
    return hooks


def rule_pitch(ctx: Ctx, lead: dict, summary: dict | None = None) -> dict:
    summary = summary or summarize(ctx, lead)
    play = PLAYBOOKS[summary["stage"]]
    return {
        "heat": f"{TIER_BADGE.get(lead.get('heat_tier') or 'cold')} · score {lead.get('heat_score') or 0:.0f}"
                + (f" · {lead['heat_reasons']}" if lead.get("heat_reasons") else ""),
        "stage": summary["stage"],
        "goal": play["goal"],
        "opener": play["opener"],
        "hooks": activity_hooks(ctx, summary),
        "points": play["points"],
        "market": market_notes(ctx, 3),
        "compliance": COMPLIANCE,
    }


def format_pitch(p: dict) -> str:
    out = [f"**Priority:** {p['heat']}", f"**Goal:** {p['goal']}", f"**Opener:** {p['opener']}", "**What they did in the app:**"]
    out += [f"- {h}" for h in p["hooks"]]
    out.append("**Talking points:**")
    out += [f"- {t}" for t in p["points"]]
    if p["market"]:
        out.append("**Market context (today's notes):**")
        out += [f"- {m}" for m in p["market"]]
    out.append(f"_{p['compliance']}_")
    return "\n".join(out)


# ---------------------------------------------------------------- AI pitch (optional)
SYSTEM_PROMPT = """You coach callers on the growth team of an Indian stockbroker. They phone new and existing \
customers to help them finish KYC, add funds and start or resume trading.

The firm's hero feature is Sarthi: ready trade ideas (entry, target, stop-loss) in equity, F&O and commodities that
the customer can review and choose to take. Lead with Sarthi, matched to the customer's segment of interest, and aim
for funds added to the account (margin). The firm offers discount brokerage pricing (see "offers"), with Sarthi ideas and
alerts included, and a dedicated Relationship Manager for customers who find managing trades difficult. If a Sarthi track record is provided, quote it exactly as given (it includes
stop-losses) followed by its disclaimer; never quote other figures or individual wins that are not provided.

Write a short call plan for the caller from the customer facts provided:
- Opener: one or two sentences that reference what the customer actually did in the app.
- Three or four talking points tailored to their stage and activity.
- The most likely objection, with a one-line response.
- A clear close: the single next action to ask for on this call.
Keep it under 170 words, in plain English the caller can read aloud. Markdown bullets are fine.

Compliance rules (SEBI): never recommend buying or selling a specific security, never promise or imply returns or \
earnings (no "earn", "assured", "sure-shot"), \
and present market information only as factual context. Use only the facts provided. If activity is missing, \
say so instead of inventing it."""


def _ai_inputs(ctx: Ctx, lead: dict, summary: dict) -> dict:
    calls = rows(ctx.conn, "SELECT called_at, outcome, notes FROM calls WHERE lead_id=? ORDER BY called_at DESC LIMIT 3", (lead["lead_id"],))
    return {
        "first_name": (lead.get("name") or "").split(" ")[0],  # no phone/email leaves the system
        "category": lead.get("category"),
        "stage": summary["stage"],
        "heat": {"tier": lead.get("heat_tier"), "score": lead.get("heat_score"), "top_activity": lead.get("heat_reasons")},
        "activity_facts": activity_hooks(ctx, summary),
        "recent_events": summary["recent"],
        "previous_calls": calls,
        "npc_attempts": lead.get("npc_count"),
        "market_notes": market_notes(ctx, 5),
        "offers": ctx.cfg.get("offers") or {},
        "hero_feature": {"name": sarthi.name(ctx),
                         "track_record": sarthi.track_line(ctx, (summary.get("segments") or [None])[0] and
                                                           SEGMENT_LABELS.get(summary["segments"][0])),
                         "disclaimer": sarthi.track(ctx)["disclaimer"]},
    }


def ai_pitch(ctx: Ctx, lead: dict, force: bool = False) -> str:
    """Generate (or return the cached) Claude-written pitch. Raises RuntimeError if unavailable."""
    import anthropic

    summary = summarize(ctx, lead)
    inputs = _ai_inputs(ctx, lead, summary)
    basis = hashlib.sha1(json.dumps(inputs, sort_keys=True, default=str).encode()).hexdigest()
    cached = one(ctx.conn, "SELECT * FROM pitches WHERE lead_id=?", (lead["lead_id"],))
    if cached and cached["basis_hash"] == basis and not force:
        return cached["text"]

    client = anthropic.Anthropic()
    try:
        response = client.beta.messages.create(
            model=ctx.cfg.get("ai_pitch", {}).get("model", "claude-opus-5-5"),
            max_tokens=16000,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": json.dumps(inputs, ensure_ascii=False, default=str)}],
            output_config={"effort": "low"},
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )
    except anthropic.AuthenticationError as e:
        raise RuntimeError("Anthropic credentials are missing or invalid (set ANTHROPIC_API_KEY).") from e
    except anthropic.RateLimitError as e:
        raise RuntimeError("Claude is rate limited right now; try again in a minute.") from e
    except anthropic.APIStatusError as e:
        raise RuntimeError(f"Claude API error {e.status_code}: {e.message}") from e
    except anthropic.APIConnectionError as e:
        raise RuntimeError("Could not reach the Claude API (network).") from e

    if response.stop_reason == "refusal":
        raise RuntimeError("Claude declined to write this pitch; use the rule-based pitch.")
    text = "\n".join(b.text for b in response.content if b.type == "text").strip()
    if not text:
        raise RuntimeError("Claude returned an empty pitch.")
    ctx.conn.execute(
        "INSERT INTO pitches (lead_id, text, basis_hash, generated_at) VALUES (?,?,?,?) "
        "ON CONFLICT(lead_id) DO UPDATE SET text=excluded.text, basis_hash=excluded.basis_hash, generated_at=excluded.generated_at",
        (lead["lead_id"], text, basis, iso(ctx.now())),
    )
    ctx.conn.commit()
    return text
