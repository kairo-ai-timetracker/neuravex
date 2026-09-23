"""
NeuravexAgent — the per-tick orchestration loop (spec's full pipeline,
steps 1-12). One call to run_tick() = one full analysis pass over every
configured symbol.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

import numpy as np

from trading_engine.decision_engine import Decision, score_signals
from trading_engine.execution.exchange_interface import Exchange, OrderType, Side
from trading_engine.execution.live_guard import assert_live_order_allowed
from trading_engine.regime.regime_detector import detect_regime
from trading_engine.risk.position_sizing import calculate_position_size
from trading_engine.risk.risk_engine import (
    PortfolioState,
    RiskLimitBreach,
    check_account_wide_limits,
    check_trade_against_limits,
)
from trading_engine.risk.risk_plan import build_risk_plan
from trading_engine.signals.indicators import atr
from trading_engine.strategies.base import OHLCV, Strategy

logger = logging.getLogger("neuravex.agent")


@dataclass
class PlannedOrder:
    symbol: str
    side: str
    quantity: float
    stop_loss: float
    take_profit_1: float
    take_profit_2: float
    confidence: float
    reasons_for: list[str]
    reasons_against: list[str]
    order_type: str = "market"
    limit_price: float | None = None
    expires_in_seconds: int = 300


class DataStore(Protocol):
    def save_signal(self, symbol: str, signal) -> None: ...
    def save_decision(self, decision: Decision, account_id: str) -> str: ...
    def save_order_result(self, order_result, decision_id: str | None, account_id: str) -> None: ...
    def save_risk_event(self, event_type: str, severity: str, message: str, context: dict) -> None: ...
    def save_pending_execution(self, account_id: str, decision_id: str | None, plan: PlannedOrder) -> None: ...
    def get_portfolio_state(self, account_id: str) -> PortfolioState: ...
    def has_pending_close(self, account_id: str, symbol: str) -> bool: ...


@dataclass
class AgentConfig:
    symbols: list[str]
    timeframe: str = "1h"
    candle_limit: int = 300
    max_trade_risk: float = 0.01
    max_position_size: float = 0.20
    max_portfolio_exposure: float = 0.60
    max_daily_loss: float = 0.03
    max_drawdown: float = 0.10
    # The user's own absolute stop (spec: "stopt bij €750") — None means
    # not yet configured, in which case the agent still enforces
    # max_daily_loss/max_drawdown but has no user-set hard floor.
    min_balance_floor: float | None = None
    max_open_positions: int = 5
    minimum_confidence: float = 0.70
    minimum_expected_edge: float = 0.003
    cooldown_seconds: int = 900
    max_trades_per_hour: int = 4
    min_time_between_entries_seconds: int = 300
    correlation_threshold: float = 0.7
    trading_mode: str = "paper"
    allow_live_flag: bool = False
    # "server": this process calls the exchange/DEX adapter directly.
    # "phone": backend only proposes orders (PendingExecution); the
    # Android app holds the credential and executes them.
    execution_mode: str = "server"
    # When True, the agent runs the FULL analysis pipeline (regime, all 4
    # strategies, decision engine, risk checks) and logs every Signal and
    # AIDecision exactly as normal — the dashboard's "why did NEURAVEX
    # decide this" is fully populated — but the final execution step is
    # skipped. This is what lets a user watch the AI's real reasoning on
    # real market data before ever granting it a credential, rather than
    # having to trust it blind. Set by the caller from the account's own
    # `ai_enabled` switch (see backend/app/core/tasks.py) — it is not
    # itself a user-facing setting.
    dry_run: bool = False


class NeuravexAgent:
    def __init__(
        self,
        exchange: Exchange,
        strategies: list[Strategy],
        store: DataStore,
        config: AgentConfig,
        account_id: str,
        strategy_performance: dict[str, float] | None = None,
        correlation_matrix: dict[str, dict[str, float]] | None = None,
    ):
        self.exchange = exchange
        self.strategies = strategies
        self.store = store
        self.config = config
        self.account_id = account_id
        self.strategy_performance = strategy_performance or {}
        self.correlation_matrix = correlation_matrix or {}

    async def run_tick(self) -> list[Decision]:
        decisions: list[Decision] = []
        try:
            state = self.store.get_portfolio_state(self.account_id)
        except Exception as e:  # noqa: BLE001
            logger.exception("Could not load portfolio state: %s", e)
            self.store.save_risk_event("state_load_failed", "error", str(e), {"account_id": self.account_id})
            return decisions

        try:
            check_account_wide_limits(
                state, self.config.max_daily_loss, self.config.max_drawdown, self.config.min_balance_floor,
            )
        except RiskLimitBreach as e:
            logger.warning("Account-wide risk limit breached: %s", e)
            self.store.save_risk_event("risk_limit_breach", "critical", str(e), {"account_id": self.account_id})
            return decisions

        for symbol in self.config.symbols:
            try:
                decision = await self._process_symbol(symbol, state)
                if decision is not None:
                    decisions.append(decision)
                    self.store.save_decision(decision, self.account_id)
            except Exception as e:  # noqa: BLE001 — one bad symbol must not kill the whole tick
                logger.exception("Error processing %s: %s", symbol, e)
                self.store.save_risk_event("symbol_processing_failed", "warning", str(e), {"symbol": symbol})

        return decisions

    async def _process_symbol(self, symbol: str, state: PortfolioState) -> Decision | None:
        candles = await self.exchange.get_candles(symbol, self.config.timeframe, self.config.candle_limit)
        if len(candles) < 60:
            return None

        data = OHLCV(
            open_time=np.array([c.open_time.timestamp() for c in candles]),
            open=np.array([c.open for c in candles]),
            high=np.array([c.high for c in candles]),
            low=np.array([c.low for c in candles]),
            close=np.array([c.close for c in candles]),
            volume=np.array([c.volume for c in candles]),
        )
        regime = detect_regime(data.high, data.low, data.close)

        signals = []
        for strategy in self.strategies:
            signal = strategy.generate_signal(symbol, data, regime)
            if signal is not None:
                signals.append(signal)
                self.store.save_signal(symbol, signal)

        correlated = self.correlation_matrix.get(symbol, {})
        correlation_penalty = max(
            (corr for other, corr in correlated.items() if other in state.open_positions), default=0.0
        )

        recent_volume = data.volume[-20:]
        volume_confirmation = 1.0 if data.volume[-1] > np.mean(recent_volume) else 0.0

        decision = score_signals(
            symbol=symbol, signals=signals, regime=regime,
            strategy_performance=self.strategy_performance,
            volume_confirmation_score=volume_confirmation,
            correlation_penalty=correlation_penalty,
            estimated_transaction_cost=0.001,
            minimum_confidence=self.config.minimum_confidence,
            minimum_expected_edge=self.config.minimum_expected_edge,
        )

        if decision.action == "NO_TRADE":
            return decision

        direction = decision.direction

        # A SHORT signal on a symbol we already hold means "close this
        # position", not "open a new short" — spot-only accounts can only
        # sell what they hold (see the NO_TRADE guard below for the
        # opposite case). This is the AI's OWN judgment call, independent
        # of any fixed profit/loss number: it may close at a €0.05 profit
        # if it doesn't expect the position to reach the account's
        # per-trade take-profit target, or cut a loss early if it judges
        # the trade unlikely to recover. That fixed target (max_balance_target,
        # enforced separately by check_live_exits/_apply_per_trade_take_profit)
        # stays as a guaranteed backstop on the profit side; it never
        # blocks an earlier, smaller exit here.
        #
        # This MUST be handled before check_trade_against_limits: that
        # function's "reject if a position is already open in this symbol"
        # rule exists to stop the agent opening a SECOND position in the
        # same symbol, but it does not distinguish that from an intentional
        # close — every AI-driven close signal was previously vetoed by
        # that exact check and silently turned into NO_TRADE, so the AI's
        # own sell judgment never actually executed for a held position
        # (only the fixed take-profit sweep, which bypasses this code path
        # entirely, could ever close one). Closing sells the FULL held
        # quantity directly, the same way check_live_exits' fixed sweep
        # does — not a freshly risk-sized amount from calculate_position_size,
        # which is meant for opening new positions, not exiting existing ones.
        if direction == "SHORT" and symbol in state.open_positions:
            held_quantity = state.open_positions[symbol].get("quantity", 0.0)
            if held_quantity <= 0:
                decision.action = "NO_TRADE"
                decision.reasons_against.append("No held quantity available to close")
                return decision
            # This tick's own PortfolioState was read at the START of
            # run_tick() and only reflects the `positions` table — it does
            # NOT know about a close that was already proposed (to the
            # phone, or by check_live_exits' fixed take-profit/stop-loss
            # sweep, which runs on its own much shorter ~30s schedule) but
            # not yet executed/reported. Without this check, the exact same
            # AI-judged SHORT signal firing again on the very next ~1-minute
            # tick — before the phone has even had time to submit, confirm,
            # and report back the FIRST close — would queue a second,
            # overlapping "sell the full held quantity" order, and a third,
            # and so on, every tick, for as long as the signal keeps firing
            # and the position hasn't actually cleared yet. Each one is a
            # real order the phone will claim and execute on-chain (real
            # gas, real slippage), most of which fail or waste gas once the
            # first one has already sold the position out from under them.
            # check_live_exits already guards its own sweep this exact way
            # (see tasks.py) — this mirrors that same guard for the AI's own
            # signal-driven close, which was missing it entirely.
            if self.store.has_pending_close(self.account_id, symbol):
                decision.action = "NO_TRADE"
                decision.reasons_against.append(
                    "A close for this position is already in flight (proposed but not yet "
                    "executed/reported) — skipping to avoid a duplicate overlapping sell"
                )
                return decision
            decision.reasons_for.append(
                f"AI-judged exit: closing full {symbol} position ({held_quantity:.6f}) on its own "
                f"signal, not a fixed profit/loss threshold"
            )
            return await self._close_position(symbol, held_quantity, decision)

        entry = float(data.close[-1])
        atr_value = float(atr(data.high, data.low, data.close)[-1])
        plan = build_risk_plan(entry=entry, direction=direction, atr_value=atr_value)

        # Spot-only guard: a SHORT cannot be opened without already holding
        # the asset (spec's spot_only=True default in backtesting/live).
        if direction == "SHORT" and symbol not in state.open_positions:
            decision.action = "NO_TRADE"
            decision.reasons_against.append(
                "SHORT signal ignored: spot-only account cannot open a short without holding the asset"
            )
            return decision

        risk_check = check_trade_against_limits(
            symbol=symbol, proposed_notional=state.equity * self.config.max_trade_risk * 10,
            state=state, max_open_positions=self.config.max_open_positions,
            max_portfolio_exposure=self.config.max_portfolio_exposure,
            max_position_size=self.config.max_position_size,
            correlated_symbols=correlated, cooldown_seconds=self.config.cooldown_seconds,
            max_trades_per_hour=self.config.max_trades_per_hour,
            min_time_between_entries_seconds=self.config.min_time_between_entries_seconds,
        )
        if not risk_check.approved:
            decision.action = "NO_TRADE"
            decision.reasons_against.append(f"Risk engine: {risk_check.reason}")
            return decision

        current_exposure_fraction = (
            sum(p["notional"] for p in state.open_positions.values()) / state.equity if state.equity else 0.0
        )
        size = calculate_position_size(
            account_equity=state.equity, entry_price=entry, stop_loss_price=plan.stop_loss,
            confidence=decision.confidence, max_trade_risk=self.config.max_trade_risk,
            max_position_size_fraction=self.config.max_position_size,
            current_portfolio_exposure_fraction=current_exposure_fraction,
            max_portfolio_exposure_fraction=self.config.max_portfolio_exposure,
            correlation_penalty=risk_check.correlation_penalty,
        )

        if size.quantity <= 0:
            decision.action = "NO_TRADE"
            decision.reasons_against.append(f"Position sizing yielded zero quantity ({size.capped_by})")
            return decision

        if self.config.dry_run:
            # Full analysis and risk-checking already happened above and is
            # already logged via save_decision — this is exactly what "why
            # did NEURAVEX decide this" shows. What stops here is only the
            # final execution step, so the user can see genuine reasoning
            # on real market data before AI trading is switched on.
            decision.reasons_for.append(
                f"Would {decision.action} {size.quantity:.6f} {symbol} @ {plan.entry:.4f} "
                f"(stop {plan.stop_loss:.4f}) — AI trading is currently off, nothing was executed"
            )
            logger.info("DRY RUN — would %s %s qty=%.6f (AI trading is off)", decision.action, symbol, size.quantity)
            return decision

        # 11. Decision already made (BUY/SELL) — 12. Execute if approved
        #
        # Minted HERE, before either execution path, not left for
        # run_tick()'s later save_decision() call: this is what stamps a
        # real ai_decisions.id onto the PendingExecution/Order about to be
        # created, which is in turn what lets a position opened by this
        # trade trace back to the strategies that called it (see
        # execution.py's _apply_fill_to_position / _update_strategy_
        # performance). save_decision() is idempotent on an already-set
        # decision.id, so run_tick()'s own call afterwards just updates
        # this same row with the final reasons_for/executed state instead
        # of inserting a duplicate.
        decision_id = self.store.save_decision(decision, self.account_id)

        if self.config.execution_mode == "phone":
            # This process never touches an exchange secret. It hands off a
            # fully risk-checked, self-expiring order description for the
            # phone to execute with its own locally-held API key.
            planned = PlannedOrder(
                symbol=symbol, side="buy" if direction == "LONG" else "sell", quantity=size.quantity,
                stop_loss=plan.stop_loss, take_profit_1=plan.take_profit_1, take_profit_2=plan.take_profit_2,
                confidence=decision.confidence, reasons_for=decision.reasons_for, reasons_against=decision.reasons_against,
            )
            self.store.save_pending_execution(self.account_id, decision_id, planned)
            decision.executed = True
            return decision

        assert_live_order_allowed(
            exchange=self.exchange, trading_mode=self.config.trading_mode,
            allow_live_flag=self.config.allow_live_flag, confirm_live=(self.config.trading_mode == "live"),
        )
        side = Side.buy if direction == "LONG" else Side.sell
        order_result = await self.exchange.create_order(symbol, side, OrderType.market, size.quantity)
        self.store.save_order_result(order_result, decision_id, self.account_id)
        decision.executed = True
        return decision

    async def _close_position(self, symbol: str, quantity: float, decision: Decision) -> Decision:
        """
        Closes a held position at the AI's own initiative (called from
        _process_symbol when a SHORT signal lands on a symbol we already
        hold). Mirrors the two execution paths BUY/entry already uses
        (phone hand-off vs. direct order) but always sells the FULL held
        quantity — there is no position-sizing step here, since sizing is
        a new-entry concept and this is an exit.
        """
        if self.config.dry_run:
            decision.reasons_for.append(
                f"Would SELL (close) {quantity:.6f} {symbol} — AI trading is currently off, nothing was executed"
            )
            logger.info("DRY RUN — would close %s qty=%.6f (AI trading is off)", symbol, quantity)
            return decision

        # See the matching comment in _process_symbol — minted early so the
        # PendingExecution/Order below carries a real decision_id. Not
        # actually needed for strategy attribution (a CLOSE's decision
        # never becomes a Position's opening_decision_id — that stays
        # fixed to whichever decision originally opened it), but kept
        # consistent with the entry path for record-keeping.
        decision_id = self.store.save_decision(decision, self.account_id)

        if self.config.execution_mode == "phone":
            planned = PlannedOrder(
                symbol=symbol, side="sell", quantity=quantity,
                # No stop_loss/take_profit_1/2 on a close — those describe
                # a NEW position's risk plan, which doesn't apply here.
                stop_loss=0.0, take_profit_1=0.0, take_profit_2=0.0,
                confidence=decision.confidence, reasons_for=decision.reasons_for,
                reasons_against=decision.reasons_against,
            )
            self.store.save_pending_execution(self.account_id, decision_id, planned)
            decision.executed = True
            return decision

        assert_live_order_allowed(
            exchange=self.exchange, trading_mode=self.config.trading_mode,
            allow_live_flag=self.config.allow_live_flag, confirm_live=(self.config.trading_mode == "live"),
        )
        order_result = await self.exchange.create_order(symbol, Side.sell, OrderType.market, quantity)
        self.store.save_order_result(order_result, decision_id, self.account_id)
        decision.executed = True
        return decision
