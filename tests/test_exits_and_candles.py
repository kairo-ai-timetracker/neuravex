import asyncio
import unittest
from datetime import datetime, timedelta, timezone

import numpy as np

from trading_engine.agent import AgentConfig, NeuravexAgent
from trading_engine.execution.exchange_interface import Candle
from trading_engine.risk.exits import atr_stop_price, evaluate_exit, net_profit_if_sold
from trading_engine.risk.risk_engine import PortfolioState
from trading_engine.strategies.base import Strategy
from trading_engine.strategies.performance import performance_score, update_running_stats


class TestExitRules(unittest.TestCase):
    def test_take_profit_is_judged_after_sell_costs(self):
        # 2,600 USDC position, gross +5 USDC: after a 0.15% sell cost it's a net loss.
        qty, basis = 1000.0, 2.6
        price = basis + 5.0 / qty
        self.assertLess(net_profit_if_sold(qty, basis, price, 0.0015), 5.0)
        self.assertIsNone(evaluate_exit(quantity=qty, cost_basis=basis, price=price, stop_price=None, take_profit_usdc=5.0))
        # Enough of a move to clear the target net of costs.
        price = basis * 1.006
        exit_ = evaluate_exit(quantity=qty, cost_basis=basis, price=price, stop_price=None, take_profit_usdc=5.0)
        self.assertEqual(exit_.reason, "take_profit")
        self.assertGreaterEqual(exit_.net_profit, 5.0)

    def test_stop_loss_fires_and_wins_over_take_profit(self):
        exit_ = evaluate_exit(quantity=10, cost_basis=100, price=95, stop_price=96, take_profit_usdc=None)
        self.assertEqual(exit_.reason, "stop_loss")
        self.assertLess(exit_.net_profit, 0)
        self.assertIsNone(evaluate_exit(quantity=10, cost_basis=100, price=97, stop_price=96, take_profit_usdc=None))

    def test_atr_stop_is_below_entry(self):
        closes = np.linspace(100, 110, 50)
        stop = atr_stop_price(105.0, closes + 1, closes - 1, closes)
        self.assertIsNotNone(stop)
        self.assertLess(stop, 105.0)
        self.assertIsNone(atr_stop_price(105.0, closes[:5] + 1, closes[:5] - 1, closes[:5]))


class TestStrategyPerformance(unittest.TestCase):
    def test_running_stats_and_score(self):
        wr, exp, n = 0.0, 0.0, 0
        for won in (True, True, True, False, True):
            wr, exp, n = update_running_stats(wr, exp, n, won=won, pnl_per_unit=1.0 if won else -1.0)
        self.assertEqual(n, 5)
        self.assertAlmostEqual(wr, 0.8)
        self.assertAlmostEqual(performance_score(wr, n), 0.6)
        self.assertIsNone(performance_score(1.0, 4))


class _AlwaysLong(Strategy):
    name = "always_long"
    allowed_regimes = set()

    def __init__(self):
        self.seen_last_close = []

    def generate_signal(self, symbol, data, regime):
        from trading_engine.strategies.base import StrategySignal
        self.seen_last_close.append(float(data.close[-1]))
        return StrategySignal(symbol=symbol, direction="LONG", confidence=0.9, expected_return=0.05,
                              risk=0.01, time_horizon="1h", strategy_name=self.name)


class _Store:
    def __init__(self, state):
        self.state, self.decisions, self.orders = state, [], []

    def save_signal(self, *a): pass
    def save_decision(self, d, account_id):
        d.id = d.id or f"d{len(self.decisions)}"
        self.decisions.append(d)
        return d.id
    def save_order_result(self, r, decision_id, account_id): self.orders.append(r)
    def save_risk_event(self, *a): pass
    def save_pending_execution(self, *a): pass
    def get_portfolio_state(self, account_id): return self.state
    def has_pending_order(self, *a): return False
    def record_realized_trade(self, *a): pass


class _CandleExchange:
    """Hourly candles where the LAST one is still forming at `now`."""
    name = "paper"

    def __init__(self, now):
        start = now.replace(minute=0, second=0, microsecond=0) - timedelta(hours=99)
        self.candles = [
            Candle(open_time=(start + timedelta(hours=i)).replace(tzinfo=timezone.utc),
                   open=100 + i, high=101 + i, low=99 + i, close=100 + i, volume=1000)
            for i in range(100)
        ]
        self.candles[-1].close = 999.0  # the forming candle — must never reach a strategy

    async def get_candles(self, symbol, timeframe, limit):
        return self.candles


class TestClosedCandlesAndOneTradePerCandle(unittest.TestCase):
    def _agent(self, now, state, strat):
        return NeuravexAgent(
            exchange=_CandleExchange(now), strategies=[strat], store=_Store(state),
            config=AgentConfig(symbols=["X/USDC"], dry_run=True), account_id="a", clock=lambda: now,
        )

    def _state(self, **kw):
        return PortfolioState(equity=10_000, peak_equity=10_000, cash=10_000, open_positions={},
                              daily_pnl=0, daily_start_equity=10_000, **kw)

    def test_strategies_never_see_the_forming_candle(self):
        now = datetime(2026, 10, 8, 12, 30)
        strat = _AlwaysLong()
        asyncio.run(self._agent(now, self._state(), strat).run_tick())
        self.assertTrue(strat.seen_last_close)
        self.assertNotEqual(strat.seen_last_close[-1], 999.0)

    def test_no_second_trade_on_the_same_closed_candle(self):
        now = datetime(2026, 10, 8, 12, 30)
        # Traded at 12:10, after the 12:00 candle closed -> wait for 13:00.
        state = self._state(last_signal_time_by_symbol={"X/USDC": datetime(2026, 10, 8, 12, 10)})
        decisions = asyncio.run(self._agent(now, state, _AlwaysLong()).run_tick())
        self.assertEqual(decisions[0].action, "NO_TRADE")
        self.assertIn("waiting for a new candle", decisions[0].reasons_against[-1])


if __name__ == "__main__":
    unittest.main()
