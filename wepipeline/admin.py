"""Admin-controlled follow-up rules. Only the Admin page (PIN-protected) writes these; GMs and SMs cannot."""
from __future__ import annotations

import hmac
import json
import os

from .context import Ctx, iso
from .db import set_setting

# key: (label, unit, min, max)
RULE_FIELDS = {
    "npc_retry_hours": ("Not picked up: call again after", "hours", 0.5, 48),
    "max_npc_attempts": ("Mark unreachable after this many NPCs in a row", "attempts", 1, 20),
    "warning_after_minutes": ("Overdue warning to the GM after", "minutes", 1, 240),
    "escalate_after_minutes": ("Escalate to the SM and count as missed after", "minutes", 5, 1440),
    "stale_after_days": ("No contact for this long: system follow-up", "days", 1, 30),
}
SLA_TIERS = {"hot": "🔥 Hot", "warm": "🌤️ Warm", "cold": "❄️ Cold"}


def admin_pin() -> str:
    return os.environ.get("ADMIN_PIN", "")


def check_pin(entered: str) -> bool:
    pin = admin_pin()
    return bool(pin) and hmac.compare_digest(entered.strip(), pin)


def current_sla(ctx: Ctx) -> dict[str, int]:
    from .heat import _settings

    return dict(_settings(ctx)["first_call_sla_minutes"])


def save_rules(ctx: Ctx, values: dict, sla: dict[str, int]) -> dict:
    """Validate and store the admin's rule overrides. Raises ValueError on an invalid value."""
    clean = {}
    for key, (label, unit, lo, hi) in RULE_FIELDS.items():
        v = float(values[key])
        if not lo <= v <= hi:
            raise ValueError(f"{label}: must be between {lo} and {hi} {unit}")
        clean[key] = v if key == "npc_retry_hours" else int(v)
    if clean["escalate_after_minutes"] <= clean["warning_after_minutes"]:
        raise ValueError("The escalation time must be later than the warning time")
    for tier, minutes in sla.items():
        if not 5 <= int(minutes) <= 1440:
            raise ValueError(f"First call for {SLA_TIERS[tier]} leads: must be between 5 and 1440 minutes")
    clean["first_call_sla_by_tier"] = {t: int(m) for t, m in sla.items()}
    set_setting(ctx.conn, "rules", json.dumps(clean), iso(ctx.now()))
    ctx.clear_rules_cache()
    return clean
