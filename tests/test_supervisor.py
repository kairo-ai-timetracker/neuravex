import asyncio
import unittest
from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from backend.app.db.sql_data_store import SqlDataStore, record_closed_trade
from backend.app.models import models as m
from trading_engine.agent import AgentConfig, NeuravexAgent
from trading_engine.supervisor import LOOKBACK, ClosedTrade, evaluate_supervisor

NOW = datetime(2026, 10, 31, 12)


def trade(pnl, days_ago=1, strategies=("momentum",), regime="BEAR"):
    return ClosedTrade(NOW - timedelta(days=days_ago), pnl, list(strategies), regime)


class TestStake(unittest.TestCase):
    def _multiplier(self, *pnls):
        trades = [trade(p, days_ago=len(pnls) - i) for i, p in enumerate(pnls)]
        return evaluate_supervisor(trades, NOW).risk_multiplier

    def test_full_stake_without_history_or_after_one_loss(self):
        self.assertEqual(self._multiplier(), 1.0)
        self.assertEqual(self._multiplier(-5), 1.0)

    def test_stake_shrinks_with_losses(self):
        self.assertEqual(self._multiplier(-5, -5), 0.75)
        self.assertEqual(self._multiplier(-5, -5, -5), 0.5)
        self.assertEqual(self._multiplier(-5, -5, -5, -5), 0.25)
        self.assertEqual(self._multiplier(*[-5] * 12), 0.25)

    def test_one_win_recovers_one_step_only(self):
        self.assertEqual(self._multiplier(-5, -5, -5, -5, 9), 0.5)
        self.assertEqual(self._multiplier(-5, -5, -5, -5, 9, 9), 0.75)

    def test_old_losses_are_forgotten(self):
        old = [trade(-5, days_ago=LOOKBACK.days + 1) for _ in range(6)]
        self.assertEqual(evaluate_supervisor(old, NOW).risk_multiplier, 1.0)


class TestStrategyGate(unittest.TestCase):
    def test_blocks_only_the_losing_regime_after_enough_trades(self):
        trades = [trade(-5) for _ in range(4)] + [trade(9, regime="BULL") for _ in range(4)]
        verdict = evaluate_supervisor(trades, NOW)
        self.assertFalse(verdict.allows("momentum", "BEAR"))
        self.assertTrue(verdict.allows("momentum", "BULL"))
        self.assertTrue(verdict.allows("breakout", "BEAR"))

    def test_three_trades_are_not_enough_evidence(self):
        self.assertTrue(evaluate_supervisor([trade(-5) for _ in range(3)], NOW).allows("momentum", "BEAR"))

    def test_net_profit_with_losers_is_not_blocked(self):
        trades = [trade(-5), trade(-5), trade(-5), trade(50)]
        self.assertTrue(evaluate_supervisor(trades, NOW).allows("momentum", "BEAR"))

    def test_block_ends_by_itself(self):
        trades = [trade(-5, days_ago=LOOKBACK.days + 1) for _ in range(4)]
        self.assertTrue(evaluate_supervisor(trades, NOW).allows("momentum", "BEAR"))


class TestStorage(unittest.TestCase):
    def setUp(self):
        engine = create_engine("sqlite://")
        m.Base.metadata.create_all(engine)
        self.session = Session(engine)
        self.account = m.Account(user_id="u1")
        self.session.add(self.account)
        self.session.flush()
        self.opening = m.AIDecision(
            account_id=self.account.id, symbol="X/USDC", action=m.DecisionAction.buy, final_score=1.0,
            contributing_strategies=["momentum"], score_breakdown={"entry_regime": "BEAR"}, executed=True,
        )
        self.session.add(self.opening)
        self.session.flush()

    def tearDown(self):
        self.session.close()

    def test_round_trip_and_simulation_is_kept_apart_from_live(self):
        record_closed_trade(self.session, self.account.id, self.opening, -12.5, simulated=True)
        self.session.flush()
        trades = SqlDataStore(self.session, simulated=True).recent_closed_trades(self.account.id)
        self.assertEqual(len(trades), 1)
        self.assertEqual((trades[0].pnl, trades[0].strategies, trades[0].regime), (-12.5, ["momentum"], "BEAR"))
        self.assertEqual(SqlDataStore(self.session, simulated=False).recent_closed_trades(self.account.id), [])


class _BrokenStore:
    def recent_closed_trades(self, account_id):
        raise RuntimeError("database down")


class TestAgentFailsSafe(unittest.TestCase):
    def test_supervisor_failure_leaves_normal_rules(self):
        agent = NeuravexAgent(
            exchange=None, strategies=[], store=_BrokenStore(),
            config=AgentConfig(symbols=[], supervisor_enabled=True), account_id="a", clock=lambda: NOW,
        )
        verdict = agent._load_supervisor_verdict()
        self.assertEqual(verdict.risk_multiplier, 1.0)
        self.assertEqual(verdict.blocked, set())


if __name__ == "__main__":
    unittest.main()
