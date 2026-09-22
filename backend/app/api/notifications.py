from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from backend.app.api.auth import require_account_owner
from backend.app.db.session import get_db
from backend.app.models import models as m

router = APIRouter(prefix="/api/notifications", tags=["notifications"])


class PendingNotificationOut(BaseModel):
    id: str
    title: str
    body: str
    created_at: str


class AckResponse(BaseModel):
    id: str
    status: str


@router.get("/{account_id}/pending", response_model=list[PendingNotificationOut])
def get_pending_notifications(
    account_id: str, db: Session = Depends(get_db), _account: m.Account = Depends(require_account_owner)
) -> list[PendingNotificationOut]:
    """
    Polled by the phone's existing background service (the same one that
    already polls /api/execution/{account_id}/pending) — anything returned
    here should be shown as a local notification, then acknowledged via
    POST /{id}/ack so it isn't shown again.
    """
    rows = (
        db.query(m.PendingNotification)
        .filter_by(account_id=account_id, delivered_at=None)
        .order_by(m.PendingNotification.created_at.asc())
        .all()
    )
    return [
        PendingNotificationOut(id=str(r.id), title=r.title, body=r.body, created_at=r.created_at.isoformat())
        for r in rows
    ]


@router.post("/{notification_id}/ack", response_model=AckResponse)
def ack_notification(notification_id: str, db: Session = Depends(get_db)) -> AckResponse:
    row = db.query(m.PendingNotification).filter_by(id=notification_id).first()
    if row is None:
        raise HTTPException(status_code=404, detail="Not found")
    row.delivered_at = datetime.utcnow()
    return AckResponse(id=str(row.id), status="acked")
