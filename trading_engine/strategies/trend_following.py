from __future__ import annotations

import numpy as np

from trading_engine.regime.regime_detector import MarketRegime, RegimeReading
from trading_engine.signals.indicators import adx, ema
from trading_engine.strategies.base import OHLCV, Strategy, StrategySignal


class TrendFollowingStrategy(Strategy):
    """EMA crossover confirmed by ADX and higher-highs/higher-lows structure."""

    name = "trend_following_ema_adx"
    family = "trend"
    allowed_regimes = {MarketRegime.STRONG_BULL, MarketRegime.BULL, MarketRegime.BEAR}

    def __init__(self, fast: int = 20, slow: int = 50, adx_threshold: float = 20.0):
        self.fast = fast
        self.slow = slow
        self.adx_threshold = adx_threshold

    def _market_structure_bullish(self, high: np.ndarray, low: np.ndarray, lookback: int = 20) -> bool:
        if len(high) < lookback + 2:
            return False
        h = high[-lookback:]
        l = low[-lookback:]
        mid = lookback // 2
        higher_highs = np.max(h[mid:]) > np.max(h[:mid])
        higher_lows = np.min(l[mid:]) > np.min(l[:mid])
        return bool(higher_highs and higher_lows)

    def generate_signal(self, symbol: str, data: OHLCV, regime: RegimeReading) -> StrategySignal | None:
        if not self.is_regime_allowed(regime):
            return None
        if len(data.close) < self.slow + 5:
            return None

        ema_fast = ema(data.close, self.fast)
        ema_slow = ema(data.close, self.slow)
        adx_value = adx(data.high, data.low, data.close)

        crossed_up = ema_fast[-2] <= ema_slow[-2] and ema_fast[-1] > ema_slow[-1]
        crossed_down = ema_fast[-2] >= ema_slow[-2] and ema_fast[-1] < ema_slow[-1]
        trending_up = ema_fast[-1] > ema_slow[-1]
        trending_down = ema_fast[-1] < ema_slow[-1]

        if adx_value < self.adx_threshold:
            return None

        structure_bullish = self._market_structure_bullish(data.high, data.low)
        price = float(data.close[-1])

        if (crossed_up or trending_up) and structure_bullish and regime.market_regime != MarketRegime.BEAR:
            confidence = min(0.95, 0.5 + adx_value / 100 + (0.15 if crossed_up else 0.0))
            atr_val = float(np.mean(data.high[-14:] - data.low[-14:]))
            risk = atr_val / price if price else 0.02
            expected_return = risk * 2.0  # baseline 2R target, decision engine refines this
            return StrategySignal(
                symbol=symbol, direction="LONG", confidence=confidence,
                expected_return=expected_return, risk=risk, time_horizon="4h",
                strategy_name=self.name,
                details={"adx": adx_value, "crossed_up": bool(crossed_up), "structure_bullish": structure_bullish},
            )

        if (crossed_down or trending_down) and not structure_bullish:
            confidence = min(0.90, 0.45 + adx_value / 100 + (0.15 if crossed_down else 0.0))
            atr_val = float(np.mean(data.high[-14:] - data.low[-14:]))
            risk = atr_val / price if price else 0.02
            expected_return = risk * 1.8
            return StrategySignal(
                symbol=symbol, direction="SHORT", confidence=confidence,
                expected_return=expected_return, risk=risk, time_horizon="4h",
                strategy_name=self.name,
                details={"adx": adx_value, "crossed_down": bool(crossed_down)},
            )

        return None
