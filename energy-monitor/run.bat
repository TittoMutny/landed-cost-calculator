@echo off
REM Start Energy Monitor in this window (Ctrl+C to stop).
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
  echo Run deploy\windows\install.ps1 first.
  pause
  exit /b 1
)
.venv\Scripts\python.exe -m ems run %*
