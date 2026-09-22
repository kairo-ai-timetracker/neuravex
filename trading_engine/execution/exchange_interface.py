"""
Exchange abstraction layer. Every exchange (paper, Binance, Polygon
public-data, ...) implements this interface. Strategy/risk/decision code
never talks to an exchange SDK directly — only through this contract.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class Side(str, Enum):
    buy = "buy"
    sell = "sell"


class OrderType(str, Enum):
    market = "market"
    limit = "limit"


class OrderStatus(str, Enum):
    open = "open"
    filled = "filled"
    partially_filled = "partially_filled"
    cancelled = "cancelled"
    rejected = "rejected"


@dataclass
class Candle:
    open_time: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass
class Ticker:
    symbol: str
    last: float
    bid: float
    ask: float
    timestamp: datetime


@dataclass
class OrderBookLevel:
    price: float
    quantity: float


@dataclass
class OrderBook:
    symbol: str
    bids: list[OrderBookLevel]
    asks: list[OrderBookLevel]
    timestamp: datetime


@dataclass
class OrderResult:
    order_id: str
    symbol: str
    side: Side
    order_type: OrderType
    requested_quantity: float
    status: OrderStatus
    filled_quantity: float = 0.0
    average_fill_price: float | None = None
    fee: float = 0.0
    fee_asset: str = ""
    raw: dict = field(default_factory=dict)


@dataclass
class Position:
    symbol: str
    side: Side
    quantity: float
    entry_price: float


class ExchangeError(Exception):
    pass


class InsufficientBalanceError(ExchangeError):
    pass


class StalePriceError(ExchangeError):
    pass


class ExchangeConnectionError(ExchangeError):
    pass


class Exchange(ABC):
    name: str = "abstract"
    supports_withdrawals: bool = False

    @abstractmethod
    async def get_balance(self, asset: str) -> float: ...

    @abstractmethod
    async def get_positions(self) -> list[Position]: ...

    @abstractmethod
    async def get_ticker(self, symbol: str) -> Ticker: ...

    @abstractmethod
    async def get_orderbook(self, symbol: str, depth: int = 20) -> OrderBook: ...

    @abstractmethod
    async def get_candles(self, symbol: str, timeframe: str, limit: int = 500) -> list[Candle]: ...

    @abstractmethod
    async def create_order(
        self,
        symbol: str,
        side: Side,
        order_type: OrderType,
        quantity: float,
        price: float | None = None,
    ) -> OrderResult: ...

    @abstractmethod
    async def cancel_order(self, order_id: str, symbol: str) -> bool: ...

    @abstractmethod
    async def get_order_status(self, order_id: str, symbol: str) -> OrderResult: ...
