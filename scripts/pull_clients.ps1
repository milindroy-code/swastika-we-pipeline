# Pulls new no-deposit clients from the back-office SQL Server into the shared database,
# then re-scores and assigns. Schedule it hourly in Task Scheduler on an office PC that can reach the SQL Server.
$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)
foreach ($name in "DATABASE_URL", "CLIENT_DB_USER", "CLIENT_DB_PASSWORD", "CLIENT_DB_CONN") {
    if (-not (Get-Item "env:$name" -ErrorAction SilentlyContinue)) {
        $value = [Environment]::GetEnvironmentVariable($name, "User")
        if ($value) { Set-Item "env:$name" $value }
    }
}
New-Item -ItemType Directory -Force data | Out-Null
python run.py pull-clients *>> data\pull_clients.log
python run.py referrals *>> data\pull_clients.log     # referral pool, once it's older than referral.refresh_hours
python run.py sarthi-pool *>> data\pull_clients.log   # Sarthi-off target list, once it's older than sarthi_activation.refresh_hours
