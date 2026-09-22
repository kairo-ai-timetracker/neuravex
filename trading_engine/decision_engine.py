"""
Decision engine — combines strategy signals into one ensemble score per
symbol (spec §6). "NO_TRADE" is a first-class outcome, not a fallback.

Note (spec §16): this module is purely deterministic/arithmetic. An LLM may
be used elsewhere for narrative summaries or research, but it never
influences final_score or the BUY/SELL/HOLD decision.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from trading_engine.regime.regime_detector import MarketRegime, RegimeReading
from trading_engine.strategies.base import StrategySignal


@dataclass
class ScoreBreakdown:
    technical_score: float
    momentum_score: float
    regime_score: float
    volume_score: float
    strategy_performance_score: float
    volatility_penalty: float
    correlation_penalty: float
    transaction_cost_penalty: float
    final_score: float


@dataclass
class Decision:
    symbol: str
    action: str
    direction: str | None
    confidence: float
    score: ScoreBreakdown
    reasons_for: list[str] = field(default_factory=list)
    reasons_against: list[str] = field(default_factory=list)
    contributing_signals: list[StrategySignal] = field(default_factory=list)
    # Set by the agent (not scored here — this class only decides WHAT to
    # do, not whether it actually happened) right before a trade is
    # genuinely placed: a real order (server execution) or handed off to
    # the phone (PendingExecution). Everything else — NO_TRADE, dry-run,
    # a risk check that vetoed the trade — leaves this False. Read by
    # save_decision() so "X executed / Y skipped today" reflects what
    # really happened, not just what the AI wanted to do.
    executed: bool = False


_REGIME_SCORE = {
    MarketRegime.STRONG_BULL: 0.20,
    MarketRegime.BULL: 0.10,
    MarketRegime.SIDEWAYS: 0.0,
    MarketRegime.BEAR: -0.05,
    MarketRegime.HIGH_VOLATILITY: -0.15,
    MarketRegime.CRASH: -0.40,
}


def score_signals(
    *,
    symbol: str,
    signals: list[StrategySignal],
    regime: RegimeReading,
    strategy_performance: dict[str, float],
    volume_confirmation_score: float,
    correlation_penalty: float,
    estimated_transaction_cost: float,
    minimum_confidence: float,
    minimum_expected_edge: float,
) -> Decision:
    if not signals:
        return Decision(
            symbol=symbol, action="NO_TRADE", direction=None, confidence=0.0,
            score=ScoreBreakdown(0, 0, 0, 0, 0, 0, 0, 0, 0),
            reasons_against=["No strategy produced a signal for this symbol"],
        )

    longs = [s for s in signals if s.direction == "LONG"]
    shorts = [s for s in signals if s.direction == "SHORT"]
    direction = "LONG" if len(longs) >= len(shorts) else "SHORT"
    relevant = longs if direction == "LONG" else shorts
    opposing = shorts if direction == "LONG" else longs

    agreement_ratio = len(relevant) / len(signals)

    technical_score = sum(s.confidence for s in relevant) / len(relevant) if relevant else 0.0
    momentum_score = sum(
        s.expected_return for s in relevant if s.strategy_name.startswith("momentum")
    )
    regime_score = _REGIME_SCORE.get(regime.market_regime, 0.0)
    volume_score = volume_confirmation_score * 0.15

    perf_scores = [strategy_performance.get(s.strategy_name, 0.0) for s in relevant]
    strategy_performance_score = (sum(perf_scores) / len(perf_scores) if perf_scores else 0.0) * 0.15

    vol_penalty = 0.0
    if regime.volatility_regime.value == "high":
        vol_penalty = 0.08
    elif regime.volatility_regime.value == "extreme":
        vol_penalty = 0.25

    correlation_pen = correlation_penalty * 0.15
    tx_cost_penalty = estimated_transaction_cost * 5

    final_score = (
        technical_score
        + momentum_score
        + regime_score
        + volume_score
        + strategy_performance_score
        - vol_penalty
        - correlation_pen
        - tx_cost_penalty
    ) * agreement_ratio

    breakdown = ScoreBreakdown(
        technical_score=technical_score, momentum_score=momentum_score, regime_score=regime_score,
        volume_score=volume_score, strategy_performance_score=strategy_performance_score,
        volatility_penalty=vol_penalty, correlation_penalty=correlation_pen,
        transaction_cost_penalty=tx_cost_penalty, final_score=final_score,
    )

    avg_confidence = sum(s.confidence for s in relevant) / len(relevant)
    avg_expected_return = sum(s.expected_return for s in relevant) / len(relevant)
    net_edge = avg_expected_return - estimated_transaction_cost

    reasons_for, reasons_against = [], []
    for s in relevant:
        reasons_for.append(f"{s.strategy_name}: {s.direction} confidence {s.confidence:.0%}")
    for s in opposing:
        reasons_against.append(f"{s.strategy_name} disagrees: {s.direction} confidence {s.confidence:.0%}")
    if vol_penalty > 0:
        reasons_against.append(f"Elevated volatility ({regime.volatility_regime.value})")
    if correlation_pen > 0:
        reasons_against.append("Existing correlated exposure reduces conviction")

    if avg_confidence < minimum_confidence:
        return Decision(
            symbol=symbol, action="NO_TRADE", direction=direction, confidence=avg_confidence,
            score=breakdown, reasons_for=reasons_for,
            reasons_against=reasons_against + [f"Confidence {avg_confidence:.0%} below minimum {minimum_confidence:.0%}"],
            contributing_signals=relevant,
        )
    if net_edge < minimum_expected_edge:
        return Decision(
            symbol=symbol, action="NO_TRADE", direction=direction, confidence=avg_confidence,
            score=breakdown, reasons_for=reasons_for,
            reasons_against=reasons_against + [
                f"Net expected edge {net_edge:.2%} below minimum {minimum_expected_edge:.2%} after costs"
            ],
            contributing_signals=relevant,
        )
    if final_score <= 0:
        return Decision(
            symbol=symbol, action="NO_TRADE", direction=direction, confidence=avg_confidence,
            score=breakdown, reasons_for=reasons_for,
            reasons_against=reasons_against + ["Ensemble score not positive after penalties"],
            contributing_signals=relevant,
        )
    if regime.market_regime == MarketRegime.CRASH:
        return Decision(
            symbol=symbol, action="NO_TRADE", direction=direction, confidence=avg_confidence,
            score=breakdown, reasons_for=reasons_for,
            reasons_against=reasons_against + ["Market regime is CRASH — capital preservation overrides signal"],
            contributing_signals=relevant,
        )

    action = "BUY" if direction == "LONG" else "SELL"
    return Decision(
        symbol=symbol, action=action, direction=direction, confidence=avg_confidence,
        score=breakdown, reasons_for=reasons_for, reasons_against=reasons_against,
        contributing_signals=relevant,
    )
