"""
Paper trading adapter. Simulates order execution without touching a real
exchange account. Default and only active adapter until backtest -> paper
-> validation has been completed by the user.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from trading_engine.execution.exchange_interface import (
    Candle,
    Exchange,
    ExchangeConnectionError,
    InsufficientBalanceError,
    OrderBook,
    OrderBookLevel,
    OrderResult,
    OrderStatus,
    OrderType,
    Position,
    Side,
    StalePriceError,
    Ticker,
)


class PaperExchange(Exchange):
    name = "paper"
    supports_withdrawals = False

    def __init__(
        self,
        starting_balances: dict[str, float],
        taker_fee: float = 0.001,
        maker_fee: float = 0.0008,
        slippage_bps: float = 5.0,
        max_price_age_seconds: int = 30,
        starting_cost_basis: dict[str, float] | None = None,
    ):
        self._balances: dict[str, float] = dict(starting_balances)
        self._locked: dict[str, float] = {k: 0.0 for k in starting_balances}
        self._positions: dict[str, Position] = {}
        self._orders: dict[str, OrderResult] = {}
        self._last_price: dict[str, tuple[float, datetime]] = {}
        self._candles: dict[tuple[str, str], list[Candle]] = {}
        # Weighted-average entry price per asset (e.g. {"WBTC": 61234.5}),
        # persisted across ticks by the caller — a fresh PaperExchange is
        # constructed every tick, so without this, per-trade profit
        # (spec: "als de winst op die trade €50 is") would have no cost
        # basis to measure from after the first tick.
        self._cost_basis: dict[str, float] = dict(starting_cost_basis or {})
        self.taker_fee = taker_fee
        self.maker_fee = maker_fee
        self.slippage_bps = slippage_bps
        self.max_price_age_seconds = max_price_age_seconds

    def get_cost_basis(self) -> dict[str, float]:
        """Current weighted-average entry price per asset held. Read by
        the caller after a tick to persist alongside get_all_balances()."""
        return dict(self._cost_basis)

    def update_price(self, symbol: str, price: float, at: datetime | None = None) -> None:
        self._last_price[symbol] = (price, at or datetime.now(timezone.utc))

    def load_candles(self, symbol: str, timeframe: str, candles: list[Candle]) -> None:
        self._candles[(symbol, timeframe)] = candles

    async def get_balance(self, asset: str) -> float:
        return self._balances.get(asset, 0.0)

    def get_all_balances(self) -> dict[str, float]:
        """
        Snapshot of every asset balance this simulated exchange currently
        holds, keyed by symbol. Used by the caller (tasks.py) to persist
        state across ticks, since a fresh PaperExchange instance is
        constructed on every tick rather than kept alive as a long-running
        object — see AccountSettings.paper_balances.
        """
        return dict(self._balances)

    async def get_positions(self) -> list[Position]:
        return list(self._positions.values())

    async def get_ticker(self, symbol: str) -> Ticker:
        price, ts = self._require_fresh_price(symbol)
        spread = price * 0.0005
        return Ticker(symbol=symbol, last=price, bid=price - spread, ask=price + spread, timestamp=ts)

    async def get_orderbook(self, symbol: str, depth: int = 20) -> OrderBook:
        price, ts = self._require_fresh_price(symbol)
        bids = [OrderBookLevel(price=price * (1 - 0.0002 * i), quantity=1.0) for i in range(1, depth + 1)]
        asks = [OrderBookLevel(price=price * (1 + 0.0002 * i), quantity=1.0) for i in range(1, depth + 1)]
        return OrderBook(symbol=symbol, bids=bids, asks=asks, timestamp=ts)

    async def get_candles(self, symbol: str, timeframe: str, limit: int = 500) -> list[Candle]:
        candles = self._candles.get((symbol, timeframe), [])
        return candles[-limit:]

    async def create_order(
        self,
        symbol: str,
        side: Side,
        order_type: OrderType,
        quantity: float,
        price: float | None = None,
    ) -> OrderResult:
        if quantity <= 0:
            raise ValueError("quantity must be positive")

        last_price, _ = self._require_fresh_price(symbol)
        base, quote = symbol.split("/")

        fill_price = price if order_type == OrderType.limit and price else last_price
        slip = fill_price * (self.slippage_bps / 10_000.0)
        fill_price = fill_price + slip if side == Side.buy else fill_price - slip

        notional = fill_price * quantity
        fee = notional * (self.maker_fee if order_type == OrderType.limit else self.taker_fee)

        if side == Side.buy:
            cost = notional + fee
            available = self._balances.get(quote, 0.0)
            if cost > available:
                raise InsufficientBalanceError(
                    f"Need {cost:.2f} {quote}, have {available:.2f} {quote}"
                )
            self._balances[quote] = available - cost
            prior_qty = self._balances.get(base, 0.0)
            self._balances[base] = prior_qty + quantity
            # Weighted-average cost basis: a second buy of the same asset
            # blends into the existing average rather than overwriting
            # it, the same way a real portfolio's average entry price
            # works.
            prior_cost = self._cost_basis.get(base, fill_price) * prior_qty
            self._cost_basis[base] = (prior_cost + cost) / (prior_qty + quantity)
        else:
            available_base = self._balances.get(base, 0.0)
            if quantity > available_base:
                raise InsufficientBalanceError(
                    f"Need {quantity} {base}, have {available_base} {base}"
                )
            proceeds = notional - fee
            self._balances[base] = available_base - quantity
            self._balances[quote] = self._balances.get(quote, 0.0) + proceeds
            # Position fully closed — nothing left to measure a cost
            # basis against. A partial sell keeps the same average price
            # for whatever quantity remains.
            if self._balances[base] <= 1e-12:
                self._cost_basis.pop(base, None)

        order_id = str(uuid.uuid4())
        result = OrderResult(
            order_id=order_id,
            symbol=symbol,
            side=side,
            order_type=order_type,
            requested_quantity=quantity,
            status=OrderStatus.filled,
            filled_quantity=quantity,
            average_fill_price=fill_price,
            fee=fee,
            fee_asset=quote,
            raw={"simulated": True},
        )
        self._orders[order_id] = result
        return result

    async def cancel_order(self, order_id: str, symbol: str) -> bool:
        order = self._orders.get(order_id)
        if order and order.status == OrderStatus.open:
            order.status = OrderStatus.cancelled
            return True
        return False

    async def get_order_status(self, order_id: str, symbol: str) -> OrderResult:
        if order_id not in self._orders:
            raise ExchangeConnectionError(f"Unknown order_id {order_id}")
        return self._orders[order_id]

    def _require_fresh_price(self, symbol: str) -> tuple[float, datetime]:
        if symbol not in self._last_price:
            raise ExchangeConnectionError(f"No price loaded for {symbol}")
        price, ts = self._last_price[symbol]
        age = (datetime.now(timezone.utc) - ts).total_seconds() if ts.tzinfo else None
        if age is not None and age > self.max_price_age_seconds:
            raise StalePriceError(f"Price for {symbol} is {age:.0f}s old")
        return price, ts
