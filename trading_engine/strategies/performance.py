"""
Strategy track-record math, shared by the live path (backend/app/api/
execution.py, when a phone fill closes a position), the simulation path
(SqlDataStore.record_realized_trade) and scripts/replay_backtest.py.
"""
from __future__ import annotations

# A strategy needs at least this many CLOSED, attributed trades before its
# win rate is trusted at all — below this one lucky or unlucky early trade
# could swing a brand-new strategy's win rate straight to 0% or 100%.
MIN_TRADES_FOR_PERFORMANCE_WEIGHTING = 5


def update_running_stats(
    win_rate: float, expectancy: float, n: int, *, won: bool, pnl_per_unit: float,
) -> tuple[float, float, int]:
    """Incremental (running) average — equivalent to recomputing the mean
    over every attributed trade, as a single O(1) update."""
    new_n = n + 1
    new_win_rate = (win_rate * n + (1.0 if won else 0.0)) / new_n
    new_expectancy = (expectancy * n + pnl_per_unit) / new_n
    return new_win_rate, new_expectancy, new_n


def performance_score(win_rate: float, closed_trades: int) -> float | None:
    """Win rate mapped from 0..1 to -1..1 around a neutral 50%; None while
    the strategy is still unproven (fewer than the minimum trades)."""
    if closed_trades < MIN_TRADES_FOR_PERFORMANCE_WEIGHTING:
        return None
    return max(-1.0, min(1.0, 2 * (win_rate - 0.5)))
