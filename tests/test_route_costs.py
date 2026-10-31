import unittest

from trading_engine.execution.route_costs import ROUND_TRIP_COST, slippage_bps_by_symbol


class TestRouteCosts(unittest.TestCase):
    def test_fee_and_slippage_add_up_to_the_measured_round_trip(self):
        slippage = slippage_bps_by_symbol(taker_fee=0.001)
        for symbol, cost in ROUND_TRIP_COST.items():
            self.assertAlmostEqual(2 * (0.001 + slippage[symbol] / 10_000), cost)
            self.assertGreater(slippage[symbol], 0)


if __name__ == "__main__":
    unittest.main()
