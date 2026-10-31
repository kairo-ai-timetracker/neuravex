"""
NeuravexAgent — the per-tick orchestration loop (spec's full pipeline,
steps 1-12). One call to run_tick() = one full analysis pass over every
configured symbol.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable, Protocol

import numpy as np

from trading_engine.decision_engine import Decision, ScoreBreakdown, score_signals
from trading_engine.execution.exchange_interface import Exchange, OrderType, Side
from trading_engine.execution.live_guard import assert_live_order_allowed
from trading_engine.regime.regime_detector import detect_regime
from trading_engine.risk.position_sizing import calculate_position_size
from trading_engine.supervisor import ClosedTrade, SupervisorVerdict, evaluate_supervisor
from trading_engine.risk.risk_engine import (
    PortfolioState,
    RiskLimitBreach,
    check_account_wide_limits,
    check_trade_against_limits,
)
from trading_engine.risk.risk_plan import build_risk_plan
from trading_engine.signals.indicators import atr, ema
from trading_engine.strategies.base import OHLCV, Strategy

logger = logging.getLogger("neuravex.agent")

_TIMEFRAME_SECONDS = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600, "4h": 14_400, "1d": 86_400}

# Tag carried by every Decision/PendingExecution produced by
# flatten_all_positions for an automatic account-wide circuit breaker
# (min_balance_floor / max_drawdown), so those closes are distinguishable
# in trade/decision history from an ordinary strategy-driven sell.
RISK_CIRCUIT_BREAKER_REASON = "risk_circuit_breaker"


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
    def recent_closed_trades(self, account_id: str) -> list[ClosedTrade]: ...
    def save_decision(self, decision: Decision, account_id: str) -> str: ...
    def save_order_result(self, order_result, decision_id: str | None, account_id: str) -> None: ...
    def save_risk_event(self, event_type: str, severity: str, message: str, context: dict) -> None: ...
    def save_pending_execution(self, account_id: str, decision_id: str | None, plan: PlannedOrder) -> None: ...
    def get_portfolio_state(self, account_id: str) -> PortfolioState: ...
    def has_pending_order(self, account_id: str, symbol: str, side: str) -> bool: ...
    def record_realized_trade(self, account_id: str, symbol: str, entry_price: float | None, order_result) -> None: ...
    def note_drawdown_halt(self, account_id: str, message: str) -> None: ...


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
    # Strategies only ever see CLOSED candles. The tick runs every minute on
    # 1h candles, and the market-data feed includes the still-forming
    # current hour as its last candle — so every indicator (RSI, MACD, the
    # "volume above average" checks) was being recomputed on a candle that
    # changes minute to minute. That made signals flip within the hour
    # ("LONG 80%" then "SHORT 80%" on the same candle) and the bot churned
    # in and out of positions, paying fees each time.
    closed_candles_only: bool = True
    # At most one trade decision per symbol per closed candle: after
    # entering or exiting a symbol, the next decision on it waits until a
    # NEW candle has closed. With closed candles the signal is identical
    # for the rest of the hour, so without this the same signal re-entered
    # right after every take-profit/stop exit.
    one_trade_per_candle: bool = True
    # Round-trip cost estimate fed to the decision engine (net edge must
    # beat it). Was hardcoded to 0.1%; the real paper/CEX round trip is
    # ~0.3% (2 x 0.1% taker fee + 2 x 5 bps slippage).
    estimated_transaction_cost: float = 0.003
    # Measured round-trip cost per symbol, overriding the estimate above.
    round_trip_cost_by_symbol: dict[str, float] | None = None
    # Entry filters (new positions only — exits are never filtered). The
    # defaults keep the original behaviour; see scripts/replay_backtest.py
    # for how alternatives are evaluated before being switched on.
    # Minimum number of strategies that must agree on a LONG entry.
    min_agreeing_strategies: int = 1
    # If set, only open new positions while the symbol's market regime is
    # one of these (MarketRegime values, e.g. ("BULL", "STRONG_BULL")).
    entry_regimes: tuple[str, ...] | None = None
    # detect_regime() was written for 1h candles: it annualizes volatility
    # with 24*365 periods and looks back 24 candles for a crash. On 4h
    # candles that overstated volatility ~2x (nearly every coin read as
    # "high", which penalized every score and switched mean reversion off)
    # and stretched the crash window to 4 days. True scales both to the
    # configured timeframe; False keeps the old 1h assumption.
    # Off by default: in the replay (Aug-Oct 2026) the correct numbers did
    # worse (+0.1% vs +8.2%) because mean reversion trades more.
    timeframe_aware_regime: bool = False
    # If set, NO_TRADE decisions and signals are only stored during this many
    # seconds after a candle closes. On closed candles they are identical for
    # the rest of the candle, and a tick per minute wrote ~1,440 rows per coin
    # per day. Real trades are always stored.
    analysis_log_window_seconds: int | None = None
    # See trading_engine/supervisor.py.
    supervisor_enabled: bool = False
    # If set, a sell signal only closes a held position once the price is
    # below this EMA, so a dip inside a rising trend doesn't end the trade.
    # Stop-losses and circuit breakers are not affected.
    signal_exit_ema: int | None = None


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
        clock: Callable[[], datetime] | None = None,
    ):
        self.exchange = exchange
        self.strategies = strategies
        self.store = store
        self.config = config
        self.account_id = account_id
        self.strategy_performance = strategy_performance or {}
        self.correlation_matrix = correlation_matrix or {}
        # Naive-UTC "now", injectable so scripts/replay_backtest.py can
        # replay history through this exact code path.
        self._now = clock or datetime.utcnow
        # Symbols past analysis_log_window_seconds on this tick.
        self._quiet_symbols: set[str] = set()
        self._verdict = SupervisorVerdict()

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
            self.store.save_risk_event(
                "risk_limit_breach", "critical", str(e),
                {"account_id": self.account_id, "limit": e.limit, "should_flatten": e.should_flatten},
            )
            # max_daily_loss only pauses new entries (should_flatten=False);
            # min_balance_floor / max_drawdown are hard stops that must also
            # close what's already open, otherwise those positions keep
            # marking to market and equity keeps sliding past the limit.
            # Runs again every tick while the breach persists — a no-op once
            # nothing is left open, and a close already in flight is skipped
            # (see flatten_all_positions), so a phone-queued sell that was
            # never claimed gets re-queued after it expires.
            #
            # ignore_dry_run=True: this is a protective stop, not an AI
            # trading decision, so it executes even with AI trading switched
            # off (including after the kill switch) — the same way
            # tasks.py's check_live_exits hard stop-loss already runs
            # regardless of ai_enabled. Otherwise a floor hit while AI is
            # off would leave every open position bleeding past it.
            if e.limit == "max_drawdown":
                self.store.note_drawdown_halt(self.account_id, str(e))
            if e.should_flatten:
                decisions.extend(
                    await self.flatten_all_positions(
                        state, RISK_CIRCUIT_BREAKER_REASON, detail=str(e), ignore_dry_run=True,
                    )
                )
            return decisions

        self._verdict = self._load_supervisor_verdict()
        for symbol in self.config.symbols:
            try:
                decision = await self._process_symbol(symbol, state)
                if decision is not None:
                    decisions.append(decision)
                    if decision.action != "NO_TRADE" or symbol not in self._quiet_symbols:
                        self.store.save_decision(decision, self.account_id)
            except Exception as e:  # noqa: BLE001 — one bad symbol must not kill the whole tick
                logger.exception("Error processing %s: %s", symbol, e)
                self.store.save_risk_event("symbol_processing_failed", "warning", str(e), {"symbol": symbol})

        return decisions

    async def _process_symbol(self, symbol: str, state: PortfolioState) -> Decision | None:
        candles = await self.exchange.get_candles(symbol, self.config.timeframe, self.config.candle_limit)
        if not candles:
            return None
        # The latest price, even when it comes from the still-forming candle
        # that the strategies below no longer look at — used as the entry
        # price for sizing and the stop.
        current_price = float(candles[-1].close)
        if self.config.closed_candles_only:
            candles = self._closed_candles(candles)
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
        regime = detect_regime(data.high, data.low, data.close, **self._regime_params())

        window = self.config.analysis_log_window_seconds
        if window is not None and (self._now() - self._candle_close_time(candles[-1])).total_seconds() > window:
            self._quiet_symbols.add(symbol)
        else:
            self._quiet_symbols.discard(symbol)

        signals = []
        for strategy in self.strategies:
            signal = strategy.generate_signal(symbol, data, regime)
            if signal is not None:
                signals.append(signal)
                if symbol not in self._quiet_symbols:
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
            estimated_transaction_cost=(self.config.round_trip_cost_by_symbol or {}).get(
                symbol, self.config.estimated_transaction_cost
            ),
            minimum_confidence=self.config.minimum_confidence,
            minimum_expected_edge=self.config.minimum_expected_edge,
        )

        decision.entry_regime = regime.market_regime.value

        if decision.action == "NO_TRADE":
            return decision

        if self.config.one_trade_per_candle and self.config.closed_candles_only:
            last_closed_at = self._candle_close_time(candles[-1])
            last_trade_at = state.last_signal_time_by_symbol.get(symbol)
            if last_trade_at is not None and _as_naive_utc(last_trade_at) >= last_closed_at:
                decision.action = "NO_TRADE"
                decision.reasons_against.append(
                    f"Already traded {symbol} since the last closed candle ({last_closed_at:%H:%M} UTC) — "
                    "waiting for a new candle before trading it again"
                )
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
            exit_ema = self.config.signal_exit_ema
            if exit_ema is not None and data.close[-1] >= ema(data.close, exit_ema)[-1]:
                decision.action = "NO_TRADE"
                decision.reasons_against.append(f"Sell signal ignored: price is still above its EMA{exit_ema}")
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
            if self.store.has_pending_order(self.account_id, symbol, "sell"):
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
            return await self._close_position(symbol, held_quantity, decision, state)

        if direction == "LONG":
            if len(decision.contributing_signals) < self.config.min_agreeing_strategies:
                decision.action = "NO_TRADE"
                decision.reasons_against.append(
                    f"Only {len(decision.contributing_signals)} strategy agrees — entries need "
                    f"{self.config.min_agreeing_strategies}"
                )
                return decision
            if self.config.entry_regimes is not None and regime.market_regime.value not in self.config.entry_regimes:
                decision.action = "NO_TRADE"
                decision.reasons_against.append(
                    f"Market regime {regime.market_regime.value} — new positions only in "
                    f"{', '.join(self.config.entry_regimes)}"
                )
                return decision

            allowed = [
                s for s in decision.contributing_signals
                if self._verdict.allows(s.strategy_name, regime.market_regime.value)
            ]
            if not allowed:
                decision.action = "NO_TRADE"
                decision.reasons_against.append(
                    f"Supervisor: these strategies recently lost money in {regime.market_regime.value}"
                )
                return decision

        entry = current_price
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

        # Same structural gap as the close path (see has_pending_order's
        # docstring), just on the BUY side: check_trade_against_limits
        # below only rejects a symbol that's already a CONFIRMED open
        # position — it has no idea about a buy that's already been
        # proposed to the phone but not yet executed/reported. Without
        # this, the exact same LONG signal firing again next tick (before
        # the first buy's fill comes back) queues a second, overlapping
        # buy for the same symbol — confirmed live on 2026-09-25 for both
        # WMATIC/USDC and AAVE/USDC: one buy went through, several more
        # queued in the following minutes each failed on-chain with
        # "Insufficient USDC" once the first had already spent it.
        if self.store.has_pending_order(self.account_id, symbol, "buy"):
            decision.action = "NO_TRADE"
            decision.reasons_against.append(
                "A buy for this symbol is already in flight (proposed but not yet "
                "executed/reported) — skipping to avoid a duplicate overlapping purchase"
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
            now=self._now(),
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
            confidence=decision.confidence,
            max_trade_risk=self.config.max_trade_risk * self._verdict.risk_multiplier,
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
                limit_price=entry,  # the phone refuses a swap far from this price
                stop_loss=plan.stop_loss, take_profit_1=plan.take_profit_1, take_profit_2=plan.take_profit_2,
                confidence=decision.confidence, reasons_for=decision.reasons_for, reasons_against=decision.reasons_against,
            )
            self.store.save_pending_execution(self.account_id, decision_id, planned)
            decision.executed = True
            self._reserve_entry_in_state(state, symbol, size.quantity, entry)
            return decision

        assert_live_order_allowed(
            exchange=self.exchange, trading_mode=self.config.trading_mode,
            allow_live_flag=self.config.allow_live_flag, confirm_live=(self.config.trading_mode == "live"),
        )
        side = Side.buy if direction == "LONG" else Side.sell
        order_result = await self.exchange.create_order(symbol, side, OrderType.market, size.quantity)
        self.store.save_order_result(order_result, decision_id, self.account_id)
        decision.executed = True
        self._reserve_entry_in_state(state, symbol, size.quantity, entry)
        return decision

    async def flatten_all_positions(
        self, state: PortfolioState, reason: str, *, detail: str = "", ignore_dry_run: bool = False,
    ) -> list[Decision]:
        """
        Closes EVERY position in state.open_positions — the "flatten" half
        of an account-wide circuit breaker (run_tick, on a RiskLimitBreach
        with should_flatten=True) and of the manual close-all endpoint
        (backend/app/api/emergency.py, via tasks.close_all_positions_for_account).

        state.open_positions already comes from SqlDataStore.get_portfolio_
        state, which reads simulation positions from AccountSettings.
        paper_balances/paper_positions and live ones from the `positions`
        table — so this works for both without re-deriving either. Each
        close goes through the same _close_position path as an AI-judged
        sell, so phone hand-off vs. direct order (and the paper exchange
        for simulation) are handled exactly as for any other sell.

        Deliberately runs NO risk check: this only ever reduces exposure,
        and must never be vetoed by the very breach it is resolving
        (check_account_wide_limits / check_trade_against_limits are not
        called anywhere on this path).

        ignore_dry_run: both callers (the circuit breaker in run_tick and
        the manual close-all button) pass True — closing exposure is a
        protective action that must happen even with AI trading switched
        off. Left False only gives the plain dry-run "would close" logging.

        One failed close must not block the others — each is isolated and
        a failure is logged as a risk event and skipped.
        """
        decisions: list[Decision] = []
        for symbol, position in list(state.open_positions.items()):
            quantity = position.get("quantity", 0.0)
            if quantity <= 0:
                continue
            # Same duplicate guard as the AI-judged close path: in phone
            # mode a sell queued on an earlier tick may not have been
            # claimed/reported yet, and the position is still "open" until
            # it is. Don't stack a second full-quantity sell on top of it.
            if self.store.has_pending_order(self.account_id, symbol, "sell"):
                logger.info("Flatten (%s): close for %s already in flight — skipping", reason, symbol)
                continue
            decision = Decision(
                symbol=symbol, action="SELL", direction="SHORT", confidence=1.0,
                score=ScoreBreakdown(
                    technical_score=0.0, momentum_score=0.0, regime_score=0.0, volume_score=0.0,
                    strategy_performance_score=0.0, volatility_penalty=0.0, correlation_penalty=0.0,
                    transaction_cost_penalty=0.0, final_score=0.0,
                ),
                reasons_for=[
                    f"[{reason}] Closing full {symbol} position ({quantity:.6f}) — not a strategy signal"
                    + (f": {detail}" if detail else "")
                ],
            )
            try:
                await self._close_position(symbol, quantity, decision, state, ignore_dry_run=ignore_dry_run)
            except Exception as e:  # noqa: BLE001 — one failed close must not block the others
                logger.exception("Flatten (%s): closing %s failed: %s", reason, symbol, e)
                decision.reasons_against.append(f"Close failed: {e}")
                self.store.save_risk_event(
                    "flatten_close_failed", "critical", f"{symbol}: {e}",
                    {"account_id": self.account_id, "symbol": symbol, "reason": reason},
                )
            self.store.save_decision(decision, self.account_id)
            decisions.append(decision)
        return decisions

    async def _close_position(
        self, symbol: str, quantity: float, decision: Decision, state: PortfolioState,
        *, ignore_dry_run: bool = False,
    ) -> Decision:
        """
        Closes a held position — at the AI's own initiative (called from
        _process_symbol when a SHORT signal lands on a symbol we already
        hold) or from flatten_all_positions. Mirrors the two execution
        paths BUY/entry already uses (phone hand-off vs. direct order) but
        always sells the FULL held quantity — there is no position-sizing
        step here, since sizing is a new-entry concept and this is an exit.
        """
        if self.config.dry_run and not ignore_dry_run:
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
                limit_price=await self._reference_price(symbol),
                # No stop_loss/take_profit_1/2 on a close — those describe
                # a NEW position's risk plan, which doesn't apply here.
                stop_loss=0.0, take_profit_1=0.0, take_profit_2=0.0,
                confidence=decision.confidence, reasons_for=decision.reasons_for,
                reasons_against=decision.reasons_against,
            )
            self.store.save_pending_execution(self.account_id, decision_id, planned)
            decision.executed = True
            self._release_position_in_state(state, symbol)
            return decision

        assert_live_order_allowed(
            exchange=self.exchange, trading_mode=self.config.trading_mode,
            allow_live_flag=self.config.allow_live_flag, confirm_live=(self.config.trading_mode == "live"),
        )
        order_result = await self.exchange.create_order(symbol, Side.sell, OrderType.market, quantity)
        self.store.save_order_result(order_result, decision_id, self.account_id)
        self.store.record_realized_trade(
            self.account_id, symbol, state.open_positions.get(symbol, {}).get("entry_price"), order_result,
        )
        decision.executed = True
        self._release_position_in_state(state, symbol)
        return decision

    def _reserve_entry_in_state(self, state: PortfolioState, symbol: str, quantity: float, entry_price: float) -> None:
        """
        BUG FIX: run_tick() fetches `state` ONCE per tick and reuses that
        same snapshot for every symbol in self.config.symbols — but every
        check above (duplicate-symbol, max_open_positions, correlation/
        exposure, cooldown_seconds, max_trades_per_hour,
        min_time_between_entries_seconds) reads straight off `state`. Without
        this, if several symbols in the SAME tick all signal a LONG, every
        one of them sees the SAME stale cash/open-positions/trade-count
        snapshot from the tick's start and independently concludes "I have
        room" — confirmed live in simulation on 2026-09-26: 8 symbols in one
        tick all sized a ~$2512 buy off the same starting equity, only ~2
        of them actually fit the real cash, and the other 6 blew up with
        InsufficientBalanceError once the (correctly enforced, real)
        exchange-level balance check caught what this stale state couldn't.
        That's the same shape of waste as the WMATIC/AAVE incident this
        codebase already fixed once (has_pending_order) — just between
        DIFFERENT symbols sharing one tick's capital instead of the SAME
        symbol proposed twice across ticks.
        Called right after decision.executed is set True (both the "phone"
        and direct-exchange paths) so every symbol processed LATER in this
        SAME tick sees the commitment this one just made — cheap, in-memory,
        and self-corrects next tick anyway once get_portfolio_state() re-reads
        the real, confirmed numbers from the database.
        """
        state.open_positions[symbol] = {
            "notional": quantity * entry_price, "quantity": quantity, "entry_price": entry_price,
        }
        state.cash = max(0.0, state.cash - quantity * entry_price)
        now = self._now()
        state.trades_this_hour += 1
        state.last_entry_time = now
        state.last_signal_time_by_symbol[symbol] = now

    def _release_position_in_state(self, state: PortfolioState, symbol: str) -> None:
        """
        Same reasoning as _reserve_entry_in_state, for the close side: a
        symbol closed earlier in this tick must stop showing up in
        state.open_positions for any symbol processed later in the same
        tick (duplicate-symbol / max_open_positions / correlation checks
        all read that dict directly). Cash is deliberately left untouched
        here rather than credited back an estimate — in "phone" mode the
        real fill price isn't known synchronously, and undercounting
        available cash only makes later same-tick checks more conservative,
        never less safe, which is the right direction to be wrong in on a
        live account.
        """
        state.open_positions.pop(symbol, None)
        state.last_signal_time_by_symbol[symbol] = self._now()

    def _timeframe_seconds(self) -> int:
        seconds = _TIMEFRAME_SECONDS.get(self.config.timeframe)
        if seconds is None:
            raise ValueError(f"Unknown timeframe {self.config.timeframe!r}")
        return seconds

    async def _reference_price(self, symbol: str) -> float | None:
        """Market price for the phone's price check. None skips that check,
        since a failed lookup must not stop a position from being closed."""
        try:
            return float((await self.exchange.get_ticker(symbol)).bid)
        except Exception as e:  # noqa: BLE001
            logger.warning("No reference price for %s: %s", symbol, e)
            return None

    def _load_supervisor_verdict(self) -> SupervisorVerdict:
        if not self.config.supervisor_enabled:
            return SupervisorVerdict()
        try:
            return evaluate_supervisor(self.store.recent_closed_trades(self.account_id), self._now())
        except Exception as e:  # noqa: BLE001 — trading continues on the unmodified rules
            logger.exception("Supervisor could not evaluate: %s", e)
            return SupervisorVerdict()

    def _regime_params(self) -> dict:
        """detect_regime() arguments for the configured candle size; see
        AgentConfig.timeframe_aware_regime."""
        if not self.config.timeframe_aware_regime:
            return {}
        hours = self._timeframe_seconds() / 3600
        return {
            "periods_per_year": round(365 * 24 / hours),
            "crash_window": max(1, round(24 / hours)),
        }

    def _candle_close_time(self, candle) -> datetime:
        return _as_naive_utc(candle.open_time) + timedelta(seconds=self._timeframe_seconds())

    def _closed_candles(self, candles: list) -> list:
        """Drops the trailing candle(s) that haven't closed yet."""
        now = self._now()
        end = len(candles)
        while end and self._candle_close_time(candles[end - 1]) > now:
            end -= 1
        return candles[:end]


def _as_naive_utc(ts: datetime) -> datetime:
    """Candle times arrive tz-aware (UTC); the rest of the engine uses naive UTC."""
    if ts.tzinfo is not None:
        return ts.astimezone(timezone.utc).replace(tzinfo=None)
    return ts
