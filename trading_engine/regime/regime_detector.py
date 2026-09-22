"""
Market regime + volatility regime classification.

Strategies consult this before generating signals: a strategy may only be
active when it fits the current regime (see spec section 5/6).
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

import numpy as np

from trading_engine.signals.indicators import adx, ema, realized_vol


class MarketRegime(str, Enum):
    STRONG_BULL = "STRONG_BULL"
    BULL = "BULL"
    SIDEWAYS = "SIDEWAYS"
    BEAR = "BEAR"
    HIGH_VOLATILITY = "HIGH_VOLATILITY"
    CRASH = "CRASH"


class VolatilityRegime(str, Enum):
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    EXTREME = "extreme"


@dataclass
class RegimeReading:
    market_regime: MarketRegime
    volatility_regime: VolatilityRegime
    trend_strength: float  # ADX-like, 0-100
    realized_vol: float  # annualized, decimal (0.5 = 50%)
    drawdown_from_high: float  # decimal, positive number = % below recent high


def detect_regime(
    high: np.ndarray,
    low: np.ndarray,
    close: np.ndarray,
    periods_per_year: int = 365 * 24,  # assume hourly candles by default
    vol_thresholds: tuple[float, float, float] = (0.4, 0.8, 1.5),
    crash_drawdown_threshold: float = 0.15,
    crash_window: int = 24,
    vol_window: int = 30,
) -> RegimeReading:
    """
    Pure function: feed it OHLC arrays (oldest -> newest), get back a regime
    reading. No side effects, no I/O — this makes it trivially testable and
    safe to reuse in both live trading and backtesting.
    """
    if len(close) < 20:
        return RegimeReading(
            market_regime=MarketRegime.SIDEWAYS,
            volatility_regime=VolatilityRegime.NORMAL,
            trend_strength=0.0,
            realized_vol=0.0,
            drawdown_from_high=0.0,
        )

    adx_value = adx(high, low, close)
    # Use a recent window for volatility, not the full history — otherwise a
    # sharp recent spike gets diluted by an earlier calm period and a real
    # crash can be misclassified as low-volatility.
    recent_close = close[-vol_window:] if len(close) > vol_window else close
    vol = realized_vol(recent_close, periods_per_year)

    ema_fast = ema(close, 20)[-1]
    ema_slow = ema(close, 50)[-1] if len(close) >= 50 else ema(close, len(close))[-1]
    trend_up = ema_fast > ema_slow
    trend_pct = (ema_fast - ema_slow) / ema_slow if ema_slow else 0.0

    recent_high = np.max(close[-crash_window:])
    drawdown = (recent_high - close[-1]) / recent_high if recent_high else 0.0

    low_t, high_t, extreme_t = vol_thresholds
    if vol < low_t:
        vol_regime = VolatilityRegime.LOW
    elif vol < high_t:
        vol_regime = VolatilityRegime.NORMAL
    elif vol < extreme_t:
        vol_regime = VolatilityRegime.HIGH
    else:
        vol_regime = VolatilityRegime.EXTREME

    if drawdown >= crash_drawdown_threshold:
        # A sharp drawdown within the crash window is a crash regardless of
        # which volatility bucket the annualized vol number lands in —
        # annualized vol can understate a fast, heavily-autocorrelated drop.
        market_regime = MarketRegime.CRASH
    elif vol_regime == VolatilityRegime.EXTREME:
        market_regime = MarketRegime.HIGH_VOLATILITY
    elif trend_up and adx_value >= 35 and trend_pct > 0.03:
        market_regime = MarketRegime.STRONG_BULL
    elif trend_up and adx_value >= 20:
        market_regime = MarketRegime.BULL
    elif not trend_up and adx_value >= 20:
        market_regime = MarketRegime.BEAR
    else:
        market_regime = MarketRegime.SIDEWAYS

    return RegimeReading(
        market_regime=market_regime,
        volatility_regime=vol_regime,
        trend_strength=adx_value,
        realized_vol=vol,
        drawdown_from_high=drawdown,
    )
