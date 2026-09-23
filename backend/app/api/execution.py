from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from backend.app.api.auth import require_account_owner
from backend.app.db.session import get_db
from backend.app.models import models as m

router = APIRouter(prefix="/api/execution", tags=["execution"])


class PendingExecutionOut(BaseModel):
    id: str
    symbol: str
    side: str
    quantity: float
    order_type: str
    limit_price: float | None
    stop_loss: float | None
    take_profit_1: float | None
    take_profit_2: float | None
    confidence: float
    reasons_for: list[str]
    reasons_against: list[str]
    expires_at: str


class ClaimResponse(BaseModel):
    id: str
    status: str


class ReportExecutionRequest(BaseModel):
    status: str  # "executed" | "failed"
    exchange_order_id: str | None = None
    average_fill_price: float | None = None
    filled_quantity: float | None = None
    failure_reason: str | None = None


@router.get("/{account_id}/pending", response_model=list[PendingExecutionOut])
def get_pending_executions(
    account_id: str, db: Session = Depends(get_db), _account: m.Account = Depends(require_account_owner)
) -> list[PendingExecutionOut]:
    now = datetime.utcnow()
    rows = (
        db.query(m.PendingExecution)
        .filter_by(account_id=account_id, status="pending")
        .filter(m.PendingExecution.expires_at > now)
        .all()
    )
    return [
        PendingExecutionOut(
            # str(r.id): same defensive coercion as settings.py's _to_out —
            # pending_executions may predate this session's model
            # reconstruction and carry a native UUID column rather than
            # the varchar this model declares.
            id=str(r.id), symbol=r.symbol, side=r.side.value, quantity=r.quantity, order_type=r.order_type,
            limit_price=r.limit_price, stop_loss=r.stop_loss, take_profit_1=r.take_profit_1,
            take_profit_2=r.take_profit_2, confidence=r.confidence, reasons_for=r.reasons_for or [],
            reasons_against=r.reasons_against or [], expires_at=r.expires_at.isoformat(),
        )
        for r in rows
    ]


@router.post("/{execution_id}/claim", response_model=ClaimResponse)
def claim_execution(execution_id: str, db: Session = Depends(get_db)) -> ClaimResponse:
    row = db.query(m.PendingExecution).filter_by(id=execution_id).first()
    if row is None:
        raise HTTPException(status_code=404, detail="Not found")
    if row.status != "pending":
        raise HTTPException(status_code=409, detail=f"Already {row.status}")
    if row.expires_at < datetime.utcnow():
        row.status = "expired"
        raise HTTPException(status_code=410, detail="Expired")
    row.status = "claimed"
    row.claimed_at = datetime.utcnow()
    return ClaimResponse(id=str(row.id), status=row.status)


@router.post("/{execution_id}/report")
def report_execution(execution_id: str, req: ReportExecutionRequest, db: Session = Depends(get_db)) -> dict:
    row = db.query(m.PendingExecution).filter_by(id=execution_id).first()
    if row is None:
        raise HTTPException(status_code=404, detail="Not found")
    row.status = req.status
    row.exchange_order_id = req.exchange_order_id
    row.average_fill_price = req.average_fill_price
    row.filled_quantity = req.filled_quantity
    row.failure_reason = req.failure_reason

    # Previously missing entirely: this endpoint only ever updated the
    # PendingExecution row itself. The Dashboard's "Open positions" list
    # (for a live account) reads the separate `positions` table, which
    # nothing ever wrote to for a phone-executed trade — so it stayed
    # empty even while the wallet visibly held real tokens. Live per-trade
    # take-profit (check_live_exits in tasks.py) also depends on this: it
    # needs a recorded entry price per open position to know what "profit"
    # even means.
    if req.status == "executed" and req.filled_quantity and req.average_fill_price:
        _apply_fill_to_position(db, row)

    return {"status": "recorded"}


def _apply_fill_to_position(db: Session, execution: m.PendingExecution) -> None:
    """
    Mirrors, for LIVE trades, the same single-row-per-asset weighted-
    average-cost-basis model already used for simulation (see tasks.py's
    paper_balances/paper_positions and _apply_per_trade_take_profit) —
    one open Position per (account, symbol), not a separate row per buy
    lot. A BUY grows the position (or opens a new one) and re-averages
    entry_price; a SELL (whether from a strategy's own signal or from
    check_live_exits' take-profit) shrinks it and closes it once fully
    sold. Spot-only by design (same guard as agent.py's SHORT check), so a
    SELL with no matching open position is a bug elsewhere, not something
    to silently paper over by inventing a short position here.
    """
    existing = (
        db.query(m.Position)
        .filter_by(account_id=execution.account_id, symbol=execution.symbol, status=m.PositionStatus.open)
        .first()
    )
    qty = execution.filled_quantity
    price = execution.average_fill_price

    if execution.side == m.OrderSide.buy:
        if existing is None:
            db.add(m.Position(
                account_id=execution.account_id, symbol=execution.symbol, side=m.OrderSide.buy,
                quantity=qty, entry_price=price,
                stop_loss=execution.stop_loss, take_profit_1=execution.take_profit_1,
                take_profit_2=execution.take_profit_2,
                # Which ai_decisions row (and therefore which strategies)
                # opened this position — see _update_strategy_performance
                # below, which is what eventually reads this back. Only
                # set on a genuinely NEW position; an add-on buy below
                # deliberately leaves an existing position's
                # opening_decision_id untouched.
                opening_decision_id=execution.decision_id,
            ))
        else:
            total_qty = existing.quantity + qty
            existing.entry_price = (existing.entry_price * existing.quantity + price * qty) / total_qty
            existing.quantity = total_qty
    else:
        if existing is None:
            return
        # Realized P&L for THIS fill, in USDC (spot/long-only — see the
        # side=buy hardcoded above; there is no short-position variant to
        # branch on). Accumulated across however many sell fills it takes
        # to fully close the position, since check_live_exits and agent.py
        # both normally sell the full held quantity in one fill, but
        # nothing here assumes that.
        realized = (price - existing.entry_price) * qty
        existing.realized_pnl = (existing.realized_pnl or 0.0) + realized
        # Captured BEFORE zeroing existing.quantity below — this is what
        # _update_strategy_performance normalizes realized_pnl by. In the
        # normal case (every exit path in this codebase sells the FULL
        # held quantity in one fill — see agent.py/_close_position and
        # tasks.py/check_live_exits) this equals the position's entire
        # size; using existing.quantity AFTER it's zeroed would divide by
        # zero and silently fall back to the raw (non-per-unit) total.
        quantity_closed = existing.quantity
        existing.quantity -= qty
        if existing.quantity <= 1e-9:
            existing.status = m.PositionStatus.closed
            existing.quantity = 0.0
            existing.closed_at = datetime.utcnow()
            _update_strategy_performance(db, existing, quantity_closed)


def _update_strategy_performance(db: Session, position: m.Position, quantity_closed: float) -> None:
    """
    Closes the feedback loop decision_engine.score_signals() was always
    built for (its strategy_performance_score term) but that nothing ever
    actually computed until now: whichever strategies contributed to the
    decision that OPENED this position (position.opening_decision_id ->
    AIDecision.contributing_strategies) get their historical_win_rate and
    historical_expectancy updated from this position's real, realized
    P&L — via a running average, so this never needs to replay past
    trades. NeuravexAgent picks the updated numbers back up on the very
    next tick (see tasks.py's _compute_strategy_performance), fed into
    exactly the score term that was always there waiting for it.

    Deliberately per-unit (realized_pnl / quantity), not the raw total —
    a strategy shouldn't look better or worse just because a particular
    trade happened to be sized bigger or smaller.

    A position with no opening_decision_id (opened before this feature
    existed, or adopted from an untracked wallet balance — see
    dashboard.py's _reconcile_untracked_positions) has no strategy to
    credit or blame, and is skipped entirely: silently doing nothing is
    correct here, not a bug to paper over.
    """
    if position.opening_decision_id is None or not position.realized_pnl:
        return
    opening_decision = db.query(m.AIDecision).filter_by(id=position.opening_decision_id).first()
    if opening_decision is None or not opening_decision.contributing_strategies:
        return

    pnl_per_unit = position.realized_pnl / quantity_closed if quantity_closed else position.realized_pnl
    won = position.realized_pnl > 0

    for strategy_name in opening_decision.contributing_strategies:
        strategy = db.query(m.Strategy).filter_by(name=strategy_name).first()
        if strategy is None:
            continue
        n = strategy.closed_trades_count
        new_n = n + 1
        # Incremental (running) average — equivalent to recomputing the
        # mean over every attributed trade so far, but as a single O(1)
        # update rather than replaying history.
        strategy.historical_win_rate = (strategy.historical_win_rate * n + (1.0 if won else 0.0)) / new_n
        strategy.historical_expectancy = (strategy.historical_expectancy * n + pnl_per_unit) / new_n
        strategy.closed_trades_count = new_n
