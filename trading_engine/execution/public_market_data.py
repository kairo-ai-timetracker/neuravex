"""
Read-only, keyless market data adapters. This is deliberately the ONLY kind
of adapter the backend/Celery process ever instantiates for live analysis —
it cannot place orders (create_order raises), so the server process
structurally cannot hold or use a trading credential. Approved trades are
queued as PendingExecution for the Android app to execute with its own
locally-held credential instead.

Two providers are implemented:
  - CoinbasePublicMarketDataExchange: the one actually used in production
    (see the `PublicMarketDataExchange` alias at the bottom of this file).
  - BinancePublicMarketDataExchange: kept for local/dev use. Binance's public
    REST API returns HTTP 451 ("Service unavailable from a restricted
    location... Eligibility") for requests coming from cloud/datacenter IP
    ranges (AWS, GCP, Railway, etc.), regardless of the server's actual
    physical location. That only surfaced once the backend moved from the
    user's residential IP (laptop) to Railway — it still works fine for
    local development on a residential connection.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx

from trading_engine.execution.exchange_interface import (
    Candle,
    Exchange,
    ExchangeConnectionError,
    OrderBook,
    OrderBookLevel,
    OrderResult,
    OrderType,
    Position,
    Side,
    Ticker,
)

# Some form of identifying User-Agent is good practice for free/keyless
# public APIs and avoids being lumped in with default-httpx-UA scraper
# traffic by some providers' edge/WAF layers.
_USER_AGENT = "neuravex-trading-bot/1.0 (+market-data-analysis-only)"

# On-chain "wrapped" token symbols (used for Polygon/Uniswap execution via
# PolygonTokenRegistry.kt) don't exist as such on centralized exchanges —
# they track their unwrapped underlying 1:1 by design, so for MARKET DATA
# purposes only we look up the underlying asset instead. This never affects
# execution: the Android app still trades the actual wrapped token address
# on-chain; this mapping is purely so the analysis layer can fetch real
# price history for a symbol the data provider actually lists.
# WMATIC -> POL, not MATIC: the MATIC->POL rebrand completed in September
# 2024 and old MATIC pairs were delisted from most venues.
_WRAPPED_TOKEN_ANALYSIS_PROXY = {
    "WMATIC": "POL",
    "WETH": "ETH",
    "WBTC": "BTC",
}


# Hourly candles per Coinbase product, kept across calls in this process
# ({open_time_epoch: [time, low, high, open, close, volume]}) so 4h candles
# (built from 1h, see get_candles) need one request per call once warm,
# instead of re-downloading ~50 days of history every tick.
_HOURLY_CACHE: dict[str, dict[int, list]] = {}


class CoinbasePublicMarketDataExchange(Exchange):
    """Keyless market data via Coinbase Exchange's public REST API.

    Chosen as the production data source because (a) it needs no API key,
    matching this adapter's read-only-by-design contract, and (b) unlike
    Binance it is reachable from cloud-hosted IPs (verified against Railway).
    """

    name = "coinbase_public"
    supports_withdrawals = False

    # Binance-style interval strings (as already used throughout the agent
    # config / DB) mapped to Coinbase's `granularity` in seconds. Coinbase
    # has no native 4h bucket — "4h" used to be silently mapped to 6h here;
    # it is now built from 1h candles instead (see _four_hour_candles), so
    # the agent gets exactly what it asked for.
    _TIMEFRAME_TO_GRANULARITY_SECONDS = {
        "1m": 60,
        "5m": 300,
        "15m": 900,
        "1h": 3600,
        "6h": 21600,
        "1d": 86400,
    }

    # Coinbase has no native pagination/limit param on this endpoint — it
    # returns up to this many of the most recent candles for the requested
    # granularity when no start/end range is given.
    _MAX_CANDLES_PER_REQUEST = 300

    def __init__(self, base_url: str = "https://api.exchange.coinbase.com", timeout: float = 10.0):
        self.base_url = base_url
        self.timeout = timeout

    @classmethod
    def _to_product_id(cls, symbol: str) -> str:
        base, _, quote = symbol.partition("/")
        base = _WRAPPED_TOKEN_ANALYSIS_PROXY.get(base, base)
        # Coinbase lists almost everything against USD, not USDC/USDT.
        # For ANALYSIS purposes only (not execution/settlement) stablecoins
        # are treated as ~$1, the same proxy principle used above for
        # wrapped tokens. Execution still uses the real on-chain quote asset.
        return f"{base}-USD"

    async def _get(self, path: str, params: dict) -> dict | list:
        headers = {"User-Agent": _USER_AGENT}
        async with httpx.AsyncClient(timeout=self.timeout, headers=headers) as client:
            resp = await client.get(f"{self.base_url}{path}", params=params)
            if resp.status_code != 200:
                raise ExchangeConnectionError(f"Coinbase public API error {resp.status_code}: {resp.text}")
            return resp.json()

    async def get_balance(self, asset: str) -> float:
        raise NotImplementedError("CoinbasePublicMarketDataExchange holds no account — no balance to query")

    async def get_positions(self) -> list[Position]:
        return []

    async def get_ticker(self, symbol: str) -> Ticker:
        product_id = self._to_product_id(symbol)
        data = await self._get(f"/products/{product_id}/ticker", {})
        last = float(data["price"])
        bid = float(data.get("bid", last))
        ask = float(data.get("ask", last))
        return Ticker(symbol=symbol, last=last, bid=bid, ask=ask, timestamp=datetime.now(timezone.utc))

    async def get_orderbook(self, symbol: str, depth: int = 20) -> OrderBook:
        product_id = self._to_product_id(symbol)
        data = await self._get(f"/products/{product_id}/book", {"level": 2})
        bids = [OrderBookLevel(price=float(p), quantity=float(q)) for p, q, *_ in data["bids"][:depth]]
        asks = [OrderBookLevel(price=float(p), quantity=float(q)) for p, q, *_ in data["asks"][:depth]]
        return OrderBook(symbol=symbol, bids=bids, asks=asks, timestamp=datetime.now(timezone.utc))

    async def get_candles(self, symbol: str, timeframe: str, limit: int = 500) -> list[Candle]:
        product_id = self._to_product_id(symbol)
        if timeframe == "4h":
            return await self._four_hour_candles(product_id, limit)
        granularity = self._TIMEFRAME_TO_GRANULARITY_SECONDS.get(timeframe)
        if granularity is None:
            raise ExchangeConnectionError(
                f"Coinbase adapter has no granularity mapping for timeframe '{timeframe}'"
            )
        raw = await self._get(f"/products/{product_id}/candles", {"granularity": granularity})
        # Coinbase returns newest-first, each entry [time, low, high, open, close, volume].
        # Sort ascending (oldest first) to match the ordering the rest of the
        # codebase expects (same ordering Binance's klines already returned),
        # then keep only the most recent `limit` candles (capped by what a
        # single request can return).
        raw_sorted = sorted(raw, key=lambda c: c[0])
        effective_limit = min(limit, self._MAX_CANDLES_PER_REQUEST)
        raw_sorted = raw_sorted[-effective_limit:]
        return [
            Candle(
                open_time=datetime.fromtimestamp(c[0], tz=timezone.utc),
                open=float(c[3]), high=float(c[2]), low=float(c[1]), close=float(c[4]), volume=float(c[5]),
            )
            for c in raw_sorted
        ]

    async def _four_hour_candles(self, product_id: str, limit: int) -> list[Candle]:
        """4h candles aggregated from 1h, in UTC-aligned buckets (00:00,
        04:00, ... 20:00) — the same buckets scripts/replay_backtest.py
        evaluated. The last bucket can still be forming; the agent drops it."""
        hourly = await self._hourly_history(product_id, hours=limit * 4 + 4)
        buckets: dict[int, list[list]] = {}
        for ts in sorted(hourly):
            buckets.setdefault(ts // 14_400 * 14_400, []).append(hourly[ts])
        candles = [
            Candle(
                open_time=datetime.fromtimestamp(start, tz=timezone.utc),
                open=float(rows[0][3]), high=max(float(r[2]) for r in rows), low=min(float(r[1]) for r in rows),
                close=float(rows[-1][4]), volume=sum(float(r[5]) for r in rows),
            )
            for start, rows in sorted(buckets.items())
        ]
        return candles[-limit:]

    async def _hourly_history(self, product_id: str, hours: int) -> dict[int, list]:
        cache = _HOURLY_CACHE.setdefault(product_id, {})
        now = datetime.now(timezone.utc)
        oldest_needed = int((now - timedelta(hours=hours)).timestamp())
        if cache and min(cache) <= oldest_needed:
            # Warm: the newest page (up to 300h) is enough to refresh the
            # forming hour and any hours closed since the last call.
            for row in await self._get(f"/products/{product_id}/candles", {"granularity": 3600}):
                cache[int(row[0])] = row
        else:
            end = now
            while int(end.timestamp()) > oldest_needed:
                start = end - timedelta(hours=self._MAX_CANDLES_PER_REQUEST)
                rows = await self._get(f"/products/{product_id}/candles", {
                    "granularity": 3600,
                    "start": start.strftime("%Y-%m-%dT%H:%M:%SZ"), "end": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
                })
                for row in rows:
                    cache[int(row[0])] = row
                end = start
        for ts in [ts for ts in cache if ts < oldest_needed - 4 * 3600]:
            del cache[ts]
        return dict(cache)

    async def create_order(self, symbol: str, side: Side, order_type: OrderType, quantity: float, price: float | None = None) -> OrderResult:
        raise NotImplementedError(
            "CoinbasePublicMarketDataExchange is read-only by design — it cannot place orders. "
            "This is what guarantees the server process never touches a real trade."
        )

    async def cancel_order(self, order_id: str, symbol: str) -> bool:
        raise NotImplementedError("CoinbasePublicMarketDataExchange is read-only by design")

    async def get_order_status(self, order_id: str, symbol: str) -> OrderResult:
        raise NotImplementedError("CoinbasePublicMarketDataExchange is read-only by design")


class BinancePublicMarketDataExchange(Exchange):
    """Keyless market data via Binance's public REST API.

    Kept for local development on a residential IP. NOT used in production
    on Railway — Binance returns HTTP 451 for cloud/datacenter IP ranges.
    See `CoinbasePublicMarketDataExchange` above for the adapter actually
    wired into the live agent.
    """

    name = "binance_public"
    supports_withdrawals = False

    def __init__(self, base_url: str = "https://api.binance.com", timeout: float = 10.0):
        self.base_url = base_url
        self.timeout = timeout

    @classmethod
    def _to_binance_symbol(cls, symbol: str) -> str:
        # Binance has no EUR pairs for most alts; callers should configure
        # symbols Binance actually lists (e.g. BTC/USDT) or point base_url
        # at an exchange that lists the EUR pair they want. This adapter
        # does not silently substitute a different quote currency.
        base, _, quote = symbol.partition("/")
        base = _WRAPPED_TOKEN_ANALYSIS_PROXY.get(base, base)
        return f"{base}{quote}"

    async def _get(self, path: str, params: dict) -> dict | list:
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.get(f"{self.base_url}{path}", params=params)
            if resp.status_code != 200:
                raise ExchangeConnectionError(f"Binance public API error {resp.status_code}: {resp.text}")
            return resp.json()

    async def get_balance(self, asset: str) -> float:
        raise NotImplementedError("BinancePublicMarketDataExchange holds no account — no balance to query")

    async def get_positions(self) -> list[Position]:
        return []

    async def get_ticker(self, symbol: str) -> Ticker:
        data = await self._get("/api/v3/ticker/bookTicker", {"symbol": self._to_binance_symbol(symbol)})
        return Ticker(
            symbol=symbol, last=float(data["bidPrice"]), bid=float(data["bidPrice"]),
            ask=float(data["askPrice"]), timestamp=datetime.now(timezone.utc),
        )

    async def get_orderbook(self, symbol: str, depth: int = 20) -> OrderBook:
        data = await self._get("/api/v3/depth", {"symbol": self._to_binance_symbol(symbol), "limit": depth})
        bids = [OrderBookLevel(price=float(p), quantity=float(q)) for p, q in data["bids"]]
        asks = [OrderBookLevel(price=float(p), quantity=float(q)) for p, q in data["asks"]]
        return OrderBook(symbol=symbol, bids=bids, asks=asks, timestamp=datetime.now(timezone.utc))

    async def get_candles(self, symbol: str, timeframe: str, limit: int = 500) -> list[Candle]:
        raw = await self._get(
            "/api/v3/klines", {"symbol": self._to_binance_symbol(symbol), "interval": timeframe, "limit": limit}
        )
        return [
            Candle(
                open_time=datetime.fromtimestamp(c[0] / 1000, tz=timezone.utc),
                open=float(c[1]), high=float(c[2]), low=float(c[3]), close=float(c[4]), volume=float(c[5]),
            )
            for c in raw
        ]

    async def create_order(self, symbol: str, side: Side, order_type: OrderType, quantity: float, price: float | None = None) -> OrderResult:
        raise NotImplementedError(
            "BinancePublicMarketDataExchange is read-only by design — it cannot place orders. "
            "This is what guarantees the server process never touches a real trade."
        )

    async def cancel_order(self, order_id: str, symbol: str) -> bool:
        raise NotImplementedError("BinancePublicMarketDataExchange is read-only by design")

    async def get_order_status(self, order_id: str, symbol: str) -> OrderResult:
        raise NotImplementedError("BinancePublicMarketDataExchange is read-only by design")


# The name every caller in the codebase (backend/app/core/tasks.py) actually
# imports and constructs. Pointed at Coinbase in production because Binance
# blocks cloud IPs (see module docstring). Swap this back to
# `BinancePublicMarketDataExchange` for local dev if preferred — both
# implement the same `Exchange` interface.
PublicMarketDataExchange = CoinbasePublicMarketDataExchange
