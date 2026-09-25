"""
Concrete DataStore (see trading_engine/agent.py's DataStore Protocol)
backed by the SQLAlchemy models. This is what wires the pure trading-engine
logic to Postgres.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from backend.app.models import models as m
from trading_engine.decision_engine import Decision
from trading_engine.risk.risk_engine import PortfolioState


def _json_safe(value):
    """
    Recursively converts numpy scalar types (np.bool_, np.float64,
    np.int64, ...) AND uuid.UUID objects into native Python equivalents
    (str for UUID, native scalar for numpy) so the value can be
    JSON-serialized into a JSON/JSONB column, OR safely bound as a plain
    numeric column parameter.

    Why this is needed: strategy signal `details` dicts and decision-engine
    arithmetic (e.g. `final_score`, built from sums over strategy
    confidence/expected_return values) are built from numpy array
    comparisons and computations. Those operations return numpy scalar
    types, not Python's native bool/float/int — e.g.
    `ema_fast[-1] < ema_slow[-1]` is a `numpy.bool_`, not a `bool`, even
    though it prints identically and behaves like one everywhere except at
    the database boundary. Separately, on this long-lived database, id
    columns on the older tables (accounts, users, ...) are native Postgres
    UUID columns — SQLAlchemy hands those back as uuid.UUID, not str —
    and a `context` dict containing one (e.g. `{"account_id": account.id}`)
    hits the exact same failure one level deeper, inside a dict value
    rather than the top-level parameter.

    Distinct failure modes this fixes:
      1. JSON columns (Signal.raw, AIDecision.score_breakdown,
         SystemEvent.context): Python's stdlib `json` module doesn't know
         how to serialize numpy scalar types or uuid.UUID at all —
         `TypeError: Object of type bool is not JSON serializable` /
         `Object of type UUID is not JSON serializable`.
      2. Plain numeric columns (AIDecision.final_score, Signal.confidence/
         expected_return/risk): psycopg2 has no registered adapter for
         numpy scalar types, so it falls back to stringifying the value
         (producing literal text like "np.float64(0.89...)") and embeds
         that UNESCAPED into the SQL statement instead of safely
         parameterizing it — Postgres then tries to parse "np" as a schema
         name and fails with `InvalidSchemaName: schema "np" does not
         exist`. This is the more dangerous failure mode: because signals
         are inserted as one batch per tick, a single bad value anywhere
         in that batch fails the ENTIRE transaction, silently discarding
         every decision from that tick — not just the offending row.

    Applying this at every value crossing into SQLAlchemy — not just
    inside dicts — is the only reliable fix, since the specific expression
    that produces a numpy scalar or a raw UUID can change over time;
    sanitizing at the single choke point all of it funnels through here is
    more robust than chasing down every individual producing expression.
    """
    if isinstance(value, uuid.UUID):
        return str(value)
    if hasattr(value, "item") and callable(value.item):
        # Covers numpy scalar types (np.bool_, np.float64, np.int64, ...) —
        # .item() is numpy's own documented way to get the native Python
        # equivalent, and native Python types don't have this method, so
        # this never misfires on things that are already safe.
        return value.item()
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    return value


def _safe_decision_action(action: str) -> "m.DecisionAction":
    """
    Value-based lookup into DecisionAction, defaulting to no_trade for any
    value that doesn't match — never raises. See the long comment at this
    function's call site (save_decision) for why this replaced an inline
    membership check that looked equivalent but was always False.
    """
    try:
        return m.DecisionAction(action)
    except ValueError:
        return m.DecisionAction.no_trade


class SqlDataStore:
    def __init__(self, session: Session, simulated: bool = False, fallback_equity: float | None = None):
        self.session = session
        # Which snapshots count as "this account's portfolio": simulation
        # and live rows share one table and must never mix (see
        # PortfolioSnapshot.is_simulated).
        self.simulated = simulated
        self.fallback_equity = fallback_equity

    def save_signal(self, symbol: str, signal) -> None:
        strategy = self.session.query(m.Strategy).filter_by(name=signal.strategy_name).first()
        if strategy is None:
            strategy = m.Strategy(name=signal.strategy_name, family=getattr(signal, "family", "unknown"))
            self.session.add(strategy)
            self.session.flush()
        # Was declared on the model but never actually incremented anywhere
        # — always stuck at its default of 0 regardless of how many
        # signals a strategy produced. Separate from closed_trades_count
        # (which only counts CLOSED, attributed trades, used for the
        # win-rate running average) — this one is exactly what its name
        # says: every signal, whether or not it ever became a trade.
        strategy.total_signals += 1
        safe_details = _json_safe(signal.details)
        row = m.Signal(
            strategy_id=strategy.id, symbol=symbol, direction=signal.direction,
            # Plain numeric (non-JSON) columns — sanitized individually,
            # same reasoning as final_score below, not just the dict
            # fields. See _json_safe's docstring for why this can't be
            # skipped just because these "should already be" native floats.
            confidence=_json_safe(signal.confidence),
            expected_return=_json_safe(signal.expected_return),
            risk=_json_safe(signal.risk),
            time_horizon=signal.time_horizon,
            regime_at_signal=safe_details.get("regime", ""), raw=safe_details,
        )
        self.session.add(row)

    def save_decision(self, decision: Decision, account_id: str) -> str:
        """
        Idempotent by design: the FIRST call for a given Decision object
        inserts a new ai_decisions row and stamps decision.id onto the
        object; every later call for that SAME object (identified by
        decision.id already being set) UPDATEs the fields that can still
        change after the initial save (reasons_for/against, executed)
        instead of inserting a duplicate row.
        Why this matters: agent.py needs a real ai_decisions.id to stamp
        onto the PendingExecution/Order it's about to create — BEFORE the
        decision's final reasons_for text and executed flag are known —
        so it calls this once early (see _process_symbol/_close_position)
        to mint that id, then run_tick()'s own call (as before this
        change) fills in the final state on the same row. Returns the id
        either way, for callers (agent.py) that need it immediately.
        """
        if decision.id is not None:
            row = self.session.query(m.AIDecision).filter_by(id=decision.id).first()
            if row is not None:
                row.reasons_for = decision.reasons_for
                row.reasons_against = decision.reasons_against
                row.executed = decision.executed
                return decision.id
            # Row vanished somehow (shouldn't happen) — fall through and
            # insert fresh rather than silently losing this decision.

        row = m.AIDecision(
            account_id=account_id, symbol=decision.symbol,
            # BUG (found while analyzing trade history — every row in this
            # column had silently been "no_trade" since this table existed):
            # `decision.action in m.DecisionAction.__members__.values()`
            # compares a plain Python string ("BUY"/"SELL"/"NO_TRADE") against
            # the ENUM MEMBERS themselves (DecisionAction.buy, ...), never
            # their .value — DecisionAction is a plain PyEnum, not a str
            # subclass, so that comparison is False for every input, and the
            # ternary always fell through to the `else` branch. `executed`
            # was never affected (it's a plain bool assigned directly), so
            # `action` was the only column silently wrong — the dashboard's
            # per-action breakdown, and any query filtering on it, has never
            # reflected reality. `DecisionAction(decision.action)` on its own
            # already does a correct VALUE-based lookup (that's how Enum's
            # constructor works) — decision.action is always one of exactly
            # "BUY"/"SELL"/"NO_TRADE" (see decision_engine.score_signals),
            # which are exactly this enum's values, so the guard was never
            # actually needed; kept as a defensive try/except in case that
            # invariant is ever broken by a future caller.
            action=_safe_decision_action(decision.action),
            # This scalar Float column is exactly what triggered
            # `psycopg2.errors.InvalidSchemaName: schema "np" does not
            # exist` — decision.score.final_score can be a numpy.float64
            # depending on the exact arithmetic path in
            # decision_engine.score_signals(), and unlike the JSON columns
            # below, this one was NOT covered by the earlier numpy fix.
            final_score=_json_safe(decision.score.final_score) if decision.score else 0.0,
            score_breakdown=_json_safe(vars(decision.score)) if decision.score else {},
            reasons_for=decision.reasons_for, reasons_against=decision.reasons_against,
            contributing_strategies=[s.strategy_name for s in decision.contributing_signals],
            executed=decision.executed,
        )
        self.session.add(row)
        # Needed to populate row.id from the DB default (gen_uuid) before
        # any caller can use it — without this, row.id is None until the
        # session actually commits/flushes on its own schedule.
        self.session.flush()
        decision.id = row.id
        return row.id

    def save_order_result(self, order_result, decision_id: str | None, account_id: str) -> None:
        row = m.Order(
            account_id=account_id,
            decision_id=decision_id, exchange_order_id=order_result.order_id,
            symbol=order_result.symbol, side=m.OrderSide(order_result.side.value),
            order_type=order_result.order_type.value, quantity=order_result.requested_quantity,
            status=m.OrderStatus(order_result.status.value), filled_quantity=order_result.filled_quantity,
            average_fill_price=order_result.average_fill_price, fee=order_result.fee,
        )
        self.session.add(row)

    def save_risk_event(self, event_type: str, severity: str, message: str, context: dict) -> None:
        row = m.SystemEvent(
            event_type=event_type, message=message,
            context=_json_safe({**context, "severity": severity}),
        )
        self.session.add(row)

    def save_pending_execution(self, account_id: str, decision_id: str | None, plan) -> None:
        row = m.PendingExecution(
            account_id=account_id, decision_id=decision_id, symbol=plan.symbol,
            side=m.OrderSide(plan.side), quantity=plan.quantity, order_type=plan.order_type,
            limit_price=plan.limit_price, stop_loss=plan.stop_loss,
            take_profit_1=plan.take_profit_1, take_profit_2=plan.take_profit_2,
            confidence=_json_safe(plan.confidence), reasons_for=plan.reasons_for or [], reasons_against=plan.reasons_against or [],
            expires_at=datetime.utcnow() + timedelta(seconds=plan.expires_in_seconds),
        )
        self.session.add(row)

    def has_pending_close(self, account_id: str, symbol: str) -> bool:
        """
        True if a sell for this symbol has already been proposed and isn't
        resolved yet — either still waiting for the phone to pick it up
        (status="pending") or already claimed by it but not yet reported
        back as executed/failed (status="claimed"). Both count: the
        window between claim and report can easily span a full
        run_agent_tick cycle (a real on-chain confirmation + the phone
        calling back /api/execution/{id}/report takes real time), and a
        second close queued during that window is just as much a
        duplicate as one queued while the first is still "pending".
        Mirrors the identical guard tasks.py's check_live_exits already
        uses for its own take-profit/stop-loss sweep (see its
        `already_pending` check) — this gives agent.py's AI-judged close
        path (trading_engine/agent.py's _process_symbol) the same
        protection, which it was missing entirely.
        """
        return (
            self.session.query(m.PendingExecution)
            .filter_by(account_id=account_id, symbol=symbol, side=m.OrderSide.sell)
            .filter(m.PendingExecution.status.in_(["pending", "claimed"]))
            .first()
            is not None
        )

    def get_portfolio_state(self, account_id: str) -> PortfolioState:
        account = self.session.query(m.Account).filter_by(id=account_id).first()
        if account is None:
            raise ValueError(f"Unknown account_id {account_id}")

        latest_snapshot = (
            self.session.query(m.PortfolioSnapshot)
            .filter_by(account_id=account_id, is_simulated=self.simulated)
            .order_by(m.PortfolioSnapshot.taken_at.desc())
            .first()
        )
        default_equity = self.fallback_equity if self.fallback_equity is not None else account.starting_balance
        equity = latest_snapshot.equity if latest_snapshot else default_equity
        peak_equity = max(
            (
                s.equity for s in
                self.session.query(m.PortfolioSnapshot).filter_by(account_id=account_id, is_simulated=self.simulated)
            ),
            default=equity,
        )

        open_positions_rows = (
            self.session.query(m.Position)
            .filter_by(account_id=account_id, status=m.PositionStatus.open)
            .all()
        )
        # quantity/entry_price (not just notional) are needed by agent.py to
        # close a position on the AI's own sell signal — a full close sells
        # the exact held quantity, not a freshly risk-sized amount, so the
        # closing path needs the real quantity available here.
        open_positions = {
            p.symbol: {"notional": p.quantity * p.entry_price, "quantity": p.quantity, "entry_price": p.entry_price}
            for p in open_positions_rows
        }

        return PortfolioState(
            equity=equity, peak_equity=peak_equity, cash=equity, open_positions=open_positions,
            daily_pnl=0.0, daily_start_equity=equity,
        )
