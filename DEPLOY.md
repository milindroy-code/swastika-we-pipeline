# Deploying the Growth follow-up dashboards

## Option A: office network (in use)

Everything runs on one office PC that can reach the back-office SQL Server. Client data never leaves the company.

| Piece | How |
|---|---|
| Main (GMs and SMs) | `http://<PC name>:8501` |
| Referral drives | `http://<PC name>:8503` |
| Sarthi activation | `http://<PC name>:8504` |
| Scheduler: sync, heat score, assignment, alerts, morning / EOD notes; hourly client DB pull; referral and Sarthi pool refresh | `python run.py loop` on the same PC, every 5 min (log: `data\logs\scheduler.log`) |
| Database | `data\growth.db` on the PC (SQLite). Back it up: copy `data\` while the dashboards are stopped |

1. **Once, as administrator** (right-click PowerShell > Run as administrator):
   `powershell -ExecutionPolicy Bypass -File "<project>\scripts\install_office.ps1"`
   Opens ports 8501 / 8503 / 8504 to this PC's subnet only, and starts everything at logon. VPN users on another range:
   add `-RemoteAddress "LocalSubnet,10.8.0.0/16"` (ask IT for the range).
2. **Start / restart:** `powershell -ExecutionPolicy Bypass -File scripts\start_dashboards.ps1 -Office -Restart`
   **Stop:** `scripts\stop_dashboards.ps1`
3. Share the links with the GMs and SMs. There is no login: anyone on the office network who has the link can open
   them, and admin rules are protected by the `ADMIN_PIN`.
4. The PC must stay on and awake during working hours (Power settings: never sleep when plugged in). A laptop that
   leaves the office or sleeps takes the dashboards down; an always-on desktop or server is better. To move: copy the
   project folder including `data\`, install Python and `pip install -r requirements.txt -r requirements-office.txt`,
   set the user environment variables (`CLIENT_DB_USER` / `CLIENT_DB_PASSWORD` if a SQL login is used, `ADMIN_PIN`), and
   run step 1 there.

## Option B: Streamlit Community Cloud (free tier)

| Piece | Runs on | Cost |
|---|---|---|
| Dashboard (GMs log calls, SMs track compliance) | Streamlit Community Cloud | Free |
| Database (clients, calls, follow-ups, alerts) | Neon Postgres | Free: 0.5 GB, about 100× what this needs |
| Scheduler: sync, heat score, assignment, warnings and escalations, morning and EOD notes | GitHub Actions, every 15 min, 10:00–19:15 IST, Mon–Fri | Free: about 900 of the 2,000 free minutes a month |
| Pull of new no-deposit clients from the back-office SQL Server | An always-on office PC (Task Scheduler, hourly) | Free |
| Live Google Sheet: every customer with score, levers and pitch | Google Sheets, written by the scheduler hourly | Free |

The SQL Server is inside the office network, so the cloud can't reach it. The office PC pulls from it and writes into the
same Neon database the dashboard reads.

Allow about 30 minutes. You need a GitHub account and an email address for Neon and Streamlit.

---

## 1. Create the database (Neon), about 5 min

1. Go to **https://neon.tech**, sign up and click **New project**.
2. Name it `growth-followups`. For region, pick **AWS US East (N. Virginia)**: the dashboard runs in the US, so this keeps it fast.
   If your compliance team needs data closer to India, pick **AWS Asia Pacific (Singapore)**; the dashboard will be slower.
3. On the project page click **Connect**, then copy the connection string. It looks like
   `postgresql://neondb_owner:...@ep-....aws.neon.tech/neondb?sslmode=require`.
   **This string is the password to all client data.** Keep it private and only paste it into the secret fields below.

The tables are created automatically the first time the app connects.

## 2. Put the code on GitHub, about 5 min

1. On **https://github.com/new** create a repository named `growth-followups`, set it to **Private**, and don't add a README.
2. In PowerShell, in this project folder (the code is already committed locally, and `data/` with the client file is excluded):

   ```powershell
   git remote add origin https://github.com/<your-username>/growth-followups.git
   git push -u origin main
   ```

3. In the repository, open **Settings → Secrets and variables → Actions → New repository secret**:
   - Name `DATABASE_URL`, value: the Neon string from step 1.
4. Open the **Actions** tab, enable workflows if asked, click **Follow-up scheduler → Run workflow**, and wait for the green tick.
   From then on it runs by itself every 15 minutes during working hours.

## 3. Deploy the dashboard (Streamlit Community Cloud), about 10 min

1. Go to **https://share.streamlit.io** and sign in with GitHub. Allow access to the private repo when asked.
2. Click **Create app → Deploy from GitHub**, then:
   - Repository: `<your-username>/growth-followups`, branch `main`, main file `app.py`
   - App URL: something like `swastika-growth` (it becomes `swastika-growth.streamlit.app`)
3. Open **Advanced settings**:
   - Python version **3.12**
   - Secrets: paste the following, with your values:

     ```toml
     DATABASE_URL = "postgresql://...neon.tech/neondb?sslmode=require"
     ADMIN_PIN = "the admin PIN (the same one you use locally in .streamlit/secrets.toml)"
     # ANTHROPIC_API_KEY = "sk-ant-..."   # optional: AI-written pitches
     ```
4. Click **Deploy**. The first build takes 2–3 minutes.
5. **Make it private** (strongly recommended: there is no login, so anyone with the link would otherwise see client codes):
   in the app, open **Share**, set who can view to **Only specific people**, and invite each GM's and SM's work email.
   They sign in with Google, or with a one-time link sent to their email. The free plan allows one private app.

### 3b. The referral dashboard (optional), about 3 min

Create a second app from the same repository with main file `referral_app.py`, and paste the same Secrets
(`DATABASE_URL`, `ADMIN_PIN`). Both apps read the same Neon database. To show the right link on the main dashboard,
add `REFERRAL_DASHBOARD_URL = "https://<your-referral-app>.streamlit.app"` to the main app's Secrets.

### 3c. The Sarthi activation dashboard (optional), about 3 min

Same as 3b with main file `sarthi_app.py`. Add `SARTHI_DASHBOARD_URL` to the main app's Secrets for the link.

## 4. First-time setup in the dashboard, about 5 min

1. Open the app and pick an **SM** in the sidebar.
2. **Data & admin**: upload the WebEngage export (for example `user_auth_after_4octandnot_added_fund.xlsx`).
   Clients are created, scored and assigned straight away.
3. **Admin: rules**: unlock with the `ADMIN_PIN`. Set the not-picked-up retry hours, warning and escalation minutes,
   and first-call times per priority. GMs see these limits but can't change them.
4. **Data & admin → Today's market notes**: add a few talking points (repeat every morning).

## 5. Pull no-deposit clients from the back-office SQL Server (office PC)

You need from IT: the **SQL Server name or IP**, and either a **Windows account** with read access to
`Repl_TargetReview`, or a read-only **SQL login**.

1. In `config.yaml`, set `sources.client_db.server` (for example `server: "10.0.0.25"`), then commit and push:
   `git commit -am "Set client DB server"; git push`.
2. On an office PC that is always on and can reach the SQL Server:

   ```powershell
   git clone https://github.com/<your-username>/growth-followups.git; cd growth-followups
   python -m pip install -r requirements-office.txt
   # "ODBC Driver 18 for SQL Server" must be installed (it is on this laptop).
   setx DATABASE_URL "postgresql://...neon.tech/neondb?sslmode=require"
   # Only for a SQL login (otherwise the Windows login of the account running the task is used):
   setx CLIENT_DB_USER "growth_readonly"; setx CLIENT_DB_PASSWORD "..."
   ```

   Open a new PowerShell window so the variables apply, then test:
   `python run.py pull-clients`. It prints how many clients fall in each 0–7 / 8–15 / 16–30 day bucket.
3. Schedule it hourly (run once, in PowerShell as the user who has database access):

   ```powershell
   $action  = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$PWD\scripts\pull_clients.ps1`""
   $trigger = New-ScheduledTaskTrigger -Daily -At 9:00
   # repeat every hour for 11 hours (09:00-20:00), every day
   $trigger.Repetition = (New-ScheduledTaskTrigger -Once -At 9:00 -RepetitionInterval (New-TimeSpan -Hours 1) -RepetitionDuration (New-TimeSpan -Hours 11)).Repetition
   Register-ScheduledTask -TaskName "Growth - pull no-deposit clients" -Action $action -Trigger $trigger
   ```

   Output goes to `data\pull_clients.log`.
4. The office firewall must allow outbound connections to Neon on port **5432**. If the test in step 2 says it can't connect, ask IT.

## 6. Live Google Sheet of all customers, about 10 min

The scheduler rewrites three tabs every hour:
- **Customers**: one row per customer. Rank, 🔥/🌤️/❄️ priority, heat score, registration date and age bucket,
  core and non-core activity (type, number of times, recency), momentum, best time to call, research segments,
  engagement levers, conversation opener, pitch ideas, next best action, objection to expect, GM/SM, next follow-up and call history.
- **Summary**: counts by category and priority, and per GM.
- **How scoring works**: the formula and the current settings.

Google needs a "service account", a robot Google account that is allowed to edit this one sheet:

1. Go to **https://console.cloud.google.com**, create a project (for example `growth-sheet`), then open
   **APIs & Services → Library**, search for **Google Sheets API**, and click **Enable**.
2. **APIs & Services → Credentials → Create credentials → Service account**. Name it `sheet-writer` and click **Done**
   (skip the optional role steps). Open it, go to **Keys → Add key → Create new key → JSON**, and a `.json` file downloads.
   Treat that file like a password.
3. Create a Google Sheet (for example "Growth – Customer Priority List"). Click **Share**, paste the service account's
   email (it looks like `sheet-writer@growth-sheet.iam.gserviceaccount.com`), choose **Editor**, and untick "Notify".
   Share it with the SMs as **Viewer** too.
4. Copy the sheet's id from its URL: `docs.google.com/spreadsheets/d/`**`<THIS PART>`**`/edit`.
5. Add two **GitHub repository secrets** (Settings → Secrets and variables → Actions):
   - `GOOGLE_SERVICE_ACCOUNT_JSON`: open the downloaded `.json` file in Notepad and paste its entire contents
   - `GOOGLE_SHEET_KEY`: the id from step 4
6. Run **Actions → Follow-up scheduler → Run workflow**. The sheet fills within a minute, then refreshes hourly.

To test from this laptop first, save the key as `secrets/service_account.json` (git ignores this folder). Set
`outputs.google_sheet.enabled: true` and `spreadsheet_key` in `config.yaml`, then run `python run.py export-sheet`.

Rows are rewritten on every run, so notes typed into the Customers tab are lost. Keep your own notes in a separate
tab or sheet (a `VLOOKUP` on the Client code column pulls in the latest data).

## Day-to-day

| Who | What |
|---|---|
| GMs | Open the app, pick their name, work **My day** from the top (🔥 hot first), and log every call. NPC retries are scheduled automatically. |
| SMs | **Team compliance**, **Escalations**, the morning team note and the EOD report. **GM view** shows any GM's queue. |
| Admin | **Admin: rules** (PIN). Change the team in `config.yaml` (`team:`), then commit and push: the app redeploys by itself. |
| Anyone | Drop new WebEngage exports in through **Data & admin**. |

## Limits of the free setup

- Alerts are checked every 15 minutes, so a 60-minute escalation fires between 60 and 75 minutes. GitHub can also delay runs a little at busy times.
- If nobody has opened the dashboard for a while, the first visit takes about a minute to wake it. Alerts and notes still go out on schedule.
- Data is stored with Neon and Streamlit (outside India unless you chose Singapore). Confirm this is acceptable with compliance before loading real client data.
- Upgrade path, if needed: Neon Launch plan or a small cloud server in Mumbai, with no code changes (only `DATABASE_URL`).
