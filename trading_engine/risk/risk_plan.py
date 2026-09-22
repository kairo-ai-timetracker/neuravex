"""
Every trade must have a risk plan BEFORE it opens (spec §8).
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class RiskPlan:
    entry: float
    stop_loss: float
    take_profit_1: float
    take_profit_2: float
    trailing_stop_distance: float
    risk_reward_ratio: float


def build_risk_plan(
    *,
    entry: float,
    direction: str,
    atr_value: float,
    atr_stop_multiplier: float = 1.5,
    reward_multiplier_1: float = 1.5,
    reward_multiplier_2: float = 3.0,
    trailing_multiplier: float = 1.0,
) -> RiskPlan:
    if atr_value <= 0:
        raise ValueError("atr_value must be positive — refusing to open a position with no volatility basis for a stop")

    stop_distance = atr_value * atr_stop_multiplier
    if direction == "LONG":
        stop_loss = entry - stop_distance
        tp1 = entry + stop_distance * reward_multiplier_1
        tp2 = entry + stop_distance * reward_multiplier_2
    elif direction == "SHORT":
        stop_loss = entry + stop_distance
        tp1 = entry - stop_distance * reward_multiplier_1
        tp2 = entry - stop_distance * reward_multiplier_2
    else:
        raise ValueError(f"unknown direction {direction!r}")

    return RiskPlan(
        entry=entry,
        stop_loss=stop_loss,
        take_profit_1=tp1,
        take_profit_2=tp2,
        trailing_stop_distance=stop_distance * trailing_multiplier,
        risk_reward_ratio=reward_multiplier_1,
    )
