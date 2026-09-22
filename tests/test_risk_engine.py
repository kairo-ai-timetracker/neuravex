import unittest
from datetime import datetime, timedelta

from trading_engine.risk.risk_engine import (
    PortfolioState,
    RiskLimitBreach,
    check_account_wide_limits,
    check_trade_against_limits,
)
from trading_engine.risk.risk_plan import build_risk_plan


class TestRiskPlan(unittest.TestCase):
    def test_long_plan_has_stop_below_entry(self):
        plan = build_risk_plan(entry=100, direction="LONG", atr_value=2.0)
        self.assertLess(plan.stop_loss, plan.entry)
        self.assertGreater(plan.take_profit_1, plan.entry)

    def test_short_plan_has_stop_above_entry(self):
        plan = build_risk_plan(entry=100, direction="SHORT", atr_value=2.0)
        self.assertGreater(plan.stop_loss, plan.entry)
        self.assertLess(plan.take_profit_1, plan.entry)

    def test_rejects_zero_atr(self):
        with self.assertRaises(ValueError):
            build_risk_plan(entry=100, direction="LONG", atr_value=0)


class TestAccountWideLimits(unittest.TestCase):
    def _state(self, equity, peak_equity, daily_pnl, daily_start_equity):
        return PortfolioState(
            equity=equity, peak_equity=peak_equity, cash=equity, open_positions={},
            daily_pnl=daily_pnl, daily_start_equity=daily_start_equity,
        )

    def test_within_daily_loss_ok(self):
        state = self._state(equity=990, peak_equity=1000, daily_pnl=-10, daily_start_equity=1000)
        check_account_wide_limits(state, max_daily_loss=0.03, max_drawdown=0.10)  # should not raise

    def test_daily_loss_breach_halts(self):
        state = self._state(equity=960, peak_equity=1000, daily_pnl=-40, daily_start_equity=1000)
        with self.assertRaises(RiskLimitBreach):
            check_account_wide_limits(state, max_daily_loss=0.03, max_drawdown=0.10)

    def test_max_drawdown_breach_halts(self):
        state = self._state(equity=890.0, peak_equity=1000.0, daily_pnl=-5.0, daily_start_equity=895.0)
        with self.assertRaises(RiskLimitBreach):
            check_account_wide_limits(state, max_daily_loss=0.03, max_drawdown=0.10)

    def test_balance_floor_halts_even_without_daily_loss_or_drawdown_breach(self):
        state = self._state(equity=750.0, peak_equity=760.0, daily_pnl=-2.0, daily_start_equity=752.0)
        with self.assertRaises(RiskLimitBreach):
            check_account_wide_limits(state, max_daily_loss=0.5, max_drawdown=0.5, min_balance_floor=750.0)

    def test_balance_floor_not_yet_reached_is_ok(self):
        state = self._state(equity=800.0, peak_equity=1000.0, daily_pnl=-1.0, daily_start_equity=801.0)
        check_account_wide_limits(state, max_daily_loss=0.5, max_drawdown=0.5, min_balance_floor=750.0)

    def test_no_balance_floor_configured_is_skipped(self):
        state = self._state(equity=100.0, peak_equity=1000.0, daily_pnl=-1.0, daily_start_equity=101.0)
        check_account_wide_limits(state, max_daily_loss=0.99, max_drawdown=0.99, min_balance_floor=None)


class TestTradeLimits(unittest.TestCase):
    def _state(self, **kwargs):
        defaults = dict(
            equity=10_000, peak_equity=10_000, cash=10_000, open_positions={},
            daily_pnl=0, daily_start_equity=10_000,
        )
        defaults.update(kwargs)
        return PortfolioState(**defaults)

    def test_approves_reasonable_trade(self):
        state = self._state()
        result = check_trade_against_limits(
            symbol="BTC/USDT", proposed_notional=500, state=state, max_open_positions=5,
            max_portfolio_exposure=0.6, max_position_size=0.2, correlated_symbols={},
            cooldown_seconds=900, max_trades_per_hour=4, min_time_between_entries_seconds=300,
        )
        self.assertTrue(result.approved)

    def test_max_open_positions_enforced(self):
        state = self._state(open_positions={f"SYM{i}/USDT": {"notional": 100} for i in range(5)})
        result = check_trade_against_limits(
            symbol="NEW/USDT", proposed_notional=100, state=state, max_open_positions=5,
            max_portfolio_exposure=1.0, max_position_size=1.0, correlated_symbols={},
            cooldown_seconds=900, max_trades_per_hour=4, min_time_between_entries_seconds=300,
        )
        self.assertFalse(result.approved)

    def test_cooldown_blocks_rapid_reentry(self):
        state = self._state(last_signal_time_by_symbol={"BTC/USDT": datetime.utcnow()})
        result = check_trade_against_limits(
            symbol="BTC/USDT", proposed_notional=100, state=state, max_open_positions=5,
            max_portfolio_exposure=1.0, max_position_size=1.0, correlated_symbols={},
            cooldown_seconds=900, max_trades_per_hour=4, min_time_between_entries_seconds=0,
        )
        self.assertFalse(result.approved)

    def test_correlated_exposure_blocks_when_combined_too_large(self):
        state = self._state(open_positions={"ETH/USDT": {"notional": 5000}})
        result = check_trade_against_limits(
            symbol="BTC/USDT", proposed_notional=2000, state=state, max_open_positions=5,
            max_portfolio_exposure=0.6, max_position_size=1.0,
            correlated_symbols={"ETH/USDT": 0.9},
            cooldown_seconds=900, max_trades_per_hour=4, min_time_between_entries_seconds=300,
        )
        self.assertFalse(result.approved)


if __name__ == "__main__":
    unittest.main()
