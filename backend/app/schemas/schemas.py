from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class AccountOverview(BaseModel):
    account_id: str
    equity: float
    cash: float
    daily_pnl: float
    daily_pnl_pct: float
    drawdown: float
    market_regime: str
    open_positions_count: int
    live_trading_enabled: bool


class PositionOut(BaseModel):
    symbol: str
    side: str
    quantity: float
    entry_price: float
    current_price: float | None = None
    unrealized_pnl: float | None = None
    stop_loss: float | None = None
    take_profit_1: float | None = None
    take_profit_2: float | None = None
    opened_at: datetime


class DecisionOut(BaseModel):
    symbol: str
    action: str
    confidence: float
    final_score: float
    reasons_for: list[str]
    reasons_against: list[str]
    entry: float | None = None
    stop_loss: float | None = None
    take_profit: float | None = None
    risk_reward: float | None = None
    created_at: datetime
    # True when this decision actually resulted in a trade (live order,
    # queued phone execution, or a filled paper-trading order); False for
    # NO_TRADE and for every "AI wanted to trade but a risk check blocked
    # it" case (see agent.py) — this is what lets the app show "X
    # executed / Y skipped today" instead of just a bare decision count.
    executed: bool = False


class DailyDecisionStats(BaseModel):
    date: str = Field(description="YYYY-MM-DD, in UTC")
    executed_count: int
    skipped_count: int
