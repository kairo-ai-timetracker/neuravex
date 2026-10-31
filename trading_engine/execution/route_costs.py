"""
What a round trip really costs per coin on Uniswap V3 Polygon. Measured with the
Quoter for 2,500 USDC on 2026-10-31; see android's PolygonRoutes, which holds the
routes these belong to. Only these coins have a usable route.
"""
from __future__ import annotations

ROUND_TRIP_COST = {
    "WETH/USDC": 0.0037,
    "LINK/USDC": 0.0063,
    "WMATIC/USDC": 0.0104,
    "WBTC/USDC": 0.0112,
    "AAVE/USDC": 0.0122,
    "UNI/USDC": 0.0204,
    "LDO/USDC": 0.0437,
}


def slippage_bps_by_symbol(taker_fee: float = 0.001) -> dict[str, float]:
    """Per-side slippage for PaperExchange, so that fee + slippage on both
    sides adds up to the measured round trip."""
    return {symbol: (cost / 2 - taker_fee) * 10_000 for symbol, cost in ROUND_TRIP_COST.items()}
