"""SQLAlchemy models for NEURAVEX."""
from __future__ import annotations

import uuid
from datetime import datetime
from enum import Enum as PyEnum

from sqlalchemy import Boolean, DateTime, Enum, Float, ForeignKey, Integer, JSON, String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def gen_uuid() -> str:
    return str(uuid.uuid4())


class Base(DeclarativeBase):
    pass


class TradingMode(PyEnum):
    backtest = "backtest"
    paper = "paper"
    live = "live"


class User(Base):
    __tablename__ = "users"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=gen_uuid)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    hashed_password: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class Account(Base):
    __tablename__ = "accounts"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=gen_uuid)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    name: Mapped[str] = mapped_column(String(100), default="default")
    mode: Mapped[TradingMode] = mapped_column(Enum(TradingMode), default=TradingMode.paper)
    starting_balance: Mapped[float] = mapped_column(Float, default=1000.0)
    live_trading_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class AccountSettings(Base):
    """
    User-configurable settings — deliberately minimal (spec: "ik doe en
    denk zo weinig mogelijk"). The user controls only:
      - which coins the AI may trade (`symbols`)
      - a single absolute balance floor (`min_balance_floor`) — trading
        halts entirely once equity reaches this amount
      - whether the AI is currently allowed to trade at all (`ai_enabled`)
      - which venue it trades on (`execution_venue`)

    Everything else the risk engine needs (per-trade risk %, max position
    size, max portfolio exposure, daily loss pacing) is intentionally NOT
    user-facing. Those are fixed, conservative system defaults — see
    DEFAULT_* constants below — chosen so the AI can size and time trades
    itself without asking the user to think in percentages. They exist in
    the risk engine exactly as before; only the UI surface changed.
    """
    __tablename__ = "account_settings"

    DEFAULT_MAX_DAILY_LOSS = 0.05
    DEFAULT_MAX_TRADE_RISK = 0.02
    DEFAULT_MAX_POSITION_SIZE = 0.25
    DEFAULT_MAX_PORTFOLIO_EXPOSURE = 0.70

    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id"), primary_key=True)
    symbols: Mapped[list] = mapped_column(JSON, default=list)

    # The one number the user sets: "stop trading once my balance reaches
    # this amount" (an absolute currency floor, not a percentage — spec:
    # "€1000 in mijn wallet ... stopt bij €750"). Null means not yet
    # configured; the agent must refuse to trade live until this is set.
    min_balance_floor: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Per-trade take-profit (spec, redesigned from an account-wide
    # target: "ik wil een algemeen getal invullen... als de winst op
    # die trade €50 is dan stopt hij"). In USDC-equivalent: once ANY
    # single open position's own unrealized profit reaches this amount,
    # that position alone is sold — the rest of the account keeps
    # trading. The loss side stays account-wide on purpose (the user's
    # explicit choice) — see min_balance_floor above, unchanged.
    max_balance_target: Mapped[float | None] = mapped_column(Float, nullable=True)

    # The on/off switch (spec: "een knop wanneer ik de AI activeer").
    ai_enabled: Mapped[bool] = mapped_column(Boolean, default=False)

    # "cex": backend holds exchange credentials and executes directly, or
    #        proposes orders for a CEX-side phone executor.
    # "wallet": the Android app holds a wallet private key and executes
    #        swaps on-chain (e.g. Uniswap on Polygon) — see dex_adapter.py.
    execution_venue: Mapped[str] = mapped_column(String(10), default="wallet")

    # "server": this process calls the exchange/DEX adapter directly.
    # "phone": backend only proposes orders (PendingExecution); the
    #        Android app holds the credential and executes them.
    execution_mode: Mapped[str] = mapped_column(String(10), default="phone")

    # Live vs. simulation toggle (spec: "kiezen tussen live traden of
    # traden met een zelf ingestelde fictie bedrag"). True (the default —
    # safest starting point) means every trade this account makes is
    # simulated against `paper_balances`, a fictional wallet that never
    # touches the phone's real wallet key or a real exchange account. When
    # False, execution proceeds exactly as before this feature existed
    # (execution_venue/execution_mode govern real money). See tasks.py for
    # how this branches the tick.
    paper_trading_enabled: Mapped[bool] = mapped_column(Boolean, default=True)

    # The fictional amount (in USDC-equivalent) a fresh simulation starts
    # from — user-configurable (spec: "een zelf ingestelde fictie
    # bedrag"). Changing this resets `paper_balances` to a new simulation
    # starting from this amount (see settings.py's update handler) — it is
    # not a running total itself.
    paper_starting_balance: Mapped[float] = mapped_column(Float, default=1000.0)

    # The simulation's actual running balances, keyed by asset symbol
    # (e.g. {"USDC": 850.0, "WBTC": 0.0021}) — persisted across ticks so
    # simulated positions and P/L carry forward from one tick to the next,
    # the same way a real wallet would. Empty means "not yet initialized
    # for the current paper_starting_balance" (see tasks.py, which seeds
    # it from paper_starting_balance on first use).
    paper_balances: Mapped[dict] = mapped_column(JSON, default=dict)
    # Weighted-average entry price per held asset — see
    # PaperExchange.get_cost_basis(). Needed to know each simulated
    # position's own unrealized profit, since a fresh PaperExchange is
    # built every tick with no memory of its own.
    paper_positions: Mapped[dict] = mapped_column(JSON, default=dict)

    # Explicit live-mode baseline for "Today's P/L": the total real wallet
    # value (including view-only networks) the first time the app reported
    # a complete, non-zero balance. Previously the baseline was "first
    # snapshot ever", which could be a 0 written while RPCs were failing
    # (or a simulation snapshot), making the whole balance show as profit.
    # Null = not yet established; see dashboard.report_balance / reset-baseline.
    live_start_equity: Mapped[float | None] = mapped_column(Float, nullable=True)

    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class Strategy(Base):
    __tablename__ = "strategies"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=gen_uuid)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    family: Mapped[str] = mapped_column(String(50))
    # Present on the live database from an earlier schema version (same
    # situation as Order.updated_at / Position.take_profit_1/2 above) but
    # never declared here until this — every insert of a NEW strategy row
    # (save_signal in sql_data_store.py, on first use of a given strategy
    # name) omitted these and got rejected with "null value in column
    # is_active violates not-null constraint" the moment a strategy that
    # wasn't already seeded in the table got used for the first time.
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    params: Mapped[dict] = mapped_column(JSON, default=dict)
    historical_win_rate: Mapped[float] = mapped_column(Float, default=0.0)
    historical_expectancy: Mapped[float] = mapped_column(Float, default=0.0)
    total_signals: Mapped[int] = mapped_column(Integer, default=0)


class Signal(Base):
    __tablename__ = "signals"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=gen_uuid)
    strategy_id: Mapped[str] = mapped_column(ForeignKey("strategies.id"))
    symbol: Mapped[str] = mapped_column(String(30))
    direction: Mapped[str] = mapped_column(String(10))
    confidence: Mapped[float] = mapped_column(Float)
    expected_return: Mapped[float] = mapped_column(Float)
    risk: Mapped[float] = mapped_column(Float)
    time_horizon: Mapped[str] = mapped_column(String(10))
    regime_at_signal: Mapped[str] = mapped_column(String(30), default="")
    raw: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class DecisionAction(PyEnum):
    buy = "BUY"
    sell = "SELL"
    no_trade = "NO_TRADE"


class AIDecision(Base):
    __tablename__ = "ai_decisions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=gen_uuid)
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id"))
    symbol: Mapped[str] = mapped_column(String(30))
    action: Mapped[DecisionAction] = mapped_column(Enum(DecisionAction))
    final_score: Mapped[float] = mapped_column(Float)
    score_breakdown: Mapped[dict] = mapped_column(JSON, default=dict)
    reasons_for: Mapped[list] = mapped_column(JSON, default=list)
    reasons_against: Mapped[list] = mapped_column(JSON, default=list)
    entry: Mapped[float | None] = mapped_column(Float, nullable=True)
    stop_loss: Mapped[float | None] = mapped_column(Float, nullable=True)
    take_profit: Mapped[float | None] = mapped_column(Float, nullable=True)
    risk_reward: Mapped[float | None] = mapped_column(Float, nullable=True)
    executed: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class OrderSide(PyEnum):
    buy = "buy"
    sell = "sell"


class OrderStatus(PyEnum):
    open = "open"
    filled = "filled"
    partially_filled = "partially_filled"
    cancelled = "cancelled"
    rejected = "rejected"


class Order(Base):
    __tablename__ = "orders"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=gen_uuid)
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id"))
    decision_id: Mapped[str | None] = mapped_column(ForeignKey("ai_decisions.id"), nullable=True)
    exchange_order_id: Mapped[str] = mapped_column(String(100), default="")
    symbol: Mapped[str] = mapped_column(String(30))
    side: Mapped[OrderSide] = mapped_column(Enum(OrderSide))
    order_type: Mapped[str] = mapped_column(String(20))
    quantity: Mapped[float] = mapped_column(Float)
    status: Mapped[OrderStatus] = mapped_column(Enum(OrderStatus))
    filled_quantity: Mapped[float] = mapped_column(Float, default=0.0)
    average_fill_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    fee: Mapped[float] = mapped_column(Float, default=0.0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    # Present on the live database from an earlier schema version (same
    # situation as pending_executions.claimed_by/claimed_at) but never
    # declared here until this — every insert into this table omitted it
    # and got rejected with "null value in column updated_at violates
    # not-null constraint" the moment this code path was first actually
    # exercised (by paper-trading's execution_mode="server" path).
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class Trade(Base):
    __tablename__ = "trades"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=gen_uuid)
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id"))
    symbol: Mapped[str] = mapped_column(String(30))
    direction: Mapped[str] = mapped_column(String(10))
    entry_price: Mapped[float] = mapped_column(Float)
    exit_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    quantity: Mapped[float] = mapped_column(Float)
    pnl: Mapped[float | None] = mapped_column(Float, nullable=True)
    opened_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class PositionStatus(PyEnum):
    open = "open"
    closed = "closed"


class Position(Base):
    __tablename__ = "positions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=gen_uuid)
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id"))
    symbol: Mapped[str] = mapped_column(String(30))
    side: Mapped[OrderSide] = mapped_column(Enum(OrderSide))
    quantity: Mapped[float] = mapped_column(Float)
    entry_price: Mapped[float] = mapped_column(Float)
    stop_loss: Mapped[float | None] = mapped_column(Float, nullable=True)
    # take_profit_1 / take_profit_2 (not a single take_profit column) — this
    # matches the actual live database schema from earlier sessions, which
    # init_db() never alters since the table already exists. Matching the
    # real column names here, rather than what would be simplest in
    # isolation, is what actually matters once a database has history.
    take_profit_1: Mapped[float | None] = mapped_column(Float, nullable=True)
    take_profit_2: Mapped[float | None] = mapped_column(Float, nullable=True)
    status: Mapped[PositionStatus] = mapped_column(Enum(PositionStatus), default=PositionStatus.open)
    opened_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class PortfolioSnapshot(Base):
    __tablename__ = "portfolio_snapshots"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=gen_uuid)
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id"))
    equity: Mapped[float] = mapped_column(Float)
    cash: Mapped[float] = mapped_column(Float)
    exposure: Mapped[float] = mapped_column(Float, default=0.0)
    drawdown: Mapped[float] = mapped_column(Float, default=0.0)
    open_positions: Mapped[int] = mapped_column(Integer, default=0)
    market_regime: Mapped[str] = mapped_column(String(30), default="SIDEWAYS")
    taken_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    # Simulation and live snapshots share this table. Without a marker,
    # whichever was written last leaked into the other mode's Portfolio,
    # P/L, drawdown peak and the risk engine's equity (e.g. resetting the
    # simulation showed the real wallet's ~114 until the next tick).
    is_simulated: Mapped[bool] = mapped_column(Boolean, default=False)


class RiskEvent(Base):
    __tablename__ = "risk_events"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=gen_uuid)
    account_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    event_type: Mapped[str] = mapped_column(String(50))
    severity: Mapped[str] = mapped_column(String(20))
    message: Mapped[str] = mapped_column(String(1000))
    context: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class SystemEvent(Base):
    __tablename__ = "system_events"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=gen_uuid)
    event_type: Mapped[str] = mapped_column(String(50))
    message: Mapped[str] = mapped_column(String(1000))
    context: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class PendingExecution(Base):
    """
    A fully risk-checked, self-expiring order description, produced by the
    server (which never holds a trading credential) for the Android app
    (which does, locally, encrypted) to claim and execute. `claim()` in
    execution.py prevents double-execution if more than one device polls.
    """
    __tablename__ = "pending_executions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=gen_uuid)
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id"))
    decision_id: Mapped[str | None] = mapped_column(ForeignKey("ai_decisions.id"), nullable=True)
    symbol: Mapped[str] = mapped_column(String(30))
    side: Mapped[OrderSide] = mapped_column(Enum(OrderSide))
    quantity: Mapped[float] = mapped_column(Float)
    order_type: Mapped[str] = mapped_column(String(20), default="market")
    limit_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    stop_loss: Mapped[float | None] = mapped_column(Float, nullable=True)
    take_profit_1: Mapped[float | None] = mapped_column(Float, nullable=True)
    take_profit_2: Mapped[float | None] = mapped_column(Float, nullable=True)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    reasons_for: Mapped[list] = mapped_column(JSON, default=list)
    reasons_against: Mapped[list] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(20), default="pending")  # pending|claimed|executed|failed|expired
    claimed_by: Mapped[str | None] = mapped_column(String(100), nullable=True)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    exchange_order_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    average_fill_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    filled_quantity: Mapped[float | None] = mapped_column(Float, nullable=True)
    failure_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime)
