"""
User-configurable settings — deliberately minimal (spec: "ik doe en denk
zo weinig mogelijk"). This endpoint only ever exposes: which coins, one
absolute balance-floor stop, the venue (exchange vs. wallet), and the
on/off switch. Every other risk parameter is a fixed system default (see
backend/app/models/models.py's AccountSettings docstring) — it is never
part of this API's request/response shape, so there is no way for a
client to accidentally expose or fiddle with it.
"""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from backend.app.api.auth import require_account_owner
from backend.app.db.session import get_db
from backend.app.models import models as m

router = APIRouter(prefix="/api/settings", tags=["settings"])


class AccountSettingsOut(BaseModel):
    account_id: str
    symbols: list[str]
    min_balance_floor: float | None
    max_balance_target: float | None
    ai_enabled: bool
    execution_venue: str
    execution_mode: str
    live_trading_enabled: bool
    paper_trading_enabled: bool
    paper_starting_balance: float
    paper_equity: float = Field(description="Current simulated equity, mark-to-market — matches what the Dashboard shows.")


class AccountSettingsUpdate(BaseModel):
    symbols: list[str] | None = Field(default=None, description="e.g. ['WBTC/USDC', 'WETH/USDC']")
    min_balance_floor: float | None = Field(
        default=None, gt=0, description="Absolute balance amount — the AI stops trading once equity reaches this."
    )
    max_balance_target: float | None = Field(
        default=None, gt=0,
        description="Per-trade take-profit, in USDC-equivalent — any single open position is sold as soon as its own unrealized profit reaches this amount. Not account-wide.",
    )
    ai_enabled: bool | None = Field(default=None, description="The on/off switch for autonomous trading.")
    execution_venue: str | None = Field(default=None, description="'cex' or 'wallet'")
    paper_trading_enabled: bool | None = Field(
        default=None, description="True = simulate every trade against a fictional balance; False = real money."
    )
    paper_starting_balance: float | None = Field(
        default=None, gt=0,
        description="Fictional starting balance for a fresh simulation, in USDC-equivalent. Setting this resets the running simulation.",
    )


@router.get("/{account_id}", response_model=AccountSettingsOut)
def get_settings(
    account_id: str, db: Session = Depends(get_db), account: m.Account = Depends(require_account_owner)
) -> AccountSettingsOut:
    settings_row = db.query(m.AccountSettings).filter_by(account_id=account_id).first()
    if settings_row is None:
        settings_row = m.AccountSettings(account_id=account_id, symbols=[])
        db.add(settings_row)
        db.flush()
    return _to_out(settings_row, account, db)


@router.patch("/{account_id}", response_model=AccountSettingsOut)
def update_settings(
    account_id: str, req: AccountSettingsUpdate, db: Session = Depends(get_db),
    account: m.Account = Depends(require_account_owner),
) -> AccountSettingsOut:
    # NOTE: an empty `symbols` list is allowed here on purpose — e.g. when
    # the Android app switches venue (CEX <-> wallet) it deliberately
    # clears the old selection first, since a CEX symbol like "BTC/USDT"
    # and a wallet symbol like "WBTC/USDC" are never valid on the other
    # venue. The actual "you need at least one coin" requirement is
    # enforced specifically at the point of enabling ai_enabled below —
    # that's the only moment an empty list is actually a problem.
    if req.execution_venue is not None and req.execution_venue not in ("cex", "wallet"):
        raise HTTPException(status_code=400, detail="execution_venue must be 'cex' or 'wallet'")

    settings_row = db.query(m.AccountSettings).filter_by(account_id=account_id).first()
    if settings_row is None:
        settings_row = m.AccountSettings(account_id=account_id, symbols=[])
        db.add(settings_row)
        db.flush()

    if req.ai_enabled and not settings_row.min_balance_floor and req.min_balance_floor is None:
        raise HTTPException(
            status_code=400,
            detail="Set your balance-floor stop before enabling AI trading.",
        )
    if req.ai_enabled and not (settings_row.symbols or req.symbols):
        raise HTTPException(status_code=400, detail="Select at least one coin before enabling AI trading.")

    changes = req.model_dump(exclude_unset=True, exclude_none=True)
    # exclude_none=True drops None fields, but an empty LIST (symbols=[])
    # is not None and survives — that's intentional, see the note above.
    if req.symbols is not None:
        changes["symbols"] = req.symbols
    for field_name, value in changes.items():
        setattr(settings_row, field_name, value)
    if req.paper_starting_balance is not None:
        # A new fictional starting amount means a fresh simulation, not a
        # top-up of the running one — reset the persisted running
        # balances AND cost basis so the next tick seeds cleanly from the
        # new amount (see tasks.py).
        settings_row.paper_balances = {}
        settings_row.paper_positions = {}
        # Write the fresh starting point as a simulation snapshot right
        # away. Until the next tick there would otherwise be no simulated
        # snapshot at all (or only the pre-reset one), so Dashboard and
        # Settings showed stale or real-wallet numbers after a reset.
        # The old simulation's snapshots go too: they would otherwise keep
        # inflating the drawdown peak (a reset from 1020 to 500 would look
        # like a 51% loss to the risk engine).
        db.query(m.PortfolioSnapshot).filter_by(account_id=account_id, is_simulated=True).delete()
        db.add(m.PortfolioSnapshot(
            account_id=account_id, equity=req.paper_starting_balance, cash=req.paper_starting_balance,
            exposure=0.0, drawdown=0.0, open_positions=0, market_regime="SIDEWAYS",
            taken_at=datetime.utcnow(), is_simulated=True,
        ))
    db.add(settings_row)

    return _to_out(settings_row, account, db)


def _to_out(settings_row: m.AccountSettings, account: m.Account, db: Session) -> AccountSettingsOut:
    # The mark-to-market equity from the most recent PortfolioSnapshot —
    # the exact same number the Dashboard's "Portfolio" figure comes from
    # (see dashboard.py's get_overview). Previously this showed only the
    # raw USDC cash portion of paper_balances, which is why the two
    # screens could show different numbers (e.g. €2261 here vs. €10001 on
    # the Dashboard) once the simulation held anything other than USDC —
    # the cash-only figure ignored every open simulated position's value.
    latest_snapshot = (
        db.query(m.PortfolioSnapshot)
        .filter_by(account_id=settings_row.account_id, is_simulated=True)
        .order_by(m.PortfolioSnapshot.taken_at.desc())
        .first()
    )
    paper_equity = latest_snapshot.equity if latest_snapshot else settings_row.paper_starting_balance
    return AccountSettingsOut(
        # str(...): account_settings.account_id is a foreign key to
        # accounts.id, and on this long-lived database accounts.id is a
        # native Postgres UUID column (from an earlier schema version) —
        # SQLAlchemy hands that back as a Python uuid.UUID object, which
        # Pydantic's `str` field type rejects outright rather than
        # silently coercing. Same root cause as the JWT "Object of type
        # UUID is not JSON serializable" bug fixed earlier in
        # core/auth.py — coerce explicitly at every boundary where an ID
        # sourced from one of these older tables crosses into a Pydantic
        # response model, since the ORM offers no guarantee the live
        # column type matches this model's current type annotation.
        account_id=str(settings_row.account_id), symbols=settings_row.symbols,
        min_balance_floor=settings_row.min_balance_floor,
        max_balance_target=settings_row.max_balance_target,
        ai_enabled=settings_row.ai_enabled,
        execution_venue=settings_row.execution_venue, execution_mode=settings_row.execution_mode,
        live_trading_enabled=account.live_trading_enabled,
        paper_trading_enabled=settings_row.paper_trading_enabled,
        paper_starting_balance=settings_row.paper_starting_balance,
        paper_equity=paper_equity,
    )
