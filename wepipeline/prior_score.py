"""App-history score: a score the client already has from their previous app history, computed outside this system and
uploaded by the SM (Customer list, step 4). It is blended into the live heat score:

    heat = strongest of (live app score, app-history score) + weight x the other   (weight 0.25 by default, capped at 100)

so a client with a strong history but a quiet week still ranks well, and fresh app activity still counts on top.
"""
from __future__ import annotations

import pandas as pd

from .context import Ctx, iso, norm_client_id
from .db import one, rows

SCORE_COLUMNS = ("App history score", "History score", "Prior score", "App score", "Score", "Propensity", "Probability",
                 "Rating", "Heat", "Heat score", "Tier", "Bucket")
LEVELS = {"very high": 95, "high": 85, "hot": 85, "medium": 55, "warm": 55, "moderate": 55, "low": 20, "cold": 20, "very low": 5}


def weight(ctx: Ctx) -> float:
    return float((ctx.cfg.get("heat") or {}).get("app_history_weight", 0.25))


def _to_points(values: pd.Series) -> pd.Series:
    """Numbers on any common scale (0-1, 0-10, 0-100) become 0-100; words like High / Medium / Low become 85 / 55 / 20."""
    nums = pd.to_numeric(values, errors="coerce")
    if nums.notna().any():
        top = nums.max()
        scale = 100 if top <= 1 else 10 if top <= 10 else 1
        return (nums * scale).clip(0, 100)
    return values.map(lambda v: LEVELS.get(str(v).strip().lower()))


def load(ctx: Ctx, df: pd.DataFrame, source: str) -> dict:
    """Store one app-history score per client (a later upload replaces an earlier one), then re-score open clients."""
    from .clientdb import ID_COLUMNS
    from .drives import col
    from .heat import refresh_heat

    id_col = col(df, *ID_COLUMNS)
    score_col = col(df, *SCORE_COLUMNS)
    if not id_col or not score_col:
        others = [c for c in df.columns if c != id_col]
        if id_col and len(others) == 1:
            score_col = others[0]
        else:
            raise ValueError("The file needs a client code column and a score column (e.g. Client code, Score). "
                             f"Found: {', '.join(map(str, df.columns))}.")
    points = _to_points(df[score_col])
    now = iso(ctx.now())
    saved, skipped = 0, 0
    for cid, raw, pts in zip(df[id_col], df[score_col], points):
        cid = norm_client_id(cid)
        if not cid or pd.isna(pts):
            skipped += 1
            continue
        ctx.conn.execute("DELETE FROM client_scores WHERE client_id=?", (cid,))
        ctx.conn.execute("INSERT INTO client_scores (client_id, score, raw, source_file, uploaded_at) VALUES (?,?,?,?,?)",
                         (cid, float(pts), str(raw), source, now))
        saved += 1
    ctx.conn.commit()
    known = one(ctx.conn, "SELECT COUNT(*) AS n FROM leads l JOIN client_scores s ON s.client_id IN (l.lead_id, l.we_user_id) "
                          "WHERE s.source_file=? AND l.status='open'", (source,))["n"]
    tiers = refresh_heat(ctx)
    return {"saved": saved, "skipped": skipped, "open_clients_matched": known, "score_column": str(score_col), "tiers": tiers}


def scores_for(ctx: Ctx) -> dict[str, float]:
    return {r["client_id"]: r["score"] for r in rows(ctx.conn, "SELECT client_id, score FROM client_scores")}


def blend(ctx: Ctx, live: float, history: float | None) -> float:
    if history is None:
        return live
    top, other = max(live, history), min(live, history)
    return round(min(100.0, top + weight(ctx) * other), 1)
