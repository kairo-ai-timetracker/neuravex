import unittest

from trading_engine.execution.binance_testnet import BinanceTestnetExchange
from trading_engine.execution.live_guard import LiveTradingBlocked, assert_live_order_allowed


class TestBinanceTestnetSafetyProperties(unittest.TestCase):
    def setUp(self):
        self.exchange = BinanceTestnetExchange(api_key="test", api_secret="test")

    def test_declares_no_withdrawal_support(self):
        self.assertFalse(self.exchange.supports_withdrawals)

    def test_still_blocked_in_paper_mode(self):
        with self.assertRaises(LiveTradingBlocked):
            assert_live_order_allowed(
                exchange=self.exchange, trading_mode="paper", allow_live_flag=True, confirm_live=True,
            )

    def test_still_blocked_without_allow_live_flag(self):
        with self.assertRaises(LiveTradingBlocked):
            assert_live_order_allowed(
                exchange=self.exchange, trading_mode="live", allow_live_flag=False, confirm_live=True,
            )

    def test_allowed_when_everything_aligned(self):
        assert_live_order_allowed(
            exchange=self.exchange, trading_mode="live", allow_live_flag=True, confirm_live=True,
        )


if __name__ == "__main__":
    unittest.main()
