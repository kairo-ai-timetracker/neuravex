from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from backend.app.core.celery_app import celery_app
from backend.app.core.config import settings as global_settings
from backend.app.db.session import get_session
from backend.app.db.sql_data_store import SqlDataStore
from backend.app.models import models as m
from trading_engine.agent import AgentConfig, NeuravexAgent, PlannedOrder
from trading_engine.execution.paper_exchange import PaperExchange
from trading_engine.execution.public_market_data import PublicMarketDataExchange
from trading_engine.strategies.breakout import BreakoutStrategy
from trading_engine.strategies.mean_reversion import MeanReversionStrategy
from trading_engine.strategies.momentum import MomentumStrategy
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

# A strategy needs at least this many CLOSED, attributed trades (see
# Strategy.closed_trades_count, updated by execution.py's
# _update_strategy_performance) before its win rate is trusted at all in
# _compute_strategy_performance below. Below this, one lucky or unlucky
# early trade could swing a brand-new strategy's win rate straight to 0%
# or 100% — it's treated as unproven (neutral, 0.0) instead, same as
# before this feature existed.
_MIN_TRADES_FOR_PERFORMANCE_WEIGHTING = 5


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
    rows = (
        session.query(m.Strategy)
        .filter(m.Strategy.closed_trades_count >= _MIN_TRADES_FOR_PERFORMANCE_WEIGHTING)
        .all()
    )
    return {s.name: max(-1.0, min(1.0, 2 * (s.historical_win_rate - 0.5))) for s in rows}


def _get_or_create_settings(session, account: m.Account) -> m.AccountSettings:
    row = session.query(m.AccountSettings).filter_by(account_id=account.id).first()
    if row is None:
        row = m.AccountSettings(account_id=account.id, symbols=_DEFAULT_SYMBOLS)
        session.add(row)
        session.flush()
    return row


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
            candles = await market_data.get_candles(symbol, "1h", 200)
            if candles:
                paper.load_candles(symbol, "1h", candles)
                last_price = candles[-1].close
                paper.update_price(symbol, last_price)
                last_prices[symbol] = last_price
        except Exception as e:  # noqa: BLE001 — one bad symbol must not block the others
            logger.warning("Could not load market data for %s (paper mode): %s", symbol, e)
    return paper, last_prices


async def _apply_per_trade_take_profit(
    exchange: PaperExchange, store: SqlDataStore, account_id: str,
    last_prices: dict[str, float], take_profit_usdc: float | None,
) -> None:
    """
    Per-trade take-profit sweep (spec, redesigned from an account-wide
    target: "ik wil een algemeen getal invullen... als de winst op die
    trade €50 is dan stopt hij"). Runs AFTER the normal buy-decision tick,
    so it sees this tick's own new positions too, not just older ones.

    For every asset currently held with a known cost basis, if its own
    unrealized profit (current value minus what was paid for it,
    including the original buy fee) has reached take_profit_usdc, that
    ONE position is sold — others keep running independently. This is
    intentionally separate from min_balance_floor, which stays
    account-wide on the loss side (the user's explicit choice).

    The trigger compares against unrealized profit BEFORE the sell's own
    fee and slippage are deducted — the actual amount credited back to
    USDC will be a small fraction (the taker fee, ~0.1%) below the
    configured target, not exactly equal to it. Not worth estimating
    ahead of the real fill just to chase an exact number.
    """
    if take_profit_usdc is None:
        return
    from trading_engine.execution.exchange_interface import OrderType, Side

    balances = exchange.get_all_balances()
    cost_basis = exchange.get_cost_basis()
    for asset, quantity in list(balances.items()):
        if asset == "USDC" or quantity <= 0:
            continue
        entry_price = cost_basis.get(asset)
        price = next((p for sym, p in last_prices.items() if sym.startswith(f"{asset}/")), None)
        if entry_price is None or price is None:
            continue
        unrealized_profit = (price - entry_price) * quantity
        if unrealized_profit >= take_profit_usdc:
            try:
                order_result = await exchange.create_order(f"{asset}/USDC", Side.sell, OrderType.market, quantity)
                # Previously missing entirely: the sell updated the
                # simulated balances but was never written to the orders
                # table, so it left no permanent record — invisible to
                # any future trade history, and unavailable to the
                # eventual learning-from-past-trades work discussed
                # earlier. Recorded exactly the same way a normal
                # buy/sell decision is (see agent.py's execution paths).
                store.save_order_result(order_result, None, account_id)
                logger.info(
                    "SIMULATION take-profit: sold %.6f %s at unrealized profit %.2f USDC (target %.2f)",
                    quantity, asset, unrealized_profit, take_profit_usdc,
                )
            except Exception as e:  # noqa: BLE001 — one failed close must not block others
                logger.warning("Take-profit sell failed for %s: %s", asset, e)


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
    store.session.add(m.PortfolioSnapshot(
        account_id=account_id, equity=equity, cash=balances.get("USDC", 0.0),
        exposure=0.0, drawdown=0.0, open_positions=sum(1 for a, v in balances.items() if a != "USDC" and v > 0),
        market_regime="SIDEWAYS", taken_at=datetime.utcnow(), is_simulated=True,
    ))


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
            store = SqlDataStore(
                session, simulated=account_settings.paper_trading_enabled,
                fallback_equity=(
                    account_settings.paper_starting_balance if account_settings.paper_trading_enabled else None
                ),
            )

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

            strategies = [
                TrendFollowingStrategy(), MomentumStrategy(),
                MeanReversionStrategy(), BreakoutStrategy(),
            ]

            if account_settings.paper_trading_enabled:
                starting_balances = account_settings.paper_balances or {"USDC": account_settings.paper_starting_balance}
                logger.info(
                    "Account %s is in SIMULATION mode — seeding paper exchange from: %s",
                    account.id, starting_balances,
                )
                exchange, last_prices = asyncio.run(
                    _build_paper_exchange(
                        account_settings.symbols, starting_balances,
                        account_settings.paper_positions,
                    )
                )
                execution_mode = "server"
                trading_mode = "paper"
                # Previously forced to None here on the reasoning that
                # "the floor governs real money" — but the Settings screen
                # shows this as a plain "stop trading when my balance
                # reaches" field with no mention of live-vs-simulation, so
                # a user testing a strategy in simulation reasonably
                # expects their own configured stop to actually apply.
                # (The profit side is per-trade now, not account-wide —
                # see the take-profit sweep below, not this field.)
                min_balance_floor = account_settings.min_balance_floor
            else:
                exchange = PublicMarketDataExchange()
                execution_mode = account_settings.execution_mode
                trading_mode = account.mode.value
                min_balance_floor = account_settings.min_balance_floor

            config = AgentConfig(
                symbols=account_settings.symbols,
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
                        _apply_per_trade_take_profit(
                            exchange, store, account.id, last_prices, account_settings.max_balance_target,
                        )
                    )
                    updated_balances = exchange.get_all_balances()
                    account_settings.paper_balances = updated_balances
                    account_settings.paper_positions = exchange.get_cost_basis()
                    _save_paper_snapshot(store, account.id, updated_balances, last_prices)
                    logger.info(
                        "SIMULATION snapshot saved for account %s — balances now: %s",
                        account.id, updated_balances,
                    )
            except Exception as e:  # noqa: BLE001 — one bad account must not kill the whole beat run
                logger.exception("Agent tick failed for account %s: %s", account.id, e)
                store.save_risk_event("agent_tick_failed", "warning", str(e), {"account_id": account.id})

    return "ok"


@celery_app.task(name="backend.app.core.tasks.check_live_exits")
def check_live_exits() -> str:
    """
    Fast, lightweight companion to run_agent_tick, on its own much shorter
    beat schedule (see celery_app.py — every ~30s vs. the main tick's
    1-minute analysis cycle). This is the LIVE counterpart of
    _apply_per_trade_take_profit, which only ever ran for simulation —
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
            if not account_settings.max_balance_target:
                continue  # no per-trade profit target configured — nothing to check

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
                if unrealized_profit >= account_settings.max_balance_target:
                    store.save_pending_execution(
                        account.id, None,
                        PlannedOrder(
                            symbol=position.symbol, side="sell", quantity=position.quantity,
                            stop_loss=0.0, take_profit_1=0.0, take_profit_2=0.0, confidence=1.0,
                            reasons_for=[
                                f"Live per-trade take-profit: unrealized profit "
                                f"{unrealized_profit:.2f} USDC >= target {account_settings.max_balance_target:.2f} USDC"
                            ],
                            reasons_against=[],
                        ),
                    )
                    logger.info(
                        "Live take-profit triggered for account %s %s: unrealized %.2f >= target %.2f",
                        account.id, position.symbol, unrealized_profit, account_settings.max_balance_target,
                    )
                elif unrealized_profit <= _HARD_STOP_LOSS_USDC:
                    # Pure noodrem — see _HARD_STOP_LOSS_USDC's comment.
                    # This is deliberately separate from the AI's own
                    # signal-driven close (agent.py's SHORT-on-held-symbol
                    # path), which is expected to exit earlier/smarter than
                    # this in almost every case; this only fires as a
                    # backstop if that hasn't happened.
                    store.save_pending_execution(
                        account.id, None,
                        PlannedOrder(
                            symbol=position.symbol, side="sell", quantity=position.quantity,
                            stop_loss=0.0, take_profit_1=0.0, take_profit_2=0.0, confidence=1.0,
                            reasons_for=[
                                f"Hard stop-loss backstop: unrealized loss "
                                f"{unrealized_profit:.2f} USDC <= {_HARD_STOP_LOSS_USDC:.2f} USDC"
                            ],
                            reasons_against=[],
                        ),
                    )
                    logger.warning(
                        "Hard stop-loss triggered for account %s %s: unrealized %.2f <= %.2f",
                        account.id, position.symbol, unrealized_profit, _HARD_STOP_LOSS_USDC,
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

            last_alert = (
                session.query(m.PendingNotification)
                .filter_by(account_id=account.id, kind="equity_move")
                .order_by(m.PendingNotification.created_at.desc())
                .first()
            )
            baseline = last_alert.meta.get("equity") if last_alert else None
            if baseline is None:
                # First run ever for this account — establish a baseline
                # silently (pre-marked delivered so the app never sees it
                # as a notification to show).
                session.add(m.PendingNotification(
                    account_id=account.id, kind="equity_move", title="", body="",
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
                account_id=account.id, kind="equity_move", title=title, body=body,
                meta={"equity": current_equity},
            ))
            logger.info(
                "Equity movement alert for account %s: %+.2f -> %.2f (threshold %.2f)",
                account.id, delta, current_equity, _EQUITY_ALERT_THRESHOLD_USDC,
            )
            send_email(title, body)

    return "ok"
