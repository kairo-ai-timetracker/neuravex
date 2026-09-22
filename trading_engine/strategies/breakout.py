from __future__ import annotations

import numpy as np

from trading_engine.regime.regime_detector import MarketRegime, RegimeReading
from trading_engine.signals.indicators import atr
from trading_engine.strategies.base import OHLCV, Strategy, StrategySignal


class BreakoutStrategy(Strategy):
    """Support/resistance breakout confirmed by volatility expansion and volume."""

    name = "breakout_sr_volume"
    family = "breakout"
    allowed_regimes = {
        MarketRegime.STRONG_BULL, MarketRegime.BULL, MarketRegime.SIDEWAYS,
        MarketRegime.HIGH_VOLATILITY,
    }

    def __init__(self, lookback: int = 20, volume_multiplier: float = 1.5, atr_expansion: float = 1.3):
        self.lookback = lookback
        self.volume_multiplier = volume_multiplier
        self.atr_expansion = atr_expansion

    def generate_signal(self, symbol: str, data: OHLCV, regime: RegimeReading) -> StrategySignal | None:
        if not self.is_regime_allowed(regime):
            return None
        if len(data.close) < self.lookback + 20:
            return None
        if regime.market_regime == MarketRegime.CRASH:
            return None

        resistance = float(np.max(data.high[-self.lookback - 1:-1]))
        support = float(np.min(data.low[-self.lookback - 1:-1]))
        price = float(data.close[-1])

        atr_series = atr(data.high, data.low, data.close)
        atr_now = float(atr_series[-1])
        atr_baseline = float(np.mean(atr_series[-self.lookback - 20:-self.lookback])) if len(atr_series) > self.lookback + 20 else atr_now
        vol_expanding = atr_baseline > 0 and atr_now > self.atr_expansion * atr_baseline

        avg_volume = float(np.mean(data.volume[-self.lookback - 1:-1]))
        volume_confirmed = avg_volume > 0 and data.volume[-1] > self.volume_multiplier * avg_volume

        risk = atr_now / price if price else 0.02

        if price > resistance and volume_confirmed and vol_expanding:
            confidence = 0.55 + (0.15 if vol_expanding else 0) + (0.15 if volume_confirmed else 0)
            return StrategySignal(
                symbol=symbol, direction="LONG", confidence=min(confidence, 0.90),
                expected_return=risk * 2.2, risk=risk, time_horizon="4h",
                strategy_name=self.name,
                details={"resistance": resistance, "vol_expanding": bool(vol_expanding), "volume_confirmed": bool(volume_confirmed)},
            )
        if price < support and volume_confirmed and vol_expanding:
            confidence = 0.55 + (0.15 if vol_expanding else 0) + (0.15 if volume_confirmed else 0)
            return StrategySignal(
                symbol=symbol, direction="SHORT", confidence=min(confidence, 0.85),
                expected_return=risk * 2.0, risk=risk, time_horizon="4h",
                strategy_name=self.name,
                details={"support": support, "vol_expanding": bool(vol_expanding), "volume_confirmed": bool(volume_confirmed)},
            )
        return None
