"""
One-off repair script — run this ONCE, then delete it (or just leave it,
it's safe to run again).

report_execution only just started writing to the `positions` table (see
backend/app/api/execution.py's _apply_fill_to_position). Every live trade
your phone executed BEFORE that fix is invisible to "Open positions" and to
the new live per-trade take-profit check (check_live_exits in tasks.py) —
even though your wallet genuinely holds those tokens. This rebuilds
`positions` from your account's actual trade history, which was already
being recorded all along (PendingExecution.average_fill_price /
filled_quantity) — it was just never being read.

It always rebuilds from scratch (deletes this account's positions, then
replays every executed trade in order), so running it twice never
double-counts.

Run from the project root, with the same venv the backend uses:
    .\\.venv\\Scripts\\python.exe backfill_positions.py
"""
from __future__ import annotations

from backend.app.api.execution import _apply_fill_to_position
from backend.app.db.session import get_session
from backend.app.models import models as m

with get_session() as session:
    accounts = session.query(m.Account).all()
    for account in accounts:
        session.query(m.Position).filter_by(account_id=account.id).delete()

        executions = (
            session.query(m.PendingExecution)
            .filter_by(account_id=account.id, status="executed")
            .filter(m.PendingExecution.filled_quantity.isnot(None))
            .filter(m.PendingExecution.average_fill_price.isnot(None))
            .order_by(m.PendingExecution.created_at.asc())
            .all()
        )
        for execution in executions:
            _apply_fill_to_position(session, execution)
        session.flush()

        open_positions = (
            session.query(m.Position)
            .filter_by(account_id=account.id, status=m.PositionStatus.open)
            .all()
        )
        print(f"Account {account.id}: replayed {len(executions)} executed trade(s) -> {len(open_positions)} open position(s)")
        for p in open_positions:
            print(f"  {p.symbol}: qty={p.quantity} entry_price={p.entry_price}")

print("Done.")
