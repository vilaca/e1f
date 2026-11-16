#!/usr/bin/env python
"""e1f pace — how far behind (or ahead) this month's deposits are (ADR-0063).

A deposit is the cash a BUY / SAVINGS_PLAN row put in (shares × price + fee, EUR as
the broker charged). The reference is the mean of the previous full calendar months,
counted from the first month with a deposit (a month with no buy counts as 0, so a
skipped month lowers the reference). Behind = reference − deposited so far this
month; negative means ahead. ``--target`` replaces the reference with a fixed amount.

Usage:
    e1f pace
    e1f pace --months 3
    e1f pace --target 1500
    e1f pace --as-of 2026-10-15
"""

import argparse
import sys
from dataclasses import dataclass
from datetime import UTC, date, datetime

from e1f.common import DEFAULT_DB, Status, load_trades

_BUY_SIDES = frozenset({"BUY", "SAVINGS_PLAN"})
DEFAULT_LOOKBACK = 6

REASON_NO_HISTORY = "NO_PREVIOUS_MONTHS"
REASON_SHORT_HISTORY = "SHORT_HISTORY"


@dataclass(frozen=True)
class PaceResult:
    """Typed outcome of one pace run (EUR)."""

    status: Status
    reason: str | None
    month: str  # the as-of month, YYYY-MM
    deposited: float  # Σ deposits in the as-of month, up to the as-of day
    reference: float | None  # monthly reference; None when UNAVAILABLE
    behind: float | None  # reference − deposited; negative = ahead
    months_requested: int
    months_used: int  # previous months actually in the mean (0 with --target)
    previous: tuple[tuple[str, float], ...]  # (YYYY-MM, EUR) of the months in the mean
    target_given: bool
    year_total: float = 0.0  # Σ deposits from Jan 1 of the as-of year to the as-of day


def _month_key(day: str) -> str:
    return day[:7]


def _shift_month(key: str, delta: int) -> str:
    year, month = int(key[:4]), int(key[5:7])
    index = year * 12 + (month - 1) + delta
    return f"{index // 12:04d}-{index % 12 + 1:02d}"


def monthly_deposits(db_path: str, as_of: str) -> dict[str, float]:
    """EUR deposited per calendar month (``YYYY-MM``) on or before ``as_of``."""
    totals: dict[str, float] = {}
    for _broker, dt, _isin, side, shares, price, fee in load_trades(db_path):
        day = str(dt)[:10]
        if side not in _BUY_SIDES or day > as_of:
            continue
        amount = (shares or 0.0) * (price or 0.0) + (fee or 0.0)
        if (shares or 0.0) <= 0.0:
            continue
        totals[_month_key(day)] = totals.get(_month_key(day), 0.0) + amount
    return totals


def compute_pace(
    totals: dict[str, float],
    as_of: str,
    *,
    months: int = DEFAULT_LOOKBACK,
    target: float | None = None,
) -> PaceResult:
    """Pace of the as-of month against the previous ``months`` full months (or ``target``)."""
    current = _month_key(as_of)
    deposited = totals.get(current, 0.0)
    year_total = sum(v for k, v in totals.items() if k[:4] == current[:4])
    if target is not None:
        return PaceResult(
            Status.CALCULATED, None, current, deposited, target, target - deposited,
            months, 0, (), True, year_total,
        )
    earlier = [key for key in totals if key < current]
    if not earlier:
        return PaceResult(
            Status.UNAVAILABLE, REASON_NO_HISTORY, current, deposited, None, None,
            months, 0, (), False, year_total,
        )
    first = min(earlier)
    window = [_shift_month(current, -k) for k in range(months, 0, -1)]
    window = [key for key in window if key >= first]
    previous = tuple((key, totals.get(key, 0.0)) for key in window)
    reference = sum(amount for _key, amount in previous) / len(previous)
    short = len(previous) < months
    return PaceResult(
        Status.BOUNDED if short else Status.CALCULATED,
        REASON_SHORT_HISTORY if short else None,
        current, deposited, reference, reference - deposited,
        months, len(previous), previous, False, year_total,
    )


def _fmt(value: float) -> str:
    return f"€{value:,.2f}"


def render(result: PaceResult, as_of: str) -> list[str]:
    lines: list[str] = []
    if result.previous:
        mean = _fmt(result.reference or 0.0)
        lines.append(f"Previous months (deposits, deviation from the {mean} mean):")
        lines.extend(
            f"  {key}  {_fmt(amount):>12}  {amount - (result.reference or 0.0):>+12,.2f}"
            for key, amount in result.previous
        )
        lines.append("")
    if result.reference is None or result.behind is None:
        lines.append(f"This month ({result.month}, to {as_of}): {_fmt(result.deposited)} deposited")
        lines.append(
            "Pace unavailable: no deposits before this month to set a reference "
            "(pass --target EUR)."
        )
        return lines
    basis = (
        f"--target {_fmt(result.reference)}"
        if result.target_given
        else f"mean of the previous {result.months_used} month(s): {_fmt(result.reference)}"
    )
    lines.append(f"This month ({result.month}, to {as_of}): {_fmt(result.deposited)} deposited")
    lines.append(f"Reference: {basis}")
    lines.append(f"Year to date ({as_of[:4]}): {_fmt(result.year_total)} deposited")
    if result.behind > 0.0:
        lines.append(f"Behind by {_fmt(result.behind)}")
    elif result.behind < 0.0:
        lines.append(f"Ahead by {_fmt(-result.behind)}")
    else:
        lines.append("On pace")
    if result.reason == REASON_SHORT_HISTORY:
        lines.append(
            f"⚠ only {result.months_used} previous month(s) since the first deposit "
            f"(asked for {result.months_requested}); the reference rests on fewer months."
        )
    return lines


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="e1f pace",
        description="How far behind (or ahead) this month's deposits are",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Deposits are BUY / SAVINGS_PLAN cash (shares × price + fee). The reference is the
mean of the previous full calendar months since your first deposit (months without a
buy count as 0). Behind = reference − deposited so far this month.

Examples:
  e1f pace
  e1f pace --months 3
  e1f pace --target 1500
        """,
    )
    parser.add_argument("--db", "-d", default=DEFAULT_DB, help="Database file path")
    parser.add_argument(
        "--as-of",
        default=datetime.now(UTC).date().isoformat(),
        metavar="YYYY-MM-DD",
        help="Measure the month containing this date, up to it (default: today)",
    )
    parser.add_argument(
        "--months",
        type=int,
        default=DEFAULT_LOOKBACK,
        help=f"Previous full months in the reference mean (default: {DEFAULT_LOOKBACK})",
    )
    parser.add_argument(
        "--target",
        type=float,
        default=None,
        metavar="EUR",
        help="Use this monthly amount as the reference instead of past deposits",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        date.fromisoformat(args.as_of)
    except ValueError:
        print(f"✗ Error: --as-of must be YYYY-MM-DD: {args.as_of}")
        return 1
    if args.months < 1:
        print("✗ Error: --months must be at least 1")
        return 1
    if args.target is not None and args.target <= 0.0:
        print("✗ Error: --target must be greater than 0")
        return 1
    try:
        totals = monthly_deposits(args.db, args.as_of)
        result = compute_pace(totals, args.as_of, months=args.months, target=args.target)
    except Exception as e:  # noqa: BLE001 — CLI top-level; all errors become exit code 1
        print(f"✗ Error: {e}")
        return 1
    for line in render(result, args.as_of):
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
