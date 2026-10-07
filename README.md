# Growth follow-up pipeline

Turns WebEngage app activity, new no-deposit clients from the back office, and the live lead sheet into a prioritised
calling queue. It enforces follow-up time limits and reports compliance.

**Roles:** **GMs** (Growth Managers) do the calling and work their queue. **SMs** (Service Managers) mentor their GMs,
receive escalations and get team reports. The **admin** (PIN) sets the follow-up time limits.

To host it for the team for free, see **[DEPLOY.md](DEPLOY.md)**.

```
WebEngage exports ───┐                         ┌─> GM queue (hot first) ─> call logged ─> next follow-up scheduled
Client DB (no FTD) ───┼─> heat score ─> assign ─┤
Lead sheet ───────────┘   (0-100)               └─> overdue? warn GM (15 min) ─> escalate to SM (60 min) ─> EOD misses
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
| Lead sheet | `sources.leads` in `config.yaml` | Local CSV/XLSX, a published Google Sheet CSV link, or a private sheet via service account. Optional: leads can come only from activity files. |
| Market notes | `data/market_notes.md` (or **Data & admin** page) | One talking point per line, used in pitches and the morning note. Update it every morning. |

Recognised WebEngage shapes:

- **Events pivot** (like `user_auth_after_4octandnot_added_fund.xlsx`): `Events | ClientCode | Sum | 4 Oct, 12AM | …`, one event per sheet.
  `Sum` gives frequency and the last non-zero hour gives recency. When several sheets share an event name
  (equity / F&O / commodity research), the sheet name keeps them apart.
- **Activity list**: user id, activity, count and/or last-done date or "days since", optional `Activity Type` (Core / Non-Core).
- **Event-level export**: user id, event name, event time.
- **User export**: user id plus phone/email, used to match leads to WebEngage users.

Users in an activity file who are not in the lead sheet become leads, keyed by ClientCode. Their category comes from
`sources.webengage.file_categories`, which matches part of the file name.

## Heat score and priority

For each activity a user did in the last 30 days:

Registration counts as a core activity (`REGISTERED`), so a client who signed up today scores higher than one from three weeks ago.
Registration alone stays ❄️ cold (about 25). It takes app activity on top to make a lead warm or hot.

```
points = weight × recency × frequency
  weight    = 10 if core, 3 if non-core          (Activity Type column if present, else heat.core_activities)
  recency   = 0.5 ^ (days since last done ÷ 3)   (halves every 3 days)
  frequency = min(1 + log2(times done), 4)       (1×→1, 2×→2, 4×→3, 8+×→4)
score = 100 × (1 − e^(−Σpoints ÷ 35))            → 0-100
```

| Tier | Score | First call within | Where it shows |
|---|---|---|---|
| 🔥 Hot | ≥ 70 | 30 min | Assigned first, top of every queue, highlighted in the morning note, "Hot leads not called yet" table for managers |
| 🌤️ Warm | 40–69 | 2 h | After hot leads |
| ❄️ Cold | < 40 | 4 h | Last |

Scores are recalculated on every sync. When a lead **turns hot**, its follow-up is pulled forward to now and the caller is notified.
All weights, tiers and SLAs live under `heat:` in `config.yaml`. The scale of 35 was tuned on the 4–7 Oct file, giving about 17% hot.

Core activities are currently `ORDER_PLACED_MOB`, `CALL_ACCEPTED_MOB` and `RECOMMENDATION_LISTING_OPENED_MOB`.
`DASHBOARD_OPENED_MOB` and `RECOMMENDATION_LANDING_SCREEN_MOB` are non-core. Adjust `heat.core_activities` to change this.

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
| New lead | Assigned to a GM (category specialist first, else the least-loaded by capacity), with a first call due per its heat tier |
| Not picked up / busy / switched off | Lead resurfaces **X h later** (admin setting, default 4); after hours it rolls to the next working morning. 6 consecutive NPCs → unreachable, manager informed |
| Customer asks for a callback | Caller enters the time and it becomes a promised follow-up |
| App activity after the last contact (deposit failed, order abandoned…) | System follow-up created (`triggers:`) |
| No contact for 3 days | System follow-up to re-engage |
| Follow-up overdue 15 min | Warning to the GM (dashboard, plus email/webhook if configured) |
| Overdue 60 min | Escalated to the GM's SM and counted as **missed**, even if called later |
| Sheet row changes | Lead details update; workflow state is kept; a new "Next Follow-up" time in the sheet becomes a promised follow-up |

Compliance per caller per day = on-time follow-ups ÷ follow-ups due. The EOD report lists every miss.

## Notifications

Every notification appears in the dashboard and is written to `data/outbox/<date>/<person>.md`.
For email, set `notifications.email` in `config.yaml` and the `SMTP_PASSWORD` environment variable. For Slack or
Google Chat, set `notifications.webhook_url`. Team members' emails go in `team:`.

## AI pitches (optional)

Each lead has a rule-based pitch built from its stage, app activity and market notes. With Anthropic credentials
(`ANTHROPIC_API_KEY`), the **AI pitch** tab writes a tailored one with Claude. Only the lead's first name, category,
activity and call notes are sent: no phone number, email or client code. The prompt forbids security-specific
recommendations and return promises.

## Before going live

- Replace the placeholder team in `config.yaml` (`team:`) with the real GMs (`role: gm`, `sm:`, `capacity:`) and SMs (`role: sm`).
- Set `sources.client_db.server` (from IT) for the no-deposit client pull.
- Point `sources.leads` at the live Google Sheet and check the `columns:` mapping against its headers.
- The dashboard has no login: anyone who can open it can pick any name. Keep it on the office network, or make it a private app on Streamlit Cloud (see DEPLOY.md).
- Sample data for a dry run: `python scripts/make_sample_data.py`, then point `sources.leads.path` at
  `data/sample/leads.csv` and `sources.webengage.folder` at `data/sample/webengage`.
