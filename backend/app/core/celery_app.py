"""Celery app definition. `beat_schedule` defines two automatic jobs:
run_agent_tick (full strategy analysis + new entries, every 1 minute — full
candle-based analysis doesn't change meaningfully faster than that) and
check_live_exits (a much cheaper, narrower job that only prices already-open
LIVE positions against their per-trade profit target, every 30 seconds, so a
target is caught within seconds rather than waiting a full analysis cycle —
see tasks.py's check_live_exits for why this is a separate task rather than
just running run_agent_tick more often). `celery beat` (a separate process
from `celery worker`) is what actually fires both — see docker-compose.yml /
scripts for both being run."""
from __future__ import annotations

import os

from celery import Celery
from celery.schedules import crontab

celery_app = Celery(
    "neuravex",
    broker=os.getenv("CELERY_BROKER_URL", "redis://localhost:6379/1"),
    backend=os.getenv("REDIS_URL", "redis://localhost:6379/0"),
    # Without this, the worker process never imports tasks.py, so
    # run_agent_tick is never registered — the worker's startup log shows
    # an empty "[tasks]" section, and any task beat sends is silently
    # unrecognized ("Received unregistered task") even though beat itself
    # works fine. `include` forces Celery to import this module in every
    # worker process before it starts consuming.
    include=["backend.app.core.tasks"],
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
)

celery_app.conf.beat_schedule = {
    "neuravex-agent-tick": {
        "task": "backend.app.core.tasks.run_agent_tick",
        "schedule": crontab(minute="*"),
    },
    "neuravex-live-exit-check": {
        "task": "backend.app.core.tasks.check_live_exits",
        # A plain number of seconds (not crontab) — crontab's finest
        # resolution is one minute, and this needs to be faster than that.
        "schedule": 30.0,
    },
    "neuravex-equity-movement-check": {
        "task": "backend.app.core.tasks.check_equity_movement",
        # Every 2 minutes — frequent enough to catch a fast move without
        # emailing/notifying on every tiny fluctuation between checks.
        "schedule": 120.0,
    },
}
