from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from tests.helpers import open_test_ctx
from wepipeline.context import load_config
from wepipeline.db import one, rows
from wepipeline.followups import add_followup
from wepipeline.reports import current_block
from wepipeline.team import save_routine, save_team, validate_team

TZ = ZoneInfo("Asia/Kolkata")
NOW = datetime(2026, 10, 8, 12, 0, tzinfo=TZ)


@pytest.fixture
def ctx(tmp_path):
    cfg = load_config()
    cfg["database"] = str(tmp_path / "test.db")
    cfg["notifications"]["outbox_dir"] = str(tmp_path / "outbox")
    c = open_test_ctx(cfg)
    c._now = NOW
    return c


REAL_TEAM = [
    {"name": "Anita Rao", "role": "SM", "email": "anita@x.in"},
    {"name": "Rahul Verma", "role": "GM", "sm": "Anita Rao", "capacity": 120},
    {"name": "Kiran Shah", "role": "GM", "sm": "Anita Rao", "capacity": 80, "categories": "Traded - dormant (30+ days)"},
    {"name": "", "role": ""},  # blank editor row
]


def test_a_new_sm_replacing_the_dummy_never_blocks_the_save():
    # the dummy SM is renamed to the real one; the GMs still say "SM 01 (dummy)" (the dropdown lists saved SMs only)
    notes = []
    team = validate_team([{"id": "1", "name": "Para", "role": "SM"},
                          {"id": "2", "name": "Gaurav Ajmery", "role": "GM", "sm": "SM 01 (dummy)", "capacity": 20},
                          {"id": "3", "name": "Poonam", "role": "GM", "sm": "", "capacity": 20}], notes)
    assert [m["sm"] for m in team if m["role"] == "gm"] == ["1", "1"] and notes == []  # the only SM: no question to ask
    team = validate_team([{"name": "Para", "role": "SM"}, {"name": "Ravi", "role": "SM"},
                          {"name": "Gaurav", "role": "GM", "sm": "SM 01 (dummy)"}, {"name": "Poonam", "role": "GM", "sm": "Ravi"}], notes)
    assert [m["sm"] for m in team if m["role"] == "gm"] == ["sm_para", "sm_ravi"]
    assert notes == ["Gaurav now reports to Para: pick their SM in 'Reports to (SM)' and save again"]


def test_validation_messages():
    with pytest.raises(ValueError, match="Add at least one SM"):
        validate_team([{"name": "B", "role": "GM"}])
    with pytest.raises(ValueError, match="capacity must be between"):
        validate_team([{"name": "A", "role": "SM"}, {"name": "B", "role": "GM", "sm": "A", "capacity": 0}])
    team = validate_team(REAL_TEAM)
    assert [m["id"] for m in team] == ["sm_anita_rao", "gm_rahul_verma", "gm_kiran_shah"]
    assert team[1]["sm"] == "sm_anita_rao" and team[2]["categories"] == ["Traded - dormant (30+ days)"]


def test_replacing_dummy_gms_moves_their_leads_with_followups(ctx):
    for i, gm in enumerate(["gm_01", "gm_02", "gm_03"]):
        ctx.conn.execute("INSERT INTO leads (lead_id, we_user_id, status, category, source, extra, assigned_to, heat_score) "
                         "VALUES (?,?,?,?,?,?,?,?)", (f"L{i}", f"L{i}", "open", "x", "s", "{}", gm, 50 - i))
        add_followup(ctx, f"L{i}", gm, NOW.replace(hour=15), "promised", "promised a call at 3")
    res = save_team(ctx, REAL_TEAM)
    assert res == {"members": 3, "gms": 2, "leads_moved": 3, "leads_back_to_pool": 0, "notes": []}
    assert {m["id"] for m in ctx.callers()} == {"gm_rahul_verma", "gm_kiran_shah"}
    owners = {r["assigned_to"] for r in rows(ctx.conn, "SELECT assigned_to FROM leads")}
    assert owners <= {"gm_rahul_verma", "gm_kiran_shah"}
    fu = rows(ctx.conn, "SELECT agent_id, kind, status FROM followups")
    assert all(f["status"] == "pending" and f["agent_id"] in owners for f in fu)  # promised callbacks moved, not dropped
    assert ctx.sm_of(ctx.member("gm_rahul_verma")) == "sm_anita_rao"


def test_routine_is_editable_and_drives_the_current_block(ctx):
    with pytest.raises(ValueError, match="not a time"):
        save_routine(ctx, [{"time": "25:00", "block": "x"}])
    with pytest.raises(ValueError, match="same time"):
        save_routine(ctx, [{"time": "10:00", "block": "a"}, {"time": "10:00", "block": "b"}])
    save_routine(ctx, [{"time": "13:30", "block": "Lunch", "nudge": False}, {"time": "10:00", "block": "Huddle"},
                       {"time": "11:00", "block": "Hot leads first"}, {"time": "", "block": ""}])
    assert [b["time"] for b in ctx.day_structure()] == ["10:00", "11:00", "13:30"]
    block, start, end = current_block(ctx, NOW)
    assert block["block"] == "Hot leads first" and f"{start:%H:%M}-{end:%H:%M}" == "11:00-13:30"
