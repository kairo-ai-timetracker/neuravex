"""
Supervisor: a feedback layer on top of the trading agent. It measures closed
trades and adjusts how the agent may enter new positions. It never touches
exits, stops or the circuit breakers.

Two rules, both pure functions of the trade history:

- Stake: a loss counter goes up on a loss and down on a win (never below 0).
  The higher it is, the smaller the risk per trade. It recovers step by step
  rather than jumping back to full size after a single lucky win.
- Strategy gate: a strategy that lost money in a market regime over at least
  MIN_TRADES_TO_BLOCK recent trades may not open new positions in that regime.
  Old trades age out after LOOKBACK, so a block always ends by itself.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

LOOKBACK = timedelta(days=45)
MIN_TRADES_TO_BLOCK = 4
RISK_BY_LOSS_COUNT = {0: 1.0, 1: 1.0, 2: 0.75, 3: 0.5}
MIN_RISK_MULTIPLIER = 0.25


@dataclass
class ClosedTrade:
    closed_at: datetime
    pnl: float
    strategies: list[str]
    regime: str | None = None


@dataclass
class SupervisorVerdict:
    risk_multiplier: float = 1.0
    blocked: set[tuple[str, str]] = field(default_factory=set)

    def allows(self, strategy: str, regime: str) -> bool:
        return (strategy, regime) not in self.blocked


def evaluate_supervisor(trades: list[ClosedTrade], now: datetime) -> SupervisorVerdict:
    recent = sorted((t for t in trades if now - t.closed_at <= LOOKBACK), key=lambda t: t.closed_at)

    loss_count = 0
    for trade in recent:
        loss_count = loss_count + 1 if trade.pnl <= 0 else max(0, loss_count - 1)

    totals: dict[tuple[str, str], list[float]] = {}
    for trade in recent:
        if trade.regime is None:
            continue
        for strategy in trade.strategies:
            totals.setdefault((strategy, trade.regime), []).append(trade.pnl)

    return SupervisorVerdict(
        risk_multiplier=RISK_BY_LOSS_COUNT.get(loss_count, MIN_RISK_MULTIPLIER),
        blocked={key for key, pnls in totals.items() if len(pnls) >= MIN_TRADES_TO_BLOCK and sum(pnls) < 0},
    )
