# Starts the three dashboards in their own windows, so they keep running after Claude Code or a terminal closes.
#   Main (GMs and SMs)      http://localhost:8501
#   Referral drives         http://localhost:8503
#   Sarthi activation       http://localhost:8504
# Run:  powershell -ExecutionPolicy Bypass -File scripts\start_dashboards.ps1
#
# -Office: serve them to the office network (http://<this PC's name>:8501 etc.) and run the scheduler here too
#          (python run.py loop: sync, scoring, assignment, alerts, morning / EOD notes, and the hourly client DB pulls).
#          The firewall rules come from scripts\install_office.ps1 (run once, as administrator).
# -Restart: stop what is running first (needed when switching between local and -Office).
# A dashboard that is already running is left alone.
param([switch]$Office, [switch]$Restart)
Set-Location (Split-Path $PSScriptRoot -Parent)
foreach ($name in "DATABASE_URL", "CLIENT_DB_USER", "CLIENT_DB_PASSWORD", "CLIENT_DB_CONN", "ADMIN_PIN", "WE_API_KEY") {
    if (-not (Get-Item "env:$name" -ErrorAction SilentlyContinue)) {
        $value = [Environment]::GetEnvironmentVariable($name, "User")
        if ($value) { Set-Item "env:$name" $value }
    }
}
if ($Restart) { & "$PSScriptRoot\stop_dashboards.ps1" }

$hostName = if ($Office) { $env:COMPUTERNAME } else { "localhost" }
$address = if ($Office) { "0.0.0.0" } else { "127.0.0.1" }
$env:REFERRAL_DASHBOARD_URL = "http://${hostName}:8503"  # the links between dashboards
$env:SARTHI_DASHBOARD_URL = "http://${hostName}:8504"

$apps = @(@{ File = "app.py"; Port = 8501 }, @{ File = "referral_app.py"; Port = 8503 }, @{ File = "sarthi_app.py"; Port = 8504 })
foreach ($a in $apps) {
    if (Get-NetTCPConnection -LocalPort $a.Port -State Listen -ErrorAction SilentlyContinue) {
        Write-Host "$($a.File) is already running on port $($a.Port) (use -Restart to restart it)"
        continue
    }
    Start-Process -FilePath "python" -WindowStyle Minimized -ArgumentList @(
        "-m", "streamlit", "run", $a.File, "--server.headless", "true", "--server.port", "$($a.Port)",
        "--server.address", $address, "--server.runOnSave", "false", "--server.fileWatcherType", "none",
        "--browser.gatherUsageStats", "false")
    Write-Host "Started $($a.File) on http://${hostName}:$($a.Port)"
}

if ($Office) {
    $running = Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object { $_.CommandLine -match "run\.py\s+loop" }
    if ($running) {
        Write-Host "Scheduler is already running"
    } else {
        New-Item -ItemType Directory -Force "data\logs" | Out-Null
        $env:CLIENT_DB_PULL = "1"  # this PC can reach the back-office SQL Server
        Start-Process -FilePath "python" -WindowStyle Hidden -ArgumentList @("run.py", "loop") `
            -RedirectStandardOutput "data\logs\scheduler.out.log" -RedirectStandardError "data\logs\scheduler.log"
        Write-Host "Started the scheduler (log: data\logs\scheduler.log)"
    }
    # Daily cohort lists: overnight WebEngage activity fetch, morning scoring (its own process: a fetch takes hours)
    $worker = Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object { $_.CommandLine -match "run\.py\s+cohorts" }
    if ($worker) {
        Write-Host "Cohort worker is already running"
    } else {
        $env:CLIENT_DB_PULL = "1"
        Start-Process -FilePath "python" -WindowStyle Hidden -ArgumentList @("run.py", "cohorts") `
            -RedirectStandardOutput "data\logs\cohorts.out.log" -RedirectStandardError "data\logs\cohorts.log"
        Write-Host "Started the cohort worker (log: data\logs\cohorts.log)"
    }
}
