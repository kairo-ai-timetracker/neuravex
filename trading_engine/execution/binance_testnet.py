"""
Binance TESTNET adapter — placeholder wiring for when the user has
completed backtest -> paper -> validation and wants to test against
Binance's own testnet (fake funds, real API shape) before ever touching
mainnet. Deliberately unimplemented beyond the safety-relevant interface
surface: this file's job in the safety architecture is only to prove that
even a "live-capable-looking" adapter declares supports_withdrawals=False
and gets stopped by live_guard.py exactly like every other non-paper
adapter would.
"""
from __future__ import annotations

from trading_engine.execution.exchange_interface import (
    Exchange,
    OrderResult,
    OrderType,
    Position,
    Side,
    Ticker,
)


class BinanceTestnetExchange(Exchange):
    name = "binance_testnet"
    # Testnet API keys are scoped to the testnet itself and cannot access
    # real funds or trigger real withdrawals — this is what makes it safe
    # to eventually flip trading.mode to "live" while pointed at testnet
    # for a validation run, per live_guard.py's interlock.
    supports_withdrawals = False

    def __init__(self, api_key: str, api_secret: str, base_url: str = "https://testnet.binance.vision"):
        self.api_key = api_key
        self.api_secret = api_secret
        self.base_url = base_url

    async def get_balance(self, asset: str) -> float:
        raise NotImplementedError("Wire up real testnet REST calls before using this for a validation run")

    async def get_positions(self) -> list[Position]:
        raise NotImplementedError

    async def get_ticker(self, symbol: str) -> Ticker:
        raise NotImplementedError

    async def get_orderbook(self, symbol: str, depth: int = 20):
        raise NotImplementedError

    async def get_candles(self, symbol: str, timeframe: str, limit: int = 500):
        raise NotImplementedError

    async def create_order(self, symbol: str, side: Side, order_type: OrderType, quantity: float, price: float | None = None) -> OrderResult:
        raise NotImplementedError

    async def cancel_order(self, order_id: str, symbol: str) -> bool:
        raise NotImplementedError

    async def get_order_status(self, order_id: str, symbol: str) -> OrderResult:
        raise NotImplementedError
