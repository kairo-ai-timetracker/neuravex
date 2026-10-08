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
    # Only pending BUYS are cancelled: they would add exposure. Pending
    # SELLS (circuit-breaker flatten, close-all, take-profit, hard
    # stop-loss) only reduce it — cancelling them would silently undo a
    # close the user or a safety stop already asked for, e.g. pressing
    # close-all and then the kill switch.
    pending = (
        db.query(m.PendingExecution)
        .filter_by(account_id=account_id, status="pending", side=m.OrderSide.buy)
        .all()
    )
    for p in pending:
        p.status = "cancelled"
    kept_sells = (
        db.query(m.PendingExecution)
        .filter_by(account_id=account_id, status="pending", side=m.OrderSide.sell)
        .count()
    )
    return {"status": "halted", "cancelled_pending": len(pending), "kept_pending_sells": kept_sells}


@router.post("/{account_id}/close-all")
def close_all_positions(
    account_id: str, db: Session = Depends(get_db), account: m.Account = Depends(require_account_owner)
) -> dict:
    """
    Actually SELLS every open position — previously this only flipped
    Position.status to "closed" in the database: no order was ever placed
    (a live position was labelled closed while still held), and simulation
    accounts (whose positions live in AccountSettings.paper_balances, not
    the `positions` table) weren't touched at all. Now shares the circuit
    breaker's flatten path (tasks.close_all_positions_for_account ->
    NeuravexAgent.flatten_all_positions). For live phone-mode accounts the
    sells are queued for the phone; each Position closes only once its
    fill is reported back (execution.py's _apply_fill_to_position).
    """
    # Imported here, not at module top: tasks pulls in Celery and the
    # whole trading engine, which this router otherwise doesn't need.
    from backend.app.core.tasks import close_all_positions_for_account

    result = close_all_positions_for_account(db, account)
    db.add(m.SystemEvent(
        event_type="manual_close_all", message=f"Manual close-all requested for account {account_id}",
        context={**result, "account_id": str(account_id)},
    ))
    if not result["open_positions"]:
        status = "nothing_open"
    else:
        status = "failed" if result["failed"] else "submitted"
    return {"status": status, "count": len(result["submitted"]), **result}
