"""Growth follow-up dashboard.   Run:  python -m streamlit run app.py"""
from __future__ import annotations

import json
import os
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
         if is_manager else ["My day", "My leads", "Notifications"])
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
    with st.expander("🗓️ Today's hourly routine"):
        st.markdown("\n".join(f"- **{x['time']}**: {x['block']}" for x in ctx.cfg.get("day_structure", [])))
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
    c0, c1, c2, c3 = st.columns(4)
    tiers = c0.multiselect("Priority", ["hot", "warm", "cold"], format_func=TIER_BADGE.get)
    status = c1.multiselect("Status", sorted(df["status"].unique()), default=["open"])
    owner = c2.multiselect("GM", [m["id"] for m in ctx.callers()], format_func=lambda i: ctx.member(i)["name"])
    cat = c3.multiselect("Category", sorted(df["category"].dropna().unique()))
    view = df[df["status"].isin(status)] if status else df
    view = view[view["heat_tier"].isin(tiers)] if tiers else view
    view = view[view["assigned_to"].isin(owner)] if owner else view
    view = view[view["category"].isin(cat)] if cat else view
    view = view.assign(GM=view["assigned_to"].map(lambda i: (ctx.member(i) or {}).get("name", "unassigned")),
                       priority=view["heat_tier"].map(TIER_BADGE.get))
    st.dataframe(view[["priority", "heat_score", "lead_id", "name", "phone", "category", "status", "GM", "heat_reasons",
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
    folder = ctx.path(ctx.cfg["sources"]["webengage"]["folder"])
    uploads = st.file_uploader("Upload WebEngage exports (.xlsx / .csv): event pivots, activity lists, events or users",
                               type=["csv", "xlsx", "json"], accept_multiple_files=True)
    if uploads and st.button("Load uploaded files", type="primary"):
        folder.mkdir(parents=True, exist_ok=True)
        for f in uploads:
            (folder / f.name).write_bytes(f.getbuffer())
        st.write(sync_webengage(ctx))
        st.write(jobs.sync(ctx)["assignment"])  # score and assign the new leads straight away
    files = rows(ctx.conn, "SELECT path, kind, rows, ingested_at FROM ingested_files ORDER BY ingested_at DESC")
    if files:
        st.dataframe(pd.DataFrame(files), hide_index=True, width="stretch")
    counts = one(ctx.conn, "SELECT (SELECT COUNT(*) FROM events) e, (SELECT COUNT(*) FROM profiles) p, (SELECT MAX(event_time) FROM events) last")
    st.caption(f"{counts['e']} events · {counts['p']} user profiles · latest event {fmt(counts['last'])}")

    st.subheader("Lead sheet")
    src = ctx.cfg["sources"]["leads"]
    st.caption(f"Source: **{src['type']}** {src.get('path') if src['type'] == 'csv' else src.get('url') or src.get('spreadsheet_key')}. "
               "Change it in config.yaml. Syncs every few minutes when the scheduler is running.")

    st.subheader("Today's market notes")
    text = st.text_area("One talking point per line. Used in pitches and the morning note.", market_notes_text(ctx), height=180)
    if st.button("Save market notes"):
        set_setting(ctx.conn, "market_notes", text, iso(ctx.now()))
        st.toast("Saved.")


@st.cache_data(ttl=300, show_spinner="Building the customer list…")
def _customer_rows(_ctx_id: int) -> list[dict]:
    return build_rows(ctx)


def customer_list_page() -> None:
    st.caption("Every customer with priority, activity, levers and pitch. The same list is published to the Google Sheet "
               "when one is configured. It refreshes every 5 minutes here.")
    data = _customer_rows(id(ctx))
    df = pd.DataFrame([{h: r.get(k, "") for h, k in COLUMNS} for r in data])
    for col in ("Rank", "Days since registration", "Days since last trade", "Holdings (₹)"):  # blank for closed leads / clients without a registration date
        df[col] = pd.to_numeric(df[col], errors="coerce").astype("Int64")
    c1, c2, c3, c4 = st.columns(4)
    pr = c1.multiselect("Priority", ["🔥 Hot", "🌤️ Warm", "❄️ Cold"])
    cat = c2.multiselect("Category", sorted(df["Category"].unique()))
    gm = c3.multiselect("GM", sorted(df["GM"].unique()))
    status = c4.multiselect("Lead status", sorted(df["Lead status"].unique()), default=["open"])
    view = df
    for col, sel in [("Priority", pr), ("Category", cat), ("GM", gm), ("Lead status", status)]:
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


def rules_page() -> None:
    st.caption("Only the admin can change these. They apply to every GM straight away, for follow-ups scheduled from now on.")
    if not admin_pin():
        st.warning("Admin editing is switched off. Set an ADMIN_PIN secret (Streamlit Cloud > Settings > Secrets) to enable it.")
        st.markdown(time_limits_md())
        return
    if not st.session_state.get("is_admin"):
        pin = st.text_input("Admin PIN", type="password")
        if st.button("Unlock"):
            if check_pin(pin):
                st.session_state["is_admin"] = True
                st.rerun()
            st.error("Wrong PIN.")
        st.markdown(time_limits_md())
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
st.title(page)
if page == "My day":
    caller_day(me_id)
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
