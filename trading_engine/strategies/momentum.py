from __future__ import annotations

import numpy as np

from trading_engine.regime.regime_detector import MarketRegime, RegimeReading
from trading_engine.signals.indicators import atr, macd, rate_of_change, rsi
from trading_engine.strategies.base import OHLCV, Strategy, StrategySignal


class MomentumStrategy(Strategy):
    """RSI + MACD + rate-of-change + volume confirmation."""

    name = "momentum_rsi_macd"
    family = "momentum"
    allowed_regimes = {MarketRegime.STRONG_BULL, MarketRegime.BULL, MarketRegime.SIDEWAYS}

    def __init__(self, rsi_period: int = 14, roc_period: int = 10, volume_lookback: int = 20):
        self.rsi_period = rsi_period
        self.roc_period = roc_period
        self.volume_lookback = volume_lookback

    def generate_signal(self, symbol: str, data: OHLCV, regime: RegimeReading) -> StrategySignal | None:
        if not self.is_regime_allowed(regime):
            return None
        if len(data.close) < max(self.rsi_period, 35) + 5:
            return None

        rsi_vals = rsi(data.close, self.rsi_period)
        macd_line, signal_line, hist = macd(data.close)
        roc = rate_of_change(data.close, self.roc_period)

        current_rsi = float(rsi_vals[-1])
        macd_bullish_cross = hist[-2] <= 0 and hist[-1] > 0
        macd_bearish_cross = hist[-2] >= 0 and hist[-1] < 0
        momentum_up = float(roc[-1]) > 0 if not np.isnan(roc[-1]) else False

        avg_volume = float(np.mean(data.volume[-self.volume_lookback:-1])) if len(data.volume) > self.volume_lookback else 0.0
        volume_confirmed = avg_volume > 0 and data.volume[-1] > 1.3 * avg_volume

        price = float(data.close[-1])
        atr_val = float(atr(data.high, data.low, data.close)[-1])
        risk = atr_val / price if price else 0.02

        bullish_votes = sum([
            40 <= current_rsi <= 70,  # momentum up but not overbought yet
            bool(macd_bullish_cross or hist[-1] > 0),
            momentum_up,
            volume_confirmed,
        ])
        bearish_votes = sum([
            30 <= current_rsi <= 60 and current_rsi < 50,
            bool(macd_bearish_cross or hist[-1] < 0),
            not momentum_up,
            volume_confirmed,
        ])

        if bullish_votes >= 3 and current_rsi < 75:
            confidence = 0.5 + 0.1 * bullish_votes
            return StrategySignal(
                symbol=symbol, direction="LONG", confidence=min(confidence, 0.92),
                expected_return=risk * 1.8, risk=risk, time_horizon="1h",
                strategy_name=self.name,
                details={"rsi": current_rsi, "macd_hist": float(hist[-1]), "volume_confirmed": bool(volume_confirmed)},
            )
        if bearish_votes >= 3 and current_rsi > 25:
            confidence = 0.5 + 0.1 * bearish_votes
            return StrategySignal(
                symbol=symbol, direction="SHORT", confidence=min(confidence, 0.88),
                expected_return=risk * 1.6, risk=risk, time_horizon="1h",
                strategy_name=self.name,
                details={"rsi": current_rsi, "macd_hist": float(hist[-1]), "volume_confirmed": bool(volume_confirmed)},
            )
        return None
