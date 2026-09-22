from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from backend.app.api.auth import require_account_owner
from backend.app.db.session import get_db
from backend.app.models import models as m

router = APIRouter(prefix="/api/emergency", tags=["emergency"])


@router.post("/{account_id}/kill-switch")
def activate_kill_switch(
    account_id: str, db: Session = Depends(get_db), account: m.Account = Depends(require_account_owner)
) -> dict:
    settings_row = db.query(m.AccountSettings).filter_by(account_id=account_id).first()
    if settings_row is not None:
        settings_row.ai_enabled = False
    account.live_trading_enabled = False
    pending = db.query(m.PendingExecution).filter_by(account_id=account_id, status="pending").all()
    for p in pending:
        p.status = "cancelled"
    return {"status": "halted", "cancelled_pending": len(pending)}


@router.post("/{account_id}/close-all")
def close_all_positions(
    account_id: str, db: Session = Depends(get_db), _account: m.Account = Depends(require_account_owner)
) -> dict:
    positions = db.query(m.Position).filter_by(account_id=account_id, status=m.PositionStatus.open).all()
    for p in positions:
        p.status = m.PositionStatus.closed
    return {"status": "closed_all", "count": len(positions)}
