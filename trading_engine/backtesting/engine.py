"""
Walk-forward backtesting engine. No look-ahead bias: at each step, only
candles up to and including the current index are visible to strategies.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from trading_engine.decision_engine import score_signals
from trading_engine.regime.regime_detector import detect_regime
from trading_engine.risk.position_sizing import calculate_position_size
from trading_engine.risk.risk_plan import build_risk_plan
from trading_engine.signals.indicators import atr
from trading_engine.strategies.base import OHLCV, Strategy


@dataclass
class Trade:
    symbol: str
    direction: str
    entry_index: int
    entry_price: float
    exit_index: int | None = None
    exit_price: float | None = None
    quantity: float = 0.0
    pnl: float = 0.0
    exit_reason: str = ""


@dataclass
class BacktestResult:
    trades: list[Trade] = field(default_factory=list)
    equity_curve: list[float] = field(default_factory=list)
    final_equity: float = 0.0
    max_drawdown: float = 0.0
    win_rate: float = 0.0
    total_return: float = 0.0


def run_backtest(
    *,
    symbol: str,
    candles: list,
    strategies: list[Strategy],
    starting_equity: float = 10_000.0,
    max_trade_risk: float = 0.01,
    max_position_size: float = 0.20,
    minimum_confidence: float = 0.70,
    minimum_expected_edge: float = 0.003,
    fee_rate: float = 0.001,
    slippage_bps: float = 5.0,
    spot_only: bool = True,
    warmup_bars: int = 60,
) -> BacktestResult:
    closes = np.array([c.close for c in candles])
    highs = np.array([c.high for c in candles])
    lows = np.array([c.low for c in candles])
    volumes = np.array([c.volume for c in candles])

    equity = starting_equity
    peak_equity = equity
    equity_curve = [equity]
    trades: list[Trade] = []
    open_trade: Trade | None = None
    max_dd = 0.0

    for i in range(warmup_bars, len(candles)):
        window = OHLCV(
            open_time=np.arange(i + 1), open=np.array([c.open for c in candles[: i + 1]]),
            high=highs[: i + 1], low=lows[: i + 1], close=closes[: i + 1], volume=volumes[: i + 1],
        )
        regime = detect_regime(window.high, window.low, window.close)

        signals = [s for s in (strat.generate_signal(symbol, window, regime) for strat in strategies) if s]
        decision = score_signals(
            symbol=symbol, signals=signals, regime=regime, strategy_performance={},
            volume_confirmation_score=1.0 if volumes[i] > np.mean(volumes[max(0, i - 20):i]) else 0.0,
            correlation_penalty=0.0, estimated_transaction_cost=fee_rate,
            minimum_confidence=minimum_confidence, minimum_expected_edge=minimum_expected_edge,
        )

        price = float(closes[i])

        if open_trade is not None:
            atr_val = float(atr(window.high, window.low, window.close)[-1])
            plan = build_risk_plan(entry=open_trade.entry_price, direction=open_trade.direction, atr_value=atr_val)
            hit_stop = (
                price <= plan.stop_loss if open_trade.direction == "LONG" else price >= plan.stop_loss
            )
            hit_target = (
                price >= plan.take_profit_1 if open_trade.direction == "LONG" else price <= plan.take_profit_1
            )
            opposite_signal = decision.action in ("BUY", "SELL") and (
                (decision.action == "SELL" and open_trade.direction == "LONG")
                or (decision.action == "BUY" and open_trade.direction == "SHORT")
            )
            if hit_stop or hit_target or opposite_signal:
                exit_price = price * (1 - slippage_bps / 10_000) if open_trade.direction == "LONG" else price * (1 + slippage_bps / 10_000)
                fee = exit_price * open_trade.quantity * fee_rate
                if open_trade.direction == "LONG":
                    pnl = (exit_price - open_trade.entry_price) * open_trade.quantity - fee
                else:
                    pnl = (open_trade.entry_price - exit_price) * open_trade.quantity - fee
                open_trade.exit_index = i
                open_trade.exit_price = exit_price
                open_trade.pnl = pnl
                open_trade.exit_reason = "stop" if hit_stop else ("target" if hit_target else "opposite_signal")
                equity += pnl
                trades.append(open_trade)
                open_trade = None

        elif decision.action in ("BUY", "SELL"):
            direction = "LONG" if decision.action == "BUY" else "SHORT"
            if direction == "SHORT" and spot_only:
                pass  # spot_only: cannot open a short without an existing holding
            else:
                atr_val = float(atr(window.high, window.low, window.close)[-1])
                plan = build_risk_plan(entry=price, direction=direction, atr_value=atr_val)
                size = calculate_position_size(
                    account_equity=equity, entry_price=price, stop_loss_price=plan.stop_loss,
                    confidence=decision.confidence, max_trade_risk=max_trade_risk,
                    max_position_size_fraction=max_position_size,
                    current_portfolio_exposure_fraction=0.0, max_portfolio_exposure_fraction=1.0,
                )
                if size.quantity > 0:
                    entry_price = price * (1 + slippage_bps / 10_000) if direction == "LONG" else price * (1 - slippage_bps / 10_000)
                    open_trade = Trade(
                        symbol=symbol, direction=direction, entry_index=i,
                        entry_price=entry_price, quantity=size.quantity,
                    )

        peak_equity = max(peak_equity, equity)
        drawdown = (peak_equity - equity) / peak_equity if peak_equity else 0.0
        max_dd = max(max_dd, drawdown)
        equity_curve.append(equity)

    wins = [t for t in trades if t.pnl > 0]
    win_rate = len(wins) / len(trades) if trades else 0.0

    return BacktestResult(
        trades=trades, equity_curve=equity_curve, final_equity=equity,
        max_drawdown=max_dd, win_rate=win_rate,
        total_return=(equity - starting_equity) / starting_equity if starting_equity else 0.0,
    )
