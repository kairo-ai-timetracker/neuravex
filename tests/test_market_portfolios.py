import asyncio
import unittest
from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from backend.app.api.portfolios import ensure_portfolios, market_of
from backend.app.api.settings import AccountSettingsUpdate, update_settings
from backend.app.db.sql_data_store import SqlDataStore
from backend.app.models import models as m
from trading_engine.execution.market_universe import symbols_for, yahoo_ticker
from trading_engine.execution.yahoo_market_data import candles_from_chart, session_is_open


class TestPortfolios(unittest.TestCase):
    def setUp(self):
        engine = create_engine("sqlite://")
        m.Base.metadata.create_all(engine)
        self.db = Session(engine)
        self.user = m.User(email="a@b.c", hashed_password="x")
        self.db.add(self.user)
        self.db.flush()
        self.crypto = m.Account(user_id=self.user.id, name="default")
        self.db.add(self.crypto)
        self.db.flush()
        self.db.add(m.AccountSettings(
            account_id=self.crypto.id, symbols=[], paper_starting_balance=11_000.0, min_balance_floor=5_600.0,
        ))
        self.db.flush()

    def tearDown(self):
        self.db.close()

    def test_creates_each_market_once_with_the_crypto_amount_and_stop(self):
        first = ensure_portfolios(self.db, self.crypto)
        self.assertEqual([p.market for p in first], ["crypto", "metals", "stocks"])
        self.assertEqual(first[0].account_id, self.crypto.id)
        second = ensure_portfolios(self.db, self.crypto)
        self.assertEqual([p.account_id for p in first], [p.account_id for p in second])

        row = self.db.query(m.AccountSettings).filter_by(account_id=first[1].account_id).one()
        self.assertEqual(row.paper_starting_balance, 11_000.0)
        self.assertEqual(row.min_balance_floor, 5_600.0)
        self.assertTrue(row.paper_trading_enabled)
        self.assertEqual(row.symbols, symbols_for("metals"))
        snapshot = self.db.query(m.PortfolioSnapshot).filter_by(account_id=first[1].account_id).one()
        self.assertEqual(snapshot.equity, 11_000.0)

    def test_market_of_treats_the_original_account_as_crypto(self):
        self.assertEqual(market_of(self.crypto), "crypto")

    def test_metals_and_stocks_can_not_go_live(self):
        metals_id = ensure_portfolios(self.db, self.crypto)[1].account_id
        metals = self.db.get(m.Account, metals_id)
        with self.assertRaises(HTTPException) as caught:
            update_settings(metals_id, AccountSettingsUpdate(paper_trading_enabled=False), self.db, metals)
        self.assertEqual(caught.exception.status_code, 400)
        self.assertTrue(self.db.query(m.AccountSettings).filter_by(account_id=metals_id).one().paper_trading_enabled)

    def test_only_the_markets_own_symbols_are_accepted(self):
        metals_id = ensure_portfolios(self.db, self.crypto)[1].account_id
        metals = self.db.get(m.Account, metals_id)
        with self.assertRaises(HTTPException):
            update_settings(metals_id, AccountSettingsUpdate(symbols=["WETH/USDC"]), self.db, metals)
        update_settings(metals_id, AccountSettingsUpdate(symbols=["GOLD/USDC"]), self.db, metals)

    def test_simulated_trades_elsewhere_stay_out_of_the_shared_strategy_record(self):
        self.assertTrue(SqlDataStore(self.db, simulated=True).shares_strategy_record)
        self.assertFalse(SqlDataStore(self.db, simulated=True, shares_strategy_record=False).shares_strategy_record)


class TestYahooParsing(unittest.TestCase):
    def test_metals_map_to_their_etf(self):
        self.assertEqual(yahoo_ticker("GOLD/USDC"), "GLD")
        self.assertEqual(yahoo_ticker("AAPL/USDC"), "AAPL")
        with self.assertRaises(ValueError):
            yahoo_ticker("WETH/USDC")

    def test_hours_group_into_candles_and_gaps_are_skipped(self):
        base = int(datetime(2026, 10, 30, 12, tzinfo=timezone.utc).timestamp())
        chart = {
            "timestamp": [base + 5400, base + 9000, base + 12600, base + 16200],
            "indicators": {"quote": [{
                "open": [10, 11, None, 13], "high": [12, 13, None, 15], "low": [9, 10, None, 12],
                "close": [11, 12, None, 14], "volume": [100, 200, None, 300],
            }]},
        }
        candles = candles_from_chart(chart, 4 * 3600)
        self.assertEqual(len(candles), 2)
        first = candles[0]
        self.assertEqual((first.open, first.high, first.low, first.close, first.volume), (10, 13, 9, 12, 300))
        self.assertEqual(candles[1].close, 14)

    def test_session_clock(self):
        chart = {"meta": {"currentTradingPeriod": {"regular": {"start": 100, "end": 200}}}}
        self.assertFalse(session_is_open(chart, 99))
        self.assertTrue(session_is_open(chart, 100))
        self.assertFalse(session_is_open(chart, 200))


if __name__ == "__main__":
    unittest.main()
