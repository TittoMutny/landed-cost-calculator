# Flexpower EMS Kit - first-time setup on Windows.
# Run from the flexpower-ems-kit folder in PowerShell:
#   powershell -ExecutionPolicy Bypass -File deploy\windows\install.ps1
$ErrorActionPreference = "Stop"
$root = Resolve-Path (Join-Path $PSScriptRoot "..\..")
Set-Location $root

$py = Get-Command py -ErrorAction SilentlyContinue
if (-not $py) { throw "Python launcher 'py' not found. Install Python 3.11+ from python.org: Customize installation, tick 'Add to PATH' and 'Install Python for all users'." }

if (-not (Test-Path ".venv")) {
    Write-Host "Creating virtual environment..."
    py -3 -m venv .venv
}
& .\.venv\Scripts\python.exe -m pip install --upgrade pip
& .\.venv\Scripts\python.exe -m pip install -r requirements.txt

if (-not (Test-Path "config.yaml")) {
    Copy-Item config.example.yaml config.yaml
    Write-Host "Created config.yaml from the example - edit it for your meters."
}

Write-Host ""
Write-Host "Done. Next steps:"
Write-Host "  1. Edit config.yaml (meters, tariff, email)."
Write-Host "  2. Optional demo data:   .\.venv\Scripts\python.exe -m ems seed-demo --days 90"
Write-Host "  3. Test a real meter:    .\.venv\Scripts\python.exe -m ems test-meter <meter_id>"
Write-Host "  4. Start it:             .\run.bat   (then open http://localhost:8080)"
Write-Host "  5. Email password:       .\.venv\Scripts\python.exe -m ems set-smtp-password"
Write-Host "  6. Run as a service:     deploy\windows\install-service.ps1 (as Administrator)"
