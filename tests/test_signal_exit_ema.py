import asyncio
import unittest
from datetime import datetime, timedelta, timezone

from tests.test_exits_and_candles import _Store
from trading_engine.agent import AgentConfig, NeuravexAgent
from trading_engine.execution.exchange_interface import Candle
from trading_engine.risk.risk_engine import PortfolioState
from trading_engine.strategies.base import Strategy, StrategySignal

NOW = datetime(2026, 10, 31, 12, 30)


class _AlwaysShort(Strategy):
    name = "always_short"

    def generate_signal(self, symbol, data, regime):
        return StrategySignal(symbol=symbol, direction="SHORT", confidence=0.9, expected_return=0.05,
                              risk=0.01, time_horizon="1h", strategy_name=self.name)


class _Exchange:
    name = "paper"

    def __init__(self, step):
        start = NOW.replace(minute=0) - timedelta(hours=99)
        self.candles = [
            Candle(open_time=(start + timedelta(hours=i)).replace(tzinfo=timezone.utc),
                   open=1000 + step * i, high=1001 + step * i, low=999 + step * i, close=1000 + step * i, volume=1000)
            for i in range(100)
        ]

    async def get_candles(self, symbol, timeframe, limit):
        return self.candles


def run(step, exit_ema):
    state = PortfolioState(equity=10_000, peak_equity=10_000, cash=9_000, daily_pnl=0, daily_start_equity=10_000,
                           open_positions={"X/USDC": {"quantity": 1.0, "notional": 1000.0}})
    agent = NeuravexAgent(
        exchange=_Exchange(step), strategies=[_AlwaysShort()], store=_Store(state),
        config=AgentConfig(symbols=["X/USDC"], dry_run=True, signal_exit_ema=exit_ema),
        account_id="a", clock=lambda: NOW,
    )
    return asyncio.run(agent.run_tick())[0]


class TestSignalExitEma(unittest.TestCase):
    def test_sell_signal_is_ignored_while_price_is_above_the_ema(self):
        decision = run(step=1, exit_ema=20)
        self.assertEqual(decision.action, "NO_TRADE")
        self.assertIn("still above its EMA20", decision.reasons_against[-1])

    def test_sell_signal_closes_once_price_is_below_the_ema(self):
        decision = run(step=-1, exit_ema=20)
        self.assertEqual(decision.action, "SELL")
        self.assertIn("Would SELL", decision.reasons_for[-1])

    def test_without_the_rule_a_sell_signal_always_closes(self):
        self.assertEqual(run(step=1, exit_ema=None).action, "SELL")


if __name__ == "__main__":
    unittest.main()
