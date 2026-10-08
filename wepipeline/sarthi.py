"""Sarthi, the hero feature in every pitch: ready trade ideas (entry, target, stop-loss) in equity, F&O and commodities.

The track record comes from the research database (every closed Sarthi idea in [ReplResearchPanelDB].[dbo].[tblScripOrder]:
profits AND losses, early exits and stop-losses included) or, failing that, from what the admin enters. Nothing is invented. Every use carries the disclaimer, and
pitch text never promises earnings.
"""
from __future__ import annotations

import json

from .context import Ctx, iso
from .db import get_setting, set_setting

SEGMENTS = ["Equity", "F&O", "Commodity"]
DEFAULT_DISCLAIMER = ("Past performance of Sarthi ideas does not guarantee future results. Trading involves risk; "
                      "the customer decides which ideas to take.")
PREFERRED_SEGMENT = {"eq": "Equity", "equity": "Equity", "cash": "Equity", "delivery": "Equity", "intraday": "Equity",
                     "fno": "F&O", "f&o": "F&O", "fo": "F&O", "derivative": "F&O", "derivatives": "F&O",
                     "commodity": "Commodity", "mcx": "Commodity", "comm": "Commodity"}


def name(ctx: Ctx) -> str:
    return (ctx.cfg.get("sarthi") or {}).get("name", "Sarthi")


def segment_from_preference(value) -> str | None:
    """'Intraday,Delivery,FNO' -> 'F&O' (the most specialised segment they enabled); 'Delivery' -> 'Equity'."""
    tokens = [t.strip().lower() for t in str(value or "").replace("/", ",").split(",") if t.strip()]
    found = {PREFERRED_SEGMENT[t] for t in tokens if t in PREFERRED_SEGMENT}
    return next((s for s in ("F&O", "Commodity", "Equity") if s in found), None)


def track(ctx: Ctx) -> dict:
    raw = get_setting(ctx.conn, "sarthi_track")
    data = json.loads(raw) if raw else {}
    data.setdefault("period", "last 30 days")
    data.setdefault("segments", {})
    data.setdefault("highlights", [])
    data.setdefault("disclaimer", DEFAULT_DISCLAIMER)
    return data


def save_track(ctx: Ctx, period: str, segments: dict[str, dict], highlights: list[str], source: str, disclaimer: str) -> dict:
    """Validate and store the track record (closed ideas, ended in profit, ended in loss). Raises ValueError on bad numbers."""
    clean = {}
    for seg, v in segments.items():
        closed, profit, loss = (int(v.get(k) or 0) for k in ("closed", "profit", "loss"))
        if min(closed, profit, loss) < 0:
            raise ValueError(f"{seg}: numbers can't be negative")
        if profit + loss > closed:
            raise ValueError(f"{seg}: ideas in profit + in loss ({profit + loss}) can't exceed ideas closed ({closed})")
        if closed:
            clean[seg] = {"closed": closed, "profit": profit, "loss": loss,
                          **{k: int(v[k]) for k in ("target_hit", "stoploss_hit") if v.get(k) is not None}}
    data = {"period": period.strip() or "last 30 days", "segments": clean, "margins": track(ctx).get("margins", {}),
            "highlights": [h.strip() for h in highlights if h.strip()], "source": source.strip(),
            "disclaimer": disclaimer.strip() or DEFAULT_DISCLAIMER, "updated": iso(ctx.now())}
    set_setting(ctx.conn, "sarthi_track", json.dumps(data, ensure_ascii=False), iso(ctx.now()))
    return data


def track_line(ctx: Ctx, segment: str | None) -> str | None:
    """'Sarthi F&O, last 30 days: 41 of 60 closed ideas ended in profit, 19 in loss' (every closed idea, never cherry-picked)."""
    t = track(ctx)
    segs = {k: v for k, v in t["segments"].items() if "profit" in v}
    if segment in segs:
        s, label = segs[segment], f"{name(ctx)} {segment}"
    elif segs:
        s = {k: sum(v[k] for v in segs.values()) for k in ("closed", "profit", "loss")}
        label = name(ctx)
    else:
        return None
    line = f"{label}, {t['period']}: {s['profit']} of {s['closed']} closed ideas ended in profit"
    if s["loss"]:
        line += f", {s['loss']} in loss"
    return line


def track_line_hi(ctx: Ctx, segment: str | None) -> str | None:
    """Hinglish: 'Sarthi F&O: last 30 din mein 44 mein se 26 ideas profit mein close hue, 18 loss mein'."""
    t = track(ctx)
    segs = {k: v for k, v in t["segments"].items() if "profit" in v}
    if segment in segs:
        s, label = segs[segment], f"{name(ctx)} {segment}"
    elif segs:
        s = {k: sum(v[k] for v in segs.values()) for k in ("closed", "profit", "loss")}
        label = name(ctx)
    else:
        return None
    period = t["period"].replace("last ", "last ").replace(" days", " din")
    line = f"{label}: {period} mein {s['closed']} mein se {s['profit']} ideas profit mein close hue"
    return line + (f", {s['loss']} loss mein" if s["loss"] else "")


def highlight_for(ctx: Ctx, segment: str | None) -> str | None:
    """An admin-approved example; lines may start with 'F&O:' / 'Equity:' / 'Commodity:' to target a segment."""
    lines = track(ctx)["highlights"]
    if segment:
        hit = next((h.split(":", 1)[1].strip() for h in lines if h.lower().startswith(segment.lower() + ":")), None)
        if hit:
            return hit
    return next((h for h in lines if not any(h.lower().startswith(s.lower() + ":") for s in SEGMENTS)), None)


def target(ctx: Ctx) -> int:
    return int(ctx.cfg.get("funding_target") or 0)


def starter_amount(ctx: Ctx, segment: str | None) -> str:
    amounts = (ctx.cfg.get("sarthi") or {}).get("starter_amount") or {}
    amt = amounts.get(segment) or amounts.get("default") or target(ctx)
    return f"₹{int(amt):,}" if amt else "an amount they're comfortable with"


def margin_facts(ctx: Ctx, segment: str | None) -> dict | None:
    """Typical margin of recent Sarthi ideas in a segment, and how much of the flow the funding target covers."""
    m = (track(ctx).get("margins") or {}).get(segment or "")
    return m if m and m.get("ideas") else None


# ---------------------------------------------------------------- official numbers from the research database
TRACK_SQL = """
SELECT
    CASE WHEN InstrumentName = 'COMMODITY' THEN 'Commodity'
         WHEN SegmentName IN ('OPTIONS', 'FUTURES') THEN 'F&O'
         ELSE 'Equity' END AS Segment,
    COUNT(*) AS closed,
    SUM(CASE WHEN CalculatedReturn > 0 THEN 1 ELSE 0 END) AS profit,
    SUM(CASE WHEN CalculatedReturn < 0 THEN 1 ELSE 0 END) AS loss,
    SUM(CASE WHEN Message LIKE 'Profit Target hit%' THEN 1 ELSE 0 END) AS target_hit,
    SUM(CASE WHEN Message LIKE 'Stop loss hit%' THEN 1 ELSE 0 END) AS stoploss_hit
FROM [ReplResearchPanelDB].[dbo].[tblScripOrder]
WHERE MessageType = 'Recommendation' AND IsDeleted = 0 AND Status = 'End'
  AND CalculatedReturn IS NOT NULL
  AND (Message IS NULL OR Message NOT LIKE 'Avoid call is not executed%')
  AND CreatedOn >= DATEADD(DAY, -{days}, CAST(GETDATE() AS DATE))
GROUP BY CASE WHEN InstrumentName = 'COMMODITY' THEN 'Commodity'
              WHEN SegmentName IN ('OPTIONS', 'FUTURES') THEN 'F&O' ELSE 'Equity' END
"""


MARGIN_SQL = """
SELECT CASE WHEN InstrumentName = 'COMMODITY' THEN 'Commodity'
            WHEN SegmentName IN ('OPTIONS', 'FUTURES') THEN 'F&O' ELSE 'Equity' END AS Segment, OrderMargin
FROM [ReplResearchPanelDB].[dbo].[tblScripOrder]
WHERE MessageType = 'Recommendation' AND IsDeleted = 0
  AND CreatedOn >= DATEADD(DAY, -{days}, CAST(GETDATE() AS DATE))
"""


def pull_track(ctx: Ctx, days: int | None = None) -> dict:
    """Recompute the track record from every closed Sarthi idea in the research database (read-only).
    Keeps the admin's approved examples and disclaimer."""
    from .clientdb import _query

    days = int(days or (ctx.cfg.get("sarthi") or {}).get("track_days", 30))
    df = _query(ctx, (ctx.cfg.get("sarthi") or {}).get("track_query") or TRACK_SQL.format(days=days))
    segments = {r["Segment"]: {k: int(r[k] or 0) for k in ("closed", "profit", "loss", "target_hit", "stoploss_hit")}
                for r in df.to_dict("records")}
    old = track(ctx)
    data = save_track(ctx, f"last {days} days", segments, old["highlights"],
                      f"[ReplResearchPanelDB].[dbo].[tblScripOrder]: every closed recommendation, last {days} days (auto)",
                      old["disclaimer"])
    goal = target(ctx)
    mdf = _query(ctx, MARGIN_SQL.format(days=days))
    margins = {}
    if not mdf.empty and {"Segment", "OrderMargin"} <= set(mdf.columns):
        import pandas as pd

        mdf["m"] = pd.to_numeric(mdf["OrderMargin"], errors="coerce")
        for seg_name, g in mdf.groupby("Segment"):
            vals = g["m"].dropna()
            if len(vals):
                margins[seg_name] = {"ideas": int(len(g)), "median_margin": float(vals.median()),
                                     "share_within_target": float((vals <= goal).mean()) if goal else None,
                                     "per_share_margin": seg_name == "Equity"}
    data["margins"] = margins
    set_setting(ctx.conn, "sarthi_track", json.dumps(data, ensure_ascii=False), iso(ctx.now()))
    return data
