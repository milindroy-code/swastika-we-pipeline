# Growth follow-up pipeline

Turns WebEngage app activity, new no-deposit clients from the back office, and the live client sheet into a prioritised
calling queue. It enforces follow-up time limits and reports compliance.


**Roles:** **GMs** (Growth Managers) do the calling and work their queue. **SMs** (Service Managers) mentor their GMs,
receive escalations and get team reports. The **admin** (PIN) sets the follow-up time limits.

To host it for the team for free, see **[DEPLOY.md](DEPLOY.md)**.

```
WebEngage exports ───┐                         ┌─> GM queue (hot first) ─> call logged ─> next follow-up scheduled
Client DB (no FTD) ───┼─> heat score ─> assign ─┤
Client sheet ───────────┘   (0-100)               └─> overdue? warn GM (15 min) ─> escalate to SM (60 min) ─> EOD misses
```

## Run it

```powershell
python -m pip install -r requirements.txt
python run.py sync                 # load sources once, score, assign, check follow-ups
python -m streamlit run app.py     # dashboard at http://localhost:8501
python run.py loop                 # keep running: sync every 5 min, morning note at day start, EOD report at day end
```

Keep `run.py loop` running all day (a terminal, or Windows Task Scheduler "at log on"). Without it, nothing syncs,
alerts never fire and the morning/EOD notes are not sent. The dashboard's **Sync now** button does one pass manually.

Other commands: `python run.py morning` / `python run.py eod` send those notes immediately. `python -m pytest` runs the tests.

## Data in

| Source | Where | Notes |
|---|---|---|
| New clients with no first deposit | back-office SQL Server, `sources.client_db` | `python run.py pull-clients` (office network only). Registered in the last 30 days and bucketed 0–7 / 8–15 / 16–30 days. Clients who deposit are closed automatically. |
| WebEngage exports | drop `.xlsx` / `.csv` into `data/webengage/`, or upload on **Data & admin** | Each file is loaded once, then again only if it changes. Every sheet of a workbook is read. |
| Client sheet | `sources.leads` in `config.yaml` | Local CSV/XLSX, a published Google Sheet CSV link, or a private sheet via service account. Optional: clients can come only from activity files. |
| Market notes | `data/market_notes.md` (or **Data & admin** page) | One talking point per line, used in pitches and the morning note. Update it every morning. |

Recognised WebEngage shapes:

- **Events pivot** (like `user_auth_after_4octandnot_added_fund.xlsx`): `Events | ClientCode | Sum | 4 Oct, 12AM | …`, one event per sheet.
  `Sum` gives frequency and the last non-zero hour gives recency. When several sheets share an event name
  (equity / F&O / commodity research), the sheet name keeps them apart.
- **Activity list**: user id, activity, count and/or last-done date or "days since", optional `Activity Type` (Core / Non-Core).
- **Event-level export**: user id, event name, event time.
- **User export**: user id plus phone/email, used to match clients to WebEngage users.

Users in an activity file who are not in the client sheet become clients, keyed by ClientCode. Their category comes from
`sources.webengage.file_categories`, which matches part of the file name.

## Heat score and priority

For each activity a user did in the last 30 days:

Registration counts as a core activity (`REGISTERED`), so a client who signed up today scores higher than one from three weeks ago.
Registration alone stays ❄️ cold (about 25). It takes app activity on top to make a client warm or hot.

```
points = weight × recency × frequency
  weight    = 10 if core, 3 if non-core          (Activity Type column if present, else heat.core_activities)
  recency   = 0.5 ^ (days since last done ÷ 3)   (halves every 3 days)
  frequency = min(1 + log2(times done), 4)       (1×→1, 2×→2, 4×→3, 8+×→4)
score = 100 × (1 − e^(−Σpoints ÷ 35))            → 0-100
```

| Tier | Score | First call within | Where it shows |
|---|---|---|---|
| 🔥 Hot | ≥ 70 | 30 min | Assigned first, top of every queue, highlighted in the morning note, "Hot clients not called yet" table for managers |
| 🌤️ Warm | 40–69 | 2 h | After hot clients |
| ❄️ Cold | < 40 | 4 h | Last |

Scores are recalculated on every sync. When a client **turns hot**, its follow-up is pulled forward to now and the caller is notified.
All weights, tiers and SLAs live under `heat:` in `config.yaml`. The scale of 35 was tuned on the 4–7 Oct file, giving about 17% hot.

Core activities are currently `ORDER_PLACED_MOB`, `CALL_ACCEPTED_MOB` and `RECOMMENDATION_LISTING_OPENED_MOB`.
`DASHBOARD_OPENED_MOB` and `RECOMMENDATION_LANDING_SCREEN_MOB` are non-core. Adjust `heat.core_activities` to change this.

## The four lists and the ₹30k goal

The OKR is a **funded account at ₹30,000** (`funding_target`). Every funding conversation aims for it. SMs upload
lists on Customer list, choosing one of:

| List | Goal on the call |
|---|---|
| New clients without deposit | ₹30k deposit; if not today, a significant first step (`closing.fallback_amount`) with the rest booked as a top-up follow-up |
| New clients with low margin (under ₹30k) | Top up to ₹30k. A margin / balance / deposit column in the file gives the exact gap |
| Churned clients showing activity | Engage (only engaged ones go to GMs), then deposit and trades |
| Clients who made payouts | Find and fix the reason; they stay invested and trade more. GMs may ask whether they'd like to cancel or reduce the request, but never delay or block a withdrawal |

**Call ideas, not scripts, in Hinglish.** Each idea is a data point, then an angle ("Sarthi ke 4 ideas accept kiye
→ puchho kaunsa pasand aaya…"). They're backed by Sarthi's real record and margins from `tblScripOrder` (e.g. "F&O idea
ka typical margin ₹12,252, toh ₹30k mein ~2 ideas"), market notes, the client's preferred segment and app activity.
Every card also has a **Swastika offer stack** (`propositions:` in config: add IPO, MTF, MF… with approved wording),
a **🎯 Close** (an assumptive ₹30k ask, a fallback, and a booked top-up), and Hinglish objection handling. There's no
false urgency and no promises; tests enforce both, in English and Hinglish. Logging **Converted** asks for the
deposit amount, and reports count **Funded ≥ ₹30k**.

## Sarthi in every pitch

Sarthi (ready trade ideas with entry, target and stop-loss in equity, F&O and commodities) clients every pitch.
It is matched to the customer's segment (from app activity or `PreferredSegment`), and Sarthi activation comes from
`IsSarthiTIOn`. The goal of the call is funds in the account: "add funds now so you're ready for Sarthi's next idea",
using `sarthi.starter_amount` when set.

Sarthi's **track record** is computed from every closed recommendation in `[ReplResearchPanelDB].[dbo].[tblScripOrder]`
over the last 30 days (`python run.py sarthi`, and hourly with the client pull). It counts profits AND losses, early exits
and stop-losses, and leaves out ideas still open or never executed. The line reads, e.g., "Sarthi Equity, last 30 days:
59 of 83 closed ideas ended in profit, 24 in loss", followed by the disclaimer. On **Admin: rules** the admin can pull it
on demand, edit the approved examples and the disclaimer, or enter numbers by hand when the database is unreachable.
Activation comes from `IsSarthiTIOn`; a blank value for a known client means never activated, so GMs ask for it. Pitches never use "earn", "assured" or "sure-shot" wording, and a
test enforces it. Have compliance approve the wording before go-live.

## SMs: analyse an uploaded list

On **Customer list → 📤 Analyse a new list**, an SM uploads an Excel/CSV: a WebEngage event export (one sheet per
event) or just a column of client codes. They say what the list is (new sign-ups with no deposit, clients to
re-activate, or mixed). The system then:
1. Reads every sheet, skips totals rows such as "Others", creates clients for new client codes and loads their activity.
2. Looks each client up in the client database when it's reachable (office network). A 3-second check skips it
   otherwise, and the list type sets the pitch instead.
3. Scores and ranks every client, and builds their opener, levers, pitch and simple call script (insight, script, ask).
4. Shows the results for that file (rank, priority, why, top call idea) and offers the analysed list as Excel.
5. Nobody goes to a GM yet: the SM checks the results and presses **✅ Assign to GMs**. Payouts and 🔥 hot clients go
   first within GM capacity; clients a GM already has stay with them. Until then the list is on hold, and the hourly
   sync and **Sync now** skip it.

Every upload is kept, so earlier lists can be reopened from "Results for".

**Re-activation lists** (choose "Re-activation customers"): every customer in the file is tagged "Re-activation", new or
already known. Only those whose app activity makes them 🔥/🌤️ (`reactivation.assign_tiers`) go to GMs. The rest wait in a
**watching** pool and are assigned automatically the moment they engage. Cold re-activation customers no GM has worked
yet (no call, no promised callback or NPC retry) are taken out of GM queues, freeing capacity for engaged clients.
Customer list and Clients have a **List** filter.

## Client ageing (every client)

`python run.py pull-clients` looks up **every** client, from any source, in the back-office client tables. It reads the
registration date, first deposit, first and last trade (`FTD`, `LTD`), holdings and account status, then gives each
client one category, which also sets the pitch stage:

| Category | Rule | Pitch stage |
|---|---|---|
| New - no deposit (0-7 / 8-15 / 16-30 days) | never deposited or traded, by days since registration | first deposit |
| No deposit (31-90 days / 90+ days) | same, registered longer ago | first deposit (re-engage) |
| Deposited - no trade yet | deposit, no trade | first trade |
| Traded - dormant (30+ days) | last trade over 30 days ago | re-activation |
| Traded - active (last 30 days) | traded recently | deepen engagement |
| Not found in client DB | id not in `tbl_ClientNew` | from app activity |

Closed accounts are dropped. WebEngage's "Others" total row is ignored. A deposit closes only clients in the no-deposit categories.

## Live market notes

Every 15 minutes the scheduler (or `python run.py market`) builds short, factual talking points:
- **NSE index feed:** Nifty 50 with breadth, Bank Nifty, best and worst sector, Midcap / Smallcap, India VIX.
- **Yahoo Finance:** Sensex, USD/INR, gold, crude.
- **ET Markets / Google News:** macro and market-wide headlines.

Each refresh is **sanity-checked**, and a failing source is dropped rather than shown:
- NSE vs Yahoo on the same indices: level within 0.5 % and change within 0.3 points (VIX 2 %).
- Sensex moves with Nifty.
- Plausible ranges for index moves, VIX, USD/INR, gold and crude.
- Breadth agrees with the index direction.
- Data freshness.

Headlines that read like stock tips ("picks", "stocks to buy", "target", "rallied up to") are always dropped.
Pitches pick the note that matches the customer's research segment (F&O → VIX / Bank Nifty, Commodity → gold / crude).
SMs can add their own notes on **Data & admin**, which appear first. The page also shows the live notes and every check.

## Admin page (PIN)

**Admin: rules** is unlocked with `ADMIN_PIN`. Locally it lives in `.streamlit/secrets.toml` (git ignores it); on
Streamlit Cloud it goes in the app's Secrets. Four sections:
- **Follow-up rules:** NPC retry hours, warning and escalation minutes, first-call times per priority.
- **👥 Team:** the real GMs and SMs, who reports to whom, capacity, email, category focus, active. Leave ID blank for
  new people. Removing or deactivating a GM moves their clients, with pending follow-ups and promised callbacks, to the
  other GMs. This overrides `team:` in config.yaml.
- **🗓️ Hourly routine:** the GM's day plan (start time, what the hour is for, nudge on or off). This overrides `day_structure:`.
- **Sarthi track record:** pull from the research database, plus approved examples and the disclaimer.

Team and routine are stored in the database, so changes apply straight away without a redeploy.

## Hourly routine

Working hours run 10:00–18:30. `day_structure` in `config.yaml` sets the GM's hour-by-hour plan: huddle, hot clients,
fresh sign-ups, lunch, NPC round 1, app-activity follow-ups, re-activation, NPC round 2 / evening callbacks, wrap-up.
At the start of each block every GM gets a nudge with their live counts (overdue, due this hour, hot), and **My day**
shows the current block. When working hours change, pending follow-ups that now fall outside them move to the next working start.

## Customer priority list (Google Sheet / CSV)

`python run.py export-sheet` (and the scheduler, hourly) publishes every customer with their rank, priority and score,
registration date and bucket, and core and non-core activity (type × times × recency). It also adds momentum, best time
to call, research segments, **engagement levers**, a **conversation opener**, **pitch ideas**, the next best action, the
likely objection, the GM/SM owner and follow-up status. It always writes `data/exports/customer_priority.csv`, and writes
the Google Sheet once one is set up (DEPLOY.md, step 6). SMs can also filter and download it on the dashboard's
**Customer list** page.

## Follow-up rules and time limits

Every follow-up has a due time and a deadline. The admin sets the numbers on **Admin: rules** (PIN in `ADMIN_PIN`).
GMs see them under "⏱️ Your follow-up time limits" but cannot change them. A GM can't move an NPC retry either:
"not picked up" always uses the admin's hours.

| Event | What the system does |
|---|---|
| New client | Assigned to a GM (category specialist first, else the least-loaded by capacity), with a first call due per its heat tier |
| Not picked up / busy / switched off | Client resurfaces **X h later** (admin setting, default 4); after hours it rolls to the next working morning. 6 consecutive NPCs → unreachable, manager informed |
| Customer asks for a callback | Caller enters the time and it becomes a promised follow-up |
| App activity after the last contact (deposit failed, order abandoned…) | System follow-up created (`triggers:`) |
| No contact for 3 days | System follow-up to re-engage |
| Follow-up overdue 15 min | Warning to the GM (dashboard, plus email/webhook if configured) |
| Overdue 60 min | Escalated to the GM's SM and counted as **missed**, even if called later |
| Sheet row changes | Client details update; workflow state is kept; a new "Next Follow-up" time in the sheet becomes a promised follow-up |

Compliance per caller per day = on-time follow-ups ÷ follow-ups due. The EOD report lists every miss.

## Notifications

Every notification appears in the dashboard and is written to `data/outbox/<date>/<person>.md`.
For email, set `notifications.email` in `config.yaml` and the `SMTP_PASSWORD` environment variable. For Slack or
Google Chat, set `notifications.webhook_url`. Team members' emails go in `team:`.

## AI pitches (optional)

Each client has a rule-based pitch built from its stage, app activity and market notes. With Anthropic credentials
(`ANTHROPIC_API_KEY`), the **AI pitch** tab writes a tailored one with Claude. Only the client's first name, category,
activity and call notes are sent: no phone number, email or client code. The prompt forbids security-specific
recommendations and return promises.

## Referral drives (separate dashboard)

Referrals run on their own dashboard: `streamlit run referral_app.py --server.port 8503` (link at the bottom of
**Customer list**). They are a drive the SM starts when they want one, not a continuous queue. Referral asks never
enter the main dialer, follow-up SLAs, warnings, escalations or the compliance and Excel reports.

**Who gets asked.** The pool is built from up to three sources, each a query pull (office network) or an upload of the
query's output:
- **Most active traders:** cash and F&O trades in the last 60 days. The Medium, High and Very High buckets join
  (`wepipeline/referral.py: REFERRAL_SQL`).
- **Past referrers:** CRM leads with source 37, rolled up per referrer as referred, made a first trade, and funded
  (`HISTORY_SQL`; LeadSubSource is the referring client's code, confirmed by the Growth team).
- **Campaign engagement:** referral campaigns on any channel. Opens, clicks, replies, shares and opt-outs, in long or wide
  form. Rows are kept per client, campaign, channel, action and day, so re-uploads don't double count.

**Score (0-100, same 🔥 ≥ 70 / 🌤️ ≥ 40 tiers).**
- Base: trading volume (square-root curve), consistency and recency, plus app heat and past referrals that traded.
- Campaign engagement is scored on its own. Shares and interest count more than replies, replies more than clicks,
  clicks more than opens. Fresher engagement counts more, and opt-outs count against.
- Final score: the higher of the two plus a quarter of the other. A client who shared the campaign link yesterday is
  hot even with no trades. Weights are in `config.yaml: referral.score / referral.engagement`.

**Rules.**
- Clients on a payout, funding or re-activation list are left to that call first.
- A client is asked at most once every 60 days (Admin: rules).
- A "no" is final. Three not-picked-up calls drop the ask.
- Calls are suggested from 15:45, after the market close.

**Reward.** The amounts are set on Admin: rules (to the referrer and to the friend, paid after the friend's first
trade). Until they are set, GMs are told not to quote or promise any amount.

**GMs** work **My referral calls**. Each card shows why the client is on the list, Hinglish talking points and the ask.
"🤝 Gave referral(s)" records the friends' names and numbers, taken only with the friend's consent.

**SMs** see:
- The pool and drive numbers.
- A campaign table: engaged → hot / warm / cold → called → gave referrals.
- The friends collected, with a CSV download. These are leads, not yet registered.

## Sarthi activation drives (separate dashboard)

`streamlit run sarthi_app.py --server.port 8504`. The target list is clients with **Sarthi off** in the client
database (`IsSarthiTIOn` not 1) and an open account, drawn from three sources:
- **Traders:** the trade query joined to the Sarthi flag, Low activity and up.
- **App users:** app heat ≥ 20, or any Sarthi interest.
- **Campaign engagement:** clients who engaged with an uploaded Sarthi channel campaign.

**Score.** The strongest of three tracks, plus a quarter of the other two:
- **Activity:** trading plus app use.
- **Sarthi interest:** accepted or opened Sarthi ideas with the switch off. Fresher counts more.
- **Campaign engagement.**

**Success.** Sarthi switched on (`SarthiActivationDate`) after the campaign started. It is credited to the GM who
called before the switch-on, or comes from the GM's "⚡ Activated" on the call. The rest switched on without a call.

The list refreshes every 24 h, from the dashboard or with `python run.py sarthi-pool` (`--force` to refresh now).

**Shared engine.** Referral and Sarthi drives run on `wepipeline/drives.py`, with campaigns, drives, channel-engagement
uploads, scoring tracks and reports. A client can be on several drive lists at once (`drive_members`, one row per
program), but is handed to a GM in only one drive at a time. Clients on a funding, payout or re-activation list are
left to that call: those calls already ask for Sarthi. Drive calls are tagged `calls.program` and never count in the
funding compliance or Excel reports.

## Before going live

- Replace the placeholder team in `config.yaml` (`team:`) with the real GMs (`role: gm`, `sm:`, `capacity:`) and SMs (`role: sm`).
- Set `sources.client_db.server` (from IT) for the no-deposit client pull.
- Point `sources.leads` at the live Google Sheet and check the `columns:` mapping against its headers.
- The dashboard has no login: anyone who can open it can pick any name. Keep it on the office network, or make it a private app on Streamlit Cloud (see DEPLOY.md).
- Sample data for a dry run: `python scripts/make_sample_data.py`, then point `sources.leads.path` at
  `data/sample/leads.csv` and `sources.webengage.folder` at `data/sample/webengage`.
