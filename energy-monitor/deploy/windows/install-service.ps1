# Install Energy Monitor as a Windows service using NSSM (https://nssm.cc).
# Run PowerShell AS ADMINISTRATOR from the energy-monitor folder:
#   powershell -ExecutionPolicy Bypass -File deploy\windows\install-service.ps1 -SmtpPassword "app-password"
# NSSM: download nssm.exe and put it on PATH or next to this script.
param(
    [string]$ServiceName = "EnergyMonitor",
    [string]$SmtpPassword = ""
)
$ErrorActionPreference = "Stop"
$root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$nssm = (Get-Command nssm -ErrorAction SilentlyContinue).Source
if (-not $nssm -and (Test-Path "$PSScriptRoot\nssm.exe")) { $nssm = "$PSScriptRoot\nssm.exe" }
if (-not $nssm) { throw "nssm.exe not found. Download it from https://nssm.cc/download and place it on PATH." }

$python = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) { throw "Run deploy\windows\install.ps1 first." }

& $nssm install $ServiceName $python "-m ems run"
& $nssm set $ServiceName AppDirectory $root
& $nssm set $ServiceName DisplayName "Energy Monitor"
& $nssm set $ServiceName Description "Modbus energy metering, dashboard and email reports"
& $nssm set $ServiceName Start SERVICE_AUTO_START
& $nssm set $ServiceName AppExit Default Restart
& $nssm set $ServiceName AppRestartDelay 10000
& $nssm set $ServiceName AppStdout (Join-Path $root "data\service.log")
& $nssm set $ServiceName AppStderr (Join-Path $root "data\service.log")
& $nssm set $ServiceName AppRotateFiles 1
& $nssm set $ServiceName AppRotateBytes 5000000
if ($SmtpPassword) { & $nssm set $ServiceName AppEnvironmentExtra "EMS_SMTP_PASSWORD=$SmtpPassword" }

New-Item -ItemType Directory -Force -Path (Join-Path $root "data") | Out-Null
& $nssm start $ServiceName
Write-Host "Service '$ServiceName' installed and started. Dashboard: http://localhost:8080"
Write-Host "Manage it with: nssm restart $ServiceName  |  nssm stop $ServiceName  |  nssm remove $ServiceName confirm"
