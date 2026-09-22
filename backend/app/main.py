from __future__ import annotations

import logging
import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

# Imported first, deliberately: this module's top-level code calls
# load_dotenv() as a side effect, so .env must be loaded into the process
# environment before any other backend module runs its own top-level
# os.getenv() calls (e.g. backend/app/core/auth.py reading
# NEURAVEX_SECRET_KEY at import time). Python executes each module's
# top-level code once, immediately, the first time it's imported — so
# import order here is load-bearing, not cosmetic.
from backend.app.core.config import settings

from backend.app.api.auth import router as auth_router
from backend.app.api.backtest import router as backtest_router
from backend.app.api.dashboard import router as dashboard_router
from backend.app.api.emergency import router as emergency_router
from backend.app.api.execution import router as execution_router
from backend.app.api.notifications import router as notifications_router
from backend.app.api.settings import router as settings_router
from backend.app.api.websocket import router as websocket_router
from backend.app.core.rate_limit import RateLimitMiddleware

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("neuravex.main")

app = FastAPI(
    title="NEURAVEX",
    description="AI TRADING. BEYOND HUMAN.",
    version="0.1.0",
)

# CORS: allows both local dev (http://localhost:3000, for `npm run dev`)
# and the real production frontend origin, read from CORS_ALLOWED_ORIGIN
# in .env (set this to your actual https://yourdomain.com once deployed —
# the Android app itself is unaffected by CORS, this only matters for the
# browser-based web dashboard).
_allowed_origins = ["http://localhost:3000"]
_production_origin = os.getenv("CORS_ALLOWED_ORIGIN")
if _production_origin:
    _allowed_origins.append(_production_origin)

app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(RateLimitMiddleware)

app.include_router(auth_router)
app.include_router(dashboard_router)
app.include_router(backtest_router)
app.include_router(execution_router)
app.include_router(notifications_router)
app.include_router(settings_router)
app.include_router(emergency_router)
app.include_router(websocket_router)


@app.on_event("startup")
def on_startup() -> None:
    logger.info("NEURAVEX starting up. trading.mode=%s allow_live=%s", settings.trading.mode, settings.allow_live)
    if settings.trading.mode == "live" and settings.allow_live:
        logger.warning("LIVE TRADING IS ENABLED. Real orders can be placed if a credentialed adapter is wired in.")
    else:
        logger.info("Live trading is disabled (mode=%s). This is the safe, expected default.", settings.trading.mode)


@app.get("/health")
def health() -> dict:
    return {
        "status": "ok",
        "trading_mode": settings.trading.mode,
        "live_trading_possible": settings.trading.mode == "live" and settings.allow_live,
    }
