import unittest

from trading_engine.risk.position_sizing import calculate_position_size


class TestPositionSizing(unittest.TestCase):
    def test_basic_sizing(self):
        result = calculate_position_size(
            account_equity=10_000, entry_price=100, stop_loss_price=98,
            confidence=0.8, max_trade_risk=0.01, max_position_size_fraction=0.5,
            current_portfolio_exposure_fraction=0.0, max_portfolio_exposure_fraction=1.0,
        )
        self.assertGreater(result.quantity, 0)
        self.assertLessEqual(result.risk_amount, 10_000 * 0.01 + 1e-6)

    def test_higher_confidence_yields_larger_size(self):
        low_conf = calculate_position_size(
            account_equity=10_000, entry_price=100, stop_loss_price=98, confidence=0.1,
            max_trade_risk=0.01, max_position_size_fraction=1.0,
            current_portfolio_exposure_fraction=0.0, max_portfolio_exposure_fraction=1.0,
        )
        high_conf = calculate_position_size(
            account_equity=10_000, entry_price=100, stop_loss_price=98, confidence=0.95,
            max_trade_risk=0.01, max_position_size_fraction=1.0,
            current_portfolio_exposure_fraction=0.0, max_portfolio_exposure_fraction=1.0,
        )
        self.assertGreater(high_conf.quantity, low_conf.quantity)

    def test_capped_by_max_position_size(self):
        result = calculate_position_size(
            account_equity=10_000, entry_price=100, stop_loss_price=99.9, confidence=1.0,
            max_trade_risk=0.5, max_position_size_fraction=0.1,
            current_portfolio_exposure_fraction=0.0, max_portfolio_exposure_fraction=1.0,
        )
        self.assertEqual(result.capped_by, "max_position_size")
        self.assertLessEqual(result.notional, 1000 + 1e-6)

    def test_capped_by_portfolio_exposure(self):
        result = calculate_position_size(
            account_equity=10_000, entry_price=100, stop_loss_price=95, confidence=1.0,
            max_trade_risk=0.5, max_position_size_fraction=1.0,
            current_portfolio_exposure_fraction=0.55, max_portfolio_exposure_fraction=0.6,
        )
        self.assertEqual(result.capped_by, "max_portfolio_exposure")

    def test_zero_room_yields_zero_quantity(self):
        result = calculate_position_size(
            account_equity=10_000, entry_price=100, stop_loss_price=95, confidence=1.0,
            max_trade_risk=0.5, max_position_size_fraction=1.0,
            current_portfolio_exposure_fraction=0.60, max_portfolio_exposure_fraction=0.6,
        )
        self.assertEqual(result.quantity, 0.0)

    def test_invalid_prices_raise(self):
        with self.assertRaises(ValueError):
            calculate_position_size(
                account_equity=10_000, entry_price=0, stop_loss_price=95, confidence=1.0,
                max_trade_risk=0.01, max_position_size_fraction=1.0,
                current_portfolio_exposure_fraction=0.0, max_portfolio_exposure_fraction=1.0,
            )
        with self.assertRaises(ValueError):
            calculate_position_size(
                account_equity=10_000, entry_price=100, stop_loss_price=100, confidence=1.0,
                max_trade_risk=0.01, max_position_size_fraction=1.0,
                current_portfolio_exposure_fraction=0.0, max_portfolio_exposure_fraction=1.0,
            )


if __name__ == "__main__":
    unittest.main()
