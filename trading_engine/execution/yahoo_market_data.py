"""
Keyless market data for the metals and stocks portfolios, from Yahoo Finance's
public chart endpoint. Read-only and used for simulation only: it is an unofficial
endpoint with no guarantees, so real money would need a proper data provider.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

import httpx

from trading_engine.execution.exchange_interface import Candle
from trading_engine.execution.market_universe import yahoo_ticker

logger = logging.getLogger(__name__)

_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
_HEADERS = {"User-Agent": "Mozilla/5.0"}
_CACHE_SECONDS = 60
_MARKET_CLOCK_TICKER = "SPY"

# ticker -> (fetched_at, chart result). The tick runs every minute over many
# symbols; bars don't change faster than that.
_CACHE: dict[str, tuple[float, dict]] = {}


def _fetch_chart(ticker: str) -> dict:
    cached = _CACHE.get(ticker)
    if cached and time.monotonic() - cached[0] < _CACHE_SECONDS:
        return cached[1]
    response = httpx.get(
        _URL.format(ticker=ticker), params={"interval": "1h", "range": "1y"}, headers=_HEADERS, timeout=15,
    )
    response.raise_for_status()
    result = response.json()["chart"]["result"][0]
    _CACHE[ticker] = (time.monotonic(), result)
    return result


def candles_from_chart(result: dict, bucket_seconds: int) -> list[Candle]:
    """Hourly bars grouped into UTC-aligned candles, like the crypto feed."""
    quote = result["indicators"]["quote"][0]
    buckets: dict[int, Candle] = {}
    rows = zip(result.get("timestamp") or [], quote["open"], quote["high"], quote["low"], quote["close"], quote["volume"])
    for ts, open_, high, low, close, volume in rows:
        if None in (open_, high, low, close):
            continue
        start = int(ts) // bucket_seconds * bucket_seconds
        candle = buckets.get(start)
        if candle is None:
            buckets[start] = Candle(
                open_time=datetime.fromtimestamp(start, tz=timezone.utc),
                open=open_, high=high, low=low, close=close, volume=volume or 0,
            )
        else:
            candle.high = max(candle.high, high)
            candle.low = min(candle.low, low)
            candle.close = close
            candle.volume += volume or 0
    return [buckets[start] for start in sorted(buckets)]


def session_is_open(result: dict, now: float) -> bool:
    regular = result["meta"]["currentTradingPeriod"]["regular"]
    return regular["start"] <= now < regular["end"]


class YahooMarketData:
    _TIMEFRAME_SECONDS = {"1h": 3600, "4h": 4 * 3600}

    async def get_candles(self, symbol: str, timeframe: str, limit: int = 300) -> list[Candle]:
        chart = _fetch_chart(yahoo_ticker(symbol))
        return candles_from_chart(chart, self._TIMEFRAME_SECONDS[timeframe])[-limit:]


def us_market_is_open() -> bool:
    """False during closed hours and when the clock can't be read: trading on
    stale prices would give simulated fills that could never have happened."""
    try:
        return session_is_open(_fetch_chart(_MARKET_CLOCK_TICKER), time.time())
    except Exception as e:  # noqa: BLE001
        logger.warning("Could not read the US market clock: %s", e)
        return False
