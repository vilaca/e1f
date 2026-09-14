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

``--against ISIN[,ISIN…]`` / ``--against-portfolio`` / ``--against-all`` (ADR-0054)
replay each valuable buy's Amount€ into a candidate fund on that buy's date and
compare euro P&L to the book. ``--against-portfolio`` is each held ISIN;
``--against-all`` is every priced ISIN, held or not.

Usage:
    e1f deposits
    e1f deposits --as-of 2025-12-31 --sort pnl --reverse
    e1f deposits --group year
    e1f deposits --group week
    e1f deposits --against IE00BK5BQT80
    e1f deposits --against-portfolio
    e1f deposits --against-all --sort delta --reverse
"""

import argparse
import os
import sqlite3
import sys
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, date, datetime

from e1f.common import (
    DEFAULT_CONFIG,
    DEFAULT_CURRENCY_META,
    DEFAULT_DB,
    ConfigManager,
    Status,
    build_series,
    load_trades,
    unit_value_on,
)

# Canonical tokens (ADR-0037): cost=Amount€, pnl=Gain€, pnl_pct=Ret%, pnl_ctr=%P&L.
# Against-table extras (ADR-0054): lots=Lots, delta=ΔGain€. Default sort stays date;
# --against / --against-portfolio / --against-all resolve an omitted --sort to delta.
SORT_FIELDS = ("date", "isin", "name", "cost", "value", "pnl", "pnl_pct", "pnl_ctr")
AGAINST_SORT_FIELDS = ("isin", "name", "lots", "cost", "value", "pnl", "pnl_pct", "delta")
_SORT_CHOICES = tuple(dict.fromkeys([*SORT_FIELDS, *AGAINST_SORT_FIELDS]))
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


@dataclass(frozen=True)
class DepositReplay:
    """Same-cash replay of valuable deposits onto one alternative ISIN (ADR-0054)."""

    isin: str
    name: str
    status: Status
    reason: str | None
    n_filled: int
    n_skipped: int
    invested: float | None
    alt_value: float | None
    book_value: float | None

    @property
    def alt_gain(self) -> float | None:
        if self.alt_value is None or self.invested is None:
            return None
        return self.alt_value - self.invested

    @property
    def book_gain(self) -> float | None:
        if self.book_value is None or self.invested is None:
            return None
        return self.book_value - self.invested

    @property
    def delta(self) -> float | None:
        if self.alt_gain is None or self.book_gain is None:
            return None
        return self.alt_gain - self.book_gain

    @property
    def alt_roic(self) -> float | None:
        if self.alt_gain is None or self.invested is None or self.invested <= 0.0:
            return None
        return 100.0 * self.alt_gain / self.invested


def _unavailable_replay(
    isin: str, name: str, reason: str, n_skipped: int
) -> DepositReplay:
    return DepositReplay(
        isin=isin,
        name=name,
        status=Status.UNAVAILABLE,
        reason=reason,
        n_filled=0,
        n_skipped=n_skipped,
        invested=None,
        alt_value=None,
        book_value=None,
    )


def replay_deposits(
    db_path: str,
    currency_meta_path: str,
    as_of: str,
    alt_isin: str,
    alt_name: str,
    impacts: list[DepositImpact],
) -> DepositReplay:
    """Spend each valuable deposit's Amount€ in ``alt_isin`` on that buy's date.

    Fill is ``unit_value_on`` (nearest-prior close, FX on the valuation day). A
    deposit the candidate cannot fill is dropped from both legs. Returns
    UNAVAILABLE when the candidate has no as-of unit value or no fillable
    deposit; BOUNDED when some valuable deposits were skipped.
    """
    valuable = [impact for impact in impacts if impact.valuable]
    series = build_series(db_path, alt_isin, [], as_of, currency_meta_path)
    unit_asof = unit_value_on(series, as_of, db_path)
    if unit_asof is None or unit_asof <= 0.0:
        return _unavailable_replay(
            alt_isin,
            alt_name,
            "no EUR close/FX for the alternative on or before as-of",
            n_skipped=len(valuable),
        )

    hyp_shares = 0.0
    invested = 0.0
    book_value = 0.0
    n_filled = 0
    n_skipped = 0
    for impact in valuable:
        unit_buy = unit_value_on(series, impact.date, db_path)
        if unit_buy is None or unit_buy <= 0.0:
            n_skipped += 1
            continue
        hyp_shares += impact.amount / unit_buy
        invested += impact.amount
        book_value += impact.value or 0.0
        n_filled += 1

    if n_filled == 0:
        return _unavailable_replay(
            alt_isin,
            alt_name,
            "no alternative close on or before any deposit date",
            n_skipped=n_skipped,
        )

    return DepositReplay(
        isin=alt_isin,
        name=alt_name,
        status=Status.BOUNDED if n_skipped else Status.CALCULATED,
        reason=(
            None
            if n_skipped == 0
            else (
                f"skipped {n_skipped} of {n_filled + n_skipped} deposits "
                "(no close on or before buy date); BookGain€/Invested€ are the overlapping lots"
            )
        ),
        n_filled=n_filled,
        n_skipped=n_skipped,
        invested=invested,
        alt_value=hyp_shares * unit_asof,
        book_value=book_value,
    )


def _priced_isins(db_path: str) -> list[str]:
    """Distinct ISINs in ``prices``, sorted; empty when the table or file is missing."""
    if not os.path.exists(db_path):
        return []
    with closing(sqlite3.connect(db_path)) as conn:
        if (
            conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='prices'"
            ).fetchone()
            is None
        ):
            return []
        return [row[0] for row in conn.execute("SELECT DISTINCT isin FROM prices ORDER BY isin")]


def _book_isins(impacts: list[DepositImpact]) -> list[str]:
    """Distinct ISINs among this as-of book's deposits, sorted."""
    return sorted({impact.isin for impact in impacts})


def _parse_against(raw: str) -> list[str]:
    """Comma-separated ISINs, stripped, de-duplicated, order preserved."""
    isins: list[str] = []
    seen: set[str] = set()
    for token in raw.split(","):
        isin = token.strip()
        if not isin or isin in seen:
            continue
        seen.add(isin)
        isins.append(isin)
    if not isins:
        raise ValueError("--against needs at least one ISIN")
    return isins


def _alt_name(config: ConfigManager, isin: str) -> str:
    return str((config.get(isin) or {}).get("name", ""))[:24] or isin


# ---------------------------------------------------------------------------
# Sorting + rendering.
# ---------------------------------------------------------------------------


def _sort_key(impact: DepositImpact, sort_by: str) -> str | float:
    if sort_by == "isin":
        return impact.isin
    if sort_by == "name":
        return impact.name.lower()
    if sort_by == "date":
        return impact.date
    value = {
        "cost": impact.amount,
        "value": impact.value,
        "pnl": impact.gain,
        "pnl_pct": impact.ret_pct,
        "pnl_ctr": impact.pnl_share,
    }[sort_by]
    return float("-inf") if value is None else value


def _against_sort_key(replay: DepositReplay, sort_by: str) -> str | float:
    if sort_by == "isin":
        return replay.isin
    if sort_by == "name":
        return replay.name.lower()
    value = {
        "lots": float(replay.n_filled),
        "cost": replay.invested,
        "value": replay.alt_value,
        "pnl": replay.alt_gain,
        "pnl_pct": replay.alt_roic,
        "delta": replay.delta,
    }[sort_by]
    return float("-inf") if value is None else value


def sort_replays(
    replays: list[DepositReplay], *, sort_by: str = "delta", reverse: bool = False
) -> list[DepositReplay]:
    return sorted(replays, key=lambda row: _against_sort_key(row, sort_by), reverse=reverse)


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
_AGAINST_HEADER = (
    f"\n{'ISIN':<14} {'Fund':<24} {'Lots':>4} {'Invested€':>10} "
    f"{'AltValue€':>10} {'AltGain€':>10} {'BookGain€':>10} {'ΔGain€':>10} {'AltROIC':>8}"
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


def _format_replay_row(replay: DepositReplay) -> str:
    lots = f"{replay.n_filled:>4}"
    return (
        f"{replay.isin:<14} {replay.name:<24} {lots} "
        f"{_fmt_money(replay.invested):>10} {_fmt_money(replay.alt_value):>10} "
        f"{_fmt_signed(replay.alt_gain):>10} {_fmt_signed(replay.book_gain):>10} "
        f"{_fmt_signed(replay.delta):>10} {_fmt_pct(replay.alt_roic):>8}"
    )


def _render_against(
    replays: list[DepositReplay], *, sort_by: str, reverse: bool
) -> None:
    header = _AGAINST_HEADER
    print(header)
    print("-" * len(header.lstrip("\n")))
    for replay in sort_replays(replays, sort_by=sort_by, reverse=reverse):
        print(_format_replay_row(replay))


def _disclose_replays(replays: list[DepositReplay]) -> None:
    """Print typed partial/unavailable outcomes (ADR-0054); one line per flagged row."""
    for replay in replays:
        if replay.status is Status.CALCULATED or replay.reason is None:
            continue
        print(f"\n⚠ {replay.isin} {replay.status}: {replay.reason}")


def _cmd_deposits(
    db_path: str,
    config_path: str,
    *,
    as_of: str,
    sort_by: str = "date",
    reverse: bool = False,
    group: str | None = None,
    against: list[str] | None = None,
    against_portfolio: bool = False,
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

    if against_portfolio:
        against = _book_isins(impacts)

    if against is not None:
        config = ConfigManager(config_path)
        replays = [
            replay_deposits(
                db_path,
                currency_meta_path,
                as_of,
                isin,
                _alt_name(config, isin),
                impacts,
            )
            for isin in against
        ]
        for line in _render_summary(as_of, summary):
            print(line)
        print(
            "\nSame-cash replay (each deposit's Amount€ bought the alternative "
            "at that day's EUR close):"
        )
        _render_against(replays, sort_by=sort_by, reverse=reverse)
        _disclose_replays(replays)
        excluded = sorted({i.isin for i in impacts if not i.valuable})
        if excluded:
            print(
                f"\n⚠ excluded from the book (no price/FX on or before {as_of}): "
                + ", ".join(excluded)
            )
        print(
            "\nReplay deploys Amount€ (shares × price + fee) at the alternative's "
            "nearest-prior EUR close; no second fee is modelled. ΔGain€ = AltGain€ − "
            "BookGain€ on the overlapping lots. Positive ΔGain€ means the alternative "
            "would have been ahead (ADR-0054)."
        )
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

--against ISIN[,ISIN…] replays each valuable buy's Amount€ into those funds.
--against-portfolio is the same table, one row per holding in this as-of book.
--against-all is the same table for every ISIN in prices, held or not. The three
are mutually exclusive with each other and with --group. Default --sort under any
replay is delta (positive ΔGain€ = alternative ahead).

Examples:
  e1f deposits
  e1f deposits --as-of 2025-12-31
  e1f deposits --sort pnl --reverse
  e1f deposits --group year          # one row per fund per calendar year
  e1f deposits --group week          # one row per fund per ISO week
  e1f deposits --against IE00BK5BQT80
  e1f deposits --against-portfolio --sort delta --reverse
  e1f deposits --against-all --sort delta --reverse
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
        "--against",
        default=None,
        metavar="ISIN[,ISIN...]",
        help="Replay each deposit's Amount€ into these ISINs and compare P&L to the book",
    )
    parser.add_argument(
        "--against-portfolio",
        dest="against_portfolio",
        action="store_true",
        help="Replay onto each held ISIN in this as-of book (mutually exclusive "
        "with --against and --against-all)",
    )
    parser.add_argument(
        "--against-all",
        dest="against_all",
        action="store_true",
        help="Replay onto every ISIN in the prices table, held or not "
        "(mutually exclusive with --against and --against-portfolio)",
    )
    parser.add_argument(
        "--sort",
        choices=_SORT_CHOICES,
        default=None,
        help="Sort column (default: date; delta under "
        "--against/--against-portfolio/--against-all)",
    )
    parser.add_argument(
        "--reverse", "-r", action="store_true", help="Descending sort order"
    )
    return parser


def _replay_flags(args: argparse.Namespace) -> list[str]:
    flags: list[str] = []
    if args.against is not None:
        flags.append("--against")
    if args.against_portfolio:
        flags.append("--against-portfolio")
    if args.against_all:
        flags.append("--against-all")
    return flags


def _resolve_against(args: argparse.Namespace) -> list[str] | None:
    flags = _replay_flags(args)
    if args.group and flags:
        raise ValueError(f"--group cannot be combined with {flags[0]}")
    if len(flags) > 1:
        raise ValueError(f"{flags[0]} and {flags[1]} are mutually exclusive")
    if args.against_all:
        return _priced_isins(args.db)
    if args.against is not None:
        return _parse_against(args.against)
    return None


def _resolve_sort(sort_by: str | None, *, against: bool) -> str:
    default = "delta" if against else "date"
    resolved = sort_by or default
    allowed = AGAINST_SORT_FIELDS if against else SORT_FIELDS
    if resolved not in allowed:
        raise ValueError(
            f"--sort {resolved} is not a column on this deposits view "
            f"(choose from {', '.join(allowed)})"
        )
    return resolved


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        date.fromisoformat(args.as_of)
    except ValueError:
        print(f"✗ Error: --as-of must be YYYY-MM-DD: {args.as_of}")
        return 1
    try:
        against = _resolve_against(args)
        if args.against_all and against is not None and not against:
            print("No price series in database")
            print("Fetch prices: e1f fetch")
            return 0
        return _cmd_deposits(
            args.db,
            args.config,
            as_of=args.as_of,
            sort_by=_resolve_sort(args.sort, against=bool(_replay_flags(args))),
            reverse=args.reverse,
            group=args.group,
            against=against,
            against_portfolio=args.against_portfolio,
            currency_meta_path=args.currency_meta,
        )
    except Exception as e:  # noqa: BLE001 — CLI top-level; all errors become exit code 1
        print(f"✗ Error: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
