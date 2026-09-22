"""
Shared, pure technical indicator functions. No I/O, no state — every
function takes numpy arrays (oldest -> newest) and returns numpy arrays or
floats. Reused by regime detection, strategies, and tests so there is
exactly one implementation of each indicator in the codebase.
"""
from __future__ import annotations

import numpy as np


def ema(values: np.ndarray, span: int) -> np.ndarray:
    alpha = 2 / (span + 1)
    out = np.empty_like(values, dtype=float)
    out[0] = values[0]
    for i in range(1, len(values)):
        out[i] = alpha * values[i] + (1 - alpha) * out[i - 1]
    return out


def sma(values: np.ndarray, period: int) -> np.ndarray:
    if len(values) < period:
        return np.full_like(values, np.nan, dtype=float)
    kernel = np.ones(period) / period
    out = np.convolve(values, kernel, mode="valid")
    pad = np.full(period - 1, np.nan)
    return np.concatenate([pad, out])


def adx(high: np.ndarray, low: np.ndarray, close: np.ndarray, period: int = 14) -> float:
    if len(close) < period + 1:
        return 0.0
    up_move = high[1:] - high[:-1]
    down_move = low[:-1] - low[1:]
    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)

    tr = np.maximum.reduce([
        high[1:] - low[1:],
        np.abs(high[1:] - close[:-1]),
        np.abs(low[1:] - close[:-1]),
    ])
    atr = ema(tr, period)
    plus_di = 100 * ema(plus_dm, period) / np.where(atr == 0, np.nan, atr)
    minus_di = 100 * ema(minus_dm, period) / np.where(atr == 0, np.nan, atr)
    dx = 100 * np.abs(plus_di - minus_di) / np.where((plus_di + minus_di) == 0, np.nan, (plus_di + minus_di))
    dx = np.nan_to_num(dx, nan=0.0)
    return float(ema(dx, period)[-1])


def atr(high: np.ndarray, low: np.ndarray, close: np.ndarray, period: int = 14) -> np.ndarray:
    prev_close = np.concatenate([[close[0]], close[:-1]])
    tr = np.maximum.reduce([high - low, np.abs(high - prev_close), np.abs(low - prev_close)])
    return ema(tr, period)


def rsi(close: np.ndarray, period: int = 14) -> np.ndarray:
    delta = np.diff(close, prepend=close[0])
    gain = np.where(delta > 0, delta, 0.0)
    loss = np.where(delta < 0, -delta, 0.0)
    avg_gain = ema(gain, period)
    avg_loss = ema(loss, period)
    rs = np.where(avg_loss == 0, np.inf, avg_gain / np.where(avg_loss == 0, 1, avg_loss))
    return 100 - (100 / (1 + rs))


def macd(close: np.ndarray, fast: int = 12, slow: int = 26, signal: int = 9) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    macd_line = ema(close, fast) - ema(close, slow)
    signal_line = ema(macd_line, signal)
    histogram = macd_line - signal_line
    return macd_line, signal_line, histogram


def rate_of_change(close: np.ndarray, period: int = 10) -> np.ndarray:
    shifted = np.concatenate([np.full(period, np.nan), close[:-period]]) if period < len(close) else np.full_like(close, np.nan)
    return (close - shifted) / shifted


def bollinger_bands(close: np.ndarray, period: int = 20, num_std: float = 2.0) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    mid = sma(close, period)
    rolling_std = np.array([
        np.std(close[max(0, i - period + 1):i + 1]) if i >= period - 1 else np.nan
        for i in range(len(close))
    ])
    upper = mid + num_std * rolling_std
    lower = mid - num_std * rolling_std
    return upper, mid, lower


def z_score(close: np.ndarray, period: int = 20) -> np.ndarray:
    mid = sma(close, period)
    rolling_std = np.array([
        np.std(close[max(0, i - period + 1):i + 1]) if i >= period - 1 else np.nan
        for i in range(len(close))
    ])
    return (close - mid) / np.where(rolling_std == 0, np.nan, rolling_std)


def realized_vol(close: np.ndarray, periods_per_year: int) -> float:
    if len(close) < 2:
        return 0.0
    returns = np.diff(np.log(close))
    if len(returns) < 2:
        return 0.0
    return float(np.std(returns, ddof=1) * np.sqrt(periods_per_year))
