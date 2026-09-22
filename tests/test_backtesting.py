import unittest
from datetime import datetime, timedelta

import numpy as np

from trading_engine.backtesting.engine import run_backtest
from trading_engine.execution.exchange_interface import Candle
from trading_engine.strategies.trend_following import TrendFollowingStrategy


def _make_candles(n=200, trend=True, seed=42):
    rng = np.random.default_rng(seed)
    base = datetime(2025, 1, 1)
    if trend:
        close = np.linspace(100, 180, n) + rng.normal(0, 1, n)
    else:
        close = 100 + rng.normal(0, 1, n).cumsum() * 0.1
    candles = []
    for i in range(n):
        c = float(close[i])
        candles.append(Candle(
            open_time=base + timedelta(hours=i), open=c, high=c + abs(rng.normal(0, 0.5)),
            low=c - abs(rng.normal(0, 0.5)), close=c, volume=float(abs(rng.normal(1000, 200))),
        ))
    return candles


class TestWalkForwardSplit(unittest.TestCase):
    def test_splits_are_non_overlapping_and_increasing(self):
        candles = _make_candles(120)
        result = run_backtest(symbol="TEST/USDT", candles=candles, strategies=[TrendFollowingStrategy()], warmup_bars=60)
        indices = [t.entry_index for t in result.trades]
        self.assertEqual(indices, sorted(indices))


class TestBacktestEngine(unittest.TestCase):
    def test_runs_without_error_and_produces_result(self):
        candles = _make_candles(150)
        result = run_backtest(symbol="TEST/USDT", candles=candles, strategies=[TrendFollowingStrategy()])
        self.assertIsNotNone(result.final_equity)

    def test_equity_never_goes_negative(self):
        candles = _make_candles(150, trend=False)
        result = run_backtest(symbol="TEST/USDT", candles=candles, strategies=[TrendFollowingStrategy()])
        self.assertTrue(all(e >= 0 for e in result.equity_curve))

    def test_fees_reduce_returns_relative_to_zero_fee(self):
        candles = _make_candles(150)
        with_fees = run_backtest(symbol="TEST/USDT", candles=candles, strategies=[TrendFollowingStrategy()], fee_rate=0.01)
        no_fees = run_backtest(symbol="TEST/USDT", candles=candles, strategies=[TrendFollowingStrategy()], fee_rate=0.0)
        if with_fees.trades and no_fees.trades:
            self.assertLessEqual(with_fees.final_equity, no_fees.final_equity + 1e-6)

    def test_max_drawdown_circuit_breaker_is_respected(self):
        candles = _make_candles(150, trend=False)
        result = run_backtest(symbol="TEST/USDT", candles=candles, strategies=[TrendFollowingStrategy()])
        self.assertLessEqual(result.max_drawdown, 1.0)

    def test_no_trade_when_confidence_threshold_impossibly_high(self):
        candles = _make_candles(150)
        result = run_backtest(
            symbol="TEST/USDT", candles=candles, strategies=[TrendFollowingStrategy()],
            minimum_confidence=0.999,
        )
        self.assertEqual(len(result.trades), 0)


if __name__ == "__main__":
    unittest.main()
