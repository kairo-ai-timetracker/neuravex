"""Monitors open positions for trailing-stop and take-profit management."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class PositionUpdate:
    symbol: str
    action: str  # "hold" | "close" | "trail_stop"
    new_stop: float | None = None
    reason: str = ""


def evaluate_position(
    symbol: str,
    direction: str,
    entry: float,
    current_price: float,
    current_stop: float,
    take_profit_1: float,
    trailing_distance: float,
    tp1_hit: bool = False,
) -> PositionUpdate:
    if direction == "LONG":
        if current_price <= current_stop:
            return PositionUpdate(symbol, "close", reason="stop_loss_hit")
        if not tp1_hit and current_price >= take_profit_1:
            new_stop = max(current_stop, entry)  # move stop to breakeven at minimum
            return PositionUpdate(symbol, "trail_stop", new_stop=new_stop, reason="tp1_hit_move_to_breakeven")
        candidate_stop = current_price - trailing_distance
        if candidate_stop > current_stop:
            return PositionUpdate(symbol, "trail_stop", new_stop=candidate_stop, reason="trailing_up")
    else:
        if current_price >= current_stop:
            return PositionUpdate(symbol, "close", reason="stop_loss_hit")
        if not tp1_hit and current_price <= take_profit_1:
            new_stop = min(current_stop, entry)
            return PositionUpdate(symbol, "trail_stop", new_stop=new_stop, reason="tp1_hit_move_to_breakeven")
        candidate_stop = current_price + trailing_distance
        if candidate_stop < current_stop:
            return PositionUpdate(symbol, "trail_stop", new_stop=candidate_stop, reason="trailing_down")

    return PositionUpdate(symbol, "hold")
