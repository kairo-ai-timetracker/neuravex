from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import func

from backend.app.core.celery_app import celery_app
from backend.app.core.config import settings as global_settings
from backend.app.db.session import get_session
from backend.app.db.sql_data_store import SqlDataStore
from backend.app.models import models as m
from trading_engine.agent import AgentConfig, NeuravexAgent, PlannedOrder
from trading_engine.decision_engine import Decision, ScoreBreakdown
from trading_engine.execution.exchange_interface import OrderType, Side
from trading_engine.execution.paper_exchange import PaperExchange
from trading_engine.execution.public_market_data import PublicMarketDataExchange
from trading_engine.risk.exits import DEFAULT_SELL_COST_RATE, atr_stop_price, evaluate_exit
from trading_engine.strategies.breakout import BreakoutStrategy
from trading_engine.strategies.mean_reversion import MeanReversionStrategy
from trading_engine.strategies.momentum import MomentumStrategy
from trading_engine.strategies.performance import performance_score
from trading_engine.strategies.trend_following import TrendFollowingStrategy

logger = logging.getLogger("neuravex.tasks")

# Default coin universe for a brand-new account, before the user has
# picked their own list in Settings.
_DEFAULT_SYMBOLS = ["WMATIC/USDC", "WETH/USDC"]

# Hard, fixed backstop on the loss side for a single live position — NOT
# user-configurable (same "ik doe en denk zo weinig mogelijk" reasoning as
# AccountSettings.DEFAULT_*). This is deliberately NOT the AI's normal exit
# mechanism (that's agent.py's signal-driven close, which can and should
# exit earlier/smarter than this) — it's a pure noodrem: if a position's
# unrealized loss reaches this amount, it is sold immediately regardless of
# what the AI's own analysis says, in case that analysis is ever wrong,
# slow, or the tick pipeline hiccups. Expected to fire rarely, if ever.
_HARD_STOP_LOSS_USDC = -10.0

# Threshold for the portfolio-equity-movement alert (see
# check_equity_movement below) — a plain fixed number, not user-configurable
# for the same reason as the other constants here.
_EQUITY_ALERT_THRESHOLD_USDC = 5.0

# Candle timeframe the agent trades on — and the stop-loss ATR is measured
# on. 4h instead of 1h after replaying both against the old bot
# (scripts/replay_backtest.py, same coins, no fixed take-profit, no circuit
# breaker): 2026-07-28..09-26 +25.9% vs -8.5% (117 vs 599 orders), and the
# untouched 2026-09-26..10-08 window -2.8% vs -20.7%. Fewer, calmer
# signals; far less paid in fees and slippage.
_TIMEFRAME = "4h"
_TIMEFRAME_SECONDS = 4 * 3600
_CANDLE_LIMIT = 300


def _compute_strategy_performance(session) -> dict[str, float]:
    """
    Feeds decision_engine.score_signals()'s strategy_performance_score
    term (weight 0.15 in the final ensemble score) with real numbers for
    the first time — that term existed from the start, but nothing ever
    computed historical_win_rate, so every strategy scored a flat 0.0
    regardless of its actual track record. See execution.py's
    _update_strategy_performance for where these numbers actually get
    updated, the moment a live position closes.

    Deliberately win-rate only, not historical_expectancy — expectancy is
    raw USDC-per-unit-of-token, which sits on wildly different scales
    across symbols (a fraction of a WBTC unit vs. a whole unit of a
    $0.02 altcoin) and would swamp or vanish depending on which token a
    strategy happened to fire on, not how good it actually was. Win rate
    is scale-free: mapped from 0..1 to -1..1 around a neutral 50%, so a
    strategy winning more than half its closed trades nudges its future
    signals up, one winning less than half nudges them down — and
    decision_engine's own 0.15 weight on this term keeps it a nudge, not
    a veto, exactly like every other score component.
    """
    scores = {}
    for s in session.query(m.Strategy).all():
        score = performance_score(s.historical_win_rate or 0.0, s.closed_trades_count or 0)
        if score is not None:
            scores[s.name] = score
    return scores


def _get_or_create_settings(session, account: m.Account) -> m.AccountSettings:
    row = session.query(m.AccountSettings).filter_by(account_id=account.id).first()
    if row is None:
        row = m.AccountSettings(account_id=account.id, symbols=_DEFAULT_SYMBOLS)
        session.add(row)
        session.flush()
    return row


def _lock_simulation_settings(session, account_settings: m.AccountSettings) -> m.AccountSettings:
    """
    Row-locks (SELECT ... FOR UPDATE) a simulation account's settings row
    and re-reads it fresh. paper_balances/paper_positions are read at the
    start of a tick, traded on in memory, and written back at the end — so
    two writers overlapping (run_agent_tick and the manual close-all
    endpoint) would otherwise silently overwrite each other, e.g. a tick
    that started before a close-all writing the just-sold positions back.
    With the lock, the second writer waits and then starts from the
    first's committed balances. Held until the caller's transaction
    commits (run_agent_tick commits per account for exactly that reason).
    Live accounts don't need it: they keep no in-memory balances.
    """
    if not account_settings.paper_trading_enabled:
        return account_settings
    return (
        session.query(m.AccountSettings)
        .filter_by(account_id=account_settings.account_id)
        .with_for_update()
        .populate_existing()
        .one()
    )


async def _build_paper_exchange(
    symbols: list[str], starting_balances: dict[str, float], starting_cost_basis: dict[str, float],
) -> tuple[PaperExchange, dict[str, float]]:
    """
    Seeds a fresh PaperExchange with real market data (candles + last
    price) pulled from the same keyless PublicMarketDataExchange used for
    live analysis — simulation mode still reasons over real prices, only
    the execution side is fictional. Returns the exchange plus a
    symbol -> last_price map, so the caller can mark open simulated
    positions to market afterwards without a second round of API calls.
    """
    market_data = PublicMarketDataExchange()
    # max_price_age_seconds raised from the default 30s: a tick over this
    # many symbols takes ~30-40s end to end (one HTTP call per symbol,
    # sequentially), so the default was tight enough to occasionally
    # flag a price fetched earlier in the same tick as "stale" by the
    # time a later symbol's trade tried to execute against it.
    paper = PaperExchange(
        starting_balances=starting_balances, max_price_age_seconds=180,
        starting_cost_basis=starting_cost_basis,
    )
    last_prices: dict[str, float] = {}
    for symbol in symbols:
        try:
            candles = await market_data.get_candles(symbol, _TIMEFRAME, _CANDLE_LIMIT)
            if candles:
                paper.load_candles(symbol, _TIMEFRAME, candles)
                last_price = candles[-1].close
                paper.update_price(symbol, last_price)
                last_prices[symbol] = last_price
        except Exception as e:  # noqa: BLE001 — one bad symbol must not block the others
            logger.warning("Could not load market data for %s (paper mode): %s", symbol, e)
    return paper, last_prices


def _exit_decision(symbol: str, reason: str, detail: str) -> Decision:
    """An executed SELL decision for a rule-based exit (stop-loss /
    take-profit), so it shows up in the decision history like any other
    trade AND counts for the cooldown / one-trade-per-candle checks.
    Previously these sells left no ai_decisions row at all, so the agent
    happily re-bought the same symbol a minute after every take-profit."""
    return Decision(
        symbol=symbol, action="SELL", direction="SHORT", confidence=1.0,
        score=ScoreBreakdown(0, 0, 0, 0, 0, 0, 0, 0, 0),
        reasons_for=[f"[{reason}] {detail}"],
    )


def _closed_candles(candles: list, timeframe_seconds: int = _TIMEFRAME_SECONDS) -> list:
    now = datetime.now(timezone.utc)
    return [c for c in candles if c.open_time + timedelta(seconds=timeframe_seconds) <= now]


async def _apply_simulated_exits(
    exchange: PaperExchange, store: SqlDataStore, account_id: str,
    last_prices: dict[str, float], take_profit_usdc: float | None,
) -> None:
    """
    Per-position exits for SIMULATION, run after the normal tick so it
    also sees positions that tick just opened:
      - stop-loss: 1.5 x ATR below the cost basis (the stop agent.py
        already computes at entry but never enforced), and
      - the user's per-trade take-profit (max_balance_target), now judged
        on profit NET of the sell's own fee + slippage.
    See trading_engine/risk/exits.py for both rules. Each other position
    keeps running independently.
    """
    import numpy as np

    balances = exchange.get_all_balances()
    cost_basis = exchange.get_cost_basis()
    for asset, quantity in list(balances.items()):
        if asset == "USDC" or quantity <= 0:
            continue
        symbol = f"{asset}/USDC"
        entry_price = cost_basis.get(asset)
        price = last_prices.get(symbol)
        if entry_price is None or price is None:
            continue
        candles = _closed_candles(await exchange.get_candles(symbol, _TIMEFRAME, _CANDLE_LIMIT))
        stop = atr_stop_price(
            entry_price, np.array([c.high for c in candles]), np.array([c.low for c in candles]),
            np.array([c.close for c in candles]),
        )
        exit_ = evaluate_exit(
            quantity=quantity, cost_basis=entry_price, price=price, stop_price=stop,
            take_profit_usdc=take_profit_usdc,
        )
        if exit_ is None:
            continue
        try:
            order_result = await exchange.create_order(symbol, Side.sell, OrderType.market, quantity)
            decision = _exit_decision(symbol, exit_.reason, exit_.detail)
            decision.executed = True
            decision_id = store.save_decision(decision, account_id)
            store.save_order_result(order_result, decision_id, account_id)
            store.record_realized_trade(account_id, symbol, entry_price, order_result)
            logger.info("SIMULATION %s: sold %.6f %s — %s", exit_.reason, quantity, asset, exit_.detail)
        except Exception as e:  # noqa: BLE001 — one failed close must not block others
            logger.warning("Simulated %s sell failed for %s: %s", exit_.reason, asset, e)


def _save_paper_snapshot(store: SqlDataStore, account_id: str, balances: dict[str, float], last_prices: dict[str, float]) -> None:
    """
    Marks every non-USDC simulated balance to its last known market price
    and writes a PortfolioSnapshot — the exact same table/shape
    report-balance (the real-wallet path) writes to, so the dashboard
    needs no separate code path to show simulated vs. real equity.
    """
    equity = balances.get("USDC", 0.0)
    for asset, amount in balances.items():
        if asset == "USDC" or amount <= 0:
            continue
        price = next((p for sym, p in last_prices.items() if sym.startswith(f"{asset}/")), None)
        if price is not None:
            equity += amount * price

    # BUG FIX (mirrors the equivalent fix in dashboard.py's report_balance,
    # the live-wallet path): drawdown used to be hardcoded to 0.0 here, so
    # a simulation account's dashboard always showed 0% drawdown no matter
    # what — even while the SAME account-wide circuit breaker a live
    # account can hit (trading_engine/risk/risk_engine.py's
    # check_account_wide_limits, fed by SqlDataStore.get_portfolio_state's
    # true historical peak_equity — run_agent_tick calls this for
    # simulation accounts exactly the same as live ones) was tripped and
    # silently halting this account's own trading. Both paths must agree
    # on the same true-historical-peak definition of drawdown, not one
    # hardcoded/partial and the other correct.
    historical_peak_equity = (
        store.session.query(func.max(m.PortfolioSnapshot.equity))
        .filter_by(account_id=account_id, is_simulated=True)
        .scalar()
    )
    peak_equity = max(equity, historical_peak_equity) if historical_peak_equity else equity
    drawdown = (peak_equity - equity) / peak_equity if peak_equity else 0.0

    store.session.add(m.PortfolioSnapshot(
        account_id=account_id, equity=equity, cash=balances.get("USDC", 0.0),
        exposure=0.0, drawdown=drawdown, open_positions=sum(1 for a, v in balances.items() if a != "USDC" and v > 0),
        market_regime="SIDEWAYS", taken_at=datetime.utcnow(), is_simulated=True,
    ))


def _build_agent_for_account(
    session, account: m.Account, account_settings: m.AccountSettings,
) -> tuple[NeuravexAgent, SqlDataStore, object, dict[str, float]]:
    """
    Builds the store/exchange/agent for one account exactly as a normal
    tick runs it — shared by run_agent_tick and close_all_positions_for_
    account (the manual close-all endpoint), so a manual close executes
    through the very same simulation-vs-live and phone-vs-server paths as
    an automatic one. Returns (agent, store, exchange, last_prices);
    last_prices is empty for live accounts.
    """
    store = SqlDataStore(
        session, simulated=account_settings.paper_trading_enabled,
        fallback_equity=(
            account_settings.paper_starting_balance if account_settings.paper_trading_enabled else None
        ),
    )
    strategies = [
        TrendFollowingStrategy(), MomentumStrategy(),
        MeanReversionStrategy(), BreakoutStrategy(),
    ]

    last_prices: dict[str, float] = {}
    if account_settings.paper_trading_enabled:
        starting_balances = account_settings.paper_balances or {"USDC": account_settings.paper_starting_balance}
        logger.info(
            "Account %s is in SIMULATION mode — seeding paper exchange from: %s",
            account.id, starting_balances,
        )
        # Every HELD asset needs a price too, not just the configured coin
        # list: a coin the user has since removed from their list can still
        # be held, and the paper exchange refuses to sell (and the snapshot
        # can't value) anything it has no price for — which would leave
        # such a position impossible to close by take-profit, circuit
        # breaker or the close-all button.
        price_symbols = list(dict.fromkeys([
            *(sym for sym in (account_settings.symbols or []) if sym.endswith("/USDC")),
            *(f"{asset}/USDC" for asset, qty in starting_balances.items() if asset != "USDC" and qty > 0),
        ]))
        exchange, last_prices = asyncio.run(
            _build_paper_exchange(
                price_symbols, starting_balances,
                account_settings.paper_positions,
            )
        )
        execution_mode = "server"
        trading_mode = "paper"
        # The simulated wallet only holds USDC, so a buy on an X/USDT pair
        # always failed with InsufficientBalanceError — ~5,800
        # symbol_processing_failed events, every one a wasted analysis.
        # Simulation trades the USDC pairs only; the USDT ones remain in
        # the user's list for live use.
        trade_symbols = [sym for sym in (account_settings.symbols or []) if sym.endswith("/USDC")]
        skipped = sorted(set(account_settings.symbols or []) - set(trade_symbols))
        if skipped:
            logger.info("Account %s SIMULATION: skipping non-USDC pairs %s", account.id, skipped)
        # Previously forced to None here on the reasoning that
        # "the floor governs real money" — but the Settings screen
        # shows this as a plain "stop trading when my balance
        # reaches" field with no mention of live-vs-simulation, so
        # a user testing a strategy in simulation reasonably
        # expects their own configured stop to actually apply.
        # (The profit side is per-trade now, not account-wide —
        # see the take-profit sweep, not this field.)
        min_balance_floor = account_settings.min_balance_floor
    else:
        exchange = PublicMarketDataExchange()
        execution_mode = account_settings.execution_mode
        trading_mode = account.mode.value
        min_balance_floor = account_settings.min_balance_floor
        trade_symbols = account_settings.symbols or []

    config = AgentConfig(
        symbols=trade_symbols,
        timeframe=_TIMEFRAME, candle_limit=_CANDLE_LIMIT,
        # Fixed system defaults — not user-configurable by design.
        # See AccountSettings' class docstring for why.
        max_trade_risk=m.AccountSettings.DEFAULT_MAX_TRADE_RISK,
        max_position_size=m.AccountSettings.DEFAULT_MAX_POSITION_SIZE,
        max_portfolio_exposure=m.AccountSettings.DEFAULT_MAX_PORTFOLIO_EXPOSURE,
        max_daily_loss=m.AccountSettings.DEFAULT_MAX_DAILY_LOSS,
        max_drawdown=global_settings.risk.max_drawdown,
        min_balance_floor=min_balance_floor,
        max_open_positions=global_settings.risk.max_open_positions,
        minimum_confidence=global_settings.strategy.minimum_confidence,
        minimum_expected_edge=global_settings.strategy.minimum_expected_edge,
        cooldown_seconds=global_settings.strategy.cooldown_seconds,
        max_trades_per_hour=global_settings.strategy.max_trades_per_hour,
        min_time_between_entries_seconds=global_settings.strategy.min_time_between_entries_seconds,
        trading_mode=trading_mode,
        allow_live_flag=global_settings.allow_live,
        execution_mode=execution_mode,
        dry_run=not account_settings.ai_enabled,
    )
    agent = NeuravexAgent(
        exchange=exchange, strategies=strategies, store=store,
        config=config, account_id=account.id,
        strategy_performance=_compute_strategy_performance(session),
    )
    return agent, store, exchange, last_prices


def _persist_paper_state(
    store: SqlDataStore, account_id: str, account_settings: m.AccountSettings,
    exchange: PaperExchange, last_prices: dict[str, float],
) -> None:
    """Writes a simulation account's in-memory PaperExchange balances back
    to AccountSettings and records a PortfolioSnapshot — the only way any
    simulated fill (buy, sell, take-profit, flatten) becomes permanent."""
    updated_balances = exchange.get_all_balances()
    account_settings.paper_balances = updated_balances
    account_settings.paper_positions = exchange.get_cost_basis()
    _save_paper_snapshot(store, account_id, updated_balances, last_prices)
    logger.info(
        "SIMULATION snapshot saved for account %s — balances now: %s",
        account_id, updated_balances,
    )


def close_all_positions_for_account(session, account: m.Account, reason: str = "manual_close_all") -> dict:
    """
    Really closes every open position for one account — the backend of
    the manual POST /api/emergency/{account_id}/close-all button. Same
    underlying helper as the automatic circuit breaker
    (NeuravexAgent.flatten_all_positions), built through the same
    _build_agent_for_account as a normal tick, so:
      - simulation: sells on the PaperExchange and persists the new
        paper_balances/paper_positions + snapshot;
      - live, phone mode: queues a full-quantity sell PendingExecution
        per position for the phone to execute (the position is marked
        closed only when the phone reports the fill, via execution.py's
        _apply_fill_to_position — never assumed);
      - live, server mode: places the order through the configured
        adapter (the keyless PublicMarketDataExchange refuses, which is
        reported back as a failure rather than silently "closed").
    ignore_dry_run=True: the user explicitly pressed the button, so this
    executes even with AI trading switched off.
    """
    account_settings = _lock_simulation_settings(session, _get_or_create_settings(session, account))
    agent, store, exchange, last_prices = _build_agent_for_account(session, account, account_settings)
    state = store.get_portfolio_state(account.id)
    open_before = sorted(state.open_positions)
    decisions = asyncio.run(agent.flatten_all_positions(state, reason, ignore_dry_run=True))
    if account_settings.paper_trading_enabled:
        _persist_paper_state(store, account.id, account_settings, exchange, last_prices)

    attempted = {d.symbol for d in decisions}
    return {
        "execution": "simulation" if account_settings.paper_trading_enabled else agent.config.execution_mode,
        "open_positions": open_before,
        "submitted": sorted(d.symbol for d in decisions if d.executed),
        "failed": sorted(d.symbol for d in decisions if not d.executed),
        # Already had a sell in flight (pending/claimed) — not re-queued.
        "already_in_flight": sorted(sym for sym in open_before if sym not in attempted),
    }


@celery_app.task(name="backend.app.core.tasks.run_agent_tick")
def run_agent_tick() -> str:
    """
    Entry point invoked by Celery beat, once per account.

    The user controls almost nothing here directly (spec: "ik doe en denk
    zo weinig mogelijk") — just their coin list, an absolute balance-floor
    stop, the on/off switch (AccountSettings.ai_enabled), and now also
    live-vs-simulation (AccountSettings.paper_trading_enabled) with a
    fictional starting balance. Every other risk parameter (per-trade risk
    %, position sizing, exposure caps) is a fixed, conservative system
    default (see AccountSettings.DEFAULT_* and the global YAML config).

    Two execution paths, chosen per-account by paper_trading_enabled:
    - Simulation (default, the safe starting point): trades run against a
      PaperExchange seeded from AccountSettings.paper_balances — no real
      wallet or exchange credential is ever touched. Results persist as
      PortfolioSnapshot rows exactly like the real-wallet path, so
      Settings/Dashboard need no special-casing to show them.
    - Live: unchanged from before this feature — market data comes from
      PublicMarketDataExchange (keyless, read-only) and this process
      NEVER holds a trading credential:
      - execution_mode == "phone" (the default): approved trades are
        queued as PendingExecution rows; the Android app executes them
        with its own locally-held credential (exchange key or wallet key).
      - execution_mode == "server": would require swapping in a real
        credentialed adapter here — a deliberate, reviewed code change,
        not something this task does automatically. See docs/SAFETY.md.
    """
    with get_session() as session:
        accounts = session.query(m.Account).all()
        for account in accounts:
            account_settings = _get_or_create_settings(session, account)

            if not account_settings.symbols:
                logger.debug("No coins selected for account %s — skipping tick", account.id)
                continue
            # NOTE: deliberately NOT skipping when ai_enabled is False —
            # the tick still runs the full analysis (regime, strategies,
            # decision engine, risk checks) and logs every Signal/AIDecision
            # exactly as normal, so the dashboard's "why did NEURAVEX
            # decide this" is populated with real reasoning even before
            # the user has switched AI trading on. Only the final
            # execution step is suppressed — see AgentConfig.dry_run and
            # NeuravexAgent's handling of it.

            account_settings = _lock_simulation_settings(session, account_settings)
            agent, store, exchange, last_prices = _build_agent_for_account(session, account, account_settings)
            try:
                decisions = asyncio.run(agent.run_tick())
                logger.info("Tick complete for account %s: %d decisions", account.id, len(decisions))
                if account_settings.paper_trading_enabled:
                    # Runs AFTER the normal tick's buy decisions, so a
                    # position opened by this very tick can still be
                    # closed by its own take-profit in the same tick
                    # (e.g. a large favorable price move) rather than
                    # waiting a full 5-minute cycle.
                    asyncio.run(
                        _apply_simulated_exits(
                            exchange, store, account.id, last_prices, account_settings.max_balance_target,
                        )
                    )
                    _persist_paper_state(store, account.id, account_settings, exchange, last_prices)
            except Exception as e:  # noqa: BLE001 — one bad account must not kill the whole beat run
                logger.exception("Agent tick failed for account %s: %s", account.id, e)
                store.save_risk_event("agent_tick_failed", "warning", str(e), {"account_id": account.id})

            # Commit per account (previously one commit for the whole beat
            # run): releases this account's settings row lock (see
            # _lock_simulation_settings) as soon as its tick is done, so a
            # close-all waits for at most one account's tick, not all of
            # them. A failed commit only loses this account's tick.
            try:
                session.commit()
            except Exception as e:  # noqa: BLE001
                session.rollback()
                logger.exception("Commit failed for account %s tick: %s", account.id, e)

    return "ok"


@celery_app.task(name="backend.app.core.tasks.check_live_exits")
def check_live_exits() -> str:
    """
    Fast, lightweight companion to run_agent_tick, on its own much shorter
    beat schedule (see celery_app.py — every ~30s vs. the main tick's
    1-minute analysis cycle). This is the LIVE counterpart of
    _apply_simulated_exits, which only ever ran for simulation —
    live accounts had no per-trade profit target enforcement at all until
    this task existed (see backend/app/api/execution.py's
    _apply_fill_to_position for the other missing half: the `positions`
    table this reads from used to never get written to for live trades).

    Deliberately narrow in scope: it does NOT run strategy analysis, fetch
    candles, or open new positions — only the account's OWN already-open
    positions are priced and checked. That keeps it cheap enough to run
    every 30 seconds without hammering the public market-data API, while
    still closing a profitable position within seconds of its target being
    hit rather than waiting up to a full analysis cycle.
    """
    with get_session() as session:
        accounts = session.query(m.Account).all()
        exchange = PublicMarketDataExchange()
        for account in accounts:
            account_settings = _get_or_create_settings(session, account)
            if account_settings.paper_trading_enabled:
                continue  # simulation's own sweep already runs inside run_agent_tick
            # NOTE: no longer skipping accounts without a take-profit target —
            # that `continue` also silently disabled the stop-loss checks
            # below for every such account.

            store = SqlDataStore(session, simulated=False)
            open_positions = (
                session.query(m.Position)
                .filter_by(account_id=account.id, status=m.PositionStatus.open)
                .all()
            )
            for position in open_positions:
                # A close for this exact symbol may already be in flight —
                # proposed to the phone but not yet executed/reported.
                # Skip it rather than queueing a second, overlapping sell
                # every 30 seconds until the first one clears.
                already_pending = (
                    session.query(m.PendingExecution)
                    .filter_by(account_id=account.id, symbol=position.symbol, status="pending")
                    .filter(m.PendingExecution.side == m.OrderSide.sell)
                    .first()
                )
                if already_pending:
                    continue
                try:
                    ticker = asyncio.run(exchange.get_ticker(position.symbol))
                except Exception as e:  # noqa: BLE001 — one bad symbol must not block the others
                    logger.warning("Live exit check: could not price %s for account %s: %s", position.symbol, account.id, e)
                    continue

                unrealized_profit = (ticker.bid - position.entry_price) * position.quantity
                # Same rule as simulation (trading_engine/risk/exits.py): the
                # ATR stop recorded at entry (Position.stop_loss, from the
                # PendingExecution's risk plan) and the take-profit judged on
                # profit net of the sell's costs. position.entry_price is the
                # raw fill price, so the buy-side cost is added back in too.
                exit_ = evaluate_exit(
                    quantity=position.quantity,
                    cost_basis=position.entry_price * (1 + DEFAULT_SELL_COST_RATE),
                    price=ticker.bid,
                    stop_price=position.stop_loss or None,
                    take_profit_usdc=account_settings.max_balance_target,
                )
                if exit_ is not None:
                    reason_text = f"[{exit_.reason}] Live {exit_.reason.replace('_', '-')}: {exit_.detail}"
                    log = logger.warning if exit_.reason == "stop_loss" else logger.info
                    log("Live %s for account %s %s: %s", exit_.reason, account.id, position.symbol, exit_.detail)
                elif unrealized_profit <= _HARD_STOP_LOSS_USDC:
                    # Pure noodrem — see _HARD_STOP_LOSS_USDC's comment.
                    # Backstop for positions without a recorded stop, or if
                    # the stop itself was set too wide.
                    reason_text = (
                        f"Hard stop-loss backstop: unrealized loss "
                        f"{unrealized_profit:.2f} USDC <= {_HARD_STOP_LOSS_USDC:.2f} USDC"
                    )
                    logger.warning(
                        "Hard stop-loss triggered for account %s %s: unrealized %.2f <= %.2f",
                        account.id, position.symbol, unrealized_profit, _HARD_STOP_LOSS_USDC,
                    )
                else:
                    continue
                store.save_pending_execution(
                    account.id, None,
                    PlannedOrder(
                        symbol=position.symbol, side="sell", quantity=position.quantity,
                        stop_loss=0.0, take_profit_1=0.0, take_profit_2=0.0, confidence=1.0,
                        reasons_for=[reason_text], reasons_against=[],
                    ),
                )

    return "ok"


@celery_app.task(name="backend.app.core.tasks.check_equity_movement")
def check_equity_movement() -> str:
    """
    Fires a portfolio-equity-movement notification (email now, in-app push
    once the phone polls for it — see /api/notifications) whenever a live
    account's total equity has moved by _EQUITY_ALERT_THRESHOLD_USDC or more
    since the last alert. Deliberately simple: one running "last alerted
    equity" baseline per account (the most recent PendingNotification of
    kind "equity_move"), not a fixed schedule or percentage — so a fast
    move triggers as soon as this task next runs (every 2 minutes, see
    celery_app.py) rather than waiting for a calendar boundary.

    Simulation accounts are skipped entirely — this is about real money
    moving, not a fictional balance.
    """
    from trading_engine.alerts import send_email

    with get_session() as session:
        accounts = session.query(m.Account).all()
        for account in accounts:
            account_settings = _get_or_create_settings(session, account)
            if account_settings.paper_trading_enabled:
                continue

            latest_snapshot = (
                session.query(m.PortfolioSnapshot)
                .filter_by(account_id=account.id, is_simulated=False)
                .order_by(m.PortfolioSnapshot.taken_at.desc())
                .first()
            )
            if latest_snapshot is None or latest_snapshot.equity <= 0:
                continue
            current_equity = latest_snapshot.equity

            # PendingNotification.account_id is a plain varchar with no FK
            # (see its model docstring — accounts.id is a native uuid
            # column in production, so a real FK there fails to create).
            # account.id, read straight off that uuid column, comes back
            # as an actual uuid.UUID object, and Postgres has no implicit
            # "character varying = uuid" operator — comparing or inserting
            # it as-is against this table raises psycopg2.UndefinedFunction.
            # Every other table here still has a real FK to accounts.id,
            # so their (also uuid, despite the String(36) declared here)
            # live columns compare fine — this str() is needed for
            # PendingNotification specifically, not those.
            account_id_str = str(account.id)

            last_alert = (
                session.query(m.PendingNotification)
                .filter_by(account_id=account_id_str, kind="equity_move")
                .order_by(m.PendingNotification.created_at.desc())
                .first()
            )
            baseline = last_alert.meta.get("equity") if last_alert else None
            if baseline is None:
                # First run ever for this account — establish a baseline
                # silently (pre-marked delivered so the app never sees it
                # as a notification to show).
                session.add(m.PendingNotification(
                    account_id=account_id_str, kind="equity_move", title="", body="",
                    meta={"equity": current_equity}, delivered_at=datetime.utcnow(),
                ))
                continue

            delta = current_equity - baseline
            if abs(delta) < _EQUITY_ALERT_THRESHOLD_USDC:
                continue

            direction = "gestegen" if delta > 0 else "gedaald"
            title = f"NEURAVEX: portfolio {direction}"
            body = f"Je portfolio is €{abs(delta):.2f} {direction} sinds de laatste melding, nu €{current_equity:.2f}."
            session.add(m.PendingNotification(
                account_id=account_id_str, kind="equity_move", title=title, body=body,
                meta={"equity": current_equity},
            ))
            logger.info(
                "Equity movement alert for account %s: %+.2f -> %.2f (threshold %.2f)",
                account.id, delta, current_equity, _EQUITY_ALERT_THRESHOLD_USDC,
            )
            send_email(title, body)

    return "ok"
