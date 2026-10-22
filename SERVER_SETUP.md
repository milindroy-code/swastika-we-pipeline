# Server setup: Growth dashboards (WE Pipeline)

Three web dashboards plus a background scheduler, all on one Windows server inside the office network. Allow about 30 minutes.

| What | Address after setup |
|---|---|
| Main dashboard (Growth Managers, Service Managers) | `http://<server name>:8501` |
| Referral drives | `http://<server name>:8503` |
| Sarthi activation | `http://<server name>:8504` |
| Scheduler (sync, scoring, alerts, hourly SQL Server pull) | background process, log in `data\logs\scheduler.log` |
| Cohort worker (daily lists: WebEngage app activity overnight from 20:00, lists scored at 08:30) | background process, log in `data\logs\cohorts.log` |

The data lives on the server in `data\growth.db` (SQLite). It is created empty on first start; no database server is needed.

## 0. What the server needs

- Windows Server 2016+ or Windows 10/11, always on, in the office network
- Network access to the back-office SQL Server **192.168.0.181, port 1433** (databases `Repl_TargetReview`, `ReplResearchPanelDB`,
  and for the clients' WebEngage IDs `Repl_CRMLive` and `Repl-eKyc`; all read-only)
- Outbound HTTPS to **dashboard.in.webengage.com** (the daily lists' app activity; one call at a time, 4 s apart)
- Inbound TCP **8501, 8503, 8504** from the office network (step 5 opens them in Windows Firewall)
- Optional: outbound HTTPS to nseindia.com, finance.yahoo.com, economictimes.indiatimes.com and news.google.com (live market notes; everything else works without it)
- About 2 GB disk, 4 GB RAM

## 1. Install the software

1. **Python 3.14** (the version it is tested on) from https://www.python.org/downloads/ : tick **"Add python.exe to PATH"** and choose **Customize → Install for all users**.
2. **Microsoft ODBC Driver 18 for SQL Server** (x64): https://learn.microsoft.com/sql/connect/odbc/download-odbc-driver-for-sql-server
3. Open a new PowerShell and check: `python --version` shows 3.14.x.

## 2. Copy the code

Unzip `we-pipeline-code-<date>.zip` to a permanent folder, for example `D:\Apps\we-pipeline` (the folder that contains `app.py`).
Then install the Python packages:

```powershell
cd D:\Apps\we-pipeline
python -m pip install -r requirements.txt -r requirements-office.txt
```

## 3. Choose the account it runs as

Create (or pick) a Windows account the dashboards run under, e.g. `DOMAIN\svc-growth`. It needs:
- read / write on the project folder (the `data\` folder is created inside it)
- "Log on as a batch job" (normally granted by default to local / domain users)

## 4. Set the secrets (System environment variables)

Control Panel → System → Advanced system settings → Environment Variables → **System variables → New**:

| Name | Value |
|---|---|
| `CLIENT_DB_USER` | the read-only SQL login for 192.168.0.181 (ask the Growth team / DBA) |
| `CLIENT_DB_PASSWORD` | its password |
| `ADMIN_PIN` | a PIN for the Admin page (rules and team); share it only with the Growth admin |
| `WE_API_KEY` | the WebEngage REST API key (WebEngage → Data Platform → Integrations → REST API) |

Then test the SQL Server connection, in a **new** PowerShell window:

```powershell
cd D:\Apps\we-pipeline
python run.py pull-clients
```

It should print counts of new clients by age bucket (0–7 / 8–15 / 16–30 days). "Login failed" = wrong login; "timeout" = no network route to 192.168.0.181:1433.

## 5. Open the firewall and start at boot

PowerShell **as administrator**:

```powershell
cd D:\Apps\we-pipeline
powershell -ExecutionPolicy Bypass -File scripts\install_office.ps1 -AtStartup -User "DOMAIN\svc-growth"
Start-ScheduledTask "Growth dashboards"
```

- Opens 8501 / 8503 / 8504 to the server's own subnet only. If users connect from other ranges (other floors, VPN), add them:
  `-RemoteAddress "LocalSubnet,10.0.0.0/16,10.8.0.0/16"` (your ranges).
- Registers the scheduled task **Growth dashboards**, which starts the three dashboards and the scheduler at every boot,
  and restarts them if they fail.

## 6. Check it works

1. From another office PC, open `http://<server name>:8501`, `:8503` and `:8504`.
2. After 5–10 minutes, `data\logs\scheduler.log` has lines starting with `tick:` (the first run, including the SQL Server pull, takes 2–3 minutes).
   `data\logs\cohorts.log` starts with `Cohort worker started`; **Customer list → Today's lists** shows the four lists building.
3. Tell the Growth team the three links. They enter the real team on the Admin page and upload the new client lists.

## Day to day

| Task | Command (PowerShell in the project folder) |
|---|---|
| Restart everything | `powershell -ExecutionPolicy Bypass -File scripts\start_dashboards.ps1 -Office -Restart` |
| Stop everything | `powershell -ExecutionPolicy Bypass -File scripts\stop_dashboards.ps1` |
| Update the code | Stop, replace the code files (**keep `data\`**), `python -m pip install -r requirements.txt -r requirements-office.txt`, start |
| Scheduler log | `data\logs\scheduler.log` |
| Daily lists log | `data\logs\cohorts.log` (one worker only: never run the cohort worker on a second machine as well) |

## Backups (please set up)

`data\growth.db` holds all client data, calls and follow-ups. Include the project's `data\` folder in the nightly server
backup. For a consistent copy while it runs, back up the files `growth.db`, `growth.db-wal` and `growth.db-shm` together.

## Security notes

- The dashboards have **no login**: anyone who can reach the ports can see client codes and activity. Keep the firewall
  rule limited to office ranges; do not expose the ports to the internet.
- Admin changes (team, rules) need the `ADMIN_PIN`.
- The SQL Server is only read (`ApplicationIntent=ReadOnly`, SELECT queries).
- Email and webhook notifications are off (`config.yaml` → `notifications`); notes and alerts appear in the dashboards only.

## Remove

```powershell
Unregister-ScheduledTask "Growth dashboards" -Confirm:$false
Remove-NetFirewallRule -Group "Growth dashboards"
```
Then stop the processes (`scripts\stop_dashboards.ps1`) and delete the folder (this deletes the data).
