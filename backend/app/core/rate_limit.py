"""Simple rate limiting middleware. Uses Redis if available, otherwise
falls back to an in-process counter (fine for single-worker local dev, not
safe for a multi-worker production deployment)."""
from __future__ import annotations

import logging
import os
import time
from collections import defaultdict

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

logger = logging.getLogger("neuravex.rate_limit")

_WINDOW_SECONDS = 60
_MAX_REQUESTS_PER_WINDOW = 120

_in_process_counters: dict[str, list[float]] = defaultdict(list)
_redis_client = None
_redis_checked = False


def _get_redis_client():
    global _redis_client, _redis_checked
    if _redis_checked:
        return _redis_client
    _redis_checked = True
    redis_url = os.getenv("REDIS_URL")
    if not redis_url:
        logger.warning("REDIS_URL not set — using in-process rate limiting counters (NOT safe for multi-worker deployments).")
        return None
    try:
        import redis
        client = redis.from_url(redis_url, socket_connect_timeout=1.0)
        client.ping()
        return client
    except Exception:
        logger.warning("Redis unavailable for rate limiting — falling back to in-process counters (NOT safe for multi-worker deployments).")
        return None


class RateLimitMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        client_ip = request.client.host if request.client else "unknown"
        now = time.time()

        redis_client = _get_redis_client()
        if redis_client is not None:
            try:
                key = f"ratelimit:{client_ip}"
                count = redis_client.incr(key)
                if count == 1:
                    redis_client.expire(key, _WINDOW_SECONDS)
                if count > _MAX_REQUESTS_PER_WINDOW:
                    return JSONResponse(status_code=429, content={"detail": "Rate limit exceeded"})
            except Exception:
                pass  # fail open rather than blocking all traffic on a Redis hiccup
        else:
            history = _in_process_counters[client_ip]
            history[:] = [t for t in history if now - t < _WINDOW_SECONDS]
            history.append(now)
            if len(history) > _MAX_REQUESTS_PER_WINDOW:
                return JSONResponse(status_code=429, content={"detail": "Rate limit exceeded"})

        return await call_next(request)
