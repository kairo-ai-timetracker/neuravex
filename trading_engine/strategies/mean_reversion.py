from __future__ import annotations

import numpy as np

from trading_engine.regime.regime_detector import MarketRegime, RegimeReading
from trading_engine.signals.indicators import atr, bollinger_bands, z_score
from trading_engine.strategies.base import OHLCV, Strategy, StrategySignal


class MeanReversionStrategy(Strategy):
    """Bollinger Bands + z-score deviation. Only trades in ranging markets —
    deliberately excluded from trending/crash regimes where 'reversion' is
    the losing side of a real trend."""

    name = "mean_reversion_bollinger_zscore"
    family = "mean_reversion"
    allowed_regimes = {MarketRegime.SIDEWAYS}

    def __init__(self, period: int = 20, num_std: float = 2.0, z_entry: float = 2.0):
        self.period = period
        self.num_std = num_std
        self.z_entry = z_entry

    def generate_signal(self, symbol: str, data: OHLCV, regime: RegimeReading) -> StrategySignal | None:
        if not self.is_regime_allowed(regime):
            return None
        if len(data.close) < self.period + 5:
            return None
        if regime.volatility_regime.value in ("high", "extreme"):
            return None

        upper, mid, lower = bollinger_bands(data.close, self.period, self.num_std)
        z = z_score(data.close, self.period)
        price = float(data.close[-1])
        current_z = float(z[-1]) if not np.isnan(z[-1]) else 0.0

        atr_val = float(atr(data.high, data.low, data.close)[-1])
        risk = atr_val / price if price else 0.015

        if current_z <= -self.z_entry and price < lower[-1]:
            confidence = min(0.90, 0.55 + 0.08 * (abs(current_z) - self.z_entry))
            expected_return = abs(float(mid[-1]) - price) / price
            return StrategySignal(
                symbol=symbol, direction="LONG", confidence=confidence,
                expected_return=max(expected_return, risk * 1.2), risk=risk, time_horizon="1h",
                strategy_name=self.name,
                details={"z_score": current_z, "lower_band": float(lower[-1]), "mid_band": float(mid[-1])},
            )
        if current_z >= self.z_entry and price > upper[-1]:
            confidence = min(0.88, 0.55 + 0.08 * (current_z - self.z_entry))
            expected_return = abs(price - float(mid[-1])) / price
            return StrategySignal(
                symbol=symbol, direction="SHORT", confidence=confidence,
                expected_return=max(expected_return, risk * 1.2), risk=risk, time_horizon="1h",
                strategy_name=self.name,
                details={"z_score": current_z, "upper_band": float(upper[-1]), "mid_band": float(mid[-1])},
            )
        return None
