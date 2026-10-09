import unittest
from datetime import datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from backend.app.db.sql_data_store import SqlDataStore
from backend.app.models import models as m
from trading_engine.risk.risk_engine import DRAWDOWN_HALT_COOLDOWN


class TestDrawdownCooldown(unittest.TestCase):
    def setUp(self):
        engine = create_engine("sqlite://")
        m.Base.metadata.create_all(engine)
        self.session = Session(engine)
        self.account = m.Account(user_id="u1")
        self.session.add(self.account)
        self.session.flush()
        self.store = SqlDataStore(self.session, simulated=True)
        self.now = datetime.utcnow()

    def tearDown(self):
        self.session.close()

    def _snapshot(self, equity, age):
        self.session.add(m.PortfolioSnapshot(
            account_id=self.account.id, equity=equity, cash=equity, taken_at=self.now - age, is_simulated=True,
        ))

    def _halt(self, age):
        self.session.add(m.RiskEvent(
            account_id=self.account.id, event_type="drawdown_halt_sim", severity="critical", message="x",
            created_at=self.now - age,
        ))

    def test_peak_kept_while_halt_is_active(self):
        self._snapshot(11_000, timedelta(days=3))
        self._halt(timedelta(days=2))
        self._snapshot(9_800, timedelta(hours=1))
        self.session.flush()
        self.assertEqual(self.store.drawdown_peak_equity(self.account.id), 11_000)

    def test_fresh_peak_after_halt_expires(self):
        self._snapshot(11_000, timedelta(days=10))
        self._halt(timedelta(days=9))
        self._snapshot(9_800, timedelta(hours=1))  # after the 7-day stop ended
        self.session.flush()
        self.assertEqual(self.store.drawdown_peak_equity(self.account.id), 9_800)
        state = self.store.get_portfolio_state(self.account.id)
        self.assertEqual(state.peak_equity, 9_800)

    def test_halt_recorded_once_per_stop(self):
        self.store.note_drawdown_halt(self.account.id, "breach")
        self.store.note_drawdown_halt(self.account.id, "breach again")
        self.session.flush()
        self.assertEqual(self.session.query(m.RiskEvent).count(), 1)
        resumes = self.session.query(m.RiskEvent).one().context["resumes_at"]
        self.assertGreater(datetime.fromisoformat(resumes), self.now + DRAWDOWN_HALT_COOLDOWN - timedelta(minutes=1))

    def test_live_and_simulation_halts_are_separate(self):
        self._snapshot(11_000, timedelta(days=10))
        self._halt(timedelta(days=9))
        self.session.flush()
        live = SqlDataStore(self.session, simulated=False)
        self.assertIsNone(live.latest_drawdown_halt(self.account.id))


if __name__ == "__main__":
    unittest.main()
