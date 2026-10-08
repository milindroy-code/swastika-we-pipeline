"""Growth follow-up dashboard.   Run:  python -m streamlit run app.py"""
from __future__ import annotations

import io
import json
import os
from pathlib import Path
from datetime import datetime, timedelta

import altair as alt
import pandas as pd
import streamlit as st

from wepipeline import jobs
from wepipeline.activity import humanize_ago, summarize
from wepipeline.context import iso, parse_dt
from wepipeline.db import one, open_ctx, rows
from wepipeline.heat import TIER_BADGE
from wepipeline.followups import KIND_LABELS, OUTCOMES, log_call, queue_for, reassign_lead
from wepipeline.ingest import sync_webengage
from wepipeline.admin import RULE_FIELDS, SLA_TIERS, admin_pin, check_pin, current_sla, save_rules
from wepipeline.db import set_setting
from wepipeline.sheet_export import COLUMNS, build_rows, export, update_customer_row
from wepipeline.insights import customer_insight
from wepipeline import sarthi
from wepipeline.team import save_routine, save_team
from wepipeline.upload import LIST_TYPES, analyse_upload, batch_rows, batch_workbook, list_uploads
from wepipeline.clientdb import available as client_db_available
from wepipeline.dialer import DUE_NOW, OVERDUE, dialer_queue, dismiss, next_call, reminders, urgency_counts
from wepipeline.market import live_notes, refresh as refresh_market
from wepipeline.excel_report import build_workbook
from wepipeline.pitch import ai_pitch, format_pitch, market_notes_text, rule_pitch
from wepipeline.reports import classify, compliance, current_block, eod_report, misses

st.set_page_config(page_title="Growth Follow-ups", page_icon="📞", layout="wide")

COLUMN_LABELS = {"caller": "GM", "manager": "SM", "title": "role"}
STATUS_COLORS = {"On time": "#0ca30c", "Late": "#fab219", "Overdue": "#ec835a", "Missed": "#d03b3b"}


def _secrets_to_env() -> None:
    """On Streamlit Community Cloud, DATABASE_URL (and optional ANTHROPIC_API_KEY) come from the app's Secrets."""
    try:
        for key in ("DATABASE_URL", "ADMIN_PIN", "ANTHROPIC_API_KEY", "SMTP_PASSWORD"):
            if key in st.secrets and not os.environ.get(key):
                os.environ[key] = str(st.secrets[key])
    except (FileNotFoundError, st.errors.StreamlitSecretNotFoundError):
        pass  # no secrets file locally: SQLite is used


@st.cache_resource
def get_ctx():
    _secrets_to_env()
    return open_ctx()


ctx = get_ctx()
tz = ctx.tz


def fmt(ts: str | None, pattern: str = "%d %b %H:%M") -> str:
    dt = parse_dt(ts, tz)
    return dt.strftime(pattern) if dt else ""


def lead_by_id(lead_id: str) -> dict:
    return one(ctx.conn, "SELECT * FROM leads WHERE lead_id=?", (lead_id,))


# ---------------------------------------------------------------- sidebar
if os.environ.get("WEP_DEMO_LABEL"):
    st.warning(f"🧪 {os.environ['WEP_DEMO_LABEL']}. The clock is frozen at {ctx.now():%a %d %b %H:%M}.")
team = ctx.team()
labels = {m["id"]: f"{m['name']} · {m.get('title') or ('SM' if ctx.is_sm(m) else 'GM')}" for m in team}
me_id = st.sidebar.selectbox("You are", list(labels), format_func=labels.get)
me = ctx.member(me_id)
is_manager = ctx.is_sm(me)

pages = (["Team compliance", "Customer list", "Escalations", "Leads", "GM view", "Reports", "Data & admin", "Admin: rules", "Notifications"]
         if is_manager else ["📞 Call now", "My leads", "Notifications"])
page = st.sidebar.radio("Page", pages)

if st.sidebar.button("Sync now", width="stretch", help="Pull the lead sheet and WebEngage exports, assign leads, check follow-ups"):
    with st.spinner("Syncing…"):
        st.session_state["last_sync"] = jobs.sync(ctx)
if st.sidebar.button("Refresh", width="stretch"):
    st.rerun()
if "last_sync" in st.session_state:
    s = st.session_state["last_sync"]
    st.sidebar.caption(f"Last sync: {s['leads'].get('new', 0)} new leads · {s['assignment']['assigned']} assigned · "
                       f"{s['system_followups']} system follow-ups · {s['overdue']['escalated']} escalations")
st.sidebar.caption(f"Now: {ctx.now():%a %d %b %H:%M}")


# ---------------------------------------------------------------- caller pieces
def call_form(lead: dict, agent_id: str, key: str) -> None:
    with st.container(border=True):
        outcome = st.selectbox("Call outcome", list(OUTCOMES), format_func=lambda o: OUTCOMES[o]["label"], key=f"{key}_o")
        spec = OUTCOMES[outcome]
        notes = st.text_area("Notes", key=f"{key}_n", height=70, placeholder="What did they say? What did you promise?")
        next_at = None
        if spec.get("npc"):
            rules = ctx.rules
            retry = ctx.roll_to_working_hours(ctx.now() + timedelta(hours=rules.get("npc_retry_hours", 4)))
            attempts_left = rules.get("max_npc_attempts", 6) - lead["npc_count"] - 1
            st.info(f"⏱️ Next attempt is set automatically for **{retry:%a %d %b %H:%M}** "
                    f"({rules.get('npc_retry_hours', 4):g} h rule set by admin). "
                    + (f"{attempts_left} attempt(s) left before the lead is marked unreachable." if attempts_left > 0
                       else "This is the last attempt: the lead will be marked unreachable."))
        elif not spec.get("status"):  # open outcomes can carry a promised time
            required = spec.get("needs_time")
            schedule = required or st.checkbox("I promised a follow-up at a specific time", key=f"{key}_c")
            if schedule:
                c1, c2 = st.columns(2)
                default = ctx.now() + timedelta(hours=2)
                d = c1.date_input("Follow-up date", default.date(), key=f"{key}_d")
                t = c2.time_input("Time", default.time().replace(second=0, minute=(default.minute // 15) * 15), step=900, key=f"{key}_t")
                next_at = datetime.combine(d, t, tz)
        if st.button("Save call", type="primary", key=f"{key}_save"):
            if next_at and next_at <= ctx.now():
                st.error("The follow-up time must be in the future.")
                return
            try:
                res = log_call(ctx, lead["lead_id"], agent_id, outcome, notes, next_at)
            except ValueError as e:
                st.error(str(e))
                return
            st.toast(f"Saved. Next follow-up: {fmt(res['next_due']) or 'none'} · lead status: {res['status']}")
            try:  # reflect the call in the live Google Sheet straight away
                if update_customer_row(ctx, lead["lead_id"]):
                    st.toast("Google Sheet updated.")
            except Exception as e:  # the call is saved either way; the hourly refresh will catch up
                st.toast(f"Saved, but the Google Sheet could not be updated now ({type(e).__name__}). It will catch up within the hour.")
            _customer_rows.clear()
            st.rerun()


def lead_panel(lead: dict, agent_id: str, key: str) -> None:
    summary = summarize(ctx, lead)
    ins = customer_insight(ctx, lead)
    left, right = st.columns([1, 1.25])
    with left:
        st.markdown(f"**{lead['name'] or lead['lead_id']}** · Client code {lead['we_user_id'] or lead['lead_id']}"
                    + (f" · {lead['phone']}" if lead["phone"] else "") + (f" · {lead['email']}" if lead["email"] else ""))
        st.markdown(f"**{TIER_BADGE.get(lead['heat_tier'], lead['heat_tier'])} · heat {lead['heat_score']:.0f}/100**"
                    + (f"  \n<small>{lead['heat_reasons']}</small>" if lead["heat_reasons"] else ""), unsafe_allow_html=True)
        st.caption(f"Category: {lead['category'] or 'n/a'} · Stage: {ins['stage']} · "
                   f"NPC attempts: {lead['npc_count']} · Last outcome: {OUTCOMES.get(lead['last_outcome'] or '', {}).get('label', 'not called yet')}")
        facts = [("Registered", f"{ins['registered_on']} ({ins['days_since_registration']} d ago)" if ins["registered_on"] else ""),
                 ("Deposit", ins["deposit_status"] + (f", first on {ins['first_deposit_date']}" if ins["first_deposit_date"] else "")),
                 ("Last trade", f"{ins['last_trade_date']} ({ins['days_since_last_trade']} d ago)" if ins["last_trade_date"] else ""),
                 ("Holdings", f"₹{ins['holdings']:,}" if ins["holdings"] else ""), ("Account", ins["account_status"]),
                 ("Best time to call", ins["best_time"]), ("Momentum", ins["momentum"]), ("Research segments", ins["segments"])]
        st.markdown("  \n".join(f"**{k}:** {v}" for k, v in facts if v))
        if ins["core_detail"]:
            st.markdown(f"**Core activity:** {ins['core_detail']}")
        if ins["non_core_detail"]:
            st.caption(f"Non-core: {ins['non_core_detail']}")
        st.markdown(f"**App activity (30 days):** {summary['events_30d']} events · last seen {humanize_ago(ctx, summary['last_seen'])}")
        if summary["recent"]:
            st.dataframe(pd.DataFrame([{"event": e["event"], "times": e.get("count", 1), "when": fmt(e["time"])} for e in summary["recent"]]),
                         hide_index=True, width="stretch", height=180)
        history = rows(ctx.conn, "SELECT called_at, outcome, notes FROM calls WHERE lead_id=? ORDER BY called_at DESC LIMIT 5", (lead["lead_id"],))
        if history:
            st.markdown("**Previous calls**")
            for h in history:
                st.caption(f"{fmt(h['called_at'])} · {OUTCOMES[h['outcome']]['label']} · {h['notes'] or ''}")
    with right:
        tab_call, tab_rule, tab_ai = st.tabs(["Call plan", "Playbook", "AI pitch"])
        with tab_call:
            st.markdown(f"**Opener:** {ins['opener']}")
            if ins["levers"]:
                st.markdown("**Engagement levers**  \n" + ins["levers"].replace("\n", "  \n"))
            st.markdown("**Pitch ideas**  \n" + ins["ideas"].replace("\n", "  \n"))
            st.markdown(f"**Next best action:** {ins['next_action']}  \n**Objection to expect:** {ins['objection']}")
        with tab_rule:
            st.markdown(format_pitch(rule_pitch(ctx, lead, summary)))
        with tab_ai:
            cached = one(ctx.conn, "SELECT text, generated_at FROM pitches WHERE lead_id=?", (lead["lead_id"],))
            if cached:
                st.markdown(cached["text"])
                st.caption(f"Generated {fmt(cached['generated_at'])}")
            if ctx.cfg.get("ai_pitch", {}).get("enabled") and st.button("Generate with Claude", key=f"{key}_ai"):
                with st.spinner("Writing pitch…"):
                    try:
                        ai_pitch(ctx, lead, force=bool(cached))
                        st.rerun()
                    except RuntimeError as e:
                        st.warning(str(e))
        call_form(lead, agent_id, f"{key}_form")


def caller_day(agent_id: str) -> None:
    now = ctx.now()
    queue = queue_for(ctx, agent_id)
    grace = timedelta(minutes=ctx.rules.get("warning_after_minutes", 15))
    end_today = iso(ctx.day_end(now.date()))
    buckets = {"overdue": [], "now": [], "today": [], "later": []}
    for q in queue:
        due = parse_dt(q["due_at"], tz)
        if due + grace < now:
            buckets["overdue"].append(q)
        elif due <= now + timedelta(minutes=30):
            buckets["now"].append(q)
        elif q["due_at"] <= end_today:
            buckets["today"].append(q)
        else:
            buckets["later"].append(q)
    today_row = next((r for r in compliance(ctx, now.date()) if r["caller_id"] == agent_id), {})

    c = st.columns(6)
    c[5].metric("🔥 Hot in queue", sum(1 for q in queue if q["heat_tier"] == "hot"))
    c[0].metric("Overdue", len(buckets["overdue"]))
    c[1].metric("Due in next 30 min", len(buckets["now"]))
    c[2].metric("Later today", len(buckets["today"]))
    c[3].metric("Calls today", today_row.get("calls", 0))
    pct = today_row.get("compliance_pct")
    c[4].metric("On-time today", f"{pct:.0f}%" if pct is not None else "n/a")

    for a in rows(ctx.conn, "SELECT * FROM alerts WHERE agent_id=? AND manager_id IS NULL AND acknowledged_at IS NULL "
                            "AND level='warning' ORDER BY created_at DESC LIMIT 5", (agent_id,)):
        st.warning(f"⚠️ {a['message']}")
    block = current_block(ctx, ctx.now())
    if block:
        b, start, end = block
        st.info(f"**Now ({start:%H:%M}–{end:%H:%M}):** {b['block']}")
    live = live_notes(ctx)["notes"]
    if live:
        with st.expander("📈 Market now (facts for context, never stock tips)"):
            st.markdown("\n".join(f"- {n['text']}" for n in live))
    with st.expander("🗓️ Today's hourly routine"):
        st.markdown("\n".join(f"- **{x['time']}**: {x['block']}" for x in ctx.day_structure()))
    with st.expander("⏱️ Your follow-up time limits (set by admin)"):
        st.markdown(time_limits_md())
    note = one(ctx.conn, "SELECT * FROM notifications WHERE recipient=? AND kind='morning_note' ORDER BY created_at DESC LIMIT 1", (agent_id,))
    if note:
        with st.expander(f"☀️ {note['subject']}", expanded=False):
            st.markdown(note["body"])

    sections = [("🔴 Overdue: call these first", "overdue", True), ("🟠 Due now", "now", True),
                ("🗓️ Later today", "today", False), ("➡️ Upcoming", "later", False)]
    for title, key, open_first in sections:
        items = buckets[key]
        st.subheader(f"{title} ({len(items)})")
        if not items:
            st.caption("Nothing here.")
        for i, q in enumerate(items[:50]):
            badge = TIER_BADGE.get(q["heat_tier"], "") + f" {q['heat_score']:.0f}"
            who = q["name"] or q["lead_id"]
            label = f"{badge} · {fmt(q['due_at'])} · {KIND_LABELS.get(q['kind'], q['kind'])} · {who}" + (f" ({q['phone']})" if q["phone"] else "") + f" · {q['reason'] or ''}"
            with st.expander(label, expanded=open_first and i == 0):
                lead_panel(lead_by_id(q["lead_id"]), agent_id, f"{key}_{q['id']}")


# ---------------------------------------------------------------- GM call screen (autodialer)


@st.fragment(run_every="60s")
def urgency_strip(agent_id: str) -> None:
    """Refreshes itself every minute so new escalation risks and hot leads show up without a click."""
    queue = dialer_queue(ctx, agent_id)
    n = urgency_counts(queue)
    today_row = next((r for r in compliance(ctx, ctx.today()) if r["caller_id"] == agent_id), {})
    dismissed = st.session_state.setdefault(f"dismissed_{agent_id}", set())
    active = [r for r in reminders(ctx, agent_id, queue) if r["key"] not in dismissed]
    # pop-up toasts for reminders that appeared since the last refresh
    seen = st.session_state.setdefault(f"toasted_{agent_id}", set())
    fresh = [r for r in active if r["key"] not in seen]
    for r in fresh[:3]:
        st.toast(f"{r['icon']} {r['text']}")
    if len(fresh) > 3:
        st.toast(f"🔔 {len(fresh) - 3} more reminder(s): open the bell")
    seen.update(r["key"] for r in fresh)
    nudge = one(ctx.conn, "SELECT id, body FROM notifications WHERE recipient=? AND kind='routine' ORDER BY created_at DESC LIMIT 1", (agent_id,))
    if nudge and st.session_state.get(f"nudge_{agent_id}") != nudge["id"]:
        st.session_state[f"nudge_{agent_id}"] = nudge["id"]
        st.toast("🗓️ " + nudge["body"].split("\n")[0][:160])

    c = st.columns([1, 1, 1, 1, 1.2, 0.9])
    with c[5].popover(f"🔔 {len(active)}", width="stretch", help="Reminders: click one to call that customer now"):
        if not active:
            st.caption("No reminders right now.")
        for r in active:
            b1, b2 = st.columns([6, 1])
            if b1.button(f"{r['icon']} {r['text']}", key=f"rem_{r['key']}", width="stretch"):
                st.session_state[f"dial_{agent_id}"] = r["lead_id"]  # pinned leads load even if not due yet
                dismiss(ctx, r)
                dismissed.add(r["key"])
                st.rerun()
            if b2.button("✕", key=f"x_{r['key']}", help="Dismiss"):
                dismiss(ctx, r)
                dismissed.add(r["key"])
                st.rerun()
        if active and st.button("Dismiss all", key=f"x_all_{agent_id}"):
            for r in active:
                dismiss(ctx, r)
                dismissed.add(r["key"])
            st.rerun()
    c[0].metric("🔴 Overdue", n["overdue"])
    c[1].metric("⚠️ Escalating in ≤15 min", n["escalating_soon"])
    c[2].metric("🔥 Hot waiting", n["hot_waiting"])
    c[3].metric("Due now", n["due_now"])
    pct = today_row.get("compliance_pct")
    c[4].metric("Calls today · on time", f"{today_row.get('calls', 0)} · {f'{pct:.0f}%' if pct is not None else 'n/a'}")
    goal = int(ctx.cfg.get("funding_target") or 0)
    if goal:
        st.caption(f"🎯 Today: {today_row.get('funded_target', 0)} account(s) funded ≥ ₹{goal:,} · "
                   f"₹{today_row.get('deposits', 0):,} deposited on your calls")
    if n["escalating_soon"]:
        st.error(f"⚠️ {n['escalating_soon']} follow-up(s) will be escalated to your SM within 15 minutes. They are first in line below.")


def _log_and_advance(agent_id: str, lead_id: str, outcome: str, notes: str, next_at=None, deposit_amount=None) -> None:
    try:
        res = log_call(ctx, lead_id, agent_id, outcome, notes, next_at, deposit_amount=deposit_amount)
    except ValueError as e:
        st.error(str(e))
        return
    st.session_state.pop(f"dial_{agent_id}", None)
    st.toast(f"Saved: {OUTCOMES[outcome]['label']}. Next follow-up: {fmt(res['next_due']) or 'none'}. Loading the next call…")
    try:
        update_customer_row(ctx, lead_id)
    except Exception:
        pass  # the hourly sheet refresh will catch up
    _customer_rows.clear()
    st.rerun()


def _chip(text: str, color: str = "gray") -> str:
    return f":{color}-badge[{text.replace('[', '(').replace(']', ')')}]"


STAGE_NAMES = {"kyc pending": "KYC pending", "kyc no deposit": "No deposit yet", "funded no trade": "Funded, no trade yet",
               "dormant trader": "Dormant trader", "active trader": "Active trader", "payout": "💸 Payout requested",
               "low margin": "Low margin (under ₹30k)"}
LIST_LABELS = {"new": "New, no deposit", "low_margin": "Low margin", "reactivation": "Churned, active again", "payout": "Payout"}


def _stage_line(ins: dict) -> str:
    stage = STAGE_NAMES.get(ins["stage"], ins["stage"].capitalize())
    if ins["days_since_registration"] != "" and ins["stage"] in {"kyc no deposit", "kyc pending"}:
        return f"{stage} · registered {ins['days_since_registration']} d ago"
    if ins["days_since_last_trade"] != "":
        return f"{stage} · last trade {ins['days_since_last_trade']} d ago"
    return stage


def call_card(agent_id: str, q: dict) -> None:
    lead = lead_by_id(q["lead_id"])
    ins = customer_insight(ctx, lead)
    lid = lead["lead_id"]
    who = lead["name"] or lid
    urgency_color = "red" if q["group"] == OVERDUE else "orange" if q["group"] == DUE_NOW else "blue"
    with st.container(border=True):
        # 1. who + why now + actions, on one line
        head, act1, act2 = st.columns([6, 1.3, 1])
        head.markdown(f"## {TIER_BADGE.get(lead['heat_tier'], '').split()[0]} {who}")
        head.markdown(_chip(q["urgency"], urgency_color) + " " + _chip(f"heat {lead['heat_score']:.0f}", "violet")
                      + " " + _chip(KIND_LABELS.get(q["kind"], q["kind"]), "gray"))
        if lead["phone"]:
            act1.link_button("📞 Call", f"tel:+91{lead['phone']}", type="primary", width="stretch")
        else:
            act1.button("📞 Dial from back office", disabled=True, width="stretch", key=f"nophone_{lid}",
                        help=f"No phone number in the data. Client code: {lead['we_user_id'] or lid}")
        if act2.button("⏭️ Skip", key=f"skip_{lid}", width="stretch", help="Move this customer to the back of the line"):
            st.session_state.setdefault(f"skipped_{agent_id}", set()).add(lid)
            st.session_state.pop(f"dial_{agent_id}", None)
            st.rerun()

        # 2. four facts that matter on the call
        last = one(ctx.conn, "SELECT called_at, outcome FROM calls WHERE lead_id=? ORDER BY called_at DESC LIMIT 1", (lid,))
        facts = [("🧭", _stage_line(ins)),
                 ("🕒", f"Best time {ins['best_time']}" if ins["best_time"] else ""),
                 ("📊", f"Interested in {ins['segments']}" if ins["segments"] else ""),
                 ("☎️", f"Last call: {OUTCOMES[last['outcome']]['label'].split(' (')[0].lower()} {fmt(last['called_at'], '%d %b %H:%M')}"
                  if last else "First call")]
        st.markdown("  ".join(_chip(f"{i} {t}", "blue") for i, t in facts if t))

        # 3. call ideas: what the data says, and an angle to take (the GM uses their own words)
        st.markdown("**💡 Call ideas** · data → angle")
        for n, idea in enumerate(ins["call_ideas"], 1):
            st.markdown(f"{n}. :blue-background[{idea['fact']}] → {idea['angle']}")
        if ins["propositions"]:
            st.markdown("**🧰 Swastika ka offer stack** (jo fit ho, wahi bolo)  \n" + "  \n".join(f"• {p}" for p in ins["propositions"]))
        st.markdown("**🎯 Close**  \n" + "  \n".join(f"{n}. {c}" for n, c in enumerate(ins["closing"], 1)))
        st.caption("**Agar customer bole:** " + ins["objections_hi"])

        # 4. the rest, folded away
        with st.expander("More about this customer"):
            c1, c2 = st.columns(2)
            with c1:
                st.markdown(f"**Client code:** {lead['we_user_id'] or lid}  \n**Category:** {lead['category'] or '–'}")
                detail = [("Registered", ins["registered_on"]), ("Deposit", ins["deposit_status"]),
                          ("First deposit", ins["first_deposit_date"]), ("Last trade", ins["last_trade_date"]),
                          ("Holdings", f"₹{ins['holdings']:,}" if ins["holdings"] else ""), ("Account", ins["account_status"]),
                          ("Momentum", ins["momentum"]),
                          ("NPC attempts", f"{lead['npc_count']} of {ctx.rules.get('max_npc_attempts', 6)}")]
                st.markdown("  \n".join(f"{k}: **{v}**" for k, v in detail if v))
            with c2:
                if ins["levers"]:
                    st.markdown("**Why they'll listen**  \n" + ins["levers"].replace("\n", "  \n"))
                st.markdown(f"**Goal:** {ins['next_action']}")
                st.markdown(f"**English opener (if you need one):** {ins['opener']}")
            if ins["core_detail"] or ins["non_core_detail"]:
                st.markdown(f"**In the app:** {ins['core_detail'] or ins['non_core_detail']}")
            history = rows(ctx.conn, "SELECT called_at, outcome, notes FROM calls WHERE lead_id=? ORDER BY called_at DESC LIMIT 5", (lid,))
            for h in history:
                st.caption(f"{fmt(h['called_at'])} · {OUTCOMES[h['outcome']]['label']}" + (f" · “{h['notes']}”" if h["notes"] else ""))

        st.divider()
        outcome_panel(agent_id, lead)


NO_REACH = [("not_picked", "📵 Not picked up"), ("busy", "⏳ Busy"), ("switched_off", "📴 Switched off"), ("wrong_number", "❌ Wrong number")]
REACHED_OPTIONS = [("converted", "💰 Converted"), ("interested", "✅ Interested"), ("callback_requested", "🔁 Wants a call back"),
                   ("not_interested", "🙅 Not interested")]
WHEN_CHOICES = ["In 1 h", "In 2 h", "Tomorrow 11:00", "Tomorrow 16:00", "Pick a time"]


def _when(choice: str, lead_id: str):
    now = ctx.now()
    if choice == "In 1 h":
        return ctx.roll_to_working_hours(now + timedelta(hours=1))
    if choice == "In 2 h":
        return ctx.roll_to_working_hours(now + timedelta(hours=2))
    if choice in {"Tomorrow 11:00", "Tomorrow 16:00"}:
        day = ctx.next_working_day(now.date())
        return datetime.combine(day, datetime.strptime(choice.split()[1], "%H:%M").time(), tz)
    if choice == "Pick a time":
        c1, c2 = st.columns(2)
        default = now + timedelta(hours=3)
        d = c1.date_input("Date", default.date(), key=f"pd_{lead_id}")
        t = c2.time_input("Time", default.time().replace(second=0, minute=(default.minute // 15) * 15), step=900, key=f"pt_{lead_id}")
        return datetime.combine(d, t, tz)
    return None


def outcome_panel(agent_id: str, lead: dict) -> None:
    """Step 1: did you reach them? Step 2 (only if yes): how it went, notes, and when to call next."""
    lid = lead["lead_id"]
    picked = st.session_state.get(f"outcome_{lid}")
    npc_h = ctx.rules.get("npc_retry_hours", 4)

    if picked is None:
        st.markdown("**Couldn't reach them?** One click saves it and brings up the next call.")
        cols = st.columns(4)
        for col, (outcome, label) in zip(cols, NO_REACH):
            if col.button(label, key=f"o_{outcome}_{lid}", width="stretch"):
                _log_and_advance(agent_id, lid, outcome, "")
        st.caption(f"Not picked up / busy / switched off come back to your queue automatically in {npc_h:g} h.")
        st.markdown("**Spoke to them?** Pick how it went.")
        cols = st.columns(4)
        for col, (outcome, label) in zip(cols, REACHED_OPTIONS):
            if col.button(label, key=f"o_{outcome}_{lid}", width="stretch", type="primary"):
                st.session_state[f"outcome_{lid}"] = outcome
                st.rerun()
        return

    label = dict(REACHED_OPTIONS)[picked]
    with st.container(border=True):
        top1, top2 = st.columns([4, 1])
        top1.markdown(f"**{label}**: add a note and save.")
        if top2.button("Change", key=f"chg_{lid}", width="stretch"):
            st.session_state.pop(f"outcome_{lid}", None)
            st.rerun()
        notes = st.text_input("What did they say? What did you promise?", key=f"notes_{lid}",
                              placeholder="e.g. Will add ₹5,000 by UPI tonight")
        when = None
        deposit = None
        if picked == "converted":
            goal = int(ctx.cfg.get("funding_target") or 0)
            deposit = st.number_input("Deposit amount (₹)", min_value=0, value=goal or 0, step=1000, key=f"dep_{lid}",
                                      help=f"Funded-account target (OKR): ₹{goal:,}")
            if goal and deposit and deposit < goal:
                st.caption(f"Below the ₹{goal:,} target: schedule a top-up follow-up if they agreed to add more.")
        if picked in {"interested", "callback_requested"}:
            required = picked == "callback_requested"
            choice = st.pills("When should you call next?" + ("" if required else " (optional)"), WHEN_CHOICES,
                              default="In 2 h" if required else None, key=f"when_{lid}")
            when = _when(choice, lid) if choice else None
            if when:
                st.caption(f"Next follow-up: **{when:%a %d %b, %H:%M}**")
        if st.button("💾 Save & next call", key=f"save_{lid}", type="primary", width="stretch"):
            if picked == "callback_requested" and not when:
                st.error("Pick when to call back.")
            elif when and when <= ctx.now():
                st.error("The follow-up time must be in the future.")
            else:
                st.session_state.pop(f"outcome_{lid}", None)
                _log_and_advance(agent_id, lid, picked, notes, when, deposit_amount=deposit)


def dialer(agent_id: str) -> None:
    urgency_strip(agent_id)
    queue = dialer_queue(ctx, agent_id)
    skipped = st.session_state.setdefault(f"skipped_{agent_id}", set())
    ahead = st.session_state.get(f"ahead_{agent_id}", False)
    # keep the same customer on screen until the GM saves an outcome or skips (the strip refreshes without moving it)
    pinned = st.session_state.get(f"dial_{agent_id}")
    current = next((q for q in queue if q["lead_id"] == pinned), None) or next_call(queue, skipped, ahead)
    if current is None:
        later = [q for q in queue if q["group"] > DUE_NOW]
        st.success("✅ You're all caught up: nothing is due right now.")
        if later:
            st.caption(f"Next follow-up: {later[0]['urgency'].lower()} ({later[0]['name'] or later[0]['lead_id']}). "
                       f"{sum(1 for q in later if q['heat_tier'] == 'hot')} hot lead(s) are scheduled later.")
            if st.button("📞 Call ahead: start on upcoming leads (hot first)", type="primary"):
                st.session_state[f"ahead_{agent_id}"] = True
                st.rerun()
        return
    st.session_state[f"dial_{agent_id}"] = current["lead_id"]
    call_card(agent_id, current)

    upcoming = [q for q in queue if q["lead_id"] != current["lead_id"]][:6]
    if upcoming:
        with st.expander(f"Up next ({sum(1 for q in queue if q['group'] <= DUE_NOW) - 1} more due now)"):
            for q in upcoming:
                c1, c2, c3 = st.columns([4, 4, 1])
                c1.markdown(f"{TIER_BADGE.get(q['heat_tier'], '').split()[0]} **{q['name'] or q['lead_id']}** · {q['urgency']}")
                c2.caption(q["reason"] or KIND_LABELS.get(q["kind"], q["kind"]))
                if c3.button("Call", key=f"jump_{q['id']}", width="stretch"):
                    st.session_state[f"dial_{agent_id}"] = q["lead_id"]
                    st.rerun()
    if ahead and st.button("Stop calling ahead"):
        st.session_state[f"ahead_{agent_id}"] = False
        st.rerun()


def gm_sidebar(agent_id: str) -> None:
    """Everything that supports the call, kept out of the way: plan, reminders, market, limits, morning note."""
    sb = st.sidebar
    sb.divider()
    block = current_block(ctx, ctx.now())
    sb.markdown("**🗓️ Day plan**")
    for x in ctx.day_structure():
        now_ = block and block[0]["time"] == x["time"]
        text = x["block"].split(":")[0] if ":" in x["block"][:60] else x["block"][:60]
        sb.markdown(f"{'▶️ **' if now_ else ''}{x['time']} {text}{'**' if now_ else ''}")
    sb.caption("🔔 Reminders pop up on screen and sit in the bell at the top: click one to call that customer.")
    live = live_notes(ctx)["notes"]
    if live:
        with sb.expander("📈 Market now"):
            st.caption("Facts for context, never stock tips.")
            st.markdown("\n".join(f"- {n['text']}" for n in live))
    with sb.expander("⏱️ Your time limits"):
        st.markdown(time_limits_md())
    note = one(ctx.conn, "SELECT * FROM notifications WHERE recipient=? AND kind='morning_note' ORDER BY created_at DESC LIMIT 1", (agent_id,))
    if note:
        with sb.expander("☀️ Morning note"):
            st.markdown(note["body"])


def caller_leads(agent_id: str) -> None:
    leads = rows(ctx.conn, "SELECT * FROM leads WHERE assigned_to=? ORDER BY status, heat_score DESC", (agent_id,))
    if not leads:
        st.info("No leads assigned yet.")
        return
    df = pd.DataFrame([{"priority": TIER_BADGE.get(l["heat_tier"]), "heat": round(l["heat_score"]), "lead": l["lead_id"],
                        "name": l["name"], "phone": l["phone"], "category": l["category"], "status": l["status"],
                        "last outcome": OUTCOMES.get(l["last_outcome"] or "", {}).get("label", ""), "last contact": fmt(l["last_contact_at"]),
                        "NPC": l["npc_count"]} for l in leads])
    st.dataframe(df, hide_index=True, width="stretch")
    st.markdown("#### Log a call for any lead (e.g. an inbound call)")
    pick = st.selectbox("Lead", [l["lead_id"] for l in leads], format_func=lambda i: f"{i} · {lead_by_id(i)['name']}")
    lead_panel(lead_by_id(pick), agent_id, f"any_{pick}")


# ---------------------------------------------------------------- manager pieces
def team_compliance() -> None:
    c1, c2 = st.columns([1, 2])
    day = c1.date_input("Day", ctx.today())
    scope = c2.radio("Team", ["My team", "Everyone"], horizontal=True)
    data = compliance(ctx, day, team_of=me_id if scope == "My team" else None)
    if not data:
        st.info("No GMs report to you. Switch to 'Everyone'.")
        return
    tot = {k: sum(r[k] for r in data) for k in ["due", "on_time", "late", "missed", "overdue", "calls", "converted", "carry_over"]}
    k = st.columns(6)
    k[0].metric("Follow-ups due", tot["due"])
    k[1].metric("On time", f"{100 * tot['on_time'] / tot['due']:.0f}%" if tot["due"] else "n/a")
    k[2].metric("Missed (escalated)", tot["missed"])
    k[3].metric("Overdue right now", tot["overdue"])
    k[4].metric("Calls", tot["calls"])
    k[5].metric("Conversions", tot["converted"])

    long = pd.DataFrame([{"caller": r["caller"], "status": label, "count": r[col]} for r in data
                         for label, col in [("On time", "on_time"), ("Late", "late"), ("Overdue", "overdue"), ("Missed", "missed")]])
    if long["count"].sum():
        order = list(STATUS_COLORS)
        chart = (alt.Chart(long).mark_bar(cornerRadiusEnd=4, stroke="white", strokeWidth=2)
                 .encode(y=alt.Y("caller:N", title="GM", sort="-x"),
                         x=alt.X("sum(count):Q", title="Follow-ups due"),
                         color=alt.Color("status:N", scale=alt.Scale(domain=order, range=list(STATUS_COLORS.values())),
                                         legend=alt.Legend(orient="top", title=None)),
                         order=alt.Order("order:Q"),
                         tooltip=["caller", "status", "count"])
                 .transform_calculate(order=f"indexof({json.dumps(order)}, datum.status)")
                 .properties(height=36 * len(data) + 40, title="Follow-up outcome by GM"))
        st.altair_chart(chart, width="stretch")

    st.dataframe(pd.DataFrame(data).drop(columns=["caller_id"]).rename(columns=COLUMN_LABELS), hide_index=True, width="stretch")

    st.subheader("Overdue right now")
    now = ctx.now()
    callers = {r["caller_id"] for r in data}
    live = [f for f in rows(ctx.conn, "SELECT f.*, l.name, l.phone FROM followups f JOIN leads l ON l.lead_id=f.lead_id "
                                      "WHERE f.status='pending' AND l.status='open' AND f.due_at<? ORDER BY f.due_at", (iso(now),))
            if f["agent_id"] in callers and classify(ctx, f, now) in {"overdue", "missed"}]
    if live:
        st.dataframe(pd.DataFrame([{"GM": ctx.member(f["agent_id"])["name"], "lead": f["name"] or f["lead_id"], "phone": f["phone"],
                                    "type": KIND_LABELS.get(f["kind"]), "due": fmt(f["due_at"]),
                                    "late by": humanize_ago(ctx, f["due_at"]).replace(" ago", ""),
                                    "escalated": "yes" if f["missed"] else ""} for f in live]),
                     hide_index=True, width="stretch")
    else:
        st.success("Nothing overdue.")

    st.subheader("🔥 Hot leads not called yet")
    hot = [l for l in rows(ctx.conn, "SELECT * FROM leads WHERE status='open' AND heat_tier='hot' AND last_contact_at IS NULL "
                                     "ORDER BY heat_score DESC") if l["assigned_to"] in callers]
    if hot:
        st.dataframe(pd.DataFrame([{"heat": round(l["heat_score"]), "lead": l["lead_id"], "name": l["name"],
                                    "GM": (ctx.member(l["assigned_to"]) or {}).get("name"), "assigned": fmt(l["assigned_at"]),
                                    "why hot": l["heat_reasons"]} for l in hot]), hide_index=True, width="stretch")
    else:
        st.success("Every hot lead has been called.")

    st.subheader(f"Missed follow-ups on {day:%d %b}")
    m = misses(ctx, day, team_of=me_id if scope == "My team" else None)
    if m:
        st.dataframe(pd.DataFrame(m).rename(columns=COLUMN_LABELS), hide_index=True, width="stretch")
    else:
        st.caption("No misses.")


def escalations() -> None:
    open_alerts = rows(ctx.conn, "SELECT * FROM alerts WHERE manager_id=? AND acknowledged_at IS NULL ORDER BY created_at DESC", (me_id,))
    st.caption(f"{len(open_alerts)} unacknowledged")
    for a in open_alerts:
        c1, c2 = st.columns([5, 1])
        icon = "🚨" if a["level"] == "escalation" else "ℹ️"
        c1.markdown(f"{icon} **{fmt(a['created_at'])}** · {a['message']}")
        if c2.button("Acknowledge", key=f"ack_{a['id']}"):
            ctx.conn.execute("UPDATE alerts SET acknowledged_at=? WHERE id=?", (iso(ctx.now()), a["id"]))
            ctx.conn.commit()
            st.rerun()
    if not open_alerts:
        st.success("No open escalations.")


def leads_page() -> None:
    leads = rows(ctx.conn, "SELECT * FROM leads ORDER BY heat_score DESC")
    if not leads:
        st.info("No leads yet. Use 'Sync now' after configuring the lead sheet in config.yaml.")
        return
    df = pd.DataFrame(leads)
    c_list, c0, c1, c2, c3 = st.columns(5)
    lists = c_list.multiselect("List", ["new", "low_margin", "reactivation", "payout", ""], format_func=lambda v: LIST_LABELS.get(v, "(none)"))
    tiers = c0.multiselect("Priority", ["hot", "warm", "cold"], format_func=TIER_BADGE.get)
    status = c1.multiselect("Status", sorted(df["status"].unique()), default=["open"])
    owner = c2.multiselect("GM", [m["id"] for m in ctx.callers()], format_func=lambda i: ctx.member(i)["name"])
    cat = c3.multiselect("Category", sorted(df["category"].dropna().unique()))
    view = df[df["status"].isin(status)] if status else df
    view = view[view["heat_tier"].isin(tiers)] if tiers else view
    view = view[view["assigned_to"].isin(owner)] if owner else view
    view = view[view["category"].isin(cat)] if cat else view
    view = view[view["campaign"].isin(lists)] if lists else view
    view = view.assign(GM=view.apply(lambda r: (ctx.member(r["assigned_to"]) or {}).get(
                           "name", "watching for engagement" if r["campaign"] == "reactivation" else "unassigned"), axis=1),
                       List=view["campaign"].map(lambda v: LIST_LABELS.get(v, "")),
                       priority=view["heat_tier"].map(TIER_BADGE.get))
    st.dataframe(view[["priority", "heat_score", "lead_id", "List", "name", "phone", "category", "status", "GM", "heat_reasons",
                       "npc_count", "last_outcome", "last_contact_at"]],
                 hide_index=True, width="stretch")

    st.subheader("Reassign a lead")
    with st.form("reassign"):
        lead_id = st.selectbox("Lead", view["lead_id"].tolist(), format_func=lambda i: f"{i} · {lead_by_id(i)['name']}")
        to = st.selectbox("New GM", [m["id"] for m in ctx.callers()], format_func=lambda i: ctx.member(i)["name"])
        if st.form_submit_button("Reassign") and lead_id:
            reassign_lead(ctx, lead_id, to)
            try:
                update_customer_row(ctx, lead_id)
            except Exception:
                pass  # the hourly refresh will catch up
            st.toast("Reassigned; pending follow-ups moved too.")
            st.rerun()


def reports_page() -> None:
    st.subheader("Follow-up outcomes (Excel)")
    c1, c2 = st.columns(2)
    start = c1.date_input("From", ctx.today() - timedelta(days=6), key="xl_from")
    end = c2.date_input("To", ctx.today(), key="xl_to")
    if st.button("Build Excel report"):
        import io

        buf = io.BytesIO()
        build_workbook(ctx, start, end, os.environ.get("WEP_DEMO_LABEL", "")).save(buf)
        st.download_button("Download follow-up outcomes (.xlsx)", buf.getvalue(), type="primary",
                           file_name=f"followup_outcomes_{start:%Y%m%d}_{end:%Y%m%d}.xlsx",
                           mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    st.caption("Success = called on time; fail = missed (escalated) or still overdue. Summary, by GM, by type, by priority, "
               "by category, a daily trend chart, and every follow-up and call as rows.")
    st.divider()
    day = st.date_input("Day", ctx.today())
    subject, body = eod_report(ctx, day, me)
    st.subheader(subject)
    st.markdown(body)
    st.download_button("Download report (.md)", f"# {subject}\n\n{body}\n", file_name=f"eod_{day.isoformat()}_{me_id}.md")
    m = misses(ctx, day, team_of=me_id)
    if m:
        st.download_button("Download misses (.csv)", pd.DataFrame(m).to_csv(index=False), file_name=f"misses_{day.isoformat()}.csv")
    st.divider()
    c1, c2 = st.columns(2)
    if c1.button("Send morning notes now"):
        jobs.send_morning_notes(ctx)
        st.toast("Morning notes sent.")
    if c2.button("Send EOD reports now"):
        jobs.send_eod_reports(ctx, day)
        st.toast("EOD reports sent.")


def admin_page() -> None:
    st.subheader("WebEngage exports")
    st.info("To upload a list, go to **Customer list → 📤 Analyse a new list**: choose the category, upload the file, and it is "
            "scored, ranked, assigned and pitched in one go. Files loaded so far:")
    files = rows(ctx.conn, "SELECT path, kind, rows, ingested_at FROM ingested_files ORDER BY ingested_at DESC")
    if files:
        st.dataframe(pd.DataFrame(files), hide_index=True, width="stretch")
    counts = one(ctx.conn, "SELECT (SELECT COUNT(*) FROM events) e, (SELECT COUNT(*) FROM profiles) p, (SELECT MAX(event_time) FROM events) last")
    st.caption(f"{counts['e']} events · {counts['p']} user profiles · latest event {fmt(counts['last'])}")

    st.subheader("Lead sheet")
    src = ctx.cfg["sources"]["leads"]
    st.caption(f"Source: **{src['type']}** {src.get('path') if src['type'] == 'csv' else src.get('url') or src.get('spreadsheet_key')}. "
               "Change it in config.yaml. Syncs every few minutes when the scheduler is running.")

    st.subheader("Live market notes")
    live = live_notes(ctx)
    c1, c2 = st.columns([3, 1])
    c1.caption(f"Sources: {', '.join(live.get('sources') or []) or 'not fetched yet'} · updated {fmt(live.get('updated')) or 'never'}"
               + (f" · NSE as of {live['as_of']}" if live.get("as_of") else "") + ". Refreshed every 15 minutes by the scheduler.")
    if c2.button("Refresh now"):
        with st.spinner("Fetching NSE, Yahoo and headlines…"):
            refresh_market(ctx)
        st.rerun()
    for n in live["notes"]:
        st.markdown(f"- {n['text']}")
    checks = live.get("checks") or []
    if checks:
        failed = [c for c in checks if not c["ok"]]
        with st.expander(f"Sanity checks: {len(checks) - len(failed)}/{len(checks)} passed" + (" ⚠️" if failed else " ✅")):
            st.dataframe(pd.DataFrame([{"": "✅" if c["ok"] else "❌", "check": c["check"], "detail": c["detail"],
                                        "action": f"dropped {c['drop']}" if c["drop"] else ""} for c in checks]),
                         hide_index=True, width="stretch")
    st.subheader("Your own market notes")
    text = st.text_area("Optional. One talking point per line; shown before the live notes in pitches and the morning note.",
                        market_notes_text(ctx), height=140)
    if st.button("Save market notes"):
        set_setting(ctx.conn, "market_notes", text, iso(ctx.now()))
        st.toast("Saved.")


@st.cache_data(ttl=300, show_spinner="Building the customer list…")
def _customer_rows(_ctx_id: int) -> list[dict]:
    return build_rows(ctx)


def upload_section() -> None:
    """SM uploads a list; the system scores, ranks, assigns and pitches it, and shows the results for that list."""
    st.subheader("📤 Analyse a new list")
    with st.container(border=True):
        list_type = st.radio("① What is this list?", ["no_deposit", "low_margin", "reactivation", "payout"],
                             format_func=lambda k: LIST_TYPES[k][0],
                             key="sm_list_type", horizontal=True, index=None)
        if list_type == "low_margin":
            st.caption("Low margin: clients who deposited less than ₹30k. Add a margin / balance / deposit column to the file "
                       "and GMs see the exact top-up gap; otherwise the first deposit from the client database is used.")
        if list_type == "payout":
            st.caption("Payout: every customer goes to a GM first in line, with a 30-minute first call. The pitch is retention: "
                       "confirm the payout goes through as normal, ask why, and offer pricing, Sarthi or an RM. If the file has "
                       "payout amount / date columns, GMs see them.")
        if list_type == "reactivation":
            tiers = " or ".join(TIER_BADGE[t] for t in (ctx.cfg.get("reactivation") or {}).get("assign_tiers", ["hot", "warm"]))
            st.caption(f"Re-activation: every customer in the file is tagged. Only those whose app activity makes them {tiers} "
                       "go to GMs; the rest wait in a watching pool and are assigned the moment they engage. Cold ones no GM "
                       "has worked yet are taken out of GM queues.")
        file = st.file_uploader("② Upload the file: a WebEngage event export (one sheet per event, like the 4 Oct file) "
                                "or simply a column of client codes", type=["xlsx", "xls", "csv"],
                                key=f"sm_upload_{st.session_state.get('upload_nonce', 0)}",
                                disabled=list_type is None)
        db_ok = client_db_available(ctx)
        use_db = st.checkbox("Look up each client in the client database (registration, deposit, last trade, Sarthi)", value=db_ok,
                             disabled=not db_ok, help="Works from the office network. Elsewhere the list type above sets the pitch.")
        if list_type is None:
            st.caption("Choose what the list is first, then upload it.")
        if file is not None and st.button("Analyse, score and assign", type="primary"):
            folder = ctx.path("data/uploads")
            folder.mkdir(parents=True, exist_ok=True)
            path = folder / f"{ctx.now():%Y%m%d_%H%M%S}_{Path(file.name).name}"
            path.write_bytes(file.getbuffer())
            with st.spinner("Reading the file, scoring, ranking, assigning to GMs and writing pitches…"):
                result = analyse_upload(ctx, path, uploaded_by=me_id, use_client_db=use_db, list_type=list_type)
            if result.get("error"):
                st.error(result["error"])
                return
            st.session_state["upload_view"] = result["upload_id"]
            st.session_state["upload_nonce"] = st.session_state.get("upload_nonce", 0) + 1  # clear the box for the next list
            _customer_rows.clear()
            st.rerun()

    uploads = list_uploads(ctx)
    if not uploads:
        return
    ids = [u["id"] for u in uploads]
    default = ids.index(st.session_state["upload_view"]) if st.session_state.get("upload_view") in ids else 0
    pick = st.selectbox("Results for", ids, index=default, key="upload_pick",
                        format_func=lambda i: next(f"{u['filename'].split('_', 2)[-1]} · {u['uploaded_at'][:16].replace('T', ' ')} · "
                                                   f"{(ctx.member(u['uploaded_by']) or {}).get('name', u['uploaded_by'] or '')}"
                                                   for u in uploads if u["id"] == i))
    up = next(u for u in uploads if u["id"] == pick)
    s = up["summary"]
    data = batch_rows(ctx, up["client_ids"])
    open_rows = [r for r in data if r["status"] == "open"]
    k = st.columns(6)
    k[0].metric("Clients in file", len(data))
    k[1].metric("New to the system", s.get("new_leads", 0))
    k[2].metric("🔥 Hot", sum(r["priority"] == "🔥 Hot" for r in open_rows))
    k[3].metric("🌤️ Warm", sum(r["priority"] == "🌤️ Warm" for r in open_rows))
    k[4].metric("Assigned to GMs", sum(not r["gm"].startswith("Waiting") for r in open_rows))
    watching = sum(r["gm"].startswith("Waiting for engagement") for r in open_rows)
    waiting = sum(r["gm"] == "Waiting for capacity" for r in open_rows)
    k[5].metric("Waiting: capacity · 👀 engagement", f"{waiting} · {watching}",
                help="Capacity: engaged, but every GM is full. Engagement: re-activation customers held back until their "
                     "app activity makes them warm or hot.")
    st.caption(f"List type: {s.get('list_type', '')} · Client database: {s.get('ageing', {}).get('detail', '')}"
               + (f" · {s['released_to_watching']} unworked cold customers moved from GM queues to watching"
                  if s.get("released_to_watching") else "")
               + (f" · {s['closed']} already closed (converted / not interested)" if s.get("closed") else ""))
    view = pd.DataFrame([{"Rank": r["batch_rank"], "Priority": r["priority"], "Heat": r["heat"], "Client code": r["client"],
                          "List": r["list"], "Category": r["category"], "GM": r["gm"], "First call due": r["next_due"], "Best time": r["best_time"],
                          "Why": (r["levers"].split("\n")[0].lstrip("• ") if r["levers"] else ""),
                          "Top call idea": (r["call_ideas"][0]["fact"] + " → " + r["call_ideas"][0]["angle"]) if r.get("call_ideas") else ""}
                         for r in data])
    view["Rank"] = pd.to_numeric(view["Rank"], errors="coerce").astype("Int64")
    st.dataframe(view, hide_index=True, width="stretch", height=420,
                 column_config={"Top call idea": st.column_config.TextColumn(width="large"), "Why": st.column_config.TextColumn(width="medium")})
    buf = io.BytesIO()
    batch_workbook(ctx, up).save(buf)
    st.download_button("⬇️ Download the analysed list (.xlsx): scores, GM, levers and full pitch for every client", buf.getvalue(),
                       file_name=f"analysed_{Path(up['filename']).stem}.xlsx", type="primary",
                       mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    st.divider()


def customer_list_page() -> None:
    upload_section()
    st.subheader("All customers")
    st.caption("Every customer with priority, activity, levers and pitch. The same list is published to the Google Sheet "
               "when one is configured. It refreshes every 5 minutes here.")
    data = _customer_rows(id(ctx))
    df = pd.DataFrame([{h: r.get(k, "") for h, k in COLUMNS} for r in data])
    for col in ("Rank", "Days since registration", "Days since last trade", "Holdings (₹)"):  # blank for closed leads / clients without a registration date
        df[col] = pd.to_numeric(df[col], errors="coerce").astype("Int64")
    c0, c1, c2, c3, c4 = st.columns(5)
    lst = c0.multiselect("List", sorted(v for v in df["List"].unique() if v) + ["(none)"])
    pr = c1.multiselect("Priority", ["🔥 Hot", "🌤️ Warm", "❄️ Cold"])
    cat = c2.multiselect("Category", sorted(df["Category"].unique()))
    gm = c3.multiselect("GM", sorted(df["GM"].unique()))
    status = c4.multiselect("Lead status", sorted(df["Lead status"].unique()), default=["open"])
    view = df.assign(List=df["List"].replace("", "(none)"))
    for col, sel in [("List", lst), ("Priority", pr), ("Category", cat), ("GM", gm), ("Lead status", status)]:
        view = view[view[col].isin(sel)] if sel else view
    k = st.columns(4)
    k[0].metric("Customers", len(view))
    k[1].metric("🔥 Hot", int((view["Priority"] == "🔥 Hot").sum()))
    k[2].metric("🌤️ Warm", int((view["Priority"] == "🌤️ Warm").sum()))
    k[3].metric("Waiting for a GM", int((view["GM"] == "Waiting for capacity").sum()))
    st.dataframe(view, hide_index=True, width="stretch", height=560)
    st.download_button("Download as CSV (opens in Excel / Google Sheets)", view.to_csv(index=False).encode("utf-8-sig"),
                       file_name=f"customer_priority_{ctx.today().isoformat()}.csv")
    if st.button("Publish to Google Sheet now"):
        with st.spinner("Publishing…"):
            res = export(ctx)
        st.write(res.get("google_sheet") or "Google Sheet not configured yet (see DEPLOY.md, step 6). CSV written.")


def time_limits_md() -> str:
    r, sla = ctx.rules, current_sla(ctx)
    return (
        "| Follow-up | Due | Deadline |\n|---|---|---|\n"
        f"| New 🔥 hot / 🌤️ warm / ❄️ cold lead | first call within {sla['hot']} / {sla['warm']} / {sla['cold']} min of assignment | |\n"
        f"| Not picked up, busy or switched off | automatically {r.get('npc_retry_hours', 4):g} h later (next morning if after hours) | |\n"
        "| Customer asked for a callback | the time you promised | |\n"
        "| App activity or lead turned hot | immediately | |\n"
        f"| **Any of the above** | | warning at **{r.get('warning_after_minutes', 15)} min** overdue · "
        f"escalated to your SM and counted as **missed** at **{r.get('escalate_after_minutes', 60)} min** |\n\n"
        f"After {r.get('max_npc_attempts', 6)} NPCs in a row the lead is marked unreachable. "
        f"Working hours: {ctx.cfg['working_hours']['start']} to {ctx.cfg['working_hours']['end']}, {', '.join(ctx.cfg['working_hours']['days'])}."
    )


def team_editor() -> None:
    st.subheader("👥 Team: GMs and SMs")
    st.caption("Add your real Growth Managers and Service Managers, set who reports to whom and how many open leads each GM "
               "can hold. Leave ID blank for new people. Removing or deactivating a GM moves their leads, with pending "
               "follow-ups and promised callbacks, to the other GMs.")
    sms = [m for m in ctx.team() if ctx.is_sm(m)]
    df = pd.DataFrame([{"ID": m["id"], "Name": m["name"], "Role": "SM" if ctx.is_sm(m) else "GM",
                        "Reports to (SM)": (ctx.member(ctx.sm_of(m)) or {}).get("name", "") if not ctx.is_sm(m) else "",
                        "Capacity (open leads)": m.get("capacity", 50) if not ctx.is_sm(m) else None,
                        "Email": m.get("email", ""), "Active": m.get("active", True),
                        "Category focus (optional)": ", ".join(m.get("categories", []))} for m in ctx.team()])
    edited = st.data_editor(
        df, num_rows="dynamic", hide_index=True, width="stretch", key="team_editor",
        column_config={
            "ID": st.column_config.TextColumn(help="Leave blank for a new person; it's generated from the name"),
            "Role": st.column_config.SelectboxColumn(options=["GM", "SM"], required=True),
            "Reports to (SM)": st.column_config.SelectboxColumn(options=[m["name"] for m in sms] + [""],
                                                                help="GMs only. Add the SM first, save, then pick them here"),
            "Capacity (open leads)": st.column_config.NumberColumn(min_value=1, max_value=1000, step=5),
            "Active": st.column_config.CheckboxColumn(default=True),
        })
    if st.button("Save team", type="primary"):
        try:
            res = save_team(ctx, [{"id": r["ID"], "name": r["Name"], "role": r["Role"], "sm": r["Reports to (SM)"],
                                   "capacity": r["Capacity (open leads)"], "email": r["Email"],
                                   "active": True if pd.isna(r["Active"]) else bool(r["Active"]),
                                   "categories": r["Category focus (optional)"]}
                                  for r in edited.fillna("").to_dict("records")])
            jobs.sync(ctx)  # hand out leads to new capacity straight away
            _customer_rows.clear()
            st.success(f"Saved: {res['members']} people, {res['gms']} active GMs. {res['leads_moved']} leads moved to other GMs"
                       + (f", {res['leads_back_to_pool']} back to the pool" if res["leads_back_to_pool"] else "") + ".")
        except ValueError as e:
            st.error(str(e))


def routine_editor() -> None:
    st.subheader("🗓️ Hourly routine")
    st.caption("The GM's day plan: shown in the morning note and the call screen. At the start of each block GMs get a nudge "
               "with their live counts (untick Nudge for lunch). Working hours are "
               f"{ctx.cfg['working_hours']['start']}–{ctx.cfg['working_hours']['end']}.")
    df = pd.DataFrame([{"Start": b["time"], "What this hour is for": b["block"], "Nudge GMs": b.get("nudge", True)}
                       for b in ctx.day_structure()])
    edited = st.data_editor(df, num_rows="dynamic", hide_index=True, width="stretch", key="routine_editor",
                            column_config={"Start": st.column_config.TextColumn(help="HH:MM, e.g. 14:00", required=True),
                                           "What this hour is for": st.column_config.TextColumn(width="large"),
                                           "Nudge GMs": st.column_config.CheckboxColumn(default=True)})
    if st.button("Save routine", type="primary"):
        try:
            clean = save_routine(ctx, [{"time": r["Start"], "block": r["What this hour is for"],
                                        "nudge": True if pd.isna(r["Nudge GMs"]) else bool(r["Nudge GMs"])}
                                       for r in edited.to_dict("records")])
            st.success(f"Saved {len(clean)} blocks. GMs see the new plan straight away.")
        except ValueError as e:
            st.error(str(e))


def sarthi_track_view(editable: bool) -> None:
    """Sarthi's track record as GMs will quote it. Only the admin can change it (official, complete data)."""
    t = sarthi.track(ctx)
    st.subheader("Sarthi track record (quoted to customers)")
    st.caption("Every closed Sarthi idea for the period, profits AND losses, pulled from the research database "
               "(tblScripOrder) every hour from the office PC. Never a hand-picked list of wins. "
               "Have compliance approve the wording before go-live.")
    if t.get("updated"):
        st.caption(f"Last updated {fmt(t['updated'])} · source: {t.get('source') or 'not given'}")
    preview = [x for x in [sarthi.track_line(ctx, s) for s in sarthi.SEGMENTS] if x]
    if not editable:
        st.markdown("\n".join(f"- {x}" for x in preview) or "_Not entered yet: pitches explain Sarthi without numbers._")
        return
    if st.button("🔄 Pull the latest numbers from the research database"):
        try:
            with st.spinner("Reading every closed Sarthi idea…"):
                sarthi.pull_track(ctx)
            _customer_rows.clear()
            st.rerun()
        except Exception as e:
            st.error(f"Research database not reachable from here ({type(e).__name__}). Use the office network, or enter the numbers below.")
    period = st.text_input("Period", t["period"], key="sarthi_period")
    table = pd.DataFrame([{"Segment": s, "Ideas closed": t["segments"].get(s, {}).get("closed", 0),
                           "Ended in profit": t["segments"].get(s, {}).get("profit", 0),
                           "Ended in loss": t["segments"].get(s, {}).get("loss", 0)} for s in sarthi.SEGMENTS])
    edited = st.data_editor(table, hide_index=True, disabled=["Segment"], key="sarthi_table", width="stretch")
    highlights = st.text_area("Approved examples (optional, one per line; start with 'F&O:', 'Equity:' or 'Commodity:' to target a segment)",
                              "\n".join(t["highlights"]), height=90, key="sarthi_highlights")
    source = st.text_input("Source of these numbers (required)", t.get("source", ""), key="sarthi_source",
                           placeholder="e.g. Research desk Sarthi performance report, 1–30 Sep")
    disclaimer = st.text_area("Disclaimer GMs must say", t["disclaimer"], height=70, key="sarthi_disclaimer")
    if st.button("Save Sarthi track record", type="primary"):
        if not source.strip():
            st.error("Say where the numbers come from.")
            return
        try:
            sarthi.save_track(ctx, period, {r["Segment"]: {"closed": r["Ideas closed"], "profit": r["Ended in profit"],
                                                           "loss": r["Ended in loss"]} for r in edited.to_dict("records")},
                              highlights.split("\n"), source, disclaimer)
            _customer_rows.clear()
            st.toast("Saved. Pitches use it from now on.")
            st.rerun()
        except ValueError as e:
            st.error(str(e))
    if preview:
        st.markdown("**GMs will say:**\n" + "\n".join(f"- {x}" for x in preview) + f"\n\n_{t['disclaimer']}_")


def rules_page() -> None:
    st.caption("Only the admin can change these. They apply to every GM straight away, for follow-ups scheduled from now on.")
    if not admin_pin():
        st.warning("Admin editing is switched off. Set an ADMIN_PIN secret (Streamlit Cloud > Settings > Secrets) to enable it.")
        st.markdown(time_limits_md())
        sarthi_track_view(editable=False)
        return
    if not st.session_state.get("is_admin"):
        pin = st.text_input("Admin PIN", type="password")
        if st.button("Unlock"):
            if check_pin(pin):
                st.session_state["is_admin"] = True
                st.rerun()
            st.error("Wrong PIN.")
        st.markdown(time_limits_md())
        sarthi_track_view(editable=False)
        return

    r, sla = ctx.rules, current_sla(ctx)
    values = {}
    for k, (label, unit, lo, hi) in RULE_FIELDS.items():
        step = 0.5 if k == "npc_retry_hours" else 1
        cast = float if k == "npc_retry_hours" else int
        values[k] = st.number_input(f"{label} ({unit})", min_value=cast(lo), max_value=cast(hi),
                                    value=cast(r.get(k, lo)), step=cast(step), key=f"rule_{k}")
    st.markdown("**First call for a newly assigned lead, by priority (minutes)**")
    cols = st.columns(3)
    new_sla = {t: cols[i].number_input(lbl, min_value=5, max_value=1440, value=int(sla[t]), step=5, key=f"sla_{t}")
               for i, (t, lbl) in enumerate(SLA_TIERS.items())}
    if st.button("Save rules", type="primary"):
        try:
            save_rules(ctx, values, new_sla)
            st.toast("Rules saved.")
            st.rerun()
        except ValueError as e:
            st.error(str(e))
    st.divider()
    team_editor()
    st.divider()
    routine_editor()
    st.divider()
    sarthi_track_view(editable=True)
    st.divider()
    if st.button("Lock"):
        st.session_state["is_admin"] = False
        st.rerun()


def notifications_page(member_id: str) -> None:
    items = rows(ctx.conn, "SELECT * FROM notifications WHERE recipient=? ORDER BY created_at DESC LIMIT 100", (member_id,))
    if not items:
        st.caption("No notifications yet.")
    for n in items:
        with st.expander(f"{fmt(n['created_at'])} · {n['kind'].replace('_', ' ')} · {n['subject']}"):
            st.markdown(n["body"])
            st.caption(f"Delivered via: {n['channels']}")


# ---------------------------------------------------------------- routing
if page != "📞 Call now":  # the call screen uses the space for the call itself
    st.title(page)
if not is_manager:
    gm_sidebar(me_id)
if page == "📞 Call now":
    dialer(me_id)
elif page == "My leads":
    caller_leads(me_id)
elif page == "Notifications":
    notifications_page(me_id)
elif page == "Team compliance":
    team_compliance()
elif page == "Escalations":
    escalations()
elif page == "Customer list":
    customer_list_page()
elif page == "Leads":
    leads_page()
elif page == "GM view":
    who = st.selectbox("Growth Manager", [m["id"] for m in ctx.callers()], format_func=lambda i: labels[i])
    caller_day(who)
elif page == "Reports":
    reports_page()
elif page == "Data & admin":
    admin_page()
elif page == "Admin: rules":
    rules_page()
