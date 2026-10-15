"""Referral dashboard: a separate, simple place for referral drives (run: streamlit run referral_app.py --server.port 8503).

SMs build and score the pool (most active traders + past referrers), start a drive when they want one, and track
what came in. GMs work their referral list here. Nothing here touches the funding queue, its SLAs or its reports.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, time, timedelta
from pathlib import Path

import pandas as pd
import streamlit as st

from wepipeline import drives, referral
from wepipeline.context import parse_dt
from wepipeline.db import one, open_ctx, rows
from wepipeline.heat import TIER_BADGE
from wepipeline.insights import customer_insight

st.set_page_config(page_title="Referral Drives", page_icon="🤝", layout="wide")


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


# ---------------------------------------------------------------- sidebar
team = ctx.team()
labels = {m["id"]: f"{m['name']} · {'SM' if ctx.is_sm(m) else 'GM'}" for m in team}
me_id = st.sidebar.selectbox("You are", list(labels), format_func=labels.get)
is_sm = ctx.is_sm(ctx.member(me_id))
pages = (["📊 Campaign report", "Overview", "Build the list", "Campaign & drives", "Business (RM) list", "Referrals collected"] if is_sm
         else ["My referral calls", "My referrals"])
page = st.sidebar.radio("Page", pages)
s = referral.settings(ctx)
st.sidebar.caption(f"Calls from {s['call_from']} (after the market close) · a client is asked at most once every "
                   f"{s['cooldown_days']} days · reward quoted: "
                   + (f"₹{s['reward_referrer']:,} + ₹{s['reward_friend']:,} after the friend's first trade"
                      if s["reward_referrer"] or s["reward_friend"] else "none (set on the main dashboard, Admin: rules)"))
st.title("🤝 Referral drives")


def refresh_status() -> None:
    """Data freshness and client-DB reachability, with an automatic refresh once per session when the pool is stale."""
    last = referral.last_refresh(ctx)
    stale = referral.is_stale(ctx)
    conn = st.session_state.get("db_status") or referral.connection_status(ctx)
    st.session_state["db_status"] = conn
    if stale and conn["ok"] and not st.session_state.get("auto_refreshed"):
        st.session_state["auto_refreshed"] = True
        with st.spinner("Refreshing most active traders and referral history from the client database…"):
            try:
                res = referral.refresh(ctx)
                last, stale = res.get("last") or last, False
                st.toast("Referral pool refreshed.")
            except Exception as e:
                st.warning(f"Refresh failed: {type(e).__name__}: {str(e)[:200]}")
    when = (f"Last refreshed {fmt(last['at'])}: {last['traders']} active traders, {last['referrers']} past referrers"
            if last else "Not refreshed from the client database yet")
    c1, c2 = st.columns([5, 1])
    if conn["ok"]:
        c1.caption(f"🟢 Client database connected · {when} · refreshes every {s['refresh_hours']:g} h")
    else:
        c1.warning(f"🔴 {conn['detail']}  \n{when}.")
    if c2.button("🔄 Check again" if not conn["ok"] else "🔄 Refresh now", width="stretch"):
        st.session_state.pop("db_status", None)
        if conn["ok"]:
            with st.spinner("Refreshing…"):
                st.session_state["refresh_result"] = referral.refresh(ctx, force=True)
        st.rerun()


if is_sm:
    refresh_status()


# ---------------------------------------------------------------- SM pages
def overview() -> None:
    days = st.segmented_control("Period", [7, 30, 90], default=30, format_func=lambda d: f"Last {d} days") or 30
    sm = referral.summary(ctx, ctx.now() - timedelta(days=days))
    k = st.columns(6)
    k[0].metric("In the pool", sm["pool"], help="Scored, not yet given to a GM")
    k[1].metric("With GMs now", sm["with_gms"])
    k[2].metric("Clients called", sm["clients_called"])
    k[3].metric("Reached", sm["reached"])
    k[4].metric("Gave referrals", sm["gave"], help="Clients who shared at least one friend")
    k[5].metric("Friends referred", sm["friends"])
    t = sm["pool_tiers"]
    st.caption(f"Pool by referral score: {TIER_BADGE['hot']} {t['hot']} · {TIER_BADGE['warm']} {t['warm']} · {TIER_BADGE['cold']} {t['cold']}")
    if sm["by_gm"]:
        st.subheader("By GM")
        st.dataframe(pd.DataFrame([{"GM": gm_name(g), "Calls": v["calls"], "Reached": v["reached"], "Gave referrals": v["gave"],
                                    "Friends": v["friends"], "Ask → referral %": round(100 * v["gave"] / v["reached"], 1) if v["reached"] else None}
                                   for g, v in sm["by_gm"].items()]), hide_index=True, width="stretch")
    else:
        st.info("No referral calls in this period yet. Build the list, then start a drive.")
    camps = referral.campaign_report(ctx)
    if camps:
        st.subheader("Campaigns")
        st.dataframe(pd.DataFrame([{"Campaign": c["campaign"], "Channel": c["channel"], "Engaged clients": c["users"],
                                    TIER_BADGE["hot"]: c["hot"], TIER_BADGE["warm"]: c["warm"], TIER_BADGE["cold"]: c["cold"],
                                    "Called": c["called"], "Gave referrals": c["gave"], "Friends": c["friends"],
                                    "Last activity": fmt(c["last"], "%d %b")} for c in camps]), hide_index=True, width="stretch")


def scope_filter(key: str) -> str:
    return st.segmented_control("Clients", list(referral.SCOPES), default=s["scope"], format_func=referral.SCOPES.get, key=key,
                                help="Centralised: a direct client in a centralised sub-branch (the business Vertical rule, config `centralised`).") or "all"


def pool_table(scope: str = "all") -> None:
    pool = [l for l in referral.pool(ctx) if referral.in_scope(l, scope)]
    if not pool:
        st.info("The pool is empty. Pull or upload the most active clients above.")
        return
    data = []
    for l in pool:
        r = referral.info(l)
        h = json.loads(l["extra"] or "{}").get("referral_history") or {}
        snap = referral.snapshot(ctx, l)
        data.append({"Cohort": snap["cohort"], "Cohort name": referral.COHORTS.get(snap["cohort"], ""),
                     "Priority": TIER_BADGE.get(l["heat_tier"] or "cold"), "Score": round(l["heat_score"] or 0), "Client": l["lead_id"],
                     "Name": l["name"] or "", "Owner": referral.OWNER_LABEL[snap["owner"]], "RM": snap["rm_name"],
                     "Centralised": "yes" if snap["centralized"] else "",
                     "Tags": ", ".join(snap["tags"]), "Last trade": snap["last_trade"],
                     "Sarthi": snap["sarthi"], "Segment": snap["segment"], "Trades (60 d)": r.get("total_trades"),
                     "Referred before": h.get("referred") or None, "High-quality referrals": h.get("high") or None,
                     "Medium": h.get("medium") or None, "Low": h.get("low") or None, "Referral quality score": snap["quality_score"] or None,
                     "Campaign engagement": round(referral.engagement(ctx, l["lead_id"])[0]) or None,
                     "GM": gm_name(l["assigned_to"]) if l["assigned_to"] else "pool", "Why": l["heat_reasons"] or ""})
    df = pd.DataFrame(data)
    c1, c2 = st.columns([3, 2])
    cohorts = c1.pills("Cohort", [k for k in referral.COHORTS if k in set(df["Cohort"])], selection_mode="multi",
                       format_func=lambda k: f"{k} · {referral.COHORTS[k]}", key="cohort_filter")
    tiers = c2.multiselect("Priority", list(TIER_BADGE.values()))
    view = df[df["Cohort"].isin(cohorts)] if cohorts else df
    view = view[view["Priority"].isin(tiers)] if tiers else view
    st.dataframe(view, hide_index=True, width="stretch")
    st.download_button("Download the scored list (CSV)", view.to_csv(index=False).encode("utf-8"), "referral_pool.csv", "text/csv")


def _save_upload(file) -> Path:
    folder = ctx.path("data/uploads")
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{ctx.now():%Y%m%d_%H%M%S}_{Path(file.name).name}"
    path.write_bytes(file.getbuffer())
    return path


def _report(p: dict, what: str) -> None:
    st.success(f"{what}: {p.get('added', 0)} new in the pool, {p.get('reopened', 0)} brought back, {p.get('updated', 0)} refreshed.")
    if p.get("busy") or p.get("asked_recently"):
        st.caption(f"Left alone: {p.get('busy', 0)} are on a funding / payout / re-activation list right now, "
                   f"{p.get('asked_recently', 0)} were asked within the last {s['cooldown_days']} days.")


def build_list() -> None:
    st.markdown(f"**① Most active traders** (cash + F&O, last {s['days']} days; buckets: "
                f"{', '.join(b.title() for b in s['buckets'])})")
    c1, c2 = st.columns(2)
    db_ok = (st.session_state.get("db_status") or {}).get("ok", False)
    if c1.button("Pull from the client database now", disabled=not db_ok, width="stretch",
                 help="Runs the most-active-clients query, read-only." if db_ok else "Not reachable from here: see the note at the top."):
        with st.spinner("Running the trade query…"):
            try:
                _report(referral.load_candidates(ctx, referral.pull(ctx)), "Most active traders")
            except Exception as e:
                st.error(f"The query failed: {type(e).__name__}: {str(e)[:300]}")
    f1 = c2.file_uploader("…or upload the query's output (xlsx / csv)", type=["xlsx", "xls", "csv"], key="up_active")
    if f1 is not None and c2.button("Add to the pool", key="add_active"):
        try:
            _report(referral.load_candidates(ctx, referral.read_file(_save_upload(f1)), source=f1.name), "Most active traders")
        except ValueError as e:
            st.error(str(e))
    st.markdown("**② Past referral history** (who referred before, and how many of those clients made a first trade)")
    h1, h2 = st.columns(2)
    if h1.button("Pull referral history from the client database", disabled=not db_ok, width="stretch",
                 help="Referred clients (CRM lead source 37) with their first trade / deposit, read-only."
                      if db_ok else "Not reachable from here: see the note at the top."):
        with st.spinner("Running the referral-history query…"):
            try:
                p = referral.load_history(ctx, referral.pull_history(ctx), source="client DB")
                _report(p, f"{p['referrers']} past referrers ({p['referred']} referred, {p['active']} made a first trade)")
            except Exception as e:
                st.error(f"The query failed: {type(e).__name__}: {str(e)[:300]}")
    f2 = h2.file_uploader("…or upload its output (refree, Client_Id, AuthorizeDate, FTD, FirstDeposit…)",
                          type=["xlsx", "xls", "csv"], key="up_hist")
    if f2 is not None and h2.button("Add history", key="add_hist"):
        try:
            p = referral.load_history(ctx, referral.read_file(_save_upload(f2)), source=f2.name)
            _report(p, f"{p['referrers']} past referrers ({p['referred']} referred, {p['active']} made a first trade)")
        except ValueError as e:
            st.error(str(e))
    st.markdown("**③ Campaign engagement** (WhatsApp / SMS / email / push… referral campaigns: who opened, clicked, replied, shared)")
    e1, e2 = st.columns(2)
    camp = e1.text_input("Campaign name (if the file doesn't say)", placeholder="e.g. Diwali refer & earn", key="eng_camp")
    chan = e2.selectbox("Channel (if the file doesn't say)", ["", "WhatsApp", "SMS", "Email", "Push", "RCS", "In-app", "Other"], key="eng_chan")
    f3 = st.file_uploader("Upload the engagement export: a client code column plus either an action column (Event / Action, "
                          "optional Count / Date / Channel / Campaign) or one count column per action (Opened, Clicked, Replied…)",
                          type=["xlsx", "xls", "csv"], key="up_eng")
    if f3 is not None and st.button("Add engagement", key="add_eng"):
        try:
            p = referral.load_engagement(ctx, referral.read_file(_save_upload(f3)), campaign=camp, channel=chan, source=f3.name)
            _report(p, f"{p['engaged']} engaged clients")
            st.caption("Actions read: " + ", ".join(f"{a} {n}" for a, n in p["actions"].items())
                       + (f" · {p['opted_out']} opt-out(s) lower those clients' scores" if p["opted_out"] else ""))
        except ValueError as e:
            st.error(str(e))
    st.caption("Score (0-100): trading (volume, consistency, recency) + app activity + past referrals that traded, and campaign "
               "engagement scored on its own (shares / replies > clicks > opens, fresher counts more, opt-outs count against). "
               "The higher of the two plus a quarter of the other; same 🔥 ≥ 70 / 🌤️ ≥ 40 tiers as the main dashboard.")
    st.markdown("**④ The pool, by cohort**")
    scope = scope_filter("pool_scope")
    cs = referral.cohort_summary(ctx, scope)
    if cs:
        st.dataframe(pd.DataFrame([{"Cohort": f"{r['cohort']} · {r['label']}", "Waiting": r["in_pool"],
                                    "Growth team (no RM)": r["growth"], "Business team (RM)": r["business"], "With GMs": r["with_gms"],
                                    "Held: Sarthi first": r["held_sarthi"], "On a funding list": r["on_funding"],
                                    "How we approach them": " → ".join(referral.PLAN.get(r["cohort"], ["Not handed out"]))}
                                   for r in cs]), hide_index=True, width="stretch")
        st.caption("Each client sits in one cohort (C1a first, then C1b, C2...). Rested, excluded and not-eligible clients are never "
                   "handed out; a client still on the Sarthi activation list waits for Sarthi first. Clients with an RM (Dealer_Id set) "
                   "go to the Business team list; GM drives hand out only clients with no RM.")
    pool_table(scope)


def campaign_box() -> None:
    camp = referral.active_campaign(ctx)
    with st.container(border=True):
        if camp:
            st.markdown(f"**Current campaign: {camp['name']}** · since {fmt(camp['started_at'], '%d %b %Y %H:%M')}")
        else:
            st.markdown("**No campaign running.** Starting a drive starts one automatically, or name one below.")
        with st.form("new_campaign"):
            c1, c2 = st.columns([3, 1])
            name = c1.text_input("New campaign name", placeholder=f"e.g. Refer & earn {ctx.now():%b %Y}")
            confirm = c2.checkbox("Yes, start fresh", help="Ends the current campaign")
            if st.form_submit_button("🔄 Start a new campaign", type="primary"):
                if not confirm:
                    st.error("Tick 'Yes, start fresh' to confirm.")
                else:
                    res = referral.start_campaign(ctx, name, by=me_id)
                    st.success(f"Started '{res['name']}'. {res['returned_to_pool']} referral asks nobody had called went back to "
                               "the pool (re-scored). Calls already made stay with their GM. Results are now counted from this moment.")
        st.caption("Use this to begin a clean campaign, e.g. after test pulls or when a new channel campaign goes out. "
                   "Nothing is deleted: past campaigns stay in the report.")


def drive() -> None:
    campaign_box()
    st.markdown("Give each GM a batch of referral calls from the Growth team's pool (clients with no RM), cohort by cohort, highest score first. "
                "Clients with an RM are on the Business (RM) list. There's no deadline and nothing "
                "escalates: it's a drive, run when it suits.")
    gms = [m for m in ctx.callers()]
    pick = st.multiselect("GMs", [m["id"] for m in gms], default=[m["id"] for m in gms], format_func=gm_name)
    c1, c2, c3 = st.columns(3)
    per = c1.number_input("Calls per GM", 1, 50, s["daily_per_gm"])
    tiers = c2.multiselect("Only these priorities", ["hot", "warm", "cold"], default=["hot", "warm", "cold"], format_func=TIER_BADGE.get)
    now_ = c3.toggle("Start now (not after the close)", value=False)
    cohorts = st.multiselect("Cohorts (handed out in this order)", referral.COHORT_ORDER, default=referral.FOCUS,
                             format_func=lambda k: f"{k} · {referral.COHORTS[k]}",
                             help="Handed out roughly in this order, C1a first. Clients on the Sarthi list wait until Sarthi is on. C6 is for re-activation, not referrals.")
    if st.button("Start the drive", type="primary", disabled=not pick or not cohorts):
        res = referral.start_drive(ctx, int(per), pick, tiers, start_now=now_, cohorts=cohorts)
        st.success(f"{res['assigned']} referral calls handed out" + (f", from {fmt(res['from'])}" if res.get("from") else "")
                   + ". GMs see them under My referral calls.")
        if res.get("held"):
            st.caption("Held back: " + " · ".join(f"{n} {why}" for why, n in res["held"].items()))
    st.divider()
    open_ = rows(ctx.conn, "SELECT assigned_to, COUNT(*) AS n FROM drive_members WHERE program='referral' AND status='open' "
                           "AND assigned_to IS NOT NULL GROUP BY assigned_to")
    if open_:
        st.caption("Open referral calls: " + " · ".join(f"{gm_name(r['assigned_to'])} {r['n']}" for r in open_))
        if st.button("End the drive: take uncalled ones back to the pool"):
            st.success(f"{referral.end_drive(ctx)} uncalled referral asks went back to the pool.")


def campaign_report_page() -> None:
    camps = referral.campaigns(ctx)
    if not camps:
        st.info("No campaign yet. Go to Campaign & drives to start one (or start a drive, which starts one).")
        return
    pick = st.selectbox("Campaign", [c["id"] for c in camps],
                        format_func=lambda i: next(f"{c['name']} · {fmt(c['started_at'], '%d %b')} → "
                                                   f"{fmt(c['ended_at'], '%d %b') if c['ended_at'] else 'now'}" for c in camps if c["id"] == i))
    db_ok = (st.session_state.get("db_status") or {}).get("ok", False)
    from wepipeline.db import get_setting

    last = json.loads(get_setting(ctx.conn, "referral_results_at") or get_setting(ctx.conn, "referral_refreshed_at") or "{}")
    c1, c2 = st.columns([4, 1])
    c1.caption("Successful referrals come from the client database (clients who joined through a referral, CRM lead source 37), "
               f"last pulled {fmt(last.get('at')) or 'never'}.")
    if c2.button("🔄 Refresh results", disabled=not db_ok, width="stretch",
                 help="Pulls new referred clients, deposits and first trades (a few seconds)." if db_ok else "Client database not reachable"):
        with st.spinner("Pulling referral results…"):
            res = referral.refresh_results(ctx)
        st.toast("Results refreshed." if res["status"] == "ok" else res.get("detail", "Not refreshed"))
        st.rerun()
    r = referral.report(ctx, pick)
    t = r["totals"]
    st.markdown("**Calling**")
    k = st.columns(6)
    k[0].metric("Asks assigned", t["assigned"], help=f"{t['returned']} taken back to the pool uncalled")
    k[1].metric("Called", t["called"])
    k[2].metric("Reached", t["reached"])
    k[3].metric("Gave referrals", t["gave"])
    k[4].metric("Friends collected", t["friends"], help="Names and numbers GMs recorded on calls")
    k[5].metric("Reach → referral", f"{100 * t['gave'] / t['reached']:.0f}%" if t["reached"] else "–")
    st.markdown("**Successful referrals (from the database, since the campaign started)**")
    k = st.columns(4)
    k[0].metric("New clients via referral", t["wins"])
    k[1].metric("…funded", t["funded"])
    k[2].metric("…made a first trade", t["traded"])
    k[3].metric("Credited to a GM's ask", t["wins_credited"],
                help="The referrer was called in this campaign before their friend joined. The rest came in without an ask.")
    if r["by_gm"]:
        st.subheader("By GM")
        st.dataframe(pd.DataFrame([{"GM": gm_name(g), "Assigned": v["assigned"], "Called": v["called"], "Reached": v["reached"],
                                    "Gave referrals": v["gave"], "Friends collected": v["friends"], "Joined (credited)": v["wins"],
                                    "Funded": v["funded"], "First trade": v["traded"],
                                    "Called %": round(100 * v["called"] / v["assigned"]) if v["assigned"] else None}
                                   for g, v in sorted(r["by_gm"].items(), key=lambda x: -x[1]["wins"])]),
                     hide_index=True, width="stretch")
    tab1, tab2, tab3 = st.tabs([f"Successful referrals ({t['wins']})", f"Asks and calls ({t['assigned']})", f"Friends collected ({t['friends']})"])
    with tab1:
        if r["wins"]:
            df = pd.DataFrame([{"Joined": fmt(w["authorized_at"], "%d %b %Y"), "New client": w["referred_id"],
                                "Referred by": f"{w['referrer_name'] or ''} ({w['referrer_id']})".strip(),
                                "Credited to": gm_name(w["gm"]) if w["gm"] else "no ask (came in on their own)",
                                "Funded": f"₹{w['first_deposit']:,.0f}" if w["funded"] else "", "First trade": fmt(w["first_trade"], "%d %b")}
                               for w in r["wins"]])
            st.dataframe(df, hide_index=True, width="stretch")
            st.download_button("Download (CSV)", df.to_csv(index=False).encode("utf-8"), "referral_successes.csv", "text/csv")
        else:
            st.caption("No client has joined through a referral since this campaign started (as of the last pull).")
    with tab2:
        if r["asks"]:
            df = pd.DataFrame([{"Assigned": fmt(a["assigned_at"]), "GM": gm_name(a["agent_id"]), "Client": a["lead_id"], "Name": a["name"] or "",
                                "Priority": TIER_BADGE.get(a["heat_tier"] or "cold"),
                                "Status": ("returned to pool" if a["returned_at"] else
                                           "not called yet" if a["lead_id"] not in r["last_call"] else
                                           referral_outcome_label(r["last_call"][a["lead_id"]]["outcome"])),
                                "Last call": fmt((r["last_call"].get(a["lead_id"]) or {}).get("called_at"))} for a in r["asks"]])
            st.dataframe(df, hide_index=True, width="stretch")
            st.download_button("Download (CSV)", df.to_csv(index=False).encode("utf-8"), "referral_asks.csv", "text/csv", key="dl_asks")
        else:
            st.caption("No asks handed out in this campaign yet.")
    with tab3:
        if r["friends"]:
            df = pd.DataFrame([{"When": fmt(f["created_at"]), "Friend": f["name"] or "", "Phone": f["phone"] or "",
                                "Referred by": f["referrer_id"], "GM": gm_name(f["agent_id"])} for f in r["friends"]])
            st.dataframe(df, hide_index=True, width="stretch")
        else:
            st.caption("None recorded on calls yet.")


def business_page() -> None:
    st.markdown("Referral asks owned by the **Business team**: clients with an RM (Dealer_Id set). Each RM calls their own clients; "
                "share the list per RM. Same cohorts, plans and talking points as the Growth team's drives.")
    cohorts = st.multiselect("Cohorts", referral.COHORT_ORDER, default=referral.FOCUS, format_func=lambda k: f"{k} · {referral.COHORTS[k]}",
                             key="biz_cohorts")
    data = referral.business_list(ctx, cohorts, scope_filter("biz_scope"))
    if not data:
        st.info("No Business-team clients in these cohorts yet. They appear after the next refresh from the client database.")
        return
    df = pd.DataFrame([{"RM": c["snapshot"]["rm_name"] or "(name not found)", "RM branch": c["snapshot"]["rm_branch"],
                        "Centralised": "yes" if c["snapshot"]["centralized"] else "",
                        "Cohort": f"{c['segment']} · {referral.COHORTS.get(c['segment'], '')}", "Priority": TIER_BADGE.get(c["heat_tier"] or "cold"),
                        "Client": c["lead_id"], "Name": c["name"] or "", "Last trade": c["snapshot"]["last_trade"], "Segment": c["snapshot"]["segment"],
                        "Sarthi": c["snapshot"]["sarthi"],
                        "Referred before": f"{c['snapshot']['referred']} ({c['snapshot']['referred_traded']} traded)" if c["snapshot"]["referred"] else "never",
                        "Plan": " → ".join(referral.PLAN.get(c["segment"], [])), "Why": c["heat_reasons"] or ""} for c in data])
    by_rm = df.groupby(["RM", "RM branch"]).size().reset_index(name="Clients").sort_values("Clients", ascending=False)
    k = st.columns(3)
    k[0].metric("Clients with an RM", f"{len(df):,}")
    k[1].metric("RMs", f"{len(by_rm):,}")
    k[2].metric("Median per RM", int(by_rm["Clients"].median()))
    st.subheader("By RM")
    st.dataframe(by_rm, hide_index=True, width="stretch")
    pick = st.selectbox("Show one RM's list", ["All RMs"] + by_rm["RM"].tolist(), key="biz_rm")
    view = df if pick == "All RMs" else df[df["RM"] == pick]
    st.dataframe(view, hide_index=True, width="stretch")
    st.download_button(f"Download {'all' if pick == 'All RMs' else pick + chr(39) + 's'} list (CSV)", view.to_csv(index=False).encode("utf-8"),
                       "business_referral_list.csv", "text/csv")
    st.caption("Clients still on the Sarthi activation list should switch Sarthi on first; the referral ask follows a week later.")


def referral_outcome_label(o: str) -> str:
    return {"referral_given": "🤝 gave referral(s)", "interested": "✅ will share later", "callback_requested": "🔁 call back",
            "not_interested": "🙅 not interested", "not_picked": "📵 not picked", "busy": "⏳ busy",
            "switched_off": "📴 switched off", "wrong_number": "❌ wrong number"}.get(o, o)


def collected(agent_id: str | None = None) -> None:
    days = st.segmented_control("Period", [7, 30, 90, 365], default=30, format_func=lambda d: f"Last {d} days", key="col_p") or 30
    data = [r for r in referral.referral_rows(ctx, ctx.now() - timedelta(days=days)) if not agent_id or r["agent_id"] == agent_id]
    if not data:
        st.info("No referrals collected in this period yet.")
        return
    df = pd.DataFrame([{"When": fmt(r["created_at"]), "Friend": r["name"] or "", "Phone": r["phone"] or "",
                        "Referred by": f"{r['referrer_name'] or r['referrer_id']} ({r['referrer_id']})", "GM": gm_name(r["agent_id"]),
                        "As typed": r["raw"]} for r in data])
    st.metric("Friends referred", len(df))
    st.dataframe(df, hide_index=True, width="stretch")
    st.download_button("Download (CSV)", df.to_csv(index=False).encode("utf-8"), "referrals.csv", "text/csv")
    st.caption("These friends are leads (not yet registered): hand them to whoever onboards new sign-ups.")


# ---------------------------------------------------------------- GM page
OUTCOMES = [("referral_given", "🤝 Gave referral(s)", True), ("interested", "✅ Will share later", True),
            ("not_interested", "🙅 Not interested", True), ("not_picked", "📵 Not picked up", False),
            ("busy", "⏳ Busy", False), ("wrong_number", "❌ Wrong number", False)]


def my_calls(agent_id: str) -> None:
    calls = referral.my_calls(ctx, agent_id)
    due = [c for c in calls if c["due"]]
    st.caption(f"{len(due)} due now · {len(calls) - len(due)} later. No deadline: these are favours, not funding follow-ups.")
    if not calls:
        st.success("No referral calls on your list. Your SM starts drives from here.")
        return
    for c in calls:
        lid = c["lead_id"]
        r = referral.info(c)
        snap = referral.snapshot(ctx, c)
        head = (f"{TIER_BADGE.get(c['heat_tier'] or 'cold', '').split()[0]} **{c['name'] or lid}** · {lid} · "
                f"{snap['cohort']} {referral.COHORTS.get(snap['cohort'], '')} · " + ("due now" if c["due"] else f"next try {fmt(c['next_at'])}"))
        with st.expander(head, expanded=c is calls[0]):
            k = st.columns(5)
            k[0].metric("Last trade", snap["last_trade"])
            k[1].metric("Segment", snap["segment"])
            k[2].metric("Sarthi", snap["sarthi"])
            qm = snap["quality"]
            k[3].metric("Referred before", (f"{snap['referred']}: {qm['high']} high · {qm['medium']} med · {qm['low']} low"
                                            if sum(qm.values()) else f"{snap['referred']} ({snap['referred_traded']} traded)") if snap["referred"] else "never")
            k[4].metric("With us", snap["with_us"])
            if snap["tags"]:
                st.caption("Also: " + " · ".join(snap["tags"]) + " (use these to personalise; one call, one ask)")
            plan = referral.PLAN.get(snap["cohort"])
            if plan:
                st.markdown("**🗺️ Plan for this client**\n" + "\n".join(f"{i}. {step}" for i, step in enumerate(plan, 1)))
            st.caption("Why on the list: " + (c["heat_reasons"] or "on the referral list"))
            if c["phone"]:
                st.link_button("📞 Call", f"tel:+91{c['phone']}", type="primary")
            else:
                st.caption(f"No phone in the data: dial client code {lid} from the back office.")
            ins = customer_insight(ctx, c)
            st.markdown("**💡 Call ideas**\n" + "\n".join(f"- **{i['fact']}** → {i['angle']}" for i in ins["call_ideas"]))
            st.markdown("**🎯 Ask**\n" + "\n".join(f"- {x}" for x in ins["closing"]))
            st.caption("Agar customer bole: " + ins["objections_hi"])
            picked = st.session_state.get(f"r_out_{lid}")
            if picked is None:
                cols = st.columns(len(OUTCOMES))
                for col, (o, label, reached) in zip(cols, OUTCOMES):
                    if col.button(label, key=f"r_{o}_{lid}", width="stretch", type="primary" if reached else "secondary"):
                        if o in {"not_picked", "busy", "wrong_number", "not_interested"}:
                            referral.log_call(ctx, lid, agent_id, o, "")
                            st.toast("Saved.")
                            st.rerun()
                        st.session_state[f"r_out_{lid}"] = o
                        st.rerun()
                continue
            friends, when = "", None
            if picked == "referral_given":
                friends = st.text_area("Friends: name and number, one per line (only with the friend's consent)", key=f"r_f_{lid}",
                                       placeholder="Rahul Sharma, 98xxxxxx01")
            else:
                d = st.date_input("When should you call back?", ctx.next_working_day(ctx.today()), key=f"r_d_{lid}")
                when = ctx.roll_to_working_hours(datetime.combine(d, time.fromisoformat(s["call_from"]), ctx.tz))
            notes = st.text_input("Notes", key=f"r_n_{lid}")
            b1, b2 = st.columns([3, 1])
            if b2.button("Change", key=f"r_chg_{lid}"):
                st.session_state.pop(f"r_out_{lid}", None)
                st.rerun()
            if b1.button("💾 Save", key=f"r_save_{lid}", type="primary", width="stretch"):
                if picked == "referral_given" and not friends.strip():
                    st.error("Add at least one friend's name and number.")
                else:
                    referral.log_call(ctx, lid, agent_id, picked, notes or friends.strip()[:200], when)
                    if friends.strip():
                        n = referral.record_referrals(ctx, lid, agent_id, friends)
                        st.toast(f"🤝 {n} referral(s) saved. Thank you!")
                    st.session_state.pop(f"r_out_{lid}", None)
                    st.rerun()


if page == "📊 Campaign report":
    campaign_report_page()
elif page == "Overview":
    overview()
elif page == "Build the list":
    build_list()
elif page == "Business (RM) list":
    business_page()
elif page == "Campaign & drives":
    drive()
elif page == "Referrals collected":
    collected()
elif page == "My referral calls":
    my_calls(me_id)
else:
    collected(me_id)
