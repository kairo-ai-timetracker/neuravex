"""
Strategy interface. All strategies produce the signal shape from spec
section 6 and declare which market regimes they're allowed to run in — the
decision engine enforces the regime gate, not the strategy itself, so this
rule can't be silently bypassed by a buggy strategy.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

import numpy as np

from trading_engine.regime.regime_detector import MarketRegime, RegimeReading


@dataclass
class OHLCV:
    open_time: np.ndarray
    open: np.ndarray
    high: np.ndarray
    low: np.ndarray
    close: np.ndarray
    volume: np.ndarray


@dataclass
class StrategySignal:
    symbol: str
    direction: str  # "LONG" | "SHORT" | "FLAT"
    confidence: float  # 0..1
    expected_return: float  # decimal, e.g. 0.021 = 2.1%
    risk: float  # decimal, expected downside if stopped out
    time_horizon: str  # e.g. "4h"
    strategy_name: str
    details: dict = field(default_factory=dict)


class Strategy(ABC):
    name: str = "base"
    family: str = "base"
    # Which market regimes this strategy is allowed to trade in.
    allowed_regimes: set[MarketRegime] = field(default_factory=set)

    @abstractmethod
    def generate_signal(self, symbol: str, data: OHLCV, regime: RegimeReading) -> StrategySignal | None:
        """Return a signal, or None if this strategy has nothing to say."""

    def is_regime_allowed(self, regime: RegimeReading) -> bool:
        return regime.market_regime in self.allowed_regimes
