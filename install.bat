@echo off
REM ============================================================
REM HCM Deck Course Agent - Windows installer (CMD fallback)
REM Use install.ps1 instead if you can (better diagnostics).
REM ============================================================
setlocal enabledelayedexpansion
cd /d "%~dp0"

echo.
echo === [1/6] Checking Python 3.11+ ===
where python >nul 2>&1
if errorlevel 1 (
    echo ERROR: python not on PATH. Install Python 3.11+ from https://www.python.org/downloads/ and tick "Add to PATH".
    exit /b 1
)
python --version

echo.
echo === [2/6] Installing uv (fast package manager) ===
python -m pip install --user --upgrade uv
if errorlevel 1 exit /b 1

echo.
echo === [3/6] Creating virtualenv .venv ===
if exist .venv (
    echo .venv already exists, skipping.
) else (
    python -m uv venv .venv
)

echo.
echo === [4/6] Installing Python dependencies ===
python -m uv pip install --python ".venv\Scripts\python.exe" -r requirements.txt
if errorlevel 1 exit /b 1

echo.
echo === [5/6] Downloading Chromium (~130 MB) ===
".venv\Scripts\python.exe" -m uvx browser-use install

echo.
echo === [6/6] Setting up .env ===
if not exist .env (
    copy .env.example .env >nul
    echo .env created from template. EDIT IT NOW and fill in HCM_DASHBOARD_URL + LLM keys.
) else (
    echo .env already exists.
)

echo.
echo ============================================================
echo DONE. Next:
echo   1) notepad .env
echo   2) .venv\Scripts\activate
echo   3) python agents\course_agent.py --smoke-test
echo   4) python agents\course_agent.py --debug
echo ============================================================
endlocal
