# One-time setup to serve the dashboards to the office network from this machine. Run as ADMINISTRATOR:
#   Right-click PowerShell > Run as administrator, then:
#   powershell -ExecutionPolicy Bypass -File "C:\path\to\we-pipeline\scripts\install_office.ps1" [-AtStartup] [-User DOMAIN\account]
#
# 1. Opens ports 8501 / 8503 / 8504 in Windows Firewall, only for the office network (-RemoteAddress; default:
#    this machine's own subnet). VPN users on another range: add it, e.g. -RemoteAddress "LocalSubnet,10.8.0.0/16".
# 2. Scheduled task "Growth dashboards" that starts the dashboards and the scheduler:
#    - default: when -User logs on (an office PC someone logs on to)
#    - -AtStartup: when the machine boots, nobody logged on (a server). Runs as -User without storing a password
#      (S4U), so it reaches the SQL Server with the SQL login in CLIENT_DB_USER / CLIENT_DB_PASSWORD (set them as
#      System environment variables), not with Windows authentication.
# Undo:  Remove-NetFirewallRule -Group "Growth dashboards"; Unregister-ScheduledTask "Growth dashboards" -Confirm:$false
param([string]$RemoteAddress = "LocalSubnet", [string]$User = "$env:USERDOMAIN\$env:USERNAME", [switch]$AtStartup)

$admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $admin) { Write-Error "Run this from PowerShell opened with 'Run as administrator'."; exit 1 }

$root = Split-Path $PSScriptRoot -Parent
Remove-NetFirewallRule -Group "Growth dashboards" -ErrorAction SilentlyContinue
New-NetFirewallRule -DisplayName "Growth dashboards (8501, 8503, 8504)" -Group "Growth dashboards" -Direction Inbound `
    -Protocol TCP -LocalPort 8501, 8503, 8504 -RemoteAddress ($RemoteAddress -split ",") -Action Allow -Profile Domain, Private | Out-Null
Write-Host "Firewall: ports 8501, 8503, 8504 open to $RemoteAddress (domain / private networks)"

$action = New-ScheduledTaskAction -Execute "powershell.exe" -WorkingDirectory $root `
    -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$root\scripts\start_dashboards.ps1`" -Office"
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit 0 `
    -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 5)
if ($AtStartup) {
    $trigger = New-ScheduledTaskTrigger -AtStartup
    $principal = New-ScheduledTaskPrincipal -UserId $User -LogonType S4U -RunLevel Limited
    Register-ScheduledTask -TaskName "Growth dashboards" -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Force | Out-Null
    Write-Host "Scheduled task 'Growth dashboards': starts everything at boot, as $User"
} else {
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $User
    Register-ScheduledTask -TaskName "Growth dashboards" -Action $action -Trigger $trigger -Settings $settings -User $User -Force | Out-Null
    Write-Host "Scheduled task 'Growth dashboards': starts everything when $User logs on"
}
Write-Host ""
Write-Host "Start now:  Start-ScheduledTask 'Growth dashboards'   (or: scripts\start_dashboards.ps1 -Office -Restart)"
Write-Host "Open:       http://$($env:COMPUTERNAME):8501   Referral: :8503   Sarthi: :8504"
