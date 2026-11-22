# Install Flexpower EMS Kit as a Windows service (uses NSSM, https://nssm.cc).
#
# Run PowerShell AS ADMINISTRATOR from the flexpower-ems-kit folder:
#   powershell -ExecutionPolicy Bypass -File deploy\windows\install-service.ps1
#
# What it does
#   * Runs the service under its own restricted virtual account
#     "NT SERVICE\FlexpowerEMS" instead of the all-powerful LocalSystem.
#   * Locks the program folder so only Administrators, SYSTEM, you and the
#     service can read it, and only the service may write to the data folder.
#   * Optionally asks for the email password and stores it encrypted (DPAPI);
#     it is never put on a command line, in the registry or in shell history.
#
# Re-running the script is safe: it replaces the existing service.
# -UseLocalSystem is a fallback if your RS-485 adapter refuses the virtual account.
param(
    [string]$ServiceName = "FlexpowerEMS",
    [switch]$SkipSmtpPassword,
    [switch]$UseLocalSystem
)
$ErrorActionPreference = "Stop"

$identity = [Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
if (-not $identity.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "Run this script from an Administrator PowerShell window."
}

$root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$data = Join-Path $root "data"
$python = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) { throw "Run deploy\windows\install.ps1 first." }

$nssm = (Get-Command nssm -ErrorAction SilentlyContinue).Source
if (-not $nssm -and (Test-Path "$PSScriptRoot\nssm.exe")) { $nssm = "$PSScriptRoot\nssm.exe" }
if (-not $nssm) { throw "nssm.exe not found. Download it from https://nssm.cc/download and put it on PATH." }

# The restricted account cannot read another user's profile, so Python itself
# must be installed for all users (C:\Program Files\Python3xx), not in AppData.
if (-not $UseLocalSystem) {
    $cfgLine = Get-Content (Join-Path $root ".venv\pyvenv.cfg") | Where-Object { $_ -match "^\s*home\s*=" } | Select-Object -First 1
    $pyHome = ($cfgLine -split "=", 2)[1].Trim()
    if ($pyHome -like "$env:SystemDrive\Users\*") {
        throw ("Python is installed inside a user profile ($pyHome), which the service account cannot read.`n" +
               "Reinstall Python with 'Install Python for all users' ticked (Customize installation > Advanced options),`n" +
               "delete the .venv folder, run deploy\windows\install.ps1 again, then re-run this script.")
    }
}

New-Item -ItemType Directory -Force -Path $data | Out-Null

# ---- (re)create the service --------------------------------------------------
if (Get-Service -Name $ServiceName -ErrorAction SilentlyContinue) {
    Write-Host "Replacing existing service $ServiceName..."
    & $nssm stop $ServiceName | Out-Null
    & $nssm remove $ServiceName confirm | Out-Null
    Start-Sleep -Seconds 2
}
& $nssm install $ServiceName $python "-m ems run"
& $nssm set $ServiceName AppDirectory $root
& $nssm set $ServiceName DisplayName "Flexpower EMS Kit"
& $nssm set $ServiceName Description "Modbus energy metering, dashboard and email reports"
& $nssm set $ServiceName Start SERVICE_AUTO_START
& $nssm set $ServiceName AppExit Default Restart
& $nssm set $ServiceName AppRestartDelay 10000
& $nssm set $ServiceName AppStdout (Join-Path $data "service.log")
& $nssm set $ServiceName AppStderr (Join-Path $data "service.log")
& $nssm set $ServiceName AppRotateFiles 1
& $nssm set $ServiceName AppRotateBytes 5000000

if ($UseLocalSystem) {
    $account = "*S-1-5-18"   # SYSTEM
    Write-Warning "Service will run as LocalSystem (full rights). Prefer the default restricted account."
} else {
    $accountName = "NT SERVICE\$ServiceName"
    sc.exe config $ServiceName obj= $accountName | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Could not set the service account (sc.exe exit $LASTEXITCODE)." }
    $account = $accountName
}

# ---- email password (encrypted) ----------------------------------------------
$secret = Join-Path $data "smtp.secret"
if (-not $SkipSmtpPassword) {
    $answer = Read-Host "Store or replace the email (SMTP) password now? [y/N]"
    if ($answer -match "^[yY]") {
        Push-Location $root
        & $python -m ems set-smtp-password
        Pop-Location
    }
}

# ---- folder permissions ------------------------------------------------------
# Well-known SIDs keep this working on non-English Windows:
#   S-1-5-32-544 = Administrators, S-1-5-18 = SYSTEM
$me = "$env:USERDOMAIN\$env:USERNAME"
Write-Host "Restricting folder permissions..."
icacls $root /inheritance:r /grant:r "*S-1-5-32-544:(OI)(CI)F" "*S-1-5-18:(OI)(CI)F" "${me}:(OI)(CI)M" "${account}:(OI)(CI)RX" /T /C /Q | Out-Null
icacls $data /grant "${account}:(OI)(CI)M" /T /C /Q | Out-Null
if (Test-Path $secret) {
    # Only Administrators, SYSTEM and the service may read the encrypted password.
    icacls $secret /inheritance:r /grant:r "*S-1-5-32-544:F" "*S-1-5-18:F" "${account}:R" /C /Q | Out-Null
}

& $nssm start $ServiceName
Start-Sleep -Seconds 3
$status = (Get-Service -Name $ServiceName).Status
Write-Host ""
Write-Host "Service '$ServiceName' is $status (account: $account)."
Write-Host "Dashboard: http://localhost:8080   Logs: $data\ems.log"
Write-Host "Manage it with: nssm restart $ServiceName  |  nssm stop $ServiceName  |  nssm remove $ServiceName confirm"
if ($status -ne "Running") { Write-Warning "The service did not start. Check $data\service.log." }
