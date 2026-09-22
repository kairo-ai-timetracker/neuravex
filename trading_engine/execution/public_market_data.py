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

from datetime import datetime, timezone

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
    # has no native 4h bucket; "4h" maps to the nearest available (6h).
    _TIMEFRAME_TO_GRANULARITY_SECONDS = {
        "1m": 60,
        "5m": 300,
        "15m": 900,
        "1h": 3600,
        "4h": 21600,  # nearest available Coinbase bucket is 6h
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
