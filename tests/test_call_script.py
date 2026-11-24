import pandas as pd

from tests.test_upload import ctx, pivot  # noqa: F401  (fixture)
from wepipeline import call_script
from wepipeline.db import one
from wepipeline.insights import _event_code, customer_insight
from wepipeline.upload import analyse_upload, batch_workbook, list_uploads

BASE = {"sname": "Sarthi", "target": 30000}


def test_unfunded_sarthi_user_gets_insight_script_and_30k_ask():
    s = call_script.build({**BASE, "stage": "kyc_no_deposit", "seg": "Equity", "sarthi_calls": 2, "research_views": 10,
                           "last24": 59, "best_time": "9 AM–10 AM"})
    assert s["script_insight"].split("\n") == ["• Account khula hai par abhi tak paisa nahi daala",
                                               "• Sarthi ke 2 idea accept kiye",
                                               "• Research ideas 10 baar dekhe, sabse zyada Equity",
                                               "• Last 24 ghante mein app pe 59 baar active: aaj hi call karein"]
    steps = s["script_text"].split("\n")
    assert len(steps) == 7 and steps[0].endswith("(best time: 9 AM–10 AM)")
    assert "Sarthi ke 2 idea accept kiye aur Equity ideas dekh rahe hain" in steps[1]
    assert steps[4] == '5. Ask: "₹30,000 abhi UPI se add kar dete hain? Main line pe hoon, 2 minute lagenge."'
    assert "returns guaranteed nahi" in steps[6]
    assert "₹10,000 se shuru karte hain, baaki ₹20,000 kab tak?" in s["script_ask"]


def test_ask_follows_stage_and_admin_target():
    low = call_script.build({**BASE, "stage": "low_margin", "seg": "F&O", "current_margin": 8000})
    assert low["script_insight"].startswith("• Margin sirf ₹8,000 hai, ₹30,000 tak ₹22,000 aur chahiye")
    assert low["script_ask"].startswith('"Abhi ₹8,000 hai. ₹22,000 aur add karke ₹30,000 kar dete hain?')
    assert "aaj ₹11,000 add kar dete hain, baaki ₹11,000 kab tak?" in low["script_ask"]
    nearly = call_script.build({**BASE, "stage": "low_margin", "current_margin": 29500})
    assert "Kab tak ₹500 add kar payenge?" in nearly["script_ask"]
    payout = call_script.build({**BASE, "stage": "payout", "target": 50000})
    assert payout["script_ask"].startswith('"Poora payout lene ki jagah ₹50,000 account mein rakh dete hain?')
    assert "payout hone dein" in payout["script_ask"]  # never block the withdrawal
    dormant = call_script.build({**BASE, "stage": "dormant_trader", "ltd_days": 400})
    assert "(last trade 1 saal pehle)" in dormant["script_insight"] and "top-up" in dormant["script_ask"]
    referral = call_script.build({**BASE, "stage": "referral"})
    assert "₹" not in referral["script_ask"].split("\n")[0] and "dost ki marzi" in referral["script_ask"]
    off = call_script.build({**BASE, "stage": "kyc_no_deposit", "sarthi_active": False})
    assert "Pehle Sarthi switch on kar dete hain" in off["script_text"]


def test_raw_event_names_are_not_segments():
    assert _event_code("RECOMMENDATION_INTERNAL_SCREEN") and _event_code("DASHBOARD_OPENED_MOB")
    assert not _event_code("F&O") and not _event_code("Equity")


def test_analysed_sheet_has_simple_talking_points_and_the_full_script_in_details(ctx, tmp_path):  # noqa: F811
    path = tmp_path / "list.xlsx"
    with pd.ExcelWriter(path) as xl:
        pivot("ORDER_PLACED_MOB", {"HOTC": {"8 Oct, 10AM": 4}}).to_excel(xl, sheet_name="ORDER_PLACED_MOB", index=False)
        pivot("CALL_ACCEPTED_MOB", {"HOTC": {"8 Oct, 9AM": 5}}).to_excel(xl, sheet_name="CALL_ACCEPTED_MOB", index=False)
    analyse_upload(ctx, path, uploaded_by="sm_01", list_type="no_deposit")
    wb = batch_workbook(ctx, list_uploads(ctx)[0])
    ws = wb["Analysed list"]
    head = [c.value for c in ws[1]]
    assert head[3:6] == ["Last key activity", "Talking point", "Ask"]
    row = {h: c.value for h, c in zip(head, ws[2])}
    assert row["Priority"].startswith("🔥 Hot · ") and row["Client"] == "HOTC"
    assert row["Talking point"].startswith("Aap") and "₹30,000" in row["Ask"]
    det = wb["Details"]
    dh = [c.value for c in det[1]]
    drow = {h: c.value for h, c in zip(dh, det[2])}
    assert "Full call script" not in dh and "Insight" not in dh and "App login" in dh  # the long script is gone
    assert drow["Client code"] == "HOTC"
