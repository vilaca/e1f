#!/usr/bin/env python
"""e1f deposits — organic-vs-reported value and per-deposit contribution impact (ADR-0033).

Decomposes the book into the money you put in (contributions) and the market-driven
gain on top (organic), reports ROIC (gain / invested), and attributes the total P&L
to individual deposits: each buy's shares valued to the as-of date, its gain, its own
return, and its share of the portfolio's P&L. The book is buy-and-hold (contributions
only, ADR-0011), so per-deposit values sum to the portfolio market value exactly; a
SELL makes the report unavailable because disposal attribution is not implemented.

Usage:
    e1f deposits
    e1f deposits --as-of 2025-12-31 --sort gain --reverse
"""

import argparse
import sys
from dataclasses import dataclass
from datetime import UTC, date, datetime

from e1f.common import (
    DEFAULT_CONFIG,
    DEFAULT_CURRENCY_META,
    DEFAULT_DB,
    ConfigManager,
    build_series,
    load_trades,
    unit_value_on,
)

SORT_FIELDS = ("date", "isin", "amount", "value", "gain", "ret")
_BUY_SIDES = frozenset({"BUY", "SAVINGS_PLAN"})


@dataclass
class DepositImpact:
    """One buy's contribution and what it grew to by the as-of date (EUR)."""

    date: str
    isin: str
    name: str
    amount: float  # EUR contributed by this buy (shares × price + fee)
    value: float | None  # EUR value of its shares at as-of, or None if unvaluable
    pnl_share: float | None = None  # % of total P&L, assigned once the total is known

    @property
    def valuable(self) -> bool:
        return self.value is not None

    @property
    def gain(self) -> float | None:
        return None if self.value is None else self.value - self.amount

    @property
    def ret_pct(self) -> float | None:
        if self.value is None or self.amount <= 0.0:
            return None
        return 100.0 * (self.value - self.amount) / self.amount


@dataclass(frozen=True)
class DepositSummary:
    """Portfolio-level organic-vs-reported decomposition over the valuable deposits."""

    invested: float  # Σ amount of valuable deposits
    reported: float  # Σ value (= portfolio market value)
    organic_gain: float  # reported − invested (the market-driven part)
    roic: float | None  # organic_gain / invested, in percent


# ---------------------------------------------------------------------------
# Valuation: EUR value of one share of an ISIN at the as-of date, matching
# ``performance``'s ``value_on`` (FX as of the as-of day, ADR-0010/0011) so the
# per-deposit values reconcile with the portfolio market value to the cent.
# ---------------------------------------------------------------------------


def _unit_value_eur(
    db_path: str, isin: str, as_of: str, currency_meta_path: str
) -> float | None:
    """EUR value of a single share at ``as_of``; None when it cannot be valued."""
    series = build_series(db_path, isin, [], as_of, currency_meta_path)
    return unit_value_on(series, as_of, db_path)


def _assign_pnl_shares(impacts: list[DepositImpact]) -> None:
    """Set each deposit's share of the total P&L (mutates); None when total P&L is 0."""
    total = sum(i.gain for i in impacts if i.gain is not None)
    for impact in impacts:
        impact.pnl_share = (
            None if impact.gain is None or total == 0.0 else 100.0 * impact.gain / total
        )


def deposit_impacts(
    db_path: str, config_path: str, currency_meta_path: str, as_of: str
) -> list[DepositImpact]:
    """One ``DepositImpact`` per BUY on or before ``as_of``, chronological.

    A buy's ``amount`` is ``shares × price + fee`` (EUR, as the broker charged); its
    ``value`` is ``shares × unit_value_eur`` at ``as_of``, or None when the fund can't
    be valued (no pinned currency, price, or FX) — such deposits are excluded from
    totals and P&L shares, never zero-valued.
    """
    trades = load_trades(db_path)
    sells = [
        (str(dt)[:10], isin)
        for _broker, dt, isin, side, _shares, _price, _fee in trades
        if side == "SELL" and str(dt)[:10] <= as_of
    ]
    if sells:
        first_day, first_isin = min(sells)
        raise ValueError(
            "deposit analysis requires a buy-and-hold book; "
            f"found {len(sells)} SELL transaction(s) on or before {as_of} "
            f"(first: {first_isin} on {first_day})"
        )

    config = ConfigManager(config_path)
    unit_value: dict[str, float | None] = {}
    impacts: list[DepositImpact] = []
    for _broker, dt, isin, side, shares, price, fee in trades:
        day = str(dt)[:10]
        if side not in _BUY_SIDES or day > as_of:
            continue
        qty = shares or 0.0
        if qty <= 0.0:
            continue
        amount = qty * (price or 0.0) + (fee or 0.0)
        if isin not in unit_value:
            unit_value[isin] = _unit_value_eur(db_path, isin, as_of, currency_meta_path)
        unit = unit_value[isin]
        impacts.append(DepositImpact(
            date=day,
            isin=isin,
            name=str((config.get(isin) or {}).get("name", ""))[:24],
            amount=amount,
            value=None if unit is None else qty * unit,
        ))
    _assign_pnl_shares(impacts)
    return impacts


def summarize(impacts: list[DepositImpact]) -> DepositSummary | None:
    """Organic-vs-reported decomposition over the valuable deposits, or None if none."""
    valuable = [i for i in impacts if i.valuable]
    if not valuable:
        return None
    invested = sum(i.amount for i in valuable)
    reported = sum(i.value or 0.0 for i in valuable)
    organic = reported - invested
    return DepositSummary(
        invested=invested,
        reported=reported,
        organic_gain=organic,
        roic=None if invested <= 0.0 else 100.0 * organic / invested,
    )


# ---------------------------------------------------------------------------
# Sorting + rendering.
# ---------------------------------------------------------------------------


def _sort_key(impact: DepositImpact, sort_by: str) -> tuple[float, str] | str | float:
    if sort_by == "isin":
        return impact.isin
    if sort_by == "date":
        return impact.date
    value = {
        "amount": impact.amount,
        "value": impact.value,
        "gain": impact.gain,
        "ret": impact.ret_pct,
    }[sort_by]
    return float("-inf") if value is None else value


def sort_impacts(
    impacts: list[DepositImpact], *, sort_by: str = "date", reverse: bool = False
) -> list[DepositImpact]:
    return sorted(impacts, key=lambda i: _sort_key(i, sort_by), reverse=reverse)


def _fmt_money(value: float | None) -> str:
    return "—" if value is None else f"{value:,.2f}"


def _fmt_signed(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{value:+,.2f}"


def _fmt_pct(value: float | None) -> str:
    return "—" if value is None else f"{value:+.1f}%"


_HEADER = (
    f"\n{'Date':<12} {'ISIN':<14} {'Fund':<24} {'Amount€':>10} {'Value€':>10} "
    f"{'Gain€':>10} {'Ret%':>7} {'%P&L':>7}"
)
_RULE_WIDTH = len(_HEADER.lstrip("\n"))


def _format_row(impact: DepositImpact) -> str:
    return (
        f"{impact.date:<12} {impact.isin:<14} {impact.name:<24} "
        f"{_fmt_money(impact.amount):>10} {_fmt_money(impact.value):>10} "
        f"{_fmt_signed(impact.gain):>10} {_fmt_pct(impact.ret_pct):>7} "
        f"{_fmt_pct(impact.pnl_share):>7}"
    )


def _render_summary(as_of: str, summary: DepositSummary) -> list[str]:
    return [
        f"\nDeposit analysis as of {as_of} (EUR)",
        "",
        f"  Invested (contributions)   {summary.invested:>12,.2f}",
        f"  Market value (reported)    {summary.reported:>12,.2f}",
        f"  Organic gain (market)      {summary.organic_gain:>+12,.2f}",
        f"  ROIC (gain / invested)     {_fmt_pct(summary.roic):>12}",
    ]


def _cmd_deposits(
    db_path: str,
    config_path: str,
    *,
    as_of: str,
    sort_by: str = "date",
    reverse: bool = False,
    currency_meta_path: str = DEFAULT_CURRENCY_META,
) -> int:
    impacts = deposit_impacts(db_path, config_path, currency_meta_path, as_of)
    if not impacts:
        print(f"No deposits (BUY transactions) on or before {as_of}")
        print("Ingest trades: e1f transactions trade-republic path/to/transactions.csv")
        return 0

    summary = summarize(impacts)
    if summary is None:
        print(f"No priceable deposits as of {as_of} — fetch prices first (e1f fetch)")
        return 0

    for line in _render_summary(as_of, summary):
        print(line)

    print("\nPer-deposit impact (each contribution's shares valued to the as-of date):")
    print(_HEADER)
    print("-" * _RULE_WIDTH)
    for impact in sort_impacts(impacts, sort_by=sort_by, reverse=reverse):
        print(_format_row(impact))

    excluded = sorted({i.isin for i in impacts if not i.valuable})
    if excluded:
        print(
            f"\n⚠ excluded from totals (no price/FX on or before {as_of}): "
            + ", ".join(excluded)
        )
    print(
        "\nAmount = shares × price + fee (EUR paid); Value = those shares at as-of; "
        "Gain = Value − Amount; %P&L = this deposit's share of total P&L. Buy-and-hold, "
        "so per-deposit values sum to the portfolio market value (ADR-0011/0033)."
    )
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="e1f deposits",
        description="Organic-vs-reported value, ROIC, and per-deposit contribution impact",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Invested is the money you contributed (Σ shares × price + fee); reported is the
current market value; organic gain is the market-driven part (reported − invested);
ROIC = organic gain / invested. Each deposit's shares are then valued to the as-of
date to show its gain, its own return, and its share of the portfolio's total P&L.

The book is buy-and-hold (contributions only), so per-deposit values sum to the
portfolio market value; a deposit whose fund has no price/FX is excluded (never
zero-valued) and disclosed. If a SELL exists on or before the as-of date, the
command refuses the report because disposal attribution is not implemented.

Examples:
  e1f deposits
  e1f deposits --as-of 2025-12-31
  e1f deposits --sort gain --reverse
        """,
    )
    parser.add_argument("--db", "-d", default=DEFAULT_DB, help="Database file path")
    parser.add_argument(
        "--config", "-c", default=DEFAULT_CONFIG, help="ETF universe config for names"
    )
    parser.add_argument(
        "--currency-meta",
        default=DEFAULT_CURRENCY_META,
        help="Pinned ftgo resolution / currency sidecar path",
    )
    parser.add_argument(
        "--as-of",
        default=datetime.now(UTC).date().isoformat(),
        metavar="YYYY-MM-DD",
        help="Value each deposit as of this date (default: today)",
    )
    parser.add_argument(
        "--sort", choices=SORT_FIELDS, default="date", help="Sort column (default: date)"
    )
    parser.add_argument(
        "--reverse", "-r", action="store_true", help="Descending sort order"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        date.fromisoformat(args.as_of)
    except ValueError:
        print(f"✗ Error: --as-of must be YYYY-MM-DD: {args.as_of}")
        return 1
    try:
        return _cmd_deposits(
            args.db,
            args.config,
            as_of=args.as_of,
            sort_by=args.sort,
            reverse=args.reverse,
            currency_meta_path=args.currency_meta,
        )
    except Exception as e:  # noqa: BLE001 — CLI top-level; all errors become exit code 1
        print(f"✗ Error: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
