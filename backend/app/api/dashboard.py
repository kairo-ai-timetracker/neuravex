from __future__ import annotations

import logging
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import desc, func
from sqlalchemy.orm import Session

from backend.app.api.auth import require_account_owner
from backend.app.db.session import get_db
from backend.app.models import models as m
from backend.app.schemas.schemas import AccountOverview, DailyDecisionStats, DecisionOut, PositionOut

logger = logging.getLogger("neuravex.dashboard")

router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])


def _get_or_create_settings(db: Session, account_id: str) -> m.AccountSettings:
    settings_row = db.query(m.AccountSettings).filter_by(account_id=account_id).first()
    if settings_row is None:
        settings_row = m.AccountSettings(account_id=account_id, symbols=[])
        db.add(settings_row)
        db.flush()
    return settings_row


@router.get("/{account_id}/overview", response_model=AccountOverview)
def get_overview(
    account_id: str, db: Session = Depends(get_db), _account: m.Account = Depends(require_account_owner)
) -> AccountOverview:
    account = db.query(m.Account).filter_by(id=account_id).first()
    if account is None:
        raise HTTPException(status_code=404, detail="Account not found")

    settings_row = _get_or_create_settings(db, account_id)

    # Only snapshots of the CURRENT mode: simulation and live share the
    # table, and the app keeps reporting the real wallet even while the
    # simulation is selected — that is what made a simulation reset show
    # the real ~114 instead of the fictional starting amount.
    latest_snapshot = (
        db.query(m.PortfolioSnapshot)
        .filter_by(account_id=account_id, is_simulated=bool(settings_row.paper_trading_enabled))
        .order_by(desc(m.PortfolioSnapshot.taken_at))
        .first()
    )
    # In simulation, the meaningful "starting point" for P/L is the
    # fictional amount the USER chose (paper_starting_balance). In live
    # mode, account.starting_balance (a fixed default from account
    # creation, unrelated to what's actually in the wallet) used to be
    # used here — that's why "Today's P/L" once showed a nonsensical
    # -€899 the moment live mode was switched on with a real wallet worth
    # €100: the comparison was against a number that had nothing to do
    # with this wallet. The first real balance this account ever reported
    # is a meaningful baseline; a fixed account default is not.
    if settings_row.paper_trading_enabled:
        starting_point = settings_row.paper_starting_balance
    else:
        # Explicit baseline (see AccountSettings.live_start_equity). If it
        # has not been established yet, fall back to the latest reading so
        # P/L shows ~0 rather than the entire balance as "profit" — never
        # to the first snapshot ever, which may be a 0 from an RPC outage.
        starting_point = (
            settings_row.live_start_equity
            if settings_row.live_start_equity is not None
            else (latest_snapshot.equity if latest_snapshot else 0.0)
        )
    equity = latest_snapshot.equity if latest_snapshot else starting_point
    cash = latest_snapshot.cash if latest_snapshot else starting_point
    drawdown = latest_snapshot.drawdown if latest_snapshot else 0.0
    regime = latest_snapshot.market_regime if latest_snapshot else "SIDEWAYS"

    if settings_row.paper_trading_enabled:
        # No Position rows exist for simulated trades (see get_positions
        # below for why) — count non-USDC balances instead, so this
        # matches what "Open positions" actually shows.
        open_positions_count = sum(1 for asset, qty in settings_row.paper_balances.items() if asset != "USDC" and qty > 0)
    else:
        open_positions_count = (
            db.query(m.Position).filter_by(account_id=account_id, status=m.PositionStatus.open).count()
        )

    daily_pnl = equity - starting_point
    daily_pnl_pct = daily_pnl / starting_point if starting_point else 0.0

    return AccountOverview(
        account_id=account_id, equity=equity, cash=cash, daily_pnl=daily_pnl,
        daily_pnl_pct=daily_pnl_pct, drawdown=drawdown, market_regime=regime,
        open_positions_count=open_positions_count, live_trading_enabled=account.live_trading_enabled,
    )


@router.get("/{account_id}/positions", response_model=list[PositionOut])
def get_positions(
    account_id: str, db: Session = Depends(get_db), _account: m.Account = Depends(require_account_owner)
) -> list[PositionOut]:
    settings_row = _get_or_create_settings(db, account_id)

    if settings_row.paper_trading_enabled:
        # Simulated trades never create Position rows — the paper-trading
        # branch in tasks.py only tracks aggregate balances per asset
        # (see AccountSettings.paper_balances), the same way a simple
        # spot wallet does, not a full per-trade position ledger. That's
        # exactly why "Open positions" always showed empty in simulation
        # even while the balance was visibly moving: this endpoint was
        # reading from a table simulation never writes to. Synthesizing
        # a position per non-zero non-USDC asset here is the fix — no
        # entry price is tracked per-position in this model, so the
        # current mark price is shown in its place (labelled as such).
        latest_snapshot = (
            db.query(m.PortfolioSnapshot)
            .filter_by(account_id=account_id, is_simulated=True)
            .order_by(desc(m.PortfolioSnapshot.taken_at))
            .first()
        )
        return [
            PositionOut(
                symbol=f"{asset}/USDC", side="buy", quantity=qty,
                entry_price=settings_row.paper_positions.get(asset, 0.0),
                stop_loss=None, take_profit_1=None, take_profit_2=None,
                opened_at=latest_snapshot.taken_at if latest_snapshot else datetime.utcnow(),
            )
            for asset, qty in settings_row.paper_balances.items()
            if asset != "USDC" and qty > 0
        ]

    positions = db.query(m.Position).filter_by(account_id=account_id, status=m.PositionStatus.open).all()
    return [
        PositionOut(
            symbol=p.symbol, side=p.side.value, quantity=p.quantity, entry_price=p.entry_price,
            stop_loss=p.stop_loss, take_profit_1=p.take_profit_1, take_profit_2=p.take_profit_2,
            opened_at=p.opened_at,
        )
        for p in positions
    ]


@router.get("/{account_id}/decisions", response_model=list[DecisionOut])
def get_recent_decisions(
    account_id: str, limit: int = 20, since_hours: int | None = None,
    db: Session = Depends(get_db), _account: m.Account = Depends(require_account_owner),
) -> list[DecisionOut]:
    """
    since_hours: when given (the phone app always passes 24 — see
    DashboardScreen), only returns decisions from that many hours ago
    onward. The server itself never deletes anything from ai_decisions
    regardless of what any client requests here — every decision this
    account has ever made stays in the database indefinitely (spec: "op
    mijn laptop/server voor altijd"), this parameter only limits what a
    given screen chooses to display.
    """
    query = db.query(m.AIDecision).filter_by(account_id=account_id)
    if since_hours is not None:
        query = query.filter(m.AIDecision.created_at >= datetime.utcnow() - timedelta(hours=since_hours))
    decisions = query.order_by(desc(m.AIDecision.created_at)).limit(limit).all()
    return [
        DecisionOut(
            symbol=d.symbol, action=d.action.value, confidence=0.0, final_score=d.final_score,
            reasons_for=d.reasons_for or [], reasons_against=d.reasons_against or [],
            entry=d.entry, stop_loss=d.stop_loss, take_profit=d.take_profit,
            risk_reward=d.risk_reward, created_at=d.created_at, executed=d.executed,
        )
        for d in decisions
    ]


@router.get("/{account_id}/daily-stats", response_model=list[DailyDecisionStats])
def get_daily_stats(
    account_id: str, days: int = 14, db: Session = Depends(get_db), _account: m.Account = Depends(require_account_owner),
) -> list[DailyDecisionStats]:
    """
    Per-day executed-vs-skipped counts (spec: "wanneer hij trades
    overslaat wil ik dat ook zien per dag, en ook hoeveel hij heeft
    uitgevoerd per dag"). Always reads from the full, permanent
    ai_decisions history on the server — this endpoint only limits how
    many days are *returned*, not what's stored.
    """
    since = datetime.utcnow() - timedelta(days=days)
    day_col = func.date(m.AIDecision.created_at)
    rows = (
        db.query(
            day_col.label("day"),
            func.sum(func.cast(m.AIDecision.executed, m.Integer)).label("executed_count"),
            func.count().label("total_count"),
        )
        .filter(m.AIDecision.account_id == account_id, m.AIDecision.created_at >= since)
        .group_by(day_col)
        .order_by(desc(day_col))
        .all()
    )
    return [
        DailyDecisionStats(
            date=str(row.day), executed_count=int(row.executed_count or 0),
            skipped_count=int(row.total_count) - int(row.executed_count or 0),
        )
        for row in rows
    ]


class AssetBalanceIn(BaseModel):
    """One held asset, as the app's own wallet/DEX balance check already
    computes it (see PolygonDexExecutionClient.getPortfolioValue() —
    previously only summed into the single `equity` total below and
    discarded per-asset, so the backend had no idea WHICH coins were
    actually held)."""
    symbol: str = Field(description="Bare asset symbol, e.g. 'WETH', 'WMATIC', 'WBTC' — not a pair")
    quantity: float = Field(gt=0)
    price_usd: float = Field(gt=0, description="Current price per unit, USD/USDC-equivalent")


class ReportBalanceRequest(BaseModel):
    # ge=0 (not gt=0): a genuinely empty wallet is a valid, real state that
    # should display as $0.00, not be rejected outright. An earlier gt=0
    # constraint caused a 422 the very first time a wallet's computed
    # value came back as exactly 0 (e.g. every per-token Uniswap quote
    # failing for a very small balance) — that's a real value worth
    # showing the user, not an error to hide behind a rejected request.
    equity: float = Field(ge=0, description="Total real wallet value, converted to the account's base currency")
    market_regime: str = "SIDEWAYS"
    # True only when `equity` covers the COMPLETE wallet (Polygon + other
    # networks such as Ethereum mainnet). Only such a report may establish
    # the P/L baseline, so an older app version reporting a partial total
    # can never set a baseline that a later full report would exceed.
    full_wallet: bool = False
    # Optional (default empty, so an older app version stays compatible)
    # per-asset breakdown — see _reconcile_untracked_positions below.
    assets: list[AssetBalanceIn] = Field(default_factory=list)


def _reconcile_untracked_positions(db: Session, account_id: str, assets: list[AssetBalanceIn]) -> None:
    """
    Adopts any real wallet holding that isn't yet tracked as an open
    `Position` row, so the AI's own exit logic (agent.py's SHORT-on-held-
    symbol close) and the fixed/hard-stop safety nets (check_live_exits)
    can actually see and manage it — previously these coins were entirely
    invisible to that machinery, since it only ever reads the `positions`
    table, and simply holding a token in the wallet never wrote a row
    there (only a bot-initiated buy did, via _apply_fill_to_position).

    Deliberately conservative: only creates a position when NONE already
    exists for that symbol — never adjusts or re-averages an existing
    one, to avoid corrupting a real entry price the bot already tracks
    with a blended guess. For a coin adopted this way, there is no way to
    recover what was originally paid for it, so entry_price is set to
    today's reported price — P/L on it is measured from the moment of
    adoption forward, not from whenever it was actually acquired.
    """
    for asset in assets:
        symbol = asset.symbol.upper()
        if symbol == "USDC" or asset.quantity <= 0:
            continue
        pair_symbol = f"{symbol}/USDC"
        existing = (
            db.query(m.Position)
            .filter_by(account_id=account_id, symbol=pair_symbol, status=m.PositionStatus.open)
            .first()
        )
        if existing is not None:
            continue
        db.add(m.Position(
            account_id=account_id, symbol=pair_symbol, side=m.OrderSide.buy,
            quantity=asset.quantity, entry_price=asset.price_usd,
        ))
        logger.info(
            "Adopted untracked wallet holding as a position: account %s %s qty=%.6f @ %.6f "
            "(cost basis = today's price, original purchase price unknown)",
            account_id, pair_symbol, asset.quantity, asset.price_usd,
        )


@router.post("/{account_id}/report-balance")
def report_balance(
    account_id: str, req: ReportBalanceRequest, db: Session = Depends(get_db),
    _account: m.Account = Depends(require_account_owner),
) -> dict:
    """
    Called by the Android app (never the server itself, which holds no
    wallet key and therefore cannot query on-chain balances on its own) to
    report the REAL, on-chain wallet value it just measured directly from
    Polygon. This writes a PortfolioSnapshot row — the same table
    get_overview() already reads from — so the dashboard shows real money
    instead of the account's fixed `starting_balance` placeholder, with no
    further change needed on the read side.
    """
    previous = (
        db.query(m.PortfolioSnapshot)
        .filter_by(account_id=account_id, is_simulated=False)
        .order_by(desc(m.PortfolioSnapshot.taken_at))
        .first()
    )
    peak_equity = max(req.equity, previous.equity) if previous else req.equity
    drawdown = (peak_equity - req.equity) / peak_equity if peak_equity else 0.0

    snapshot = m.PortfolioSnapshot(
        account_id=account_id, equity=req.equity, cash=req.equity, exposure=0.0,
        drawdown=drawdown, open_positions=0, market_regime=req.market_regime,
        is_simulated=False,
    )
    db.add(snapshot)

    if req.assets:
        _reconcile_untracked_positions(db, account_id, req.assets)

    settings_row = _get_or_create_settings(db, account_id)
    if (
        req.full_wallet and req.equity > 0
        and not settings_row.paper_trading_enabled
        and settings_row.live_start_equity is None
    ):
        settings_row.live_start_equity = req.equity
    return {"status": "recorded", "equity": req.equity}


@router.post("/{account_id}/reset-baseline")
def reset_baseline(
    account_id: str, db: Session = Depends(get_db), _account: m.Account = Depends(require_account_owner),
) -> dict:
    """Re-anchor live "Today's P/L" to the most recent reported wallet value."""
    latest = (
        db.query(m.PortfolioSnapshot)
        .filter_by(account_id=account_id, is_simulated=False)
        .order_by(desc(m.PortfolioSnapshot.taken_at))
        .first()
    )
    if latest is None or latest.equity <= 0:
        raise HTTPException(status_code=409, detail="No non-zero wallet balance reported yet")
    settings_row = _get_or_create_settings(db, account_id)
    settings_row.live_start_equity = latest.equity
    return {"status": "reset", "live_start_equity": latest.equity}
