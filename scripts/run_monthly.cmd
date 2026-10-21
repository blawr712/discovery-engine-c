@echo off
rem Run the monthly research cycle and keep its output.
rem
rem Scheduled tasks discard console output, so everything goes to a
rem timestamped log under data\runs\monthly\logs. A batch file rather than
rem PowerShell, so it runs without changing the execution policy. Extra
rem arguments are passed through, which is how the wrapper is tested:
rem   scripts\run_monthly.cmd --help
setlocal
cd /d "%~dp0.."

set "PYTHONUNBUFFERED=1"
set "PYTHONIOENCODING=utf-8"
set "LOG_DIR=data\runs\monthly\logs"
if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"

for /f %%i in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd-HHmmss"') do set "STAMP=%%i"
set "LOG=%LOG_DIR%\monthly-%STAMP%.log"

echo Monthly cycle started %DATE% %TIME% > "%LOG%"
".venv\Scripts\python.exe" main.py --monthly %* >> "%LOG%" 2>&1
set "CODE=%ERRORLEVEL%"
echo Exit code %CODE% at %DATE% %TIME% >> "%LOG%"
exit /b %CODE%
