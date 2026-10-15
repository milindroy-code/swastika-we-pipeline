# One-time setup to serve the dashboards to the office network from this PC. Run as ADMINISTRATOR:
#   Right-click PowerShell > Run as administrator, then:
#   powershell -ExecutionPolicy Bypass -File "C:\path\to\WE Pipeline\scripts\install_office.ps1"
#
# 1. Opens ports 8501 / 8503 / 8504 in Windows Firewall, only for the office network (-RemoteAddress; default:
#    this PC's own subnet). VPN users on another range: add it, e.g. -RemoteAddress "LocalSubnet,10.8.0.0/16".
# 2. Starts the dashboards and the scheduler when -User logs on (scheduled task "Growth dashboards").
# Undo:  Remove-NetFirewallRule -Group "Growth dashboards"; Unregister-ScheduledTask "Growth dashboards" -Confirm:$false
param([string]$RemoteAddress = "LocalSubnet", [string]$User = "$env:USERDOMAIN\$env:USERNAME")

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
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $User
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit 0
Register-ScheduledTask -TaskName "Growth dashboards" -Action $action -Trigger $trigger -Settings $settings -User $User -Force | Out-Null
Write-Host "Scheduled task 'Growth dashboards': starts everything when $User logs on"
Write-Host ""
Write-Host "Now start them:  powershell -ExecutionPolicy Bypass -File `"$root\scripts\start_dashboards.ps1`" -Office -Restart"
Write-Host "GMs open:  http://$($env:COMPUTERNAME):8501   Referral: :8503   Sarthi: :8504"
