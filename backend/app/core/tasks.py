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

    return "ok"
