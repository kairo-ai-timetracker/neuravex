import asyncio
import unittest
from datetime import datetime, timedelta, timezone

import numpy as np

from backend.app.core.tasks import _entry_rules
from tests.test_exits_and_candles import _Store
from trading_engine.agent import AgentConfig, NeuravexAgent
from trading_engine.execution.exchange_interface import Candle
from trading_engine.risk.exits import atr_stop_price
from trading_engine.risk.risk_engine import PortfolioState
from trading_engine.strategies.base import Strategy, StrategySignal

NOW = datetime(2026, 10, 31, 12, 30)


class _AlwaysLong(Strategy):
    name = "always_long"

    def generate_signal(self, symbol, data, regime):
        return StrategySignal(symbol=symbol, direction="LONG", confidence=0.9, expected_return=0.05,
                              risk=0.01, time_horizon="1h", strategy_name=self.name)


class _Exchange:
    name = "paper"

    def __init__(self, steps, count=300):
        start = NOW.replace(minute=0) - timedelta(hours=count - 1)
        self.candles = {
            symbol: [
                Candle(open_time=(start + timedelta(hours=i)).replace(tzinfo=timezone.utc),
                       open=1000 + step * i, high=1001 + step * i, low=999 + step * i, close=1000 + step * i, volume=1000)
                for i in range(count)
            ]
            for symbol, step in steps.items()
        }

    async def get_candles(self, symbol, timeframe, limit):
        return self.candles[symbol]


def decide(steps, count=300, **rules):
    state = PortfolioState(equity=10_000, peak_equity=10_000, cash=10_000, daily_pnl=0, daily_start_equity=10_000,
                           open_positions={})
    agent = NeuravexAgent(
        exchange=_Exchange(steps, count), strategies=[_AlwaysLong()], store=_Store(state),
        config=AgentConfig(symbols=list(steps), dry_run=True, **rules),
        account_id="a", clock=lambda: NOW,
    )
    return asyncio.run(agent.run_tick())[0]


class TestEntryFilters(unittest.TestCase):
    def test_no_entry_below_the_trend_ema(self):
        decision = decide({"X/USDC": -1}, entry_trend_ema=200)
        self.assertEqual(decision.action, "NO_TRADE")
        self.assertIn("below its EMA200", " ".join(decision.reasons_against))

    def test_too_little_history_blocks_the_entry(self):
        decision = decide({"X/USDC": 1}, count=100, entry_trend_ema=200)
        self.assertEqual(decision.action, "NO_TRADE")
        self.assertIn("below its EMA200", " ".join(decision.reasons_against))

    def test_weak_market_blocks_a_strong_coin(self):
        decision = decide({"X/USDC": 1, "Y/USDC": -1, "Z/USDC": -1}, entry_trend_ema=200, entry_min_breadth=0.5)
        self.assertEqual(decision.action, "NO_TRADE")
        self.assertIn("33% of this market", " ".join(decision.reasons_against))

    def test_strong_market_is_not_blocked_by_these_filters(self):
        reasons = " ".join(decide({"X/USDC": 1, "Y/USDC": 1}, entry_trend_ema=200, entry_min_breadth=0.5).reasons_against)
        self.assertNotIn("EMA200", reasons)

    def test_stop_distance_follows_the_multiple(self):
        closes = np.linspace(100, 110, 50)
        narrow = atr_stop_price(110.0, closes + 1, closes - 1, closes)
        wide = atr_stop_price(110.0, closes + 1, closes - 1, closes, 3.0)
        self.assertAlmostEqual(110.0 - wide, 2 * (110.0 - narrow))

    def test_metals_keeps_the_old_rules(self):
        self.assertEqual(_entry_rules("metals"), {})
        self.assertEqual(_entry_rules("stocks")["stop_atr_multiple"], 3.0)
        self.assertEqual(_entry_rules("crypto")["entry_trend_ema"], 200)


if __name__ == "__main__":
    unittest.main()
