"""
Per-position exit rules for an already-open (spot, long-only) position —
the stop-loss and the user's per-trade take-profit. Pure functions, shared
by the simulation sweep and the live sweep (backend/app/core/tasks.py) and
by scripts/replay_backtest.py, so all three apply exactly the same rule.

Two problems this replaces (found in the 2026-09-26..10-08 simulation run):

1. No stop-loss was ever enforced. agent.py computes an ATR-based stop for
   every entry (build_risk_plan) but only used it for position SIZING —
   nothing ever sold when price reached it, so a losing position stayed
   open until an opposite strategy signal or the account-wide circuit
   breaker happened to close it (e.g. 1INCH -9.5%, WMATIC -5.8%).

2. The take-profit compared the GROSS unrealized profit against the
   target. With a 5 USDC target on a ~2,600 USDC position, the sell's own
   fee + slippage (~4 USDC) left ~+1 USDC net — while losses ran to
   -50..-250 USDC. The target is now checked against profit NET of the
   estimated cost of the sell itself.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from trading_engine.risk.risk_plan import build_risk_plan
from trading_engine.signals.indicators import atr

# Default cost of SELLING, as a fraction of notional: taker fee (0.1%) +
# slippage (5 bps) — matches PaperExchange's defaults. A round trip
# (buy + sell) therefore costs ~0.3%.
DEFAULT_SELL_COST_RATE = 0.0015


@dataclass
class ExitDecision:
    reason: str  # "stop_loss" | "take_profit"
    net_profit: float  # estimated profit after the sell's own costs, in quote currency
    detail: str


def atr_stop_price(cost_basis: float, highs: np.ndarray, lows: np.ndarray, closes: np.ndarray) -> float | None:
    """
    The same ATR stop agent.py's build_risk_plan sets at entry (1.5 x ATR
    below the entry), recomputed from the position's cost basis and the
    latest CLOSED candles. Recomputing (rather than storing the entry-time
    stop) is what the backtesting engine already does, and avoids a schema
    change for simulated positions, which only persist a cost basis.
    Returns None when there isn't enough history for an ATR.
    """
    if len(closes) < 15 or cost_basis <= 0:
        return None
    atr_value = float(atr(highs, lows, closes)[-1])
    if not np.isfinite(atr_value) or atr_value <= 0:
        return None
    return build_risk_plan(entry=cost_basis, direction="LONG", atr_value=atr_value).stop_loss


def net_profit_if_sold(quantity: float, cost_basis: float, price: float, sell_cost_rate: float) -> float:
    """Profit realized by selling `quantity` at `price` now, after the sell's
    fee + slippage. cost_basis already includes the buy-side fee."""
    return quantity * price * (1 - sell_cost_rate) - quantity * cost_basis


def evaluate_exit(
    *,
    quantity: float,
    cost_basis: float,
    price: float,
    stop_price: float | None,
    take_profit_usdc: float | None,
    sell_cost_rate: float = DEFAULT_SELL_COST_RATE,
) -> ExitDecision | None:
    """Stop-loss first (protecting capital wins ties), then take-profit."""
    if quantity <= 0 or cost_basis <= 0 or price <= 0:
        return None
    net = net_profit_if_sold(quantity, cost_basis, price, sell_cost_rate)
    if stop_price is not None and price <= stop_price:
        return ExitDecision(
            "stop_loss", net,
            f"price {price:.6g} <= stop {stop_price:.6g} (entry {cost_basis:.6g}); est. result {net:+.2f}",
        )
    if take_profit_usdc is not None and take_profit_usdc > 0 and net >= take_profit_usdc:
        return ExitDecision(
            "take_profit", net,
            f"est. net profit {net:.2f} >= target {take_profit_usdc:.2f} after sell costs",
        )
    return None
