import unittest
from datetime import datetime, timedelta, timezone

import numpy as np

from trading_engine.execution.exchange_interface import InsufficientBalanceError, OrderType, Side, StalePriceError
from trading_engine.execution.live_guard import LiveTradingBlocked, assert_live_order_allowed
from trading_engine.execution.paper_exchange import PaperExchange
from trading_engine.regime.regime_detector import MarketRegime, detect_regime


def _run(coro):
    import asyncio
    return asyncio.run(coro)


class TestPaperExchange(unittest.TestCase):
    def setUp(self):
        self.exchange = PaperExchange(starting_balances={"USDT": 10_000, "BTC": 1.0})
        self.exchange.update_price("BTC/USDT", 50_000.0)

    def test_buy_deducts_quote_and_credits_base(self):
        result = _run(self.exchange.create_order("BTC/USDT", Side.buy, OrderType.market, 0.1))
        self.assertEqual(result.status.value, "filled")
        self.assertGreater(_run(self.exchange.get_balance("BTC")), 1.0)

    def test_cannot_buy_more_than_balance(self):
        with self.assertRaises(InsufficientBalanceError):
            _run(self.exchange.create_order("BTC/USDT", Side.buy, OrderType.market, 100))

    def test_cannot_sell_more_than_held(self):
        with self.assertRaises(InsufficientBalanceError):
            _run(self.exchange.create_order("BTC/USDT", Side.sell, OrderType.market, 100))

    def test_paper_exchange_declares_no_withdrawals(self):
        self.assertFalse(self.exchange.supports_withdrawals)

    def test_stale_price_rejected(self):
        stale_exchange = PaperExchange(starting_balances={"USDT": 1000})
        old_time = datetime.now(timezone.utc) - timedelta(seconds=120)
        stale_exchange.update_price("BTC/USDT", 50_000.0, at=old_time)
        with self.assertRaises(StalePriceError):
            _run(stale_exchange.create_order("BTC/USDT", Side.buy, OrderType.market, 0.01))


class TestLiveGuard(unittest.TestCase):
    def test_paper_orders_always_allowed(self):
        exchange = PaperExchange(starting_balances={"USDT": 1000})
        assert_live_order_allowed(exchange=exchange, trading_mode="paper", allow_live_flag=False, confirm_live=False)

    def test_live_blocked_when_mode_not_live(self):
        class FakeLiveExchange:
            name = "fake_live"
            supports_withdrawals = False

        with self.assertRaises(LiveTradingBlocked):
            assert_live_order_allowed(exchange=FakeLiveExchange(), trading_mode="paper", allow_live_flag=True, confirm_live=True)

    def test_live_blocked_when_flag_false(self):
        class FakeLiveExchange:
            name = "fake_live"
            supports_withdrawals = False

        with self.assertRaises(LiveTradingBlocked):
            assert_live_order_allowed(exchange=FakeLiveExchange(), trading_mode="live", allow_live_flag=False, confirm_live=True)

    def test_live_blocked_for_withdrawal_capable_exchange(self):
        class WithdrawableExchange:
            name = "withdrawable"
            supports_withdrawals = True

        with self.assertRaises(LiveTradingBlocked):
            assert_live_order_allowed(exchange=WithdrawableExchange(), trading_mode="live", allow_live_flag=True, confirm_live=True)

    def test_live_blocked_without_explicit_confirm(self):
        class FakeLiveExchange:
            name = "fake_live"
            supports_withdrawals = False

        with self.assertRaises(LiveTradingBlocked):
            assert_live_order_allowed(exchange=FakeLiveExchange(), trading_mode="live", allow_live_flag=True, confirm_live=False)

    def test_live_allowed_only_when_everything_aligned(self):
        class FakeLiveExchange:
            name = "fake_live"
            supports_withdrawals = False

        assert_live_order_allowed(exchange=FakeLiveExchange(), trading_mode="live", allow_live_flag=True, confirm_live=True)


class TestRegimeDetector(unittest.TestCase):
    def test_insufficient_data_defaults_safely(self):
        reading = detect_regime(np.array([1, 2]), np.array([1, 2]), np.array([1, 2]))
        self.assertEqual(reading.market_regime, MarketRegime.SIDEWAYS)

    def test_strong_uptrend_detected_as_bull(self):
        n = 100
        close = np.linspace(100, 200, n)
        high = close + 1
        low = close - 1
        reading = detect_regime(high, low, close)
        self.assertIn(reading.market_regime, (MarketRegime.BULL, MarketRegime.STRONG_BULL))

    def test_crash_detected_after_sharp_drop(self):
        n = 100
        close = np.concatenate([np.full(76, 100.0), np.linspace(100, 70, 24)])
        high = close + 1
        low = close - 1
        reading = detect_regime(high, low, close)
        self.assertEqual(reading.market_regime, MarketRegime.CRASH)


if __name__ == "__main__":
    unittest.main()
