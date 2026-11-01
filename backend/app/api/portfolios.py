"""
One portfolio per market, each its own Account with its own balance, history and
risk limits. The account the user signed up with is the crypto portfolio; metals
and stocks are simulation only (see market_universe.py).
"""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from backend.app.api.auth import require_account_owner
from backend.app.db.session import get_db
from backend.app.models import models as m
from trading_engine.execution.market_universe import METALS, STOCKS, TICKERS_BY_MARKET, symbols_for

router = APIRouter(prefix="/api/portfolios", tags=["portfolios"])

CRYPTO = "crypto"


def market_of(account: m.Account) -> str:
    return account.name if account.name in TICKERS_BY_MARKET else CRYPTO


class PortfolioOut(BaseModel):
    market: str
    account_id: str
    symbols: list[str]


def ensure_portfolios(db: Session, account: m.Account) -> list[PortfolioOut]:
    """Creates any missing market portfolio, starting from the crypto
    portfolio's amount and stop so the results can be compared fairly."""
    accounts = db.query(m.Account).filter_by(user_id=account.user_id).order_by(m.Account.created_at).all()
    crypto = next(a for a in accounts if market_of(a) == CRYPTO)
    crypto_settings = db.query(m.AccountSettings).filter_by(account_id=crypto.id).first()
    start = crypto_settings.paper_starting_balance if crypto_settings else 1000.0
    floor = crypto_settings.min_balance_floor if crypto_settings else None
    by_market = {market_of(a): a for a in accounts}

    for market in (METALS, STOCKS):
        if market in by_market:
            continue
        new = m.Account(user_id=account.user_id, name=market)
        db.add(new)
        db.flush()
        db.add(m.AccountSettings(
            account_id=new.id, symbols=symbols_for(market), min_balance_floor=floor, ai_enabled=True,
            paper_trading_enabled=True, paper_starting_balance=start, paper_balances={}, paper_positions={},
        ))
        db.add(m.PortfolioSnapshot(
            account_id=new.id, equity=start, cash=start, exposure=0.0, drawdown=0.0, open_positions=0,
            market_regime="SIDEWAYS", taken_at=datetime.utcnow(), is_simulated=True,
        ))
        by_market[market] = new
    db.flush()

    return [
        PortfolioOut(
            market=market, account_id=by_market[market].id,
            symbols=[] if market == CRYPTO else symbols_for(market),
        )
        for market in (CRYPTO, METALS, STOCKS)
    ]


@router.post("/{account_id}/ensure", response_model=list[PortfolioOut])
def ensure(
    account_id: str, account: m.Account = Depends(require_account_owner), db: Session = Depends(get_db),
) -> list[PortfolioOut]:
    return ensure_portfolios(db, account)
