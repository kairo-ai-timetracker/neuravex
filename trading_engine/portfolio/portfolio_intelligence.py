"""Portfolio-level intelligence: exposure breakdowns, correlation summaries."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class PortfolioIntelligenceReport:
    total_exposure_fraction: float
    largest_position_fraction: float
    concentration_warning: bool
    correlated_clusters: list[list[str]]


def analyze_portfolio(
    equity: float,
    open_positions: dict[str, dict],
    correlation_matrix: dict[str, dict[str, float]],
    concentration_threshold: float = 0.35,
    correlation_cluster_threshold: float = 0.7,
) -> PortfolioIntelligenceReport:
    if equity <= 0 or not open_positions:
        return PortfolioIntelligenceReport(0.0, 0.0, False, [])

    notionals = {sym: pos["notional"] for sym, pos in open_positions.items()}
    total_exposure = sum(notionals.values()) / equity
    largest = max(notionals.values()) / equity if notionals else 0.0

    clusters: list[list[str]] = []
    seen: set[str] = set()
    symbols = list(open_positions.keys())
    for i, sym_a in enumerate(symbols):
        if sym_a in seen:
            continue
        cluster = [sym_a]
        for sym_b in symbols[i + 1:]:
            corr = correlation_matrix.get(sym_a, {}).get(sym_b, 0.0)
            if corr >= correlation_cluster_threshold:
                cluster.append(sym_b)
                seen.add(sym_b)
        if len(cluster) > 1:
            clusters.append(cluster)
        seen.add(sym_a)

    return PortfolioIntelligenceReport(
        total_exposure_fraction=total_exposure, largest_position_fraction=largest,
        concentration_warning=largest > concentration_threshold, correlated_clusters=clusters,
    )
