from datetime import timedelta

import pandas as pd

from tests.test_heat import NOW, ctx  # noqa: F401  (fixture)
from tests.test_upload import pivot
from wepipeline.heat import active_days, recency_factor, score
from wepipeline.upload import analyse_upload, batch_workbook, list_uploads


def hits(*pairs):
    """(hours ago, times) -> one activity's hour buckets."""
    out = [((NOW - timedelta(hours=h)).isoformat(), n) for h, n in pairs]
    return {"count": sum(n for _, n in pairs), "last": max(t for t, _ in out), "type": "core", "hits": out}


def test_recency_is_graded_within_the_day(ctx):  # noqa: F811
    assert recency_factor(ctx, 0) == 1
    one_h, twenty_h, three_d = recency_factor(ctx, 1), recency_factor(ctx, 20), recency_factor(ctx, 72)
    assert one_h > 0.95 and 0.55 < twenty_h < 0.65 and 0.33 < three_d < 0.37
    assert score(ctx, {"ORDER_PLACED_MOB": hits((1, 1))})[0] > score(ctx, {"ORDER_PLACED_MOB": hits((20, 1))})[0] + 5


def test_old_repeats_plus_one_recent_tap_is_not_fresh_activity(ctx):  # noqa: F811
    burst_then_tap = {"ORDER_PLACED_MOB": hits((1, 1), (14 * 24, 40))}  # 40 orders two weeks ago, one today
    steady = {"ORDER_PLACED_MOB": hits((1, 1), *((d * 24, 6) for d in range(1, 7)))}  # 6 a day all week
    assert score(ctx, burst_then_tap)[0] < score(ctx, steady)[0] - 15
    ctx.cfg["heat"]["recency_mode"] = "last_seen"  # the old rule could not tell them apart
    assert abs(score(ctx, burst_then_tap)[0] - score(ctx, steady)[0]) < 5


def test_coming_back_day_after_day_beats_a_one_day_burst(ctx):  # noqa: F811
    one_day = {"CALL_ACCEPTED_MOB": hits((2, 7))}
    every_day = {"CALL_ACCEPTED_MOB": hits((2, 1), *((d * 24 + 2, 1) for d in range(1, 7)))}
    assert active_days(ctx, one_day) == 1 and active_days(ctx, every_day) == 7
    assert score(ctx, every_day)[0] > score(ctx, one_day)[0]


def test_analysed_sheet_has_hour_and_day_breakdown(ctx, tmp_path):  # noqa: F811
    ctx._now = NOW.replace(day=8)  # the upload pivot's hours are 7 Oct 6PM, 8 Oct 9AM / 10AM
    path = tmp_path / "list.xlsx"
    with pd.ExcelWriter(path) as xl:
        pivot("ORDER_PLACED_MOB", {"HOTC": {"8 Oct, 10AM": 4, "7 Oct, 6PM": 2}}).to_excel(xl, sheet_name="ORDER_PLACED_MOB", index=False)
    analyse_upload(ctx, path, list_type="no_deposit")
    wb = batch_workbook(ctx, list_uploads(ctx)[0])
    ws = wb["Activity timeline"]
    row = {h.value: c.value for h, c in zip(ws[1], ws[2])}
    assert row["Client"] == "HOTC" and row["Last active"] == "1 h ago"
    assert (row["Actions last 1 h"], row["Actions last 24 h"], row["Active days (7 d)"]) == (4, 6, 2)
    assert row["Thu 08 Oct"] == 4 and row["Wed 07 Oct"] == 2 and row["10:00"] == 4 and row["18:00"] == 2
    assert row["Peak hour"] == "10:00–11:00"
    assert wb["Analysed list"]["E2"].value.startswith("Last active: 1 h ago")
