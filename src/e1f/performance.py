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
    PositionEvent,
    convert_to_eur,
    load_trades,
    pinned_quote_currency,
    position_timeline,
)

SORT_FIELDS = ("isin", "name", "value", "cost", "pnl", "xirr")
_TRADING_DAYS = 252
_SHARE_EPSILON = 1e-9
_SHORT_HISTORY_DAYS = 365


# ---------------------------------------------------------------------------
# Pure return math (no DB) — the silent-bug-prone core, tested in isolation.
# ---------------------------------------------------------------------------


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
    flows = [
        ((date.fromisoformat(d) - start).days / 365.0, amount)
        for d, amount in cash_flows
    ]
    return _newton(flows) or _bisect(flows)


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
# Valuation layer (reads prices + FX) and per-ISIN series assembly.
# ---------------------------------------------------------------------------


@dataclass
class HoldingSeries:
    """Everything needed to value and measure one held ISIN as of a date."""

    isin: str
    events: list[PositionEvent]  # filtered to date <= as_of, chronological
    price_dates: list[str]       # sorted, <= as_of
    price_closes: list[float]    # parallel to price_dates, native currency
    currency: str | None


def _position_asof(events: list[PositionEvent], day: str) -> tuple[float, float]:
    """Shares held and average-cost basis after the last event on or before ``day``."""
    shares, cost = 0.0, 0.0
    for event in events:
        if event.date > day:
            break
        shares, cost = event.shares_held, event.cost_basis
    return shares, cost


def _close_asof(series: HoldingSeries, day: str) -> float | None:
    """Nearest-prior close on or before ``day``; None if the day precedes history."""
    import bisect

    index = bisect.bisect_right(series.price_dates, day) - 1
    if index < 0:
        return None
    return series.price_closes[index]


def _value_on(series: HoldingSeries, day: str, db_path: str) -> float | None:
    """EUR market value of the position on ``day``; None when it cannot be valued.

    None means: no pinned currency, no price on or before the day, or no FX rate
    on or before the day (``convert_to_eur`` raising) — every path that would
    otherwise force a silent or wrong number.
    """
    if series.currency is None:
        return None
    shares, _cost = _position_asof(series.events, day)
    if shares <= _SHARE_EPSILON:
        return 0.0
    close = _close_asof(series, day)
    if close is None:
        return None
    try:
        return convert_to_eur(shares * close, series.currency, day, db_path)
    except ValueError:
        return None


def _contribution_on(events: list[PositionEvent], day: str) -> float:
    return sum(event.cash_flow for event in events if event.date == day)


def _load_price_series(db_path: str, isin: str, as_of: str) -> tuple[list[str], list[float]]:
    """Sorted ``(dates, closes)`` for an ISIN, deduped to one close per day, <= as_of."""
    import sqlite3
    from contextlib import closing

    with closing(sqlite3.connect(db_path)) as conn:
        if conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='prices'"
        ).fetchone() is None:
            return [], []
        rows = conn.execute(
            "SELECT date, close FROM prices WHERE isin = ? ORDER BY date", (isin,)
        ).fetchall()

    by_day: dict[str, float] = {}
    for raw_date, close in rows:
        day = str(raw_date)[:10]
        if close is None or day > as_of:
            continue
        by_day[day] = float(close)  # last write wins if a day repeats
    dates = sorted(by_day)
    return dates, [by_day[d] for d in dates]


def _build_series(
    db_path: str,
    isin: str,
    events: list[PositionEvent],
    as_of: str,
    currency_meta_path: str,
) -> HoldingSeries:
    dates, closes = _load_price_series(db_path, isin, as_of)
    return HoldingSeries(
        isin=isin,
        events=events,
        price_dates=dates,
        price_closes=closes,
        currency=pinned_quote_currency(isin, currency_meta_path),
    )


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


def _fmt_money(value: float | None) -> str:
    return "n/a" if value is None else f"{value:,.2f}"


def _fmt_pct(value: float | None, *, scaled: bool = False, flag: bool = False) -> str:
    if value is None:
        return "n/a"
    pct = value if scaled else value * 100.0
    return f"{pct:.1f}%" + ("*" if flag else "")


_HEADER = (
    f"\n{'ISIN':<14} {'Name':<28} {'MktVal€':>13} {'Cost€':>13} {'P&L€':>13} "
    f"{'P&L%':>7} {'XIRR':>7} {'TWR':>7} {'Vol':>7} {'MaxDD':>8} {'CAGR':>8}"
)
_RULE_WIDTH = 14 + 28 + 13 * 3 + 7 * 4 + 8 + 8 + 10


def _format_row(row: PerformanceRow) -> str:
    flag = row.short_history
    return (
        f"{row.isin:<14} {row.name:<28} "
        f"{_fmt_money(row.market_value):>13} {_fmt_money(row.cost):>13} "
        f"{_fmt_money(row.pnl):>13} {_fmt_pct(row.pnl_pct, scaled=True):>7} "
        f"{_fmt_pct(row.xirr):>7} {_fmt_pct(row.twr):>7} "
        f"{_fmt_pct(row.volatility, flag=flag):>7} {_fmt_pct(row.max_drawdown):>8} "
        f"{_fmt_pct(row.cagr, flag=flag):>8}"
    )


def _cmd_performance(
    db_path: str,
    config_path: str,
    *,
    as_of: str,
    sort_by: str = "isin",
    reverse: bool = False,
    currency_meta_path: str = DEFAULT_CURRENCY_META,
) -> int:
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
    rows = sort_rows(rows, sort_by=sort_by, reverse=reverse)

    print(f"\nPortfolio performance as of {as_of} (EUR)")
    print(_HEADER)
    print("-" * _RULE_WIDTH)
    for row in rows:
        print(_format_row(row))
    print("-" * _RULE_WIDTH)
    print(_format_row(_total_row(rows, holdings, as_of, db_path)))

    if any(row.short_history for row in rows):
        print("\n* < 1y or short history — annualized figures (Vol, CAGR) extrapolated")
    if excluded:
        print(
            f"\n⚠ excluded from TOTAL (no price/FX on or before {as_of}): "
            + ", ".join(sorted(excluded))
        )
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="e1f performance",
        description="Market value, unrealized P&L, and return metrics (XIRR, TWR, "
        "volatility, max drawdown, CAGR) per holding and portfolio-wide",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Metrics (all EUR, base currency per ADR-0010):
  XIRR   money-weighted annualized return (headline — accounts for when you paid in)
  TWR    time-weighted cumulative return (contribution timing neutralized)
  CAGR   annualized TWR
  Vol    annualized volatility of daily returns (x sqrt(252))
  MaxDD  deepest peak-to-trough decline of the time-weighted wealth index

A holding with no price/FX on or before the as-of date shows n/a and is excluded
from the TOTAL (with a warning). Vol/CAGR on under a year of history are flagged *.

Examples:
  e1f performance
  e1f performance --as-of 2025-12-31
  e1f performance --sort value --reverse
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
            currency_meta_path=args.currency_meta,
        )
    except Exception as e:  # noqa: BLE001 — CLI top-level; all errors become exit code 1
        print(f"✗ Error: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
