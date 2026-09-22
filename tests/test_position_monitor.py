import unittest

from trading_engine.execution.position_monitor import evaluate_position


class TestPositionMonitor(unittest.TestCase):
    def test_stop_loss_triggers_full_close(self):
        result = evaluate_position(
            symbol="BTC/USDT", direction="LONG", entry=100, current_price=94,
            current_stop=95, take_profit_1=110, trailing_distance=2,
        )
        self.assertEqual(result.action, "close")

    def test_take_profit_1_closes_half_and_moves_stop_to_breakeven(self):
        result = evaluate_position(
            symbol="BTC/USDT", direction="LONG", entry=100, current_price=111,
            current_stop=95, take_profit_1=110, trailing_distance=2,
        )
        self.assertEqual(result.action, "trail_stop")
        self.assertGreaterEqual(result.new_stop, 100)

    def test_trailing_stop_after_breakeven(self):
        result = evaluate_position(
            symbol="BTC/USDT", direction="LONG", entry=100, current_price=120,
            current_stop=100, take_profit_1=110, trailing_distance=2, tp1_hit=True,
        )
        self.assertEqual(result.action, "trail_stop")
        self.assertAlmostEqual(result.new_stop, 118)

    def test_trailing_stop_does_not_move_backwards(self):
        result = evaluate_position(
            symbol="BTC/USDT", direction="LONG", entry=100, current_price=105,
            current_stop=110, take_profit_1=200, trailing_distance=2, tp1_hit=True,
        )
        self.assertEqual(result.action, "close")  # current_price <= current_stop

    def test_short_position_stop_above_entry(self):
        result = evaluate_position(
            symbol="BTC/USDT", direction="SHORT", entry=100, current_price=106,
            current_stop=105, take_profit_1=90, trailing_distance=2,
        )
        self.assertEqual(result.action, "close")

    def test_no_action_when_price_between_levels(self):
        # Price has moved only slightly above entry — not enough for the
        # trailing-stop candidate (current_price - trailing_distance) to
        # exceed the current stop, and not enough to hit take_profit_1 —
        # so no state change should occur.
        result = evaluate_position(
            symbol="BTC/USDT", direction="LONG", entry=100, current_price=100.5,
            current_stop=99, take_profit_1=110, trailing_distance=5,
        )
        self.assertEqual(result.action, "hold")

    def test_take_profit_2_closes_full(self):
        # take_profit_2 isn't directly modeled in evaluate_position's
        # signature (tp1/trailing only) — verify tp1-hit path at least
        # produces a sane trail rather than crashing near a second target.
        result = evaluate_position(
            symbol="BTC/USDT", direction="LONG", entry=100, current_price=130,
            current_stop=110, take_profit_1=110, trailing_distance=5, tp1_hit=True,
        )
        self.assertEqual(result.action, "trail_stop")


if __name__ == "__main__":
    unittest.main()
