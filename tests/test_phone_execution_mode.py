import asyncio
import unittest
from datetime import datetime, timedelta, timezone

import numpy as np

from trading_engine.agent import AgentConfig, NeuravexAgent
from trading_engine.execution.exchange_interface import Candle
from trading_engine.execution.paper_exchange import PaperExchange
from trading_engine.risk.risk_engine import PortfolioState
from trading_engine.strategies.momentum import MomentumStrategy
from trading_engine.strategies.trend_following import TrendFollowingStrategy


class RecordingStore:
    def __init__(self):
        self.signals = []
        self.decisions = []
        self.order_results = []
        self.pending_executions = []
        self.risk_events = []

    def save_signal(self, symbol, signal):
        self.signals.append((symbol, signal))

    def save_decision(self, decision, account_id):
        self.decisions.append(decision)

    def save_order_result(self, order_result, decision_id):
        self.order_results.append(order_result)

    def save_risk_event(self, event_type, severity, message, context):
        self.risk_events.append((event_type, severity, message, context))

    def save_pending_execution(self, account_id, decision_id, plan):
        self.pending_executions.append(plan)

    def get_portfolio_state(self, account_id):
        return PortfolioState(
            equity=1000.0, peak_equity=1000.0, cash=1000.0, open_positions={},
            daily_pnl=0.0, daily_start_equity=1000.0,
        )


def _feed_trending_candles(exchange: PaperExchange, symbol: str, n: int = 200):
    base = datetime(2025, 1, 1, tzinfo=timezone.utc)
    rng = np.random.default_rng(7)
    close = np.linspace(100, 160, n) + rng.normal(0, 0.5, n)
    candles = [
        Candle(
            open_time=base + timedelta(hours=i), open=float(close[i]), high=float(close[i] + 1),
            low=float(close[i] - 1), close=float(close[i]), volume=float(abs(rng.normal(1000, 100))),
        )
        for i in range(n)
    ]
    exchange.load_candles(symbol, "1h", candles)
    exchange.update_price(symbol, float(close[-1]))


class TestPhoneExecutionMode(unittest.TestCase):
    def test_phone_mode_never_calls_create_order(self):
        symbol = "BTC/EUR"
        exchange = PaperExchange(starting_balances={"EUR": 1000.0})
        _feed_trending_candles(exchange, symbol)

        original_create_order = exchange.create_order
        called = {"count": 0}

        async def spy_create_order(*args, **kwargs):
            called["count"] += 1
            return await original_create_order(*args, **kwargs)
        exchange.create_order = spy_create_order  # type: ignore

        store = RecordingStore()
        config = AgentConfig(
            symbols=[symbol], execution_mode="phone", minimum_confidence=0.5, minimum_expected_edge=0.0,
        )
        strategies = [TrendFollowingStrategy(), MomentumStrategy()]
        agent = NeuravexAgent(exchange=exchange, strategies=strategies, store=store, config=config, account_id="test")

        asyncio.run(agent.run_tick())

        self.assertEqual(called["count"], 0, "phone mode must never call exchange.create_order()")

    def test_server_mode_still_calls_create_order_in_paper(self):
        symbol = "BTC/EUR"
        exchange = PaperExchange(starting_balances={"EUR": 1000.0})
        _feed_trending_candles(exchange, symbol)

        store = RecordingStore()
        config = AgentConfig(
            symbols=[symbol], execution_mode="server", minimum_confidence=0.5, minimum_expected_edge=0.0,
        )
        strategies = [TrendFollowingStrategy(), MomentumStrategy()]
        agent = NeuravexAgent(exchange=exchange, strategies=strategies, store=store, config=config, account_id="test")

        decisions = asyncio.run(agent.run_tick())

        self.assertEqual(len(store.pending_executions), 0, "server mode must not queue phone executions")
        if any(d.action in ("BUY", "SELL") for d in decisions):
            self.assertGreater(len(store.order_results), 0)

    def test_dry_run_logs_decision_but_never_executes(self):
        symbol = "BTC/EUR"
        exchange = PaperExchange(starting_balances={"EUR": 1000.0})
        _feed_trending_candles(exchange, symbol)

        original_create_order = exchange.create_order
        called = {"count": 0}

        async def spy_create_order(*args, **kwargs):
            called["count"] += 1
            return await original_create_order(*args, **kwargs)
        exchange.create_order = spy_create_order  # type: ignore

        store = RecordingStore()
        config = AgentConfig(
            symbols=[symbol], execution_mode="server", minimum_confidence=0.5,
            minimum_expected_edge=0.0, dry_run=True,
        )
        strategies = [TrendFollowingStrategy(), MomentumStrategy()]
        agent = NeuravexAgent(exchange=exchange, strategies=strategies, store=store, config=config, account_id="test")

        decisions = asyncio.run(agent.run_tick())

        self.assertEqual(called["count"], 0, "dry_run must never call exchange.create_order()")
        self.assertEqual(len(store.pending_executions), 0, "dry_run must never queue a phone execution either")
        self.assertGreater(len(store.decisions), 0)
        buy_or_sell = [d for d in decisions if d.action in ("BUY", "SELL")]
        if buy_or_sell:
            self.assertTrue(any("AI trading is currently off" in r for r in buy_or_sell[0].reasons_for))


if __name__ == "__main__":
    unittest.main()
