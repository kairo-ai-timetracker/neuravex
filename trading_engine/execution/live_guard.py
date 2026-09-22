"""
Hard interlock that makes it structurally difficult to place a live order
by accident. To route an order to a real (non-paper) exchange, ALL of the
following must be true at call time:
  1. settings.trading.mode == "live"
  2. settings.allow_live is True
  3. The exchange adapter declares supports_withdrawals = False
  4. An explicit confirm_live=True is passed by the caller
"""
from __future__ import annotations

from trading_engine.execution.exchange_interface import Exchange


class LiveTradingBlocked(Exception):
    pass


def assert_live_order_allowed(
    *,
    exchange: Exchange,
    trading_mode: str,
    allow_live_flag: bool,
    confirm_live: bool,
) -> None:
    if exchange.name == "paper":
        return

    if trading_mode != "live":
        raise LiveTradingBlocked(
            f"Refusing order on non-paper exchange '{exchange.name}': "
            f"trading.mode is '{trading_mode}', not 'live'."
        )
    if not allow_live_flag:
        raise LiveTradingBlocked(
            "Refusing live order: NEURAVEX_ALLOW_LIVE is not set to true."
        )
    if getattr(exchange, "supports_withdrawals", True):
        raise LiveTradingBlocked(
            f"Refusing to use exchange adapter '{exchange.name}': it does not "
            "explicitly declare supports_withdrawals=False. NEURAVEX will "
            "never route orders through an adapter capable of withdrawals."
        )
    if not confirm_live:
        raise LiveTradingBlocked(
            "Refusing live order: caller did not pass confirm_live=True for "
            "this specific execution call."
        )
