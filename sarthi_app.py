"""Sarthi activation dashboard (run: streamlit run sarthi_app.py --server.port 8504).

Clients with Sarthi switched off are the target list; trading, app use (including opening Sarthi ideas without the
switch on) and Sarthi channel-campaign engagement mark them hot / warm / cold. SMs build the list, run campaigns and
drives, and see activations from the client database; GMs work their Sarthi list. Nothing here touches the funding
queue, its SLAs or its reports.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, time, timedelta
from pathlib import Path

import pandas as pd
import streamlit as st

from wepipeline import activation as act
from wepipeline import drives
from wepipeline.context import parse_dt
from wepipeline.db import get_setting, open_ctx, rows
from wepipeline.heat import TIER_BADGE
from wepipeline.insights import customer_insight

st.set_page_config(page_title="Sarthi Activation", page_icon="⚡", layout="wide")


@st.cache_resource
def get_ctx():
    try:
        for key in ("DATABASE_URL", "ADMIN_PIN"):
            if key in st.secrets and not os.environ.get(key):
                os.environ[key] = str(st.secrets[key])
    except (FileNotFoundError, st.errors.StreamlitSecretNotFoundError):
        pass
    c = open_ctx()
    drives.migrate_legacy(c)  # one-off: older pools kept on the client row
    return c


ctx = get_ctx()


def fmt(ts, pattern="%d %b %H:%M") -> str:
    dt = parse_dt(ts, ctx.tz)
    return dt.strftime(pattern) if dt else ""


def gm_name(gid) -> str:
    return (ctx.member(gid) or {}).get("name", gid or "")


team = ctx.team()
labels = {m["id"]: f"{m['name']} · {'SM' if ctx.is_sm(m) else 'GM'}" for m in team}
me_id = st.sidebar.selectbox("You are", list(labels), format_func=labels.get)
is_sm = ctx.is_sm(ctx.member(me_id))
pages = ["📋 Cohort lists", "📊 Campaign report", "Target list", "Campaign & drives"] if is_sm else ["My Sarthi calls"]
page = st.sidebar.radio("Page", pages)
s = act.settings(ctx)
st.sidebar.caption(f"Target: clients with Sarthi off · traders ({', '.join(b.title() for b in s['buckets'])}) and app users "
                   f"(app heat ≥ {s['min_app_heat']:g} or any Sarthi interest) · asked at most once every {s['cooldown_days']} days")
st.title("⚡ Sarthi activation")


def refresh_status() -> None:
    last, stale = act.last_refresh(ctx), act.is_stale(ctx)
    conn = st.session_state.get("db_status") or drives.connection_status(ctx)
    st.session_state["db_status"] = conn
    if stale and conn["ok"] and not st.session_state.get("auto_refreshed"):
        st.session_state["auto_refreshed"] = True
        with st.spinner("Refreshing the Sarthi-off target list and activations from the client database…"):
            try:
                res = act.refresh(ctx)
                last = res.get("last") or last
                st.toast("Target list refreshed.")
            except Exception as e:
                st.warning(f"Refresh failed: {type(e).__name__}: {str(e)[:200]}")
    when = (f"Last refreshed {fmt(last['at'])}: {last.get('new_clients', 0)} new clients, {last['traders']} traders and "
            f"{last['app_users']} app users with Sarthi off, "
            f"{last['activated']} recent activations" if last else "Not refreshed from the client database yet")
    c1, c2 = st.columns([5, 1])
    if conn["ok"]:
        c1.caption(f"🟢 Client database connected · {when} · refreshes every {s['refresh_hours']:g} h")
    else:
        c1.warning(f"🔴 {conn['detail']}  \n{when}.")
    if c2.button("🔄 Check again" if not conn["ok"] else "🔄 Refresh now", width="stretch"):
        st.session_state.pop("db_status", None)
        if conn["ok"]:
            with st.spinner("Refreshing…"):
                act.refresh(ctx, force=True)
        st.rerun()


if is_sm:
    refresh_status()
db_ok = (st.session_state.get("db_status") or {}).get("ok", False)


def _save_upload(file) -> Path:
    folder = ctx.path("data/uploads")
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{ctx.now():%Y%m%d_%H%M%S}_{Path(file.name).name}"
    path.write_bytes(file.getbuffer())
    return path


def _report(p: dict, what: str) -> None:
    st.success(f"{what}: {p.get('added', 0)} new in the list, {p.get('reopened', 0)} brought back, {p.get('updated', 0)} refreshed"
               + (f", {p['now_on']} already switched on (left the list)" if p.get("now_on") else "") + ".")
    if p.get("busy") or p.get("asked_recently"):
        st.caption(f"Left alone: {p.get('busy', 0)} are on a funding / payout / re-activation / referral list right now (those "
                   f"calls already ask for Sarthi), {p.get('asked_recently', 0)} were asked in the last {s['cooldown_days']} days.")


# ---------------------------------------------------------------- SM pages
def _cohort_xlsx(data: list[dict], title: str) -> bytes:
    import io

    from openpyxl.styles import Font, PatternFill

    buf = io.BytesIO()
    df = pd.DataFrame(data)
    with pd.ExcelWriter(buf, engine="openpyxl") as xl:
        df.to_excel(xl, sheet_name=title[:31], index=False)
        ws = xl.sheets[title[:31]]
        fills = {"🔥": "FCDDD9", "🌤": "FFF2CC", "❄": "E6EFFC"}
        for row in ws.iter_rows():
            for c in row:
                c.font = Font(name="Arial", bold=c.row == 1)
        if "Priority" in df:
            col = list(df.columns).index("Priority") + 1
            for i, v in enumerate(df["Priority"], start=2):
                fill = next((f for k, f in fills.items() if str(v).startswith(k)), None)
                if fill:
                    ws.cell(row=i, column=col).fill = PatternFill("solid", fgColor=fill)
        for i, w in enumerate([12, 22, 22, 11, 10, 13, 10, 10, 14, 13, 16, 10, 16, 10, 90], start=1):
            ws.column_dimensions[chr(64 + i)].width = w
        ws.freeze_panes = "B2"
    return buf.getvalue()


@st.fragment(run_every="30s")
def cohort_lists() -> None:
    """Like Today's lists on the main dashboard: per client type, the list → WebEngage app activity → heat score → download."""
    from wepipeline import cohorts

    st.subheader("📋 Cohort lists")
    st.caption("Clients with Sarthi off, by client type. For each: the list (from the trade books, new-client pull and app use) → "
               "WebEngage app activity for those clients → heat score (trading: how often, ₹ traded, how recently; app activity; "
               "Sarthi interest) → download. Refreshed every night with the daily lists; fetch a cohort now with its button.")
    status = act.cohort_status(ctx)
    live = {r["cohort"]: r for r in rows(ctx.conn, "SELECT * FROM cohort_runs WHERE cohort LIKE 'sarthi_pool%' "
                                                   "AND status IN ('queued','running') ORDER BY id")}
    whole = live.get(cohorts.POOL)
    types = list(act.TYPE_ORDER)
    cols = [c for i in range(0, len(types), 2) for c in st.columns(2)]
    for col, t in zip(cols, types):
        d = status.get(t) or {"clients": 0, "fetched": 0, "hot": 0, "warm": 0, "cold": 0, "scored": None}
        run = live.get(f"{cohorts.POOL}:{t}") or whole
        with col.container(border=True):
            st.markdown(f"**{act.CLIENT_TYPES[t]}**")
            st.markdown(f"**1. List** · {d['clients']:,} clients")
            share = d["fetched"] / d["clients"] if d["clients"] else 0
            st.progress(share, text=f"2. App activity · fetched for {d['fetched']:,} of {d['clients']:,}")
            if run:
                step = run["step"] or "Waiting to start"
                st.caption(f"⏳ {step}" + (f": {run['fetched']:,} / {run['to_fetch']:,}" if run["to_fetch"] else "")
                           + (" (whole pool)" if run is whole else ""))
            st.markdown(f"**3. Heat score** · 🔥 {d['hot']:,} · 🌤️ {d['warm']:,} · ❄️ {d['cold']:,}"
                        + (f" · scored {fmt(d['scored'])}" if d["scored"] else ""))
            b1, b2 = st.columns(2)
            if b1.button("🔄 Fetch & re-score", key=f"coh_fetch_{t}", disabled=bool(run) or not d["clients"], width="stretch",
                         help="WebEngage app activity for everyone in this cohort not fetched in the last 16 hours (about 50 "
                              "a minute), then every client's heat score. Runs after any daily list being built."):
                cohorts.queue(ctx, [f"{cohorts.POOL}:{t}"], publish=True, requested_by=me_id)
                st.rerun()
            if d["clients"]:
                if b2.button("⬇️ Prepare download", key=f"coh_prep_{t}", width="stretch"):
                    st.session_state[f"coh_file_{t}"] = _cohort_xlsx(act.cohort_rows(ctx, t), act.CLIENT_TYPES[t].split(" ", 1)[1])
                if st.session_state.get(f"coh_file_{t}"):
                    st.download_button("Download (.xlsx)", st.session_state[f"coh_file_{t}"], width="stretch",
                                       file_name=f"sarthi_{t}_{ctx.now():%Y%m%d_%H%M}.xlsx", key=f"coh_dl_{t}",
                                       mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    st.divider()
    pick = st.selectbox("Preview", types, format_func=act.CLIENT_TYPES.get, key="coh_preview")
    data = act.cohort_rows(ctx, pick)
    st.dataframe(pd.DataFrame(data[:200]), hide_index=True, width="stretch", height=420)
    if len(data) > 200:
        st.caption(f"Showing the top 200 of {len(data):,} by heat score: download the cohort for all of them.")


def pool_activity_line() -> None:
    """How much of the pool has WebEngage app activity behind its score; fetch it now if wanted."""
    from wepipeline import cohorts

    n = rows(ctx.conn, """SELECT COUNT(*) AS n, SUM(CASE WHEN w.client_id IS NOT NULL THEN 1 ELSE 0 END) AS fetched,
                         MAX(w.fetched_at) AS last FROM drive_members m LEFT JOIN we_fetch w ON w.client_id = m.lead_id
                         WHERE m.program='sarthi' AND m.status='open'""")[0]
    last = rows(ctx.conn, "SELECT status, step, to_fetch, fetched FROM cohort_runs WHERE cohort=? ORDER BY id DESC LIMIT 1", (cohorts.POOL,))
    busy = bool(last and last[0]["status"] in ("queued", "running"))
    c1, c2 = st.columns([4, 1])
    c1.caption(f"Score = trading (how often, how much ₹, how recently) + WebEngage app activity + Sarthi interest. App activity "
               f"fetched for {n['fetched'] or 0:,} of {n['n'] or 0:,} clients" + (f", last {fmt(n['last'])}" if n["last"] else "")
               + "; refreshed every night with the daily lists."
               + (f" ⏳ {last[0]['step']}: {last[0]['fetched']:,} / {last[0]['to_fetch']:,}" if busy and last[0]["to_fetch"] else
                  " ⏳ queued" if busy else ""))
    if c2.button("Fetch app activity now", disabled=busy, width="stretch",
                 help="Queued for the cohort worker, after any daily list being built: about 50 clients a minute."):
        cohorts.queue(ctx, [cohorts.POOL], publish=True, requested_by=me_id if "me_id" in globals() else None)
        st.rerun()


def target_list() -> None:
    sm = act.summary(ctx)
    k = st.columns(5)
    k[0].metric("Sarthi off, in the list", sm["pool"])
    k[1].metric(TIER_BADGE["hot"], sm["pool_tiers"]["hot"])
    k[2].metric(TIER_BADGE["warm"], sm["pool_tiers"]["warm"])
    k[3].metric("With GMs now", sm["with_gms"])
    k[4].metric("Switched on (30 days, all clients)", sm["activated_30d"])
    if sm["new_on_funding_lists"]:
        st.info(f"🆕 {sm['new_on_funding_lists']} more new clients with Sarthi off are on funding lists: their funding call asks "
                "for Sarthi, so they aren't duplicated here.")
    st.caption(f"From trades: {sm['by_source']['trades']} · new clients: {sm['by_source']['new']} · from app use: "
               f"{sm['by_source']['app']} · from campaigns: {sm['by_source']['campaign']} · "
               + " · ".join(f"{act.CLIENT_TYPES[t]}: {n}" for t, n in sm["by_type"].items()))
    pool_activity_line()
    with st.expander("Add to the list by file (when the client database can't be reached)"):
        st.markdown("**Traders with the Sarthi flag** (the Sarthi pool query's output: CLIENT_ID, TotalTrades, …, IsSarthiTIOn)")
        f1 = st.file_uploader("Upload", type=["xlsx", "xls", "csv"], key="up_traders")
        if f1 is not None and st.button("Add traders", key="add_traders"):
            try:
                _report(act.load_traders(ctx, act.read_file(_save_upload(f1)), source=f1.name), "Traders with Sarthi off")
            except ValueError as e:
                st.error(str(e))
        st.markdown("**Activations** (CLIENT_ID, SarthiActivationDate): who switched Sarthi on, for the report")
        f2 = st.file_uploader("Upload", type=["xlsx", "xls", "csv"], key="up_acts")
        if f2 is not None and st.button("Add activations", key="add_acts"):
            try:
                r = act.load_activations(ctx, act.read_file(_save_upload(f2)))
                st.success(f"{r['stored']} activations stored; {r['closed_in_pool']} left the list.")
            except ValueError as e:
                st.error(str(e))
    st.markdown("**Sarthi campaign engagement** (WhatsApp / SMS / email / push: who opened, clicked, replied, activated)")
    e1, e2 = st.columns(2)
    camp = e1.text_input("Campaign name (if the file doesn't say)", placeholder="e.g. Sarthi Diwali push", key="eng_camp")
    chan = e2.selectbox("Channel (if the file doesn't say)", ["", "WhatsApp", "SMS", "Email", "Push", "RCS", "In-app", "Other"], key="eng_chan")
    f3 = st.file_uploader("Upload the engagement export: a client code column plus an action column (Event / Action, optional "
                          "Count / Date / Channel / Campaign) or one count column per action (Opened, Clicked, Replied…)",
                          type=["xlsx", "xls", "csv"], key="up_eng")
    if f3 is not None and st.button("Add engagement", key="add_eng"):
        try:
            p = act.load_engagement(ctx, act.read_file(_save_upload(f3)), campaign=camp, channel=chan, source=f3.name)
            _report(p, f"{p['engaged']} engaged clients")
        except ValueError as e:
            st.error(str(e))
    st.caption("Score (0-100): the strongest of activity (trading + app use), Sarthi interest (opened or accepted Sarthi ideas "
               "with the switch off; fresher counts more) and campaign engagement, plus a quarter of the other two. "
               "Same 🔥 ≥ 70 / 🌤️ ≥ 40 tiers as the main dashboard.")
    pool = act.pool(ctx)
    if not pool:
        st.info("The list is empty. Refresh from the client database (top) or upload above.")
        return
    data = []
    for l in pool:
        a = act.info(l)
        data.append({"Priority": TIER_BADGE.get(l["heat_tier"] or "cold"), "Score": round(l["heat_score"] or 0), "Client": l["lead_id"],
                     "Name": l["name"] or "", "Client type": act.CLIENT_TYPES.get(l["segment"] or "active", ""),
                     "Joined": fmt(a.get("registered") or json.loads(l.get("extra") or "{}").get("registration_date"), "%d %b %Y"),
                     "From": "app use" if a.get("via") == "app" else "trades",
                     "Trades": a.get("total_trades"), "Segment": a.get("segment") or "",
                     "Sarthi interest": round(act.interest(ctx, l["lead_id"])[0]) or None,
                     "GM": gm_name(l["assigned_to"]) if l["assigned_to"] else "list", "Why": l["heat_reasons"] or ""})
    df = pd.DataFrame(data)
    f1, f2 = st.columns(2)
    types = f1.pills("Client type", list(act.CLIENT_TYPES.values()), selection_mode="multi", key="type_filter",
                     help=f"New = joined in the last {s['new_days']} days (Admin: rules), then {s['new_days'] + 1}–{s['new_later_days']} days · "
                          f"Pre-churn = older clients with no trade in {s['quiet_days']} days or trading much less lately · "
                          "Existing = the rest")
    tiers = f2.multiselect("Priority", list(TIER_BADGE.values()))
    view = df[df["Client type"].isin(types)] if types else df
    view = view[view["Priority"].isin(tiers)] if tiers else view
    st.caption(f"New clients get +{s['new_boost']:g} on the score (0–{s['new_days']} days) or +{s['new_later_boost']:g} "
               f"({s['new_days'] + 1}–{s['new_later_days']} days) and are handed out first: the Growth team's focus.")
    st.dataframe(view, hide_index=True, width="stretch")
    st.download_button("Download the list (CSV)", view.to_csv(index=False).encode("utf-8"), "sarthi_targets.csv", "text/csv")


def campaign_box() -> None:
    camp = act.active_campaign(ctx)
    with st.container(border=True):
        st.markdown(f"**Current campaign: {camp['name']}** · since {fmt(camp['started_at'], '%d %b %Y %H:%M')}" if camp
                    else "**No campaign running.** Starting a drive starts one automatically, or name one below.")
        with st.form("new_campaign"):
            c1, c2 = st.columns([3, 1])
            name = c1.text_input("New campaign name", placeholder=f"e.g. Sarthi switch-on {ctx.now():%b %Y}")
            confirm = c2.checkbox("Yes, start fresh", help="Ends the current campaign")
            if st.form_submit_button("🔄 Start a new campaign", type="primary"):
                if not confirm:
                    st.error("Tick 'Yes, start fresh' to confirm.")
                else:
                    res = act.start_campaign(ctx, name, by=me_id)
                    st.success(f"Started '{res['name']}'. {res['returned_to_pool']} asks nobody had called went back to the list "
                               "(re-scored). Activations are now counted from this moment.")
        st.caption("Nothing is deleted: past campaigns stay in the report.")


def drive_page() -> None:
    campaign_box()
    st.markdown("Give each GM a batch of Sarthi calls from the list, highest score first. No deadline, nothing escalates.")
    gms = ctx.callers()
    pick = st.multiselect("GMs", [m["id"] for m in gms], default=[m["id"] for m in gms], format_func=gm_name)
    c1, c2, c3 = st.columns(3)
    per = c1.number_input("Calls per GM", 1, 100, s["daily_per_gm"])
    tiers = c2.multiselect("Only these priorities", ["hot", "warm", "cold"], default=["hot", "warm"], format_func=TIER_BADGE.get)
    now_ = c3.toggle("Start now", value=True)
    c4, c5 = st.columns([3, 1])
    types = c4.multiselect("Client types", list(act.CLIENT_TYPES), default=list(act.CLIENT_TYPES), format_func=act.CLIENT_TYPES.get)
    new_first = c5.toggle("New clients first", value=True, help="Hand out new clients, then pre-churn, then existing; "
                                                                "highest score first within each")
    if st.button("Start the drive", type="primary", disabled=not pick):
        res = act.start_drive(ctx, int(per), pick, tiers, start_now=now_, segments=types, new_first=new_first)
        st.success(f"{res['assigned']} Sarthi calls handed out" + (f", from {fmt(res['from'])}" if res.get("from") else "")
                   + ". GMs see them under My Sarthi calls.")
    open_ = rows(ctx.conn, "SELECT assigned_to, COUNT(*) AS n FROM drive_members WHERE program='sarthi' AND status='open' "
                           "AND assigned_to IS NOT NULL GROUP BY assigned_to")
    if open_:
        st.caption("Open Sarthi calls: " + " · ".join(f"{gm_name(r['assigned_to'])} {r['n']}" for r in open_))
        if st.button("End the drive: take uncalled ones back to the list"):
            st.success(f"{act.end_drive(ctx)} uncalled asks went back to the list.")


OUTCOME_LABEL = {"activated": "⚡ activated on the call", "interested": "✅ will do it later", "callback_requested": "🔁 call back",
                 "not_interested": "🙅 not interested", "not_picked": "📵 not picked", "busy": "⏳ busy",
                 "switched_off": "📴 switched off", "wrong_number": "❌ wrong number"}


def report_page() -> None:
    camps = act.campaigns(ctx)
    if not camps:
        st.info("No campaign yet. Go to Campaign & drives to start one (or start a drive, which starts one).")
        return
    pick = st.selectbox("Campaign", [c["id"] for c in camps],
                        format_func=lambda i: next(f"{c['name']} · {fmt(c['started_at'], '%d %b')} → "
                                                   f"{fmt(c['ended_at'], '%d %b') if c['ended_at'] else 'now'}" for c in camps if c["id"] == i))
    last = json.loads(get_setting(ctx.conn, "sarthi_results_at") or get_setting(ctx.conn, "sarthi_refreshed_at") or "{}")
    c1, c2 = st.columns([4, 1])
    c1.caption(f"Activations come from the client database (SarthiActivationDate), last pulled {fmt(last.get('at')) or 'never'}.")
    if c2.button("🔄 Refresh results", disabled=not db_ok, width="stretch"):
        with st.spinner("Pulling activations…"):
            res = act.refresh_results(ctx)
        st.toast("Results refreshed." if res["status"] == "ok" else res.get("detail", "Not refreshed"))
        st.rerun()
    r = act.report(ctx, pick)
    t = r["totals"]
    k = st.columns(5)
    k[0].metric("Asks assigned", t["assigned"], help=f"{t['returned']} taken back uncalled")
    k[1].metric("Called", t["called"])
    k[2].metric("Reached", t["reached"])
    k[3].metric("Activated on the call", t["activated_on_call"], help="Marked by the GM")
    k[4].metric("Reach → activation", f"{100 * t['activated_on_call'] / t['reached']:.0f}%" if t["reached"] else "–")
    k = st.columns(3)
    k[0].metric("Switched on since the campaign started (all clients)", t["wins"])
    k[1].metric("…credited to a GM's call", t["wins_credited"], help="Called in this campaign before Sarthi was switched on")
    k[2].metric("…among clients in this campaign's asks", t["wins_asked"])
    if t["assigned"]:
        st.subheader("By client type")
        st.dataframe(pd.DataFrame([{"Client type": act.CLIENT_TYPES[ty], "Asked": v["asked"], "Called": v["called"],
                                    "Switched on (credited)": v["switched_on"],
                                    "Asked → on %": round(100 * v["switched_on"] / v["asked"]) if v["asked"] else None}
                                   for ty, v in r["by_type"].items()]), hide_index=True, width="stretch")
    if r["by_gm"]:
        st.subheader("By GM")
        st.dataframe(pd.DataFrame([{"GM": gm_name(g), "Assigned": v["assigned"], "Called": v["called"], "Reached": v["reached"],
                                    "Activated on call": v["on_call"], "Switched on (credited)": v["wins"],
                                    "Called %": round(100 * v["called"] / v["assigned"]) if v["assigned"] else None}
                                   for g, v in sorted(r["by_gm"].items(), key=lambda x: -x[1]["wins"])]), hide_index=True, width="stretch")
    tab1, tab2 = st.tabs([f"Activations ({t['wins']})", f"Asks and calls ({t['assigned']})"])
    with tab1:
        if r["wins"]:
            df = pd.DataFrame([{"Switched on": fmt(w["activated_at"], "%d %b %Y %H:%M"), "Client": w["client_id"], "Name": w.get("name") or "",
                                "Client type": act.CLIENT_TYPES.get(w.get("segment") or "", "not on the list"),
                                "Credited to": gm_name(w["gm"]) if w["gm"] else ("asked, not credited" if w["asked"] else "on their own"),
                                "Source": "GM marked (database not updated yet)" if w.get("pending_db") else "client database"}
                               for w in r["wins"]])
            st.dataframe(df, hide_index=True, width="stretch")
            st.download_button("Download (CSV)", df.to_csv(index=False).encode("utf-8"), "sarthi_activations.csv", "text/csv")
        else:
            st.caption("No activations since this campaign started (as of the last pull).")
    with tab2:
        if r["asks"]:
            df = pd.DataFrame([{"Assigned": fmt(a["assigned_at"]), "GM": gm_name(a["agent_id"]), "Client": a["lead_id"], "Name": a["name"] or "",
                                "Client type": act.CLIENT_TYPES.get(a["segment"] or "active", ""),
                                "Priority": TIER_BADGE.get(a["heat_tier"] or "cold"),
                                "Status": ("returned to the list" if a["returned_at"] else "not called yet" if a["lead_id"] not in r["last_call"]
                                           else OUTCOME_LABEL.get(r["last_call"][a["lead_id"]]["outcome"], r["last_call"][a["lead_id"]]["outcome"])),
                                "Last call": fmt((r["last_call"].get(a["lead_id"]) or {}).get("called_at"))} for a in r["asks"]])
            st.dataframe(df, hide_index=True, width="stretch")
            st.download_button("Download (CSV)", df.to_csv(index=False).encode("utf-8"), "sarthi_asks.csv", "text/csv", key="dl_asks")
        else:
            st.caption("No asks handed out in this campaign yet.")
    camps_ch = act.channel_report(ctx)
    if camps_ch:
        st.subheader("Channel campaigns")
        st.dataframe(pd.DataFrame([{"Campaign": c["campaign"], "Channel": c["channel"], "Engaged": c["users"],
                                    TIER_BADGE["hot"]: c["hot"], TIER_BADGE["warm"]: c["warm"], TIER_BADGE["cold"]: c["cold"],
                                    "Called": c["called"], "Switched on": c["activated"], "Last activity": fmt(c["last"], "%d %b")}
                                   for c in camps_ch]), hide_index=True, width="stretch")


# ---------------------------------------------------------------- GM page
OUTCOMES = [("activated", "⚡ Activated", True), ("interested", "✅ Will do it later", True),
            ("not_interested", "🙅 Not interested", True), ("not_picked", "📵 Not picked up", False),
            ("busy", "⏳ Busy", False), ("wrong_number", "❌ Wrong number", False)]


def my_calls(agent_id: str) -> None:
    calls = act.my_calls(ctx, agent_id)
    due = [c for c in calls if c["due"]]
    st.caption(f"{len(due)} due now · {len(calls) - len(due)} later. No deadline: switch Sarthi on together while on the call.")
    if not calls:
        st.success("No Sarthi calls on your list. Your SM starts drives from here.")
        return
    for c in calls:
        lid = c["lead_id"]
        head = (f"{TIER_BADGE.get(c['heat_tier'] or 'cold', '').split()[0]} **{c['name'] or lid}** · {lid} · "
                f"{act.CLIENT_TYPES.get(c.get('segment') or 'active', '').split(' (')[0]} · score {c['heat_score']:.0f} · "
                + ("due now" if c["due"] else f"next try {fmt(c['next_at'])}"))
        with st.expander(head, expanded=c is calls[0]):
            st.caption("Why: " + (c["heat_reasons"] or "Sarthi is off"))
            if c["phone"]:
                st.link_button("📞 Call", f"tel:+91{c['phone']}", type="primary")
            else:
                st.caption(f"No phone in the data: dial client code {lid} from the back office.")
            ins = customer_insight(ctx, c)
            st.markdown("**💡 Call ideas**\n" + "\n".join(f"- **{i['fact']}** → {i['angle']}" for i in ins["call_ideas"]))
            st.markdown("**🎯 Ask**\n" + "\n".join(f"- {x}" for x in ins["closing"]))
            st.caption("Agar customer bole: " + ins["objections_hi"])
            picked = st.session_state.get(f"s_out_{lid}")
            if picked is None:
                cols = st.columns(len(OUTCOMES))
                for col, (o, label, reached) in zip(cols, OUTCOMES):
                    if col.button(label, key=f"s_{o}_{lid}", width="stretch", type="primary" if reached else "secondary"):
                        if o != "interested":
                            act.log_call(ctx, lid, agent_id, o, "")
                            st.toast("⚡ Sarthi activated. Thank you!" if o == "activated" else "Saved.")
                            st.rerun()
                        st.session_state[f"s_out_{lid}"] = o
                        st.rerun()
                continue
            d = st.date_input("When should you call back?", ctx.next_working_day(ctx.today()), key=f"s_d_{lid}")
            when = ctx.roll_to_working_hours(datetime.combine(d, time.fromisoformat(s["call_from"]), ctx.tz))
            notes = st.text_input("Notes", key=f"s_n_{lid}")
            b1, b2 = st.columns([3, 1])
            if b2.button("Change", key=f"s_chg_{lid}"):
                st.session_state.pop(f"s_out_{lid}", None)
                st.rerun()
            if b1.button("💾 Save", key=f"s_save_{lid}", type="primary", width="stretch"):
                act.log_call(ctx, lid, agent_id, picked, notes, when)
                st.session_state.pop(f"s_out_{lid}", None)
                st.rerun()


if page == "📋 Cohort lists":
    cohort_lists()
elif page == "📊 Campaign report":
    report_page()
elif page == "Target list":
    target_list()
elif page == "Campaign & drives":
    drive_page()
else:
    my_calls(me_id)
