"""
Dynamic, ATR-based position sizing.

Core idea (fixed-fractional risk model): risk a fixed, small fraction of
account equity per trade, sized off the distance to the stop-loss — NOT off
a fixed dollar amount or a fixed number of coins.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class PositionSizeResult:
    quantity: float
    notional: float
    risk_amount: float
    capped_by: str | None


def calculate_position_size(
    *,
    account_equity: float,
    entry_price: float,
    stop_loss_price: float,
    confidence: float,
    max_trade_risk: float,
    max_position_size_fraction: float,
    current_portfolio_exposure_fraction: float,
    max_portfolio_exposure_fraction: float,
    correlation_penalty: float = 0.0,
) -> PositionSizeResult:
    if entry_price <= 0 or stop_loss_price <= 0:
        raise ValueError("entry_price and stop_loss_price must be positive")
    stop_distance = abs(entry_price - stop_loss_price)
    if stop_distance <= 0:
        raise ValueError("stop_loss_price must differ from entry_price")

    confidence = max(0.0, min(1.0, confidence))
    confidence_scalar = 0.5 + 0.5 * confidence
    correlation_scalar = 1.0 - max(0.0, min(1.0, correlation_penalty)) * 0.6

    risk_amount = account_equity * max_trade_risk * confidence_scalar * correlation_scalar
    quantity = risk_amount / stop_distance
    notional = quantity * entry_price
    capped_by = None

    max_notional_by_position_cap = max_position_size_fraction * account_equity
    if notional > max_notional_by_position_cap:
        notional = max_notional_by_position_cap
        quantity = notional / entry_price
        capped_by = "max_position_size"

    remaining_exposure_room = max(
        0.0, (max_portfolio_exposure_fraction - current_portfolio_exposure_fraction) * account_equity
    )
    if notional > remaining_exposure_room:
        notional = remaining_exposure_room
        quantity = notional / entry_price if entry_price else 0.0
        capped_by = "max_portfolio_exposure"

    if quantity <= 0:
        return PositionSizeResult(quantity=0.0, notional=0.0, risk_amount=0.0, capped_by=capped_by or "no_room")

    actual_risk_amount = quantity * stop_distance
    return PositionSizeResult(
        quantity=quantity, notional=notional, risk_amount=actual_risk_amount, capped_by=capped_by
    )
