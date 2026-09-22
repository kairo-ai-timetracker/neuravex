"""
Read-only, keyless market data adapter using Binance's public REST API.
This is deliberately the ONLY adapter the backend/Celery process ever
instantiates for live analysis — it cannot place orders (create_order
raises), so the server process structurally cannot hold or use a trading
credential. Approved trades are queued as PendingExecution for the Android
app to execute with its own locally-held credential instead.
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


class PublicMarketDataExchange(Exchange):
    name = "binance_public"
    supports_withdrawals = False

    # On-chain "wrapped" token symbols (used for Polygon/Uniswap execution
    # via PolygonTokenRegistry.kt) don't exist as such on Binance — they
    # track their unwrapped underlying 1:1 by design, so for MARKET DATA
    # purposes only we look up the underlying asset instead. This never
    # affects execution: the Android app still trades the actual wrapped
    # token address on-chain; this mapping is purely so the analysis layer
    # can fetch real price history for a symbol Binance actually lists.
    # WMATIC -> POL, not MATIC: Binance completed the MATIC->POL rebrand in
    # September 2024 and delisted the old MATIC pairs entirely.
    _WRAPPED_TOKEN_ANALYSIS_PROXY = {
        "WMATIC": "POL",
        "WETH": "ETH",
        "WBTC": "BTC",
    }

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
        base = cls._WRAPPED_TOKEN_ANALYSIS_PROXY.get(base, base)
        return f"{base}{quote}"

    async def _get(self, path: str, params: dict) -> dict | list:
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.get(f"{self.base_url}{path}", params=params)
            if resp.status_code != 200:
                raise ExchangeConnectionError(f"Binance public API error {resp.status_code}: {resp.text}")
            return resp.json()

    async def get_balance(self, asset: str) -> float:
        raise NotImplementedError("PublicMarketDataExchange holds no account — no balance to query")

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
            "PublicMarketDataExchange is read-only by design — it cannot place orders. "
            "This is what guarantees the server process never touches a real trade."
        )

    async def cancel_order(self, order_id: str, symbol: str) -> bool:
        raise NotImplementedError("PublicMarketDataExchange is read-only by design")

    async def get_order_status(self, order_id: str, symbol: str) -> OrderResult:
        raise NotImplementedError("PublicMarketDataExchange is read-only by design")
