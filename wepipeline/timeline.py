"""Hour- and day-level view of a client's app activity: what the hourly recency in the heat score is built from.

WebEngage pivots carry hourly counts, so every event row is one hour bucket (event_time) with a count.
"""
from __future__ import annotations

import json
from collections import Counter
from datetime import timedelta

from .activity import lead_user_ids
from .context import Ctx, iso, parse_dt
from .db import rows

DAYS_SHOWN = 14
WINDOWS_H = (1, 6, 24, 72)


def _ago(hours: float) -> str:
    if hours < 1:
        return "within the hour"
    if hours < 24:
        return f"{int(hours)} h ago"
    return f"{int(hours // 24)} d {int(hours % 24)} h ago"


def timeline(ctx: Ctx, lead: dict, lookback_days: int = 30) -> dict:
    """Actions in the last 1/6/24/72 h, active days, peak hour, per-day counts (last 14 days) and per-hour-of-day counts."""
    now = ctx.now()
    ids = lead_user_ids(ctx, lead)
    by_day, by_hour, windows, last = Counter(), Counter(), Counter(), None
    if ids:
        marks = ",".join("?" * len(ids))
        for ev in rows(ctx.conn, f"SELECT event_time, attrs FROM events WHERE user_id IN ({marks}) AND event_time >= ?",
                       (*ids, iso(now - timedelta(days=lookback_days)))):
            t = parse_dt(ev["event_time"], ctx.tz)
            if not t:
                continue
            n = json.loads(ev["attrs"] or "{}").get("count", 1)
            n = int(n) if str(n).isdigit() else 1
            by_day[t.date()] += n
            by_hour[t.hour] += n
            age = (now - t).total_seconds() / 3600
            for w in WINDOWS_H:
                windows[w] += n if age <= w else 0
            last = max(last, t) if last else t
    today = now.date()
    days = [today - timedelta(days=i) for i in range(DAYS_SHOWN)]
    peak = by_hour.most_common(1)[0][0] if by_hour else None
    return {
        "last_active": _ago((now - last).total_seconds() / 3600) if last else "no activity seen",
        **{f"last_{w}h": windows[w] for w in WINDOWS_H},
        "active_days_7": sum(1 for d in by_day if (today - d).days < 7),
        "active_days_30": len(by_day),
        "peak_hour": f"{peak:02d}:00–{(peak + 1) % 24:02d}:00" if peak is not None else "",
        "days": {d: by_day.get(d, 0) for d in days},
        "hours": {h: by_hour.get(h, 0) for h in range(24)},
    }
