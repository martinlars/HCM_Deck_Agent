# ============================================================
# HCM Deck Course Agent — Windows installer (PowerShell)
# ============================================================
# Idempotent: safe to run multiple times.
# What it does:
#   1. Verifies Python 3.11+ is installed
#   2. Installs `uv` (fast Python package manager)
#   3. Creates .venv/ in this folder
#   4. Installs dependencies from requirements.txt
#   5. Downloads Chromium via browser-use (~130 MB)
#   6. Copies .env.example -> .env if missing
# ============================================================

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ProjectRoot

function Write-Section($msg) {
    Write-Host ""
    Write-Host "============================================================" -ForegroundColor Cyan
    Write-Host $msg -ForegroundColor Cyan
    Write-Host "============================================================" -ForegroundColor Cyan
}

# --- 1. Python -----------------------------------------------------------
Write-Section "[1/6] Checking Python 3.11+"
try {
    $pyVersion = & python --version 2>&1
    Write-Host "Found: $pyVersion"
    $matched = $pyVersion -match "Python (\d+)\.(\d+)"
    if (-not $matched) { throw "could not parse python version" }
    $major = [int]$Matches[1]; $minor = [int]$Matches[2]
    if ($major -lt 3 -or ($major -eq 3 -and $minor -lt 11)) {
        throw "Python 3.11+ required, got $pyVersion"
    }
}
catch {
    Write-Host "ERROR: $_" -ForegroundColor Red
    Write-Host "Install Python 3.11+ from https://www.python.org/downloads/ (tick 'Add to PATH')"
    exit 1
}

# --- 2. uv ----------------------------------------------------------------
Write-Section "[2/6] Installing uv (fast package manager)"
$uvExists = $false
try { & uv --version | Out-Null; $uvExists = $true } catch {}
if (-not $uvExists) {
    & python -m pip install --user --upgrade uv
} else {
    Write-Host "uv already installed."
}

# --- 3. venv --------------------------------------------------------------
Write-Section "[3/6] Creating virtualenv .venv"
if (-not (Test-Path ".venv")) {
    & python -m uv venv .venv
} else {
    Write-Host ".venv already exists, skipping create."
}

# --- 4. deps --------------------------------------------------------------
Write-Section "[4/6] Installing Python dependencies"
& python -m uv pip install --python ".venv\Scripts\python.exe" -r requirements.txt

# --- 5. Chromium ----------------------------------------------------------
Write-Section "[5/6] Downloading Chromium (~130 MB) via browser-use"
& ".venv\Scripts\python.exe" -m uvx browser-use install
if ($LASTEXITCODE -ne 0) {
    Write-Host "Note: 'uvx browser-use install' returned non-zero. If you already have a Chrome profile to reuse, you can skip this step." -ForegroundColor Yellow
}

# --- 6. .env --------------------------------------------------------------
Write-Section "[6/6] Setting up .env"
if (-not (Test-Path ".env")) {
    Copy-Item ".env.example" ".env"
    Write-Host ".env created from template. EDIT IT NOW and fill in:"
    Write-Host "  - HCM_DASHBOARD_URL (your HCM Deck tenant home page)"
    Write-Host "  - AZURE_OPENAI_* keys (or alternative provider — see README)"
    Write-Host "  - CHROME_EXE / CHROME_USER_DATA (only if Chrome is in a non-default location)"
} else {
    Write-Host ".env already exists, leaving as-is."
}

Write-Section "DONE"
Write-Host "Next steps:"
Write-Host "  1) notepad .env             # fill in credentials"
Write-Host "  2) .\.venv\Scripts\activate"
Write-Host "  3) python agents\course_agent.py --smoke-test    # validate wiring"
Write-Host "  4) python agents\course_agent.py --debug         # full run"
