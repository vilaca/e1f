"""Shared TER and annual-fee calculations (ADR-0031, ADR-0032)."""

from collections.abc import Iterable


def annual_fee_estimate(ter_percent: float | None, market_value: float | None) -> float | None:
    """Estimated annual fee in EUR for one valuable holding."""
    if ter_percent is None or market_value is None or market_value <= 0.0:
        return None
    return ter_percent / 100.0 * market_value


def weighted_ter_cost(
    holdings: Iterable[tuple[float | None, float | None]],
) -> tuple[float | None, float | None]:
    """Market-value-weighted TER (%) and annual fee across holdings.

    Each item is ``(ter_percent, market_value_eur)``. Valuable holdings with
    missing TER remain in the denominator and therefore dilute the blend.
    """
    total_value = 0.0
    annual_fee = 0.0
    covered = False
    for ter_percent, market_value in holdings:
        if market_value is None or market_value <= 0.0:
            continue
        total_value += market_value
        fee = annual_fee_estimate(ter_percent, market_value)
        if fee is not None:
            covered = True
            annual_fee += fee
    if not covered or total_value <= 0.0:
        return None, None
    return 100.0 * annual_fee / total_value, annual_fee
