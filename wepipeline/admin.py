"""Admin-controlled follow-up rules. Only the Admin page (PIN-protected) writes these; GMs and SMs cannot."""
from __future__ import annotations

import hmac
import json
import os

from .context import Ctx, iso
from .db import set_setting

# key: (label, unit, min, max)
RULE_FIELDS = {
    "sarthi_daily_per_gm": ("Sarthi activation calls per GM per drive", "calls", 0, 100),
    "sarthi_cooldown_days": ("Ask the same client to switch on Sarthi at most once every", "days", 7, 365),
    "referral_daily_per_gm": ("Referral asks per GM per day (most active traders)", "calls", 0, 50),
    "referral_cooldown_days": ("Ask the same client for a referral at most once every", "days", 7, 365),
    "referral_reward_referrer": ("Referral reward to the client who refers (0 = don't quote an amount)", "₹", 0, 100000),
    "referral_reward_friend": ("Referral reward to the friend, after their first trade on the app", "₹", 0, 100000),
    "funding_target": ("Funded-account target (OKR): deposit at which a client counts as funded", "₹", 1000, 1000000),
    "registration_window_days": ("Registration window: work new clients for this many days after registration", "days", 7, 365),
    "default_followup_hours": ("Spoke to them, no time agreed: follow up again after", "hours", 1, 168),
    "not_interested_revisit_days": ("'Not interested': try once more after", "days", 1, 90),
    "unreachable_revisit_days": ("Unreachable after the NPC limit: try again after", "days", 1, 90),
    "npc_retry_hours": ("Not picked up: call again after", "hours", 0.5, 48),
    "max_npc_attempts": ("Mark unreachable after this many NPCs in a row", "attempts", 1, 20),
    "warning_after_minutes": ("Overdue warning to the GM after", "minutes", 1, 240),
    "escalate_after_minutes": ("Escalate to the SM and count as missed after", "minutes", 5, 1440),
    "stale_after_days": ("No contact for this long: system follow-up", "days", 1, 30),
}
RULE_DEFAULTS = {"funding_target": 30000, "registration_window_days": 30, "default_followup_hours": 24,
                 "not_interested_revisit_days": 7, "unreachable_revisit_days": 7,
                 "referral_daily_per_gm": 5, "referral_cooldown_days": 60,
                 "sarthi_daily_per_gm": 10, "sarthi_cooldown_days": 30, "referral_reward_referrer": 0,
                 "referral_reward_friend": 0}
SLA_TIERS = {"hot": "🔥 Hot", "warm": "🌤️ Warm", "cold": "❄️ Cold"}


def admin_pin() -> str:
    return os.environ.get("ADMIN_PIN", "")


def check_pin(entered: str) -> bool:
    pin = admin_pin()
    return bool(pin) and hmac.compare_digest(str(entered or "").strip().encode(), pin.encode())


def current_sla(ctx: Ctx) -> dict[str, int]:
    from .heat import _settings

    return dict(_settings(ctx)["first_call_sla_minutes"])


def save_rules(ctx: Ctx, values: dict, sla: dict[str, int]) -> dict:
    """Validate and store the admin's rule overrides. Raises ValueError on an invalid value."""
    clean = {}
    current = {**RULE_DEFAULTS, **ctx.rules}
    for key, (label, unit, lo, hi) in RULE_FIELDS.items():
        v = float(values.get(key, current.get(key, lo)))
        if not lo <= v <= hi:
            raise ValueError(f"{label}: must be between {lo} and {hi} {unit}")
        clean[key] = v if key in {"npc_retry_hours", "default_followup_hours"} else int(v)
    if clean["escalate_after_minutes"] <= clean["warning_after_minutes"]:
        raise ValueError("The escalation time must be later than the warning time")
    for tier, minutes in sla.items():
        if not 5 <= int(minutes) <= 1440:
            raise ValueError(f"First call for {SLA_TIERS[tier]} clients: must be between 5 and 1440 minutes")
    clean["first_call_sla_by_tier"] = {t: int(m) for t, m in sla.items()}
    set_setting(ctx.conn, "rules", json.dumps(clean), iso(ctx.now()))
    ctx.clear_rules_cache()
    return clean
