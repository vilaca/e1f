#!/usr/bin/env python
"""e1f performance — market value, unrealized P&L, and return metrics (ADR-0011).

Values the buy-and-hold portfolio in EUR (shares x close x FX) and reports, per
held ISIN and for the portfolio as a whole: XIRR (money-weighted, headline), TWR
(time-weighted cumulative), annualized volatility, max drawdown, and CAGR.

Usage:
    e1f performance
    e1f performance --as-of 2025-12-31 --sort value --reverse
"""

import argparse
import statistics
import sys
from dataclasses import dataclass
from datetime import UTC, date, datetime

from e1f.common import (
    DEFAULT_CONFIG,
    DEFAULT_CURRENCY_META,
    DEFAULT_DB,
    ConfigManager,
    HoldingSeries,
    MetricContract,
    PositionEvent,
    Status,
    _explain_metric,
    build_series as _build_series,
    load_trades,
    position_asof as _position_asof,
    position_timeline,
    price_date_asof as _price_date_asof,
    value_on as _value_on,
    xirr,
)

SORT_FIELDS = ("isin", "name", "value", "cost", "pnl", "xirr")
_TRADING_DAYS = 252
_SHARE_EPSILON = 1e-9
_SHORT_HISTORY_DAYS = 365


# ---------------------------------------------------------------------------
# Provenance contracts (ADR-0014). ``Status`` / ``MetricContract`` and the
# ``--explain`` helpers live in ``common`` (ADR-0013 decision 8); these instances
# stay here — performance's metrics fall into two provenance families.
# ---------------------------------------------------------------------------


VALUATION_CONTRACT = MetricContract(
    method_version="eur_valuation_v1",
    requires=(
        "a close on/before the as-of date",
        "an FX rate to EUR for a foreign-priced fund",
    ),
    does_not_require=("look-through holdings", "canonical security identity"),
    supports=("market value", "unrealized P&L", "P&L %", "P&L share"),
    limitations=(
        "shares × close × FX at the as-of date; a stale close is carried forward "
        "and flagged (~), never re-priced",
    ),
)
RETURN_CONTRACT = MetricContract(
    method_version="xirr_twr_v1",
    requires=("a dated contribution series", "a terminal EUR value"),
    does_not_require=("a benchmark", "intraday prices"),
    supports=("XIRR", "TWR", "CAGR", "volatility", "max drawdown"),
    limitations=(
        "annualized figures (Vol, CAGR) under a year of history are extrapolated "
        "and flagged (*)",
        "XIRR/TWR are n/a without a sign change or ≥2 valuation points",
    ),
)


# ---------------------------------------------------------------------------
# Pure return math (no DB) — the silent-bug-prone core, tested in isolation.
# ``xirr`` (and its Newton/bisection helpers) graduated to ``common`` in
# ADR-0019 so the backtest core can share it; imported above, re-exported here
# so ``from e1f.performance import xirr`` keeps working.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RiskMetrics:
    """Time-weighted return, annualized volatility, and max drawdown."""

    twr: float | None
    volatility: float | None
    max_drawdown: float | None


def risk_metrics(series: list[tuple[str, float, float]]) -> RiskMetrics:
    """TWR, volatility, and max drawdown from a dated value/contribution series.

    ``series`` is chronological ``(date, end_value, contribution_on_day)``. Each
    sub-period return is ``r_t = V_t / (V_prev + CF_t) - 1`` (contribution treated
    as start-of-day), chain-linked into TWR. Volatility is
    ``stdev(r_t) * sqrt(252)``; max drawdown is the deepest peak-to-trough decline
    of the wealth index ``W_t = prod(1 + r_i)``, not of the raw value line, which
    contributions would keep rising (ADR-0011).
    """
    returns: list[float] = []
    previous_value = 0.0
    wealth = 1.0
    peak = 1.0
    max_drawdown = 0.0

    for _day, value, contribution in series:
        denominator = previous_value + contribution
        if denominator > 0.0:
            period_return = value / denominator - 1.0
            returns.append(period_return)
            wealth *= 1.0 + period_return
            peak = max(peak, wealth)
            max_drawdown = min(max_drawdown, wealth / peak - 1.0)
        previous_value = value

    if not returns:
        return RiskMetrics(twr=None, volatility=None, max_drawdown=None)
    twr = wealth - 1.0
    volatility = (
        statistics.stdev(returns) * (_TRADING_DAYS ** 0.5) if len(returns) >= 2 else None
    )
    return RiskMetrics(twr=twr, volatility=volatility, max_drawdown=max_drawdown)


def annualize(twr: float | None, days: int) -> float | None:
    """CAGR from cumulative TWR: ``(1+twr)^(365/days) - 1``; None if not defined."""
    if twr is None or days <= 0 or twr <= -1.0:
        return None
    return float((1.0 + twr) ** (365.0 / days) - 1.0)


# ---------------------------------------------------------------------------
# Per-ISIN series assembly on the shared valuation core (graduated to
# ``common``, ADR-0013 decision 4). Breakpoint-day assembly and per-point series
# stay here — they are performance's own return-metric machinery.
# ---------------------------------------------------------------------------


def _contribution_on(events: list[PositionEvent], day: str) -> float:
    return sum(event.cash_flow for event in events if event.date == day)


def _breakpoint_days(series: HoldingSeries, first_day: str, as_of: str) -> list[str]:
    """Trading days plus contribution days within ``[first_day, as_of]``, sorted."""
    days = {d for d in series.price_dates if first_day <= d <= as_of}
    days.update(e.date for e in series.events if first_day <= e.date <= as_of)
    days.add(as_of)
    return sorted(days)


def _isin_series_points(
    series: HoldingSeries, first_day: str, as_of: str, db_path: str
) -> list[tuple[str, float, float]]:
    points: list[tuple[str, float, float]] = []
    for day in _breakpoint_days(series, first_day, as_of):
        value = _value_on(series, day, db_path)
        if value is None:
            continue
        points.append((day, value, _contribution_on(series.events, day)))
    return points


def _aggregate_series(
    holdings: list[HoldingSeries], first_day: str, as_of: str, db_path: str
) -> list[tuple[str, float, float]]:
    """Portfolio value/contribution series: sum per-ISIN values on shared days.

    A day is dropped when a currently-held ISIN cannot be valued on it (missing
    prior price/FX), rather than treating the gap as zero — which would spike the
    aggregate return when the price later appears. Held ISINs' short-history rows
    still carry their own flag.
    """
    days: set[str] = {as_of}
    for series in holdings:
        days.update(d for d in series.price_dates if first_day <= d <= as_of)
        days.update(e.date for e in series.events if first_day <= e.date <= as_of)

    points: list[tuple[str, float, float]] = []
    for day in sorted(days):
        total_value = 0.0
        total_contribution = 0.0
        valuable = True
        for series in holdings:
            shares, _cost = _position_asof(series.events, day)
            if shares <= _SHARE_EPSILON:
                continue
            value = _value_on(series, day, db_path)
            if value is None:
                valuable = False
                break
            total_value += value
            total_contribution += _contribution_on(series.events, day)
        if valuable:
            points.append((day, total_value, total_contribution))
    return points


# ---------------------------------------------------------------------------
# Row assembly + metrics.
# ---------------------------------------------------------------------------


@dataclass
class PerformanceRow:
    isin: str
    name: str
    cost: float
    market_value: float | None
    xirr: float | None
    twr: float | None
    volatility: float | None
    max_drawdown: float | None
    cagr: float | None
    short_history: bool
    # Date of the close backing ``market_value`` (nearest-prior <= as-of), and
    # whether that date precedes the as-of date — i.e. the value is carried
    # forward from stale data rather than priced on the as-of day itself.
    price_date: str | None = None
    estimated: bool = False
    # Share of the portfolio's total unrealized P&L this holding accounts for,
    # as a percentage (assigned post-hoc once the total is known; see
    # ``_assign_pnl_contributions``). None when the holding has no P&L or the
    # total P&L is zero.
    pnl_contribution: float | None = None

    @property
    def valuable(self) -> bool:
        return self.market_value is not None

    @property
    def pnl(self) -> float | None:
        return None if self.market_value is None else self.market_value - self.cost

    @property
    def pnl_pct(self) -> float | None:
        if self.market_value is None or self.cost <= 0.0:
            return None
        return 100.0 * (self.market_value - self.cost) / self.cost


def _contribution_cash_flows(
    events: list[PositionEvent], terminal_value: float | None, as_of: str
) -> list[tuple[str, float]]:
    flows = [(e.date, -e.cash_flow) for e in events if e.cash_flow > 0.0]
    if terminal_value is not None:
        flows.append((as_of, terminal_value))
    return flows


def _window_days(first_day: str, as_of: str) -> int:
    return (date.fromisoformat(as_of) - date.fromisoformat(first_day)).days


def _metrics_from_series(
    points: list[tuple[str, float, float]], window_days: int
) -> tuple[RiskMetrics, float | None]:
    risk = risk_metrics(points)
    return risk, annualize(risk.twr, window_days)


def _build_row(
    isin: str, series: HoldingSeries, as_of: str, config_path: str, db_path: str
) -> PerformanceRow | None:
    """One per-ISIN row, or None when the ISIN is not held as of ``as_of``."""
    events = series.events
    if not events:
        return None
    first_day = events[0].date
    shares, cost = _position_asof(events, as_of)
    if shares <= _SHARE_EPSILON:
        return None

    market_value = _value_on(series, as_of, db_path)
    points = _isin_series_points(series, first_day, as_of, db_path)
    window_days = _window_days(first_day, as_of)
    risk, cagr = _metrics_from_series(points, window_days)

    first_priced = series.price_dates[0] if series.price_dates else None
    short_history = window_days < _SHORT_HISTORY_DAYS or (
        first_priced is not None and first_priced > first_day
    )

    price_date = _price_date_asof(series, as_of) if market_value is not None else None
    estimated = price_date is not None and price_date < as_of

    return PerformanceRow(
        isin=isin,
        name=_etf_name(config_path, isin),
        cost=cost,
        market_value=market_value,
        xirr=xirr(_contribution_cash_flows(events, market_value, as_of)),
        twr=risk.twr,
        volatility=risk.volatility,
        max_drawdown=risk.max_drawdown,
        cagr=cagr,
        short_history=short_history,
        price_date=price_date,
        estimated=estimated,
    )


def _etf_name(config_path: str, isin: str) -> str:
    data = ConfigManager(config_path).get(isin)
    return str((data or {}).get("name", ""))[:28]


def _total_row(
    rows: list[PerformanceRow], holdings: list[HoldingSeries], as_of: str, db_path: str
) -> PerformanceRow:
    """Portfolio TOTAL over the valuable holdings only (P&L stays coherent)."""
    valuable = [series for series in holdings if _value_on(series, as_of, db_path) is not None]
    valuable_isins = {series.isin for series in valuable}
    included = [row for row in rows if row.valuable and row.isin in valuable_isins]

    cost = sum(row.cost for row in included)
    market_value = sum(row.market_value or 0.0 for row in included)

    first_day = min((series.events[0].date for series in valuable), default=as_of)
    points = _aggregate_series(valuable, first_day, as_of, db_path)
    window_days = _window_days(first_day, as_of)
    risk, cagr = _metrics_from_series(points, window_days)

    flows: list[tuple[str, float]] = []
    for series in valuable:
        flows.extend((e.date, -e.cash_flow) for e in series.events if e.cash_flow > 0.0)
    flows.append((as_of, market_value))

    return PerformanceRow(
        isin="TOTAL",
        name="",
        cost=cost,
        market_value=market_value,
        xirr=xirr(flows),
        twr=risk.twr,
        volatility=risk.volatility,
        max_drawdown=risk.max_drawdown,
        cagr=cagr,
        short_history=any(row.short_history for row in included),
        estimated=any(row.estimated for row in included),
    )


# ---------------------------------------------------------------------------
# Sorting + rendering.
# ---------------------------------------------------------------------------


def _sort_key(row: PerformanceRow, sort_by: str) -> tuple[float, str] | str | float:
    if sort_by == "isin":
        return row.isin
    if sort_by == "name":
        return row.name.lower()
    # Numeric fields: None sorts to the bottom regardless of direction.
    value = {
        "value": row.market_value,
        "cost": row.cost,
        "pnl": row.pnl,
        "xirr": row.xirr,
    }[sort_by]
    return float("-inf") if value is None else value


def sort_rows(
    rows: list[PerformanceRow], *, sort_by: str = "isin", reverse: bool = False
) -> list[PerformanceRow]:
    return sorted(rows, key=lambda row: _sort_key(row, sort_by), reverse=reverse)


def _assign_pnl_contributions(rows: list[PerformanceRow]) -> None:
    """Set each row's share of the total unrealized P&L (mutates in place).

    The denominator is the sum of every valuable holding's P&L — the same set
    the ``TOTAL`` row aggregates — so contributions add up to 100%. When the
    net P&L is zero the shares are undefined and left as None.
    """
    total = sum(row.pnl for row in rows if row.pnl is not None)
    for row in rows:
        if row.pnl is None or total == 0.0:
            row.pnl_contribution = None
        else:
            row.pnl_contribution = 100.0 * row.pnl / total


def _fmt_money(value: float | None, *, flag: bool = False) -> str:
    if value is None:
        return "n/a"
    return f"{value:,.2f}" + ("~" if flag else "")


def _fmt_pct(value: float | None, *, scaled: bool = False, flag: bool = False) -> str:
    if value is None:
        return "n/a"
    pct = value if scaled else value * 100.0
    return f"{pct:.1f}%" + ("*" if flag else "")


_HEADER = (
    f"\n{'ISIN':<14} {'Name':<28} {'MktVal€':>10} {'Cost€':>10} {'P&L€':>10} "
    f"{'P&L%':>7} {'P&Lctr':>7} {'XIRR':>7} {'TWR':>7} {'Vol':>7} {'MaxDD':>7} "
    f"{'CAGR':>8}"
)
_RULE_WIDTH = 14 + 28 + 10 * 3 + 7 * 5 + 7 + 8 + 11
_STATUS_COL = 11


def row_status(row: PerformanceRow) -> Status:
    """The row's valuation gate: CALCULATED with a EUR value, else UNAVAILABLE (ADR-0014)."""
    return Status.CALCULATED if row.valuable else Status.UNAVAILABLE


def _header(show_status: bool) -> str:
    return _HEADER + (f" {'Status':>{_STATUS_COL}}" if show_status else "")


def _rule_width(show_status: bool) -> int:
    return _RULE_WIDTH + (_STATUS_COL + 1 if show_status else 0)


def _format_row(row: PerformanceRow, *, show_status: bool = False) -> str:
    flag = row.short_history
    base = (
        f"{row.isin:<14} {row.name:<28} "
        f"{_fmt_money(row.market_value, flag=row.estimated):>10} {_fmt_money(row.cost):>10} "
        f"{_fmt_money(row.pnl):>10} {_fmt_pct(row.pnl_pct, scaled=True):>7} "
        f"{_fmt_pct(row.pnl_contribution, scaled=True):>7} "
        f"{_fmt_pct(row.xirr):>7} {_fmt_pct(row.twr):>7} "
        f"{_fmt_pct(row.volatility, flag=flag):>7} {_fmt_pct(row.max_drawdown):>7} "
        f"{_fmt_pct(row.cagr, flag=flag):>8}"
    )
    if show_status:
        base += f" {row_status(row).value:>{_STATUS_COL}}"
    return base


def render_row_explain(row: PerformanceRow) -> list[str]:
    """Reconstruct a holding's provenance chain from the row itself.

    Nothing is read from a persisted log — the chain is recomputed from the row's
    fields, so it is always what the code did (ADR-0012 decision 7, ADR-0014).
    """
    title = f"{row.isin}  {row.name}".rstrip()
    lines = [f"\n{title}"]

    if row.valuable:
        when = f" @ {row.price_date}" if row.price_date else ""
        stale = " (carried forward — stale close)" if row.estimated else ""
        val_result = (
            f"MktVal €{_fmt_money(row.market_value)} ; "
            f"P&L €{_fmt_money(row.pnl)} ({_fmt_pct(row.pnl_pct, scaled=True)}) ; "
            f"P&L share {_fmt_pct(row.pnl_contribution, scaled=True)}"
        )
        val_inputs = f"shares × close × FX{when}{stale}"
    else:
        val_result = "unavailable — no close/FX on or before the as-of date (excluded from TOTAL)"
        val_inputs = "no price/FX for this holding"
    lines.extend(_explain_metric(
        "Market valuation",
        row_status(row),
        val_result,
        val_inputs,
        "shares × close × FX → EUR (ADR-0010/0011)",
        VALUATION_CONTRACT,
    ))

    return_metrics = (row.xirr, row.twr, row.cagr, row.volatility, row.max_drawdown)
    ret_status = (
        Status.CALCULATED if any(m is not None for m in return_metrics) else Status.UNAVAILABLE
    )
    extrapolated = (
        " ; annualized figures extrapolated (short history)"
        if row.short_history and ret_status is Status.CALCULATED
        else ""
    )
    ret_result = (
        f"XIRR {_fmt_pct(row.xirr)} ; TWR {_fmt_pct(row.twr)} ; CAGR {_fmt_pct(row.cagr)} ; "
        f"Vol {_fmt_pct(row.volatility)} ; MaxDD {_fmt_pct(row.max_drawdown)}{extrapolated}"
    )
    lines.extend(_explain_metric(
        "Return metrics",
        ret_status,
        ret_result,
        "dated contribution series + terminal EUR value",
        "XIRR money-weighted ; TWR chain-linked ; CAGR = annualized TWR ; "
        "Vol = stdev(daily r)×√252 ; MaxDD on the wealth index",
        RETURN_CONTRACT,
    ))
    return lines


def _cmd_performance(
    db_path: str,
    config_path: str,
    *,
    as_of: str,
    sort_by: str = "isin",
    reverse: bool = False,
    show_status: bool = False,
    explain: bool = False,
    currency_meta_path: str = DEFAULT_CURRENCY_META,
) -> int:
    show_status = show_status or explain  # --explain implies status visibility (ADR-0014)
    timeline = position_timeline(load_trades(db_path))
    if not timeline:
        print("No ETF holdings in database")
        print("Ingest trades: e1f transactions trade-republic path/to/transactions.csv")
        return 0

    holdings: list[HoldingSeries] = []
    rows: list[PerformanceRow] = []
    for isin, events in timeline.items():
        capped = [event for event in events if event.date <= as_of]
        if not capped:
            continue
        series = _build_series(db_path, isin, capped, as_of, currency_meta_path)
        row = _build_row(isin, series, as_of, config_path, db_path)
        if row is None:
            continue
        holdings.append(series)
        rows.append(row)

    if not rows:
        print(f"No holdings as of {as_of}")
        return 0

    excluded = [row.isin for row in rows if not row.valuable]
    _assign_pnl_contributions(rows)
    rows = sort_rows(rows, sort_by=sort_by, reverse=reverse)

    total = _total_row(rows, holdings, as_of, db_path)
    total.pnl_contribution = None if not total.pnl else 100.0

    print(f"\nPortfolio performance as of {as_of} (EUR)")
    print(_header(show_status))
    print("-" * _rule_width(show_status))
    for row in rows:
        print(_format_row(row, show_status=show_status))
    print("-" * _rule_width(show_status))
    print(_format_row(total, show_status=show_status))

    estimated = [row for row in rows if row.estimated]
    if any(row.short_history for row in rows):
        print("\n* < 1y or short history — annualized figures (Vol, CAGR) extrapolated")
    if estimated:
        dates = {row.price_date for row in estimated}
        if len(dates) == 1:
            price_date = dates.pop()
            assert price_date is not None  # estimated rows always carry one
            stale = _window_days(price_date, as_of)
            scope = (
                "all holdings"
                if len(estimated) == len(rows)
                else f"{len(estimated)} holdings"
            )
            print(
                f"\n~ MktVal estimated: no close on {as_of} — freshest data is "
                f"{price_date} ({stale}d stale) for {scope} (fetch to refresh)."
            )
        else:
            print(
                f"\n~ MktVal estimated from the latest price before {as_of} "
                f"(no close on the as-of day — fetch to refresh):"
            )
            for row in sorted(estimated, key=lambda r: r.isin):
                assert row.price_date is not None  # estimated rows always carry one
                stale = _window_days(row.price_date, as_of)
                print(f"    {row.isin}  {row.price_date} ({stale}d stale)")
    if excluded:
        print(
            f"\n⚠ excluded from TOTAL (no price/FX on or before {as_of}): "
            + ", ".join(sorted(excluded))
        )

    if explain:
        print("\nProvenance (--explain) — reconstructed from source, not a log:")
        for row in rows:
            for line in render_row_explain(row):
                print(line)
        for line in render_row_explain(total):
            print(line)
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="e1f performance",
        description="Market value, unrealized P&L, and return metrics (XIRR, TWR, "
        "volatility, max drawdown, CAGR) per holding and portfolio-wide",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Metrics (all EUR, base currency per ADR-0010):
  P&Lctr this holding's share of the portfolio's total unrealized P&L (sums to 100%)
  XIRR   money-weighted annualized return (headline — accounts for when you paid in)
  TWR    time-weighted cumulative return (contribution timing neutralized)
  CAGR   annualized TWR
  Vol    annualized volatility of daily returns (x sqrt(252))
  MaxDD  deepest peak-to-trough decline of the time-weighted wealth index

A holding with no price/FX on or before the as-of date shows n/a and is excluded
from the TOTAL (with a warning). Vol/CAGR on under a year of history are flagged *.
A MktVal carried forward from an earlier close (no price on the as-of day itself)
is flagged ~, with the price date and how stale it is listed below the table.

Provenance (ADR-0014, off by default): --show-status adds a Status column
(CALCULATED / UNAVAILABLE, the row's valuation gate); --explain adds per-holding
provenance blocks and implies --show-status.

Examples:
  e1f performance
  e1f performance --as-of 2025-12-31
  e1f performance --sort value --reverse
  e1f performance --show-status
  e1f performance --explain
        """,
    )
    parser.add_argument("--db", "-d", default=DEFAULT_DB, help="Database file path")
    parser.add_argument(
        "--config",
        "-c",
        default=DEFAULT_CONFIG,
        help="ETF universe config for security names",
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
        help="Value the portfolio as of this date (default: today)",
    )
    parser.add_argument(
        "--sort",
        choices=SORT_FIELDS,
        default="isin",
        help="Sort holdings by column (default: isin)",
    )
    parser.add_argument(
        "--reverse", "-r", action="store_true", help="Descending sort order"
    )
    parser.add_argument(
        "--show-status",
        action="store_true",
        help="Add a per-holding provenance Status column (ADR-0014)",
    )
    parser.add_argument(
        "--explain",
        action="store_true",
        help="Per-holding provenance blocks (Status/contract/limited-by; implies --show-status)",
    )
    return parser


def _validate_as_of(as_of: str) -> None:
    try:
        date.fromisoformat(as_of)
    except ValueError as exc:
        raise ValueError(f"--as-of must be YYYY-MM-DD: {as_of}") from exc


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        _validate_as_of(args.as_of)
        return _cmd_performance(
            args.db,
            args.config,
            as_of=args.as_of,
            sort_by=args.sort,
            reverse=args.reverse,
            show_status=args.show_status,
            explain=args.explain,
            currency_meta_path=args.currency_meta,
        )
    except Exception as e:  # noqa: BLE001 — CLI top-level; all errors become exit code 1
        print(f"✗ Error: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
