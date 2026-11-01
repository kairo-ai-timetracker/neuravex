"""
The non-crypto portfolios: simulation only. Symbols keep the "X/USDC" shape the
agent and PaperExchange use everywhere, with USDC standing in for dollar cash.
Precious metals are priced through the big ETFs that hold them, which is also
the simplest way to own them for real later.
"""
from __future__ import annotations

METALS = "metals"
STOCKS = "stocks"

METAL_TICKERS = {"GOLD": "GLD", "SILVER": "SLV", "PLATINUM": "PPLT", "PALLADIUM": "PALL"}
STOCK_TICKERS = {
    ticker: ticker
    for ticker in (
        "AAPL", "MSFT", "NVDA", "GOOGL", "AMZN", "META", "TSLA", "AVGO",
        "JPM", "V", "JNJ", "WMT", "XOM", "KO", "PG",
    )
}
TICKERS_BY_MARKET = {METALS: METAL_TICKERS, STOCKS: STOCK_TICKERS}


def symbols_for(market: str) -> list[str]:
    return [f"{name}/USDC" for name in TICKERS_BY_MARKET[market]]


def yahoo_ticker(symbol: str) -> str:
    base = symbol.partition("/")[0]
    for tickers in TICKERS_BY_MARKET.values():
        if base in tickers:
            return tickers[base]
    raise ValueError(f"Not a metals or stocks symbol: {symbol}")
