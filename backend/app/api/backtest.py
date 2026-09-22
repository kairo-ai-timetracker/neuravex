from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from trading_engine.backtesting.engine import run_backtest
from trading_engine.execution.exchange_interface import Candle
from trading_engine.strategies.breakout import BreakoutStrategy
from trading_engine.strategies.mean_reversion import MeanReversionStrategy
from trading_engine.strategies.momentum import MomentumStrategy
from trading_engine.strategies.trend_following import TrendFollowingStrategy

router = APIRouter(prefix="/api/backtest", tags=["backtest"])


class CandleIn(BaseModel):
    open_time: str
    open: float
    high: float
    low: float
    close: float
    volume: float


class BacktestRequest(BaseModel):
    symbol: str
    candles: list[CandleIn]
    starting_equity: float = 10_000.0


class BacktestResponse(BaseModel):
    final_equity: float
    total_return: float
    max_drawdown: float
    win_rate: float
    trade_count: int


@router.post("/run", response_model=BacktestResponse)
def run(req: BacktestRequest) -> BacktestResponse:
    from datetime import datetime
    candles = [
        Candle(
            open_time=datetime.fromisoformat(c.open_time), open=c.open, high=c.high,
            low=c.low, close=c.close, volume=c.volume,
        )
        for c in req.candles
    ]
    strategies = [TrendFollowingStrategy(), MomentumStrategy(), MeanReversionStrategy(), BreakoutStrategy()]
    result = run_backtest(
        symbol=req.symbol, candles=candles, strategies=strategies, starting_equity=req.starting_equity,
    )
    return BacktestResponse(
        final_equity=result.final_equity, total_return=result.total_return,
        max_drawdown=result.max_drawdown, win_rate=result.win_rate, trade_count=len(result.trades),
    )
