"""
Hard risk limits engine — the most important module in NEURAVEX (spec §7).

This module NEVER approves a trade; it only ever says NO or "yes, but
smaller". Evaluated *after* the decision engine so a strong signal can
still be vetoed here. Every check is deterministic and cannot be
overridden by an LLM or by strategy confidence (spec §16).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class PortfolioState:
    equity: float
    peak_equity: float
    cash: float
    open_positions: dict[str, dict]
    daily_pnl: float
    daily_start_equity: float
    trades_this_hour: int = 0
    last_entry_time: datetime | None = None
    last_signal_time_by_symbol: dict[str, datetime] = field(default_factory=dict)


@dataclass
class RiskCheckResult:
    approved: bool
    reason: str
    severity: str = "info"
    correlation_penalty: float = 0.0


class RiskLimitBreach(Exception):
    """Raised for account-wide breaches that should halt all new trading,
    not just reject a single trade (max_drawdown, max_daily_loss, or the
    user's own min_balance_floor)."""


def check_account_wide_limits(
    state: PortfolioState,
    max_daily_loss: float,
    max_drawdown: float,
    min_balance_floor: float | None = None,
) -> None:
    """Call this before evaluating ANY new trade. Raises if the account is
    in a state where no new trades should be opened at all.

    min_balance_floor is the user's own absolute stop (spec: "stopt bij
    €750") — distinct from max_drawdown (a percentage off the peak). It is
    checked first and takes priority in its error message, since it is the
    one limit the user set themselves and should recognize immediately in
    logs/alerts. This is the only account-wide stop by design — the user's
    profit-side stop is per-trade instead (see tasks.py's take-profit
    sweep), not account-wide, so it isn't checked here.
    """
    if min_balance_floor is not None and state.equity <= min_balance_floor:
        raise RiskLimitBreach(
            f"Balance floor reached: equity {state.equity:.2f} <= your configured stop of "
            f"{min_balance_floor:.2f}. No new trades — this is the limit you set yourself."
        )

    daily_loss_fraction = -state.daily_pnl / state.daily_start_equity if state.daily_start_equity else 0.0
    if daily_loss_fraction >= max_daily_loss:
        raise RiskLimitBreach(
            f"Daily loss limit breached: {daily_loss_fraction:.2%} >= {max_daily_loss:.2%}. "
            "No new trades until the next trading day."
        )

    drawdown = (state.peak_equity - state.equity) / state.peak_equity if state.peak_equity else 0.0
    if drawdown >= max_drawdown:
        raise RiskLimitBreach(
            f"Max drawdown breached: {drawdown:.2%} >= {max_drawdown:.2%}. "
            "Circuit breaker: flatten all positions and halt trading."
        )


def check_trade_against_limits(
    *,
    symbol: str,
    proposed_notional: float,
    state: PortfolioState,
    max_open_positions: int,
    max_portfolio_exposure: float,
    max_position_size: float,
    correlated_symbols: dict[str, float],
    cooldown_seconds: int,
    max_trades_per_hour: int,
    min_time_between_entries_seconds: int,
    now: datetime | None = None,
) -> RiskCheckResult:
    now = now or datetime.utcnow()

    # Correlation / concentration check (spec §10) — computed up front so
    # every returned result (approved or not) carries the true correlation
    # penalty, regardless of which check below ends up firing.
    correlation_penalty = 0.0
    correlated_notional = 0.0
    for other_symbol, corr in correlated_symbols.items():
        pos = state.open_positions.get(other_symbol)
        if pos and corr >= 0.7:
            correlated_notional += pos["notional"]
            correlation_penalty = max(correlation_penalty, corr)

    if symbol in state.open_positions:
        return RiskCheckResult(False, f"Position already open in {symbol}", "info", correlation_penalty)

    if len(state.open_positions) >= max_open_positions:
        return RiskCheckResult(False, f"max_open_positions reached ({max_open_positions})", "warning", correlation_penalty)

    if proposed_notional / state.equity > max_position_size if state.equity else True:
        return RiskCheckResult(False, "proposed size exceeds max_position_size", "warning", correlation_penalty)

    if correlated_notional > 0:
        combined_fraction = (correlated_notional + proposed_notional) / state.equity if state.equity else 1.0
        if combined_fraction > max_portfolio_exposure:
            return RiskCheckResult(
                False,
                f"combined correlated exposure ({combined_fraction:.1%}) would exceed "
                f"max_portfolio_exposure even though individual symbols are within limits",
                "warning",
                correlation_penalty=correlation_penalty,
            )

    current_exposure = sum(p["notional"] for p in state.open_positions.values())
    if (current_exposure + proposed_notional) / state.equity > max_portfolio_exposure if state.equity else True:
        return RiskCheckResult(False, "would exceed max_portfolio_exposure", "warning", correlation_penalty)

    last_signal = state.last_signal_time_by_symbol.get(symbol)
    if last_signal and (now - last_signal).total_seconds() < cooldown_seconds:
        return RiskCheckResult(False, f"cooldown active for {symbol}", "info", correlation_penalty)

    if state.trades_this_hour >= max_trades_per_hour:
        return RiskCheckResult(False, "max_trades_per_hour reached", "info", correlation_penalty)

    if state.last_entry_time and (now - state.last_entry_time).total_seconds() < min_time_between_entries_seconds:
        return RiskCheckResult(False, "min_time_between_entries not met", "info", correlation_penalty)

    return RiskCheckResult(True, "approved", "info", correlation_penalty=correlation_penalty)
