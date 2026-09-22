# NEURAVEX quick-start script.
#
# Usage: double-click start.bat instead of this file directly — it
# handles the "not digitally signed" unblock step for you automatically.
#
# What it does:
#   1. Moves to the project root (wherever this script lives).
#   2. Creates .venv if it doesn't exist yet, and installs dependencies.
#   3. Activates .venv for this session.
#   4. Starts the backend (uvicorn) on port 8000.

$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $projectRoot

Write-Host "NEURAVEX -- project root: $projectRoot" -ForegroundColor Cyan

if (-not (Test-Path ".venv")) {
    Write-Host "No .venv found -- creating one now (first run only)..." -ForegroundColor Yellow
    python -m venv .venv

    Write-Host "Activating .venv and installing dependencies (this takes a minute)..." -ForegroundColor Yellow
    & ".venv\Scripts\Activate.ps1"

    python -m pip install --upgrade pip

    $packages = @(
        "fastapi", "uvicorn", "sqlalchemy", "alembic", "psycopg2-binary",
        "redis", "celery", "pydantic", "pydantic-settings", "python-jose",
        "bcrypt", "python-multipart", "websockets", "numpy", "pyyaml",
        "httpx", "pytest", "pytest-asyncio", "python-dotenv", "email-validator"
    )
    python -m pip install $packages
} else {
    Write-Host "Activating existing .venv..." -ForegroundColor Yellow
    & ".venv\Scripts\Activate.ps1"
}

Write-Host ""
Write-Host "Starting NEURAVEX backend on http://0.0.0.0:8000 ..." -ForegroundColor Green
Write-Host "(Press Ctrl+C to stop)" -ForegroundColor DarkGray
Write-Host ""

uvicorn backend.app.main:app --host 0.0.0.0 --port 8000
