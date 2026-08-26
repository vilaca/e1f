"""Shared metric vocabulary (ADR-0013/0014) and the XIRR solver (ADR-0019)."""

from dataclasses import dataclass
from datetime import date
from enum import StrEnum


class Status(StrEnum):
    """Four-state per-metric status — the single status vocabulary (ADR-0012 decision 7)."""

    CALCULATED = "CALCULATED"  # enough evidence for a point value
    BOUNDED = "BOUNDED"  # no exact value, but defensible math bounds exist
    UNAVAILABLE = "UNAVAILABLE"  # not enough reliable info for even a useful bound
    UNRESOLVED = "UNRESOLVED"  # identity is the blocker, not coverage (v1b)


@dataclass(frozen=True)
class MetricContract:
    """A metric's data requirements — drives method id + limited-by / not-limited-by."""

    method_version: str
    requires: tuple[str, ...]  # what, if improved, would tighten/unblock it
    does_not_require: tuple[str, ...]  # what would not help (or is refused)
    supports: tuple[str, ...]  # what the metric enables
    limitations: tuple[str, ...]  # standing caveats that travel with the figure


def _limited_by(contract: MetricContract) -> list[str]:
    limited = "; ".join(contract.requires) if contract.requires else "nothing (complete)"
    not_limited = "; ".join(contract.does_not_require) if contract.does_not_require else "—"
    lines = [f"    Limited by:     {limited}", f"    Not limited by: {not_limited}"]
    if contract.supports:
        lines.append(f"    Supports:       {'; '.join(contract.supports)}")
    if contract.limitations:
        lines.append(f"    Limitations:    {'; '.join(contract.limitations)}")
    return lines


def _explain_metric(
    title: str,
    status: Status,
    result: str,
    inputs: str,
    method: str,
    contract: MetricContract,
) -> list[str]:
    return [
        f"  {title}",
        f"    Status:         {status.value}   (method = {contract.method_version})",
        f"    Result:         {result}",
        f"    Inputs:         {inputs}",
        f"    Method:         {method}",
        *_limited_by(contract),
    ]


# XIRR: Newton-Raphson with a bisection fallback, Actual/365 (ADR-0019).
def _npv(rate: float, flows: list[tuple[float, float]]) -> float:
    return float(sum(amount / (1.0 + rate) ** t for t, amount in flows))


def _npv_derivative(rate: float, flows: list[tuple[float, float]]) -> float:
    return float(sum(-t * amount / (1.0 + rate) ** (t + 1.0) for t, amount in flows))


def _newton(
    flows: list[tuple[float, float]],
    *,
    guess: float = 0.1,
    tol: float = 1e-9,
    iterations: int = 100,
) -> float | None:
    """Newton-Raphson root of NPV(rate); None if it leaves the domain or stalls."""
    rate = guess
    for _ in range(iterations):
        try:
            derivative = _npv_derivative(rate, flows)
            if derivative == 0.0:
                return None
            step = _npv(rate, flows) / derivative
        except (OverflowError, ZeroDivisionError):
            return None
        rate -= step
        if rate <= -1.0:  # (1+rate) must stay positive for fractional powers
            return None
        if abs(step) < tol:
            return rate if abs(_npv(rate, flows)) < 1e-6 else None
    return None


def _bisect(
    flows: list[tuple[float, float]],
    *,
    low: float = -0.9999,
    high: float = 100.0,
    iterations: int = 500,
) -> float | None:
    """Bisection fallback on a bracket with a guaranteed sign change."""
    f_low = _npv(low, flows)
    f_high = _npv(high, flows)
    if f_low == 0.0:
        return low
    if f_high == 0.0:
        return high
    if (f_low > 0.0) == (f_high > 0.0):
        return None  # no sign change in the bracket — no root to find
    mid = low
    for _ in range(iterations):
        mid = (low + high) / 2.0
        f_mid = _npv(mid, flows)
        if abs(f_mid) < 1e-9 or (high - low) / 2.0 < 1e-12:
            return mid
        if (f_mid > 0.0) == (f_low > 0.0):
            low, f_low = mid, f_mid
        else:
            high = mid
    return mid


def xirr(cash_flows: list[tuple[str, float]]) -> float | None:
    """Money-weighted annualized return over dated cash flows (Actual/365).

    ``cash_flows`` are ``(YYYY-MM-DD, amount)`` with contributions negative
    (money out) and the terminal value positive (money notionally back). Solves
    ``sum(amount / (1+r)^(days/365)) = 0`` by Newton with a bisection fallback.
    Returns ``None`` (never a wrong number) when there is no sign change (all
    same-sign flows) or neither method converges (ADR-0011 guards this to
    ``n/a``).
    """
    if len(cash_flows) < 2:
        return None
    amounts = [amount for _, amount in cash_flows]
    if not (any(a > 0 for a in amounts) and any(a < 0 for a in amounts)):
        return None

    start = min(date.fromisoformat(d) for d, _ in cash_flows)
    flows = [((date.fromisoformat(d) - start).days / 365.0, amount) for d, amount in cash_flows]
    return _newton(flows) or _bisect(flows)
