# NEURAVEX — AI Trading. Beyond Human.

Autonomous AI crypto trading platform. Risk-first design (capital
protection > returns). Backend (FastAPI + Celery) never holds a trading
credential — the Android app holds it locally, encrypted, and executes
approved trades itself.

## Quickstart (local, no Docker)

```powershell
scripts\start.bat
```

This creates a venv, installs dependencies, and starts the backend on
`http://0.0.0.0:8000`. You'll also need Postgres and (for the automatic
5-minute analysis loop) Redis + two Celery processes — see
`docs/DEPLOYMENT.md` for the full walkthrough, including deploying to a
real server so the Android app works without your laptop running.

## Running tests

```powershell
python -m pytest tests/ -v
```

Expect 62 passing tests.

## Android app

See `android/README.md`.
