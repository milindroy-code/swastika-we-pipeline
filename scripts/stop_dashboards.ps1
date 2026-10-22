# Stops the three dashboards, the scheduler and the cohort worker started by start_dashboards.ps1 (data is untouched).
# Run:  powershell -ExecutionPolicy Bypass -File scripts\stop_dashboards.ps1
$targets = Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object {
    $_.CommandLine -match "streamlit\s+run\s+(app|referral_app|sarthi_app)\.py" -or $_.CommandLine -match "run\.py\s+(loop|cohorts)"
}
foreach ($p in $targets) {
    Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
}
Write-Host "Stopped $(@($targets).Count) process(es)"
Start-Sleep -Seconds 2
