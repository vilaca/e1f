#!/usr/bin/env python
"""e1f deposits — organic-vs-reported value and per-deposit contribution impact (ADR-0033).

Decomposes the book into the money you put in (contributions) and the market-driven
gain on top (organic), reports ROIC (gain / invested), and attributes the total P&L
to individual deposits: each buy's shares valued to the as-of date, its gain, its own
return, and its share of the portfolio's P&L. The book is buy-and-hold (contributions
only, ADR-0011), so per-deposit values sum to the portfolio market value exactly; a
SELL makes the report unavailable because disposal attribution is not implemented.

``--group week|month|year`` (ADR-0036) collapses the per-buy table into deposit
vintages (one row per calendar period × fund). Week labels are ISO-8601
(``YYYY-Www``, Monday-start).

Usage:
    e1f deposits
    e1f deposits --as-of 2025-12-31 --sort gain --reverse
    e1f deposits --group year
    e1f deposits --group week
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


GROUP_FIELDS = ("month", "year", "week")
_AS_OF_TAIL = "valued to the as-of date; a total closes each valuable section"
_GROUP_INTRO = {
    "month": f"Per-month impact (each fund's deposits summed by month, {_AS_OF_TAIL}):",
    "year": f"Per-year impact (each fund's deposits summed by year, {_AS_OF_TAIL}):",
    "week": f"Per-week impact (each fund's deposits summed by ISO week, {_AS_OF_TAIL}):",
}


def _period_key(day: str, by: str) -> str:
    """Calendar-period label for a ``YYYY-MM-DD`` buy date.

    ``year`` / ``month`` are ISO-date prefixes (``YYYY`` / ``YYYY-MM``). ``week`` is
    ISO-8601 ``YYYY-Www`` using the week-numbering year (Monday-start; a late-December
    day can fall in week 1 of the next year). Week numbers are zero-padded so labels
    sort lexicographically.
    """
    if by == "year":
        return day[:4]
    if by == "month":
        return day[:7]
    if by == "week":
        iso = date.fromisoformat(day).isocalendar()
        return f"{iso.year}-W{iso.week:02d}"
    raise KeyError(by)


def group_impacts(impacts: list[DepositImpact], by: str) -> list[DepositImpact]:
    """Aggregate per-buy impacts into one row per (calendar period, ISIN).

    ``by`` is "month" (``YYYY-MM``), "year" (``YYYY``), or "week" (ISO-8601
    ``YYYY-Www``). Amounts and values sum within a bucket; a bucket is unvaluable
    exactly when its ISIN is (all buys of one ISIN share the same unit value, so a
    bucket never mixes valued and None). %P&L is reassigned across the grouped rows.
    Grouping only partitions the same buys, so the summary totals and the
    reconciliation with the portfolio market value are unchanged.
    """
    buckets: dict[tuple[str, str], list[DepositImpact]] = {}
    for impact in impacts:
        buckets.setdefault((_period_key(impact.date, by), impact.isin), []).append(impact)
    grouped: list[DepositImpact] = []
    for (period, isin), members in sorted(buckets.items()):
        unvaluable = any(m.value is None for m in members)
        grouped.append(DepositImpact(
            date=period,
            isin=isin,
            name=members[0].name,
            amount=sum(m.amount for m in members),
            value=None if unvaluable else sum(m.value or 0.0 for m in members),
        ))
    _assign_pnl_shares(grouped)
    return grouped


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


_COLUMNS = (
    f"{'ISIN':<14} {'Fund':<24} {'Amount€':>10} {'Value€':>10} "
    f"{'Gain€':>10} {'Ret%':>7} {'%P&L':>7}"
)


def _table_header(first_col: str) -> str:
    return f"\n{first_col:<12} {_COLUMNS}"


def _grouped_header() -> str:
    """Column header for the grouped table — no date column (the period is a heading)."""
    return _COLUMNS


def _row_cells(impact: DepositImpact) -> str:
    return (
        f"{impact.isin:<14} {impact.name:<24} "
        f"{_fmt_money(impact.amount):>10} {_fmt_money(impact.value):>10} "
        f"{_fmt_signed(impact.gain):>10} {_fmt_pct(impact.ret_pct):>7} "
        f"{_fmt_pct(impact.pnl_share):>7}"
    )


def _format_row(impact: DepositImpact) -> str:
    return f"{impact.date:<12} {_row_cells(impact)}"


def _total_row(members: list[DepositImpact], *, label: str) -> DepositImpact:
    """A total row over the *valuable* members (grand-summary rule, applied to a slice).

    Amount/Value/Gain/Ret% and %P&L are computed over the valuable members only, so the
    row is internally consistent (Gain = Value − Amount, Ret% = ROIC) and totals
    reconcile: Value totals sum to the reported market value and %P&L totals sum to
    100%, exactly as the grand summary excludes unvaluable deposits. Used for both the
    per-period subtotal and the bottom ``── ALL ──`` grand total.
    """
    valuable = [m for m in members if m.valuable]
    shares = [m.pnl_share for m in valuable if m.pnl_share is not None]
    row = DepositImpact(
        date="",
        isin="",
        name=label,
        amount=sum(m.amount for m in valuable),
        value=sum(m.value or 0.0 for m in valuable) if valuable else None,
    )
    row.pnl_share = sum(shares) if shares else None
    return row


def _subtotal_row(members: list[DepositImpact]) -> DepositImpact:
    """Per-period subtotal — a ``── total ──`` total over the period's valuable funds."""
    return _total_row(members, label="── total ──")


def _render_grouped(
    grouped: list[DepositImpact], *, sort_by: str, reverse: bool
) -> None:
    """Print grouped rows period by period.

    Each period is its own section: a period heading, the column header, the fund
    rows, then a ``── total ──`` subtotal when the period has at least one valuable
    fund. There is no date column — the heading carries the period. Blank lines
    separate the sections. ``--sort`` orders funds within each period; ``--reverse``
    also flips period order.
    """
    by_period: dict[str, list[DepositImpact]] = {}
    for row in grouped:
        by_period.setdefault(row.date, []).append(row)
    header = _grouped_header()
    for period in sorted(by_period, reverse=reverse):
        print(f"\n{period}")
        print(header)
        print("-" * len(header))
        members = sort_impacts(by_period[period], sort_by=sort_by, reverse=reverse)
        for member in members:
            print(_row_cells(member))
        # Omit the subtotal when nothing in the period is valuable — a 0.00/—
        # row under detail amounts looks like a broken total (ADR-0036).
        if any(m.valuable for m in members):
            print(_row_cells(_subtotal_row(members)))


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
    group: str | None = None,
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

    if group:
        # No top summary block: the bottom ── ALL ── row carries the grand total.
        grouped = group_impacts(impacts, group)
        print(f"\n{_GROUP_INTRO[group]}")
        _render_grouped(grouped, sort_by=sort_by, reverse=reverse)
        print()
        print(_row_cells(_total_row(grouped, label="── ALL ──")))
    else:
        for line in _render_summary(as_of, summary):
            print(line)
        print("\nPer-deposit impact (each contribution's shares valued to the "
              "as-of date):")
        header = _table_header("Date")
        print(header)
        print("-" * len(header.lstrip("\n")))
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

With --group week|month|year the per-deposit table collapses to one row per
calendar period × fund (deposit vintages) in per-period sections, each closed by a
subtotal. Week uses ISO-8601 labels (YYYY-Www, Monday-start). Under --group the top
summary block is dropped and a bottom ── ALL ── grand-total row carries the
Invested/Reported/Organic-gain(Gain€)/ROIC(Ret%) figures instead. Grouping only
partitions the same buys, so the totals and reconciliation are unchanged. --sort
orders funds within each period; --reverse also flips period order.

Examples:
  e1f deposits
  e1f deposits --as-of 2025-12-31
  e1f deposits --sort gain --reverse
  e1f deposits --group year          # one row per fund per calendar year
  e1f deposits --group week          # one row per fund per ISO week
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
        "--group",
        choices=GROUP_FIELDS,
        default=None,
        help="Aggregate the table into deposit vintages: one row per period × fund "
        "(week, month, or year)",
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
            group=args.group,
            currency_meta_path=args.currency_meta,
        )
    except Exception as e:  # noqa: BLE001 — CLI top-level; all errors become exit code 1
        print(f"✗ Error: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
