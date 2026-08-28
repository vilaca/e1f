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
import itertools
import statistics
import sys
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

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
    aggregate_value_series as _aggregate_series,
    build_series as _build_series,
    contribution_on as _contribution_on,
    contribution_to_return,
    load_price_series,
    load_trades,
    position_asof as _position_asof,
    position_timeline,
    price_date_asof as _price_date_asof,
    value_on as _value_on,
    wealth_and_returns as _wealth_and_returns,
    weighted_ter_cost,
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
# Extended drawdown-shape + daily-extreme metrics (ADR-0033, Phase A). Computed
# from the same time-weighted daily return series as ``risk_metrics``, so the
# MaxDD they report is identical; these add its duration, the total underwater
# time, the recovery factor, and the best/worst single-period moves. Pure, no DB.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _DrawdownEpisode:
    """One peak-to-recovery drawdown of the wealth index.

    ``peak_date`` is the last all-time-high before the decline; ``end_date`` is the
    day the index regained that peak, or — when ``recovered`` is False — the last
    day in the series (the drawdown is still open as of that day). ``trough`` is the
    deepest ``wealth/peak − 1`` reached within the episode (≤ 0).
    """

    peak_date: str
    end_date: str
    recovered: bool
    trough: float


@dataclass(frozen=True)
class ExtendedMetrics:
    """Drawdown-shape, extreme-period, and trailing-window metrics over a series.

    ``max_drawdown`` equals ``risk_metrics``'s (same return series). Durations are
    **calendar days** between the dated wealth-index points; ``max_dd_ongoing``
    marks a deepest drawdown not yet recovered as of the last day (its duration is
    then measured to that day). ``days_since_high`` counts calendar days since the
    *current* running peak (0 when the last day is itself a new high) — distinct
    from ``max_dd_duration_days``, which tracks the *deepest* episode; the two
    coincide only while the deepest drawdown is also the open one. Any field is
    ``None`` when the series is too short to define it — and ``recovery_factor`` is
    ``None`` when there is no drawdown to recover from.
    """

    max_drawdown: float | None
    max_dd_duration_days: int | None
    max_dd_peak_date: str | None
    max_dd_recovery_date: str | None
    max_dd_ongoing: bool
    underwater_days: int | None
    days_since_high: int | None
    recovery_factor: float | None
    best_day: float | None
    best_day_date: str | None
    worst_day: float | None
    worst_day_date: str | None
    gain_loss_ratio: float | None
    # Calendar-month buckets of the daily return series, chain-linked (partial first/
    # last months included); labels are ``YYYY-MM``. None on an empty series.
    best_month: float | None
    best_month_label: str | None
    worst_month: float | None
    worst_month_label: str | None
    # Trailing time-weighted returns over the 1/3/6-month windows ending at the latest
    # valued day; None when the window's start predates inception (not enough history).
    trailing_1m: float | None
    trailing_3m: float | None
    trailing_6m: float | None


def _drawdown_episodes(wealth_path: list[tuple[str, float]]) -> list[_DrawdownEpisode]:
    """Peak-to-recovery drawdown episodes of the wealth index, in chronological order.

    The running peak is seeded at 1.0 (the pre-investment wealth), matching
    ``risk_metrics``, so a first-day loss already counts as a drawdown. An episode
    opens on the first day the index sits below its running peak and closes when it
    regains that peak; a still-open episode at the end is emitted with
    ``recovered=False`` and ``end_date`` = the last day.
    """
    if not wealth_path:
        return []
    episodes: list[_DrawdownEpisode] = []
    peak = 1.0
    peak_date = wealth_path[0][0]
    open_trough: float | None = None  # None while the index is at/above its peak
    for day, wealth in wealth_path:
        if wealth >= peak:
            if open_trough is not None:
                episodes.append(_DrawdownEpisode(peak_date, day, True, open_trough))
                open_trough = None
            peak, peak_date = wealth, day
        else:
            drawdown = wealth / peak - 1.0
            open_trough = drawdown if open_trough is None else min(open_trough, drawdown)
    if open_trough is not None:
        episodes.append(_DrawdownEpisode(peak_date, wealth_path[-1][0], False, open_trough))
    return episodes


def _monthly_returns(returns: list[tuple[str, float]]) -> list[tuple[str, float]]:
    """Calendar-month returns ``(YYYY-MM, return)`` from dated daily sub-period returns.

    Buckets the date-sorted daily time-weighted returns by the ``YYYY-MM`` of their date
    and chain-links each month; partial first/last months are included as-is. Empty in,
    empty out.
    """
    monthly: list[tuple[str, float]] = []
    for month, group in itertools.groupby(returns, key=lambda dr: dr[0][:7]):
        wealth = 1.0
        for _day, period_return in group:
            wealth *= 1.0 + period_return
        monthly.append((month, wealth - 1.0))
    return monthly


def _subtract_months(anchor: date, months: int) -> date:
    """``anchor`` shifted back ``months`` calendar months, clamping the day to the target
    month's length (e.g. Mar 31 − 1 month → Feb 28)."""
    month_index = anchor.year * 12 + (anchor.month - 1) - months
    year, month_zero = divmod(month_index, 12)
    month = month_zero + 1
    next_month = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    last_day = (next_month - timedelta(days=1)).day
    return date(year, month, min(anchor.day, last_day))


def _trailing_return(
    wealth_path: list[tuple[str, float]], inception: str, months: int
) -> float | None:
    """Time-weighted return over the trailing ``months``-calendar-month window.

    Anchored at the latest wealth-index day; ``None`` when there is no wealth path or the
    window's start predates ``inception`` (not enough history). The base is the last
    wealth-index value on/before the window start, defaulting to 1.0 (the inception seed)
    when the start falls before the first defined return.
    """
    if not wealth_path:
        return None
    anchor_day, end_wealth = wealth_path[-1]
    start = _subtract_months(date.fromisoformat(anchor_day), months).isoformat()
    if start < inception:
        return None
    base = 1.0
    for day, wealth in wealth_path:
        if day <= start:
            base = wealth
        else:
            break
    return end_wealth / base - 1.0


def extended_metrics(
    points: list[tuple[str, float, float]], twr: float | None
) -> ExtendedMetrics:
    """Drawdown-shape and daily-extreme metrics over a value/contribution series.

    ``twr`` (from ``risk_metrics``) feeds the recovery factor ``twr/|MaxDD|`` so the
    two stay consistent; it is the only input not derivable here. The deepest
    drawdown episode (by trough) supplies the duration / peak / recovery / ongoing
    fields; underwater time sums every episode's peak-to-recovery span.
    """
    wealth_path, returns = _wealth_and_returns(points)

    best_day: float | None
    worst_day: float | None
    best_day_date: str | None
    worst_day_date: str | None
    if returns:
        best_day_date, best_day = max(returns, key=lambda dr: dr[1])
        worst_day_date, worst_day = min(returns, key=lambda dr: dr[1])
        gain_loss_ratio = abs(best_day) / abs(worst_day) if worst_day != 0.0 else None
    else:
        best_day = worst_day = gain_loss_ratio = None
        best_day_date = worst_day_date = None

    episodes = _drawdown_episodes(wealth_path)
    if episodes:
        deepest = min(episodes, key=lambda episode: episode.trough)
        max_drawdown: float | None = deepest.trough
        max_dd_duration: int | None = _window_days(deepest.peak_date, deepest.end_date)
        max_dd_peak_date: str | None = deepest.peak_date
        max_dd_recovery_date: str | None = deepest.end_date
        max_dd_ongoing = not deepest.recovered
        underwater_days: int | None = sum(
            _window_days(episode.peak_date, episode.end_date) for episode in episodes
        )
    elif wealth_path:  # priced days but never below the seed peak — no drawdown
        max_drawdown = 0.0
        max_dd_duration = 0
        max_dd_peak_date = max_dd_recovery_date = None
        max_dd_ongoing = False
        underwater_days = 0
    else:  # no defined return at all
        max_drawdown = max_dd_duration = underwater_days = None
        max_dd_peak_date = max_dd_recovery_date = None
        max_dd_ongoing = False

    # Days since the current running peak: the last episode when it is still open
    # (its peak is the running high), else 0 — the index sits at a new high as of
    # the last day. Distinct from the deepest episode's duration above.
    days_since_high: int | None
    if not wealth_path:
        days_since_high = None
    elif episodes and not episodes[-1].recovered:
        current = episodes[-1]
        days_since_high = _window_days(current.peak_date, current.end_date)
    else:
        days_since_high = 0

    recovery_factor = (
        None if twr is None or not max_drawdown else twr / abs(max_drawdown)
    )

    best_month: float | None
    worst_month: float | None
    best_month_label: str | None
    worst_month_label: str | None
    monthly = _monthly_returns(returns)
    if monthly:
        best_month_label, best_month = max(monthly, key=lambda mr: mr[1])
        worst_month_label, worst_month = min(monthly, key=lambda mr: mr[1])
    else:
        best_month = worst_month = None
        best_month_label = worst_month_label = None

    inception = points[0][0] if points else None
    if inception is None:
        trailing_1m = trailing_3m = trailing_6m = None
    else:
        trailing_1m = _trailing_return(wealth_path, inception, 1)
        trailing_3m = _trailing_return(wealth_path, inception, 3)
        trailing_6m = _trailing_return(wealth_path, inception, 6)

    return ExtendedMetrics(
        max_drawdown=max_drawdown,
        max_dd_duration_days=max_dd_duration,
        max_dd_peak_date=max_dd_peak_date,
        max_dd_recovery_date=max_dd_recovery_date,
        max_dd_ongoing=max_dd_ongoing,
        underwater_days=underwater_days,
        days_since_high=days_since_high,
        recovery_factor=recovery_factor,
        best_day=best_day,
        best_day_date=best_day_date,
        worst_day=worst_day,
        worst_day_date=worst_day_date,
        gain_loss_ratio=gain_loss_ratio,
        best_month=best_month,
        best_month_label=best_month_label,
        worst_month=worst_month,
        worst_month_label=worst_month_label,
        trailing_1m=trailing_1m,
        trailing_3m=trailing_3m,
        trailing_6m=trailing_6m,
    )


# ---------------------------------------------------------------------------
# Per-ISIN series assembly on the shared valuation core (graduated to
# ``common``, ADR-0013 decision 4). Breakpoint-day assembly and per-point series
# stay here — they are performance's own return-metric machinery.
# ---------------------------------------------------------------------------


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


@dataclass
class DiffRow:
    """Per-ISIN signed delta row for ``--diff`` mode (ADR-0029)."""

    isin: str
    name: str
    delta_market_value: float | None  # None = held but unpriceable at either endpoint
    delta_cost: float
    estimated: bool  # at least one endpoint's price was carried forward

    @property
    def valuable(self) -> bool:
        return self.delta_market_value is not None

    @property
    def delta_pnl(self) -> float | None:
        if self.delta_market_value is None:
            return None
        return self.delta_market_value - self.delta_cost


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


def _snapshot(
    db_path: str,
    config_path: str,
    currency_meta_path: str,
    timeline: dict[str, list[PositionEvent]],
    as_of: str,
) -> tuple[list[PerformanceRow], list[HoldingSeries]]:
    """Per-ISIN rows + their series for every ISIN held on/before ``as_of``.

    The single definition of "the portfolio as of a day" — shared by the snapshot
    command and the ``--series`` per-day totals so both agree to the cent. Events
    are capped to ``as_of`` before the series is built, since the return-metric
    flow helpers read ``series.events`` unfiltered.
    """
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
    return rows, holdings


def _snapshot_total(
    db_path: str,
    config_path: str,
    currency_meta_path: str,
    timeline: dict[str, list[PositionEvent]],
    as_of: str,
) -> PerformanceRow | None:
    """The portfolio TOTAL row as of ``as_of``, or None when nothing is valuable.

    Identical to the TOTAL line ``performance --as-of <as_of>`` prints (same
    ``_snapshot`` + ``_total_row`` path). None when no held ISIN can be valued on
    the day, so ``--series`` never emits a phantom €0 row.
    """
    rows, holdings = _snapshot(db_path, config_path, currency_meta_path, timeline, as_of)
    if not any(row.valuable for row in rows):
        return None
    return _total_row(rows, holdings, as_of, db_path)


# ---------------------------------------------------------------------------
# Diff-mode merge (pure, no DB — the primary test seam for --diff).
# ---------------------------------------------------------------------------


def _build_endpoint_rows(
    db_path: str,
    config_path: str,
    currency_meta_path: str,
    timeline: dict[str, list[PositionEvent]],
    as_of: str,
) -> dict[str, PerformanceRow]:
    """Return {isin: PerformanceRow} for all ISINs held at *as_of* (including unvaluable)."""
    rows: dict[str, PerformanceRow] = {}
    for isin, events in timeline.items():
        capped = [e for e in events if e.date <= as_of]
        if not capped:
            continue
        series = _build_series(db_path, isin, capped, as_of, currency_meta_path)
        row = _build_row(isin, series, as_of, config_path, db_path)
        if row is not None:
            rows[isin] = row
    return rows


def _diff_rows(
    start_rows: dict[str, PerformanceRow],
    end_rows: dict[str, PerformanceRow],
) -> list[DiffRow]:
    """Merge two endpoint dicts into signed delta rows over their ISIN union.

    An absent key means the ISIN was not held at that endpoint (contributes 0).
    A row present with ``valuable=False`` means held-but-unpriceable — the whole
    delta is marked unavailable for that ISIN (never collapsed to zero).
    """
    result: list[DiffRow] = []
    for isin in sorted(set(start_rows) | set(end_rows)):
        s = start_rows.get(isin)
        e = end_rows.get(isin)

        if (s is not None and not s.valuable) or (e is not None and not e.valuable):
            delta_mv: float | None = None
        else:
            # In this branch s.valuable=True (s.market_value is float) when s is not None;
            # same for e. The or-0.0 fallbacks are unreachable but satisfy the type checker.
            s_mv = 0.0 if s is None else (s.market_value if s.market_value is not None else 0.0)
            e_mv = 0.0 if e is None else (e.market_value if e.market_value is not None else 0.0)
            delta_mv = e_mv - s_mv

        delta_cost = (e.cost if e is not None else 0.0) - (s.cost if s is not None else 0.0)
        estimated = (s is not None and s.estimated) or (e is not None and e.estimated)
        name = e.name if e is not None else (s.name if s is not None else "")

        result.append(
            DiffRow(
                isin=isin,
                name=name,
                delta_market_value=delta_mv,
                delta_cost=delta_cost,
                estimated=estimated,
            )
        )
    return result


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


def diff_row_status(row: DiffRow) -> Status:
    """Diff row gate: CALCULATED when the delta is known, UNAVAILABLE otherwise."""
    return Status.CALCULATED if row.valuable else Status.UNAVAILABLE


def _diff_sort_key(row: DiffRow, sort_by: str) -> str | float:
    if sort_by == "isin":
        return row.isin
    if sort_by == "name":
        return row.name.lower()
    # Fields absent in diff output (xirr) fall back to delta_market_value (value).
    value = {
        "value": row.delta_market_value,
        "cost": row.delta_cost,
        "pnl": row.delta_pnl,
    }.get(sort_by, row.delta_market_value)
    return float("-inf") if value is None else value


def sort_diff_rows(
    rows: list[DiffRow], *, sort_by: str = "isin", reverse: bool = False
) -> list[DiffRow]:
    return sorted(rows, key=lambda row: _diff_sort_key(row, sort_by), reverse=reverse)


def _fmt_signed_money(value: float | None, *, flag: bool = False) -> str:
    if value is None:
        return "—"  # em dash — unavailable
    prefix = "+" if value > 0 else ""
    return f"{prefix}{value:,.2f}" + ("~" if flag else "")


_DIFF_HEADER = (
    f"\n{'ISIN':<14} {'Name':<28} {'ΔMktVal€':>12} {'ΔCost€':>12} {'ΔP&L€':>12}"
)
_DIFF_RULE_WIDTH = 14 + 1 + 28 + 1 + 12 + 1 + 12 + 1 + 12  # = 82
_DIFF_STATUS_COL = 11


def _diff_header(show_status: bool) -> str:
    return _DIFF_HEADER + (f" {'Status':>{_DIFF_STATUS_COL}}" if show_status else "")


def _diff_rule_width(show_status: bool) -> int:
    return _DIFF_RULE_WIDTH + (_DIFF_STATUS_COL + 1 if show_status else 0)


def _format_diff_row(row: DiffRow, *, show_status: bool = False) -> str:
    if row.valuable:
        mv = _fmt_signed_money(row.delta_market_value, flag=row.estimated)
        cost = _fmt_signed_money(row.delta_cost)
        pnl = _fmt_signed_money(row.delta_pnl)
    else:
        mv = cost = pnl = "—"  # em dash for all three when unpriceable
    base = (
        f"{row.isin:<14} {row.name:<28} "
        f"{mv:>12} {cost:>12} {pnl:>12}"
    )
    if show_status:
        base += f" {diff_row_status(row).value:>{_DIFF_STATUS_COL}}"
    return base


def _cmd_performance_diff(
    db_path: str,
    config_path: str,
    *,
    end: str,
    start: str,
    sort_by: str = "isin",
    reverse: bool = False,
    show_status: bool = False,
    explain: bool = False,
    currency_meta_path: str = DEFAULT_CURRENCY_META,
) -> int:
    show_status = show_status or explain
    timeline = position_timeline(load_trades(db_path))
    if not timeline:
        print("No ETF holdings in database")
        print("Ingest trades: e1f transactions trade-republic path/to/transactions.csv")
        return 0

    start_rows = _build_endpoint_rows(db_path, config_path, currency_meta_path, timeline, start)
    end_rows = _build_endpoint_rows(db_path, config_path, currency_meta_path, timeline, end)

    rows = _diff_rows(start_rows, end_rows)
    if not rows:
        print(f"No holdings in window {start} → {end}")
        return 0

    rows = sort_diff_rows(rows, sort_by=sort_by, reverse=reverse)

    valuable = [r for r in rows if r.valuable]
    total_mv: float | None = (
        sum(r.delta_market_value for r in valuable if r.delta_market_value is not None)
        if valuable
        else None
    )
    total_cost = sum(r.delta_cost for r in valuable)
    total_row = DiffRow(
        isin="TOTAL",
        name="",
        delta_market_value=total_mv,
        delta_cost=total_cost,
        estimated=any(r.estimated for r in valuable),
    )

    excluded = [r.isin for r in rows if not r.valuable]

    print(f"\nPerformance change {start} → {end} (EUR)")
    print(_diff_header(show_status))
    print("-" * _diff_rule_width(show_status))
    for row in rows:
        print(_format_diff_row(row, show_status=show_status))
    print("-" * _diff_rule_width(show_status))
    print(_format_diff_row(total_row, show_status=show_status))

    estimated = [r for r in rows if r.estimated and r.valuable]
    if estimated:
        print(
            "\n~ ΔMktVal estimated: at least one window endpoint used a "
            "carried-forward close (fetch to refresh)."
        )
    if excluded:
        print(
            "\n⚠ excluded from TOTAL (held but unpriceable at an endpoint): "
            + ", ".join(sorted(excluded))
        )
    if explain:
        print(
            "\nProvenance (--explain): reading-A — each endpoint valued at its own prices"
            " (shares × close × FX; carry-forward if no close on the day)."
        )
    return 0


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

    rows, holdings = _snapshot(db_path, config_path, currency_meta_path, timeline, as_of)
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


# ---------------------------------------------------------------------------
# Series mode (ADR-0030): one TOTAL row per trading day over the window.
# ---------------------------------------------------------------------------


_SERIES_HEADER = (
    f"\n{'Date':<12} {'MktVal€':>13} {'Cost€':>13} {'P&L€':>13} "
    f"{'P&L%':>8} {'XIRR':>8} {'TWR':>8} {'Vol':>8} {'MaxDD':>8} {'CAGR':>8} "
    f"{'WTER':>8} {'Fee€/yr':>10}"
)
_SERIES_RULE_WIDTH = len(_SERIES_HEADER.lstrip("\n"))


@dataclass
class SeriesPoint:
    """One trading day's portfolio TOTAL plus its cost-of-ownership columns (ADR-0031)."""

    day: str
    total: PerformanceRow
    weighted_ter: float | None  # market-value-weighted TER, in percent
    annual_cost: float | None  # estimated EUR/yr in fees at that day's MktVal


def _ter_by_isin(config_path: str, isins: list[str]) -> dict[str, float | None]:
    """TER (percent) per ISIN from config metadata; None where unset (ADR-0007)."""
    config = ConfigManager(config_path)
    result: dict[str, float | None] = {}
    for isin in isins:
        ter = (config.get(isin) or {}).get("ter")
        result[isin] = float(ter) if isinstance(ter, (int, float)) else None
    return result


def _weighted_ter_cost(
    rows: list[PerformanceRow], ter_by_isin: dict[str, float | None]
) -> tuple[float | None, float | None]:
    """Command-row adapter for the shared fee primitive."""
    return weighted_ter_cost(
        (
            ter_by_isin.get(row.isin),
            row.market_value if row.valuable else None,
        )
        for row in rows
    )


def _format_series_row(point: SeriesPoint) -> str:
    """One dated TOTAL row; ~ flags a carried-forward close, * a short history."""
    row = point.total
    flag = row.short_history
    return (
        f"{point.day:<12} "
        f"{_fmt_money(row.market_value, flag=row.estimated):>13} "
        f"{_fmt_money(row.cost):>13} {_fmt_money(row.pnl):>13} "
        f"{_fmt_pct(row.pnl_pct, scaled=True):>8} "
        f"{_fmt_pct(row.xirr):>8} {_fmt_pct(row.twr):>8} "
        f"{_fmt_pct(row.volatility, flag=flag):>8} {_fmt_pct(row.max_drawdown):>8} "
        f"{_fmt_pct(row.cagr, flag=flag):>8} "
        f"{_fmt_ter(point.weighted_ter):>8} {_fmt_money(point.annual_cost):>10}"
    )


def _fmt_ter(ter: float | None) -> str:
    """Weighted TER as ``x.xxx%``; n/a when no held holding has TER metadata."""
    return "n/a" if ter is None else f"{ter:.3f}%"


def _trading_days(
    db_path: str, timeline: dict[str, list[PositionEvent]], start: str, end: str
) -> list[str]:
    """Sorted days with a close in the DB within ``[start, end]`` across held ISINs.

    The price data defines a trading day, so weekends and market holidays (no
    close) drop out with no hardcoded calendar. A day that predates the first
    holding is dropped later, when its snapshot has nothing valuable.
    """
    days: set[str] = set()
    for isin in timeline:
        dates, _closes = load_price_series(db_path, isin, end)
        days.update(d for d in dates if start <= d)
    return sorted(days)


def _series_point(
    db_path: str,
    config_path: str,
    currency_meta_path: str,
    timeline: dict[str, list[PositionEvent]],
    ter_by_isin: dict[str, float | None],
    day: str,
) -> SeriesPoint | None:
    """The day's TOTAL + weighted-TER columns, or None when nothing is valuable.

    The TOTAL comes from the same ``_snapshot`` + ``_total_row`` path as
    ``_snapshot_total`` (and thus ``performance --as-of <day>``), so the shared
    columns stay identical; the TER columns are computed from the same per-ISIN
    rows in one snapshot pass.
    """
    rows, holdings = _snapshot(db_path, config_path, currency_meta_path, timeline, day)
    if not any(row.valuable for row in rows):
        return None
    total = _total_row(rows, holdings, day, db_path)
    weighted_ter, annual_cost = _weighted_ter_cost(rows, ter_by_isin)
    return SeriesPoint(day=day, total=total, weighted_ter=weighted_ter, annual_cost=annual_cost)


def _series_rows(
    db_path: str,
    config_path: str,
    currency_meta_path: str,
    timeline: dict[str, list[PositionEvent]],
    *,
    start: str,
    end: str,
) -> list[SeriesPoint]:
    """One ``SeriesPoint`` per trading day in the window, cumulative since inception.

    Each point's TOTAL is the same row ``performance --as-of <day>`` prints. Days
    with nothing valuable are skipped.
    """
    ter_by_isin = _ter_by_isin(config_path, list(timeline))
    result: list[SeriesPoint] = []
    for day in _trading_days(db_path, timeline, start, end):
        point = _series_point(
            db_path, config_path, currency_meta_path, timeline, ter_by_isin, day
        )
        if point is not None:
            result.append(point)
    return result


def _cmd_performance_series(
    db_path: str,
    config_path: str,
    *,
    as_of: str,
    n: int,
    reverse: bool = False,
    currency_meta_path: str = DEFAULT_CURRENCY_META,
) -> int:
    timeline = position_timeline(load_trades(db_path))
    if not timeline:
        print("No ETF holdings in database")
        print("Ingest trades: e1f transactions trade-republic path/to/transactions.csv")
        return 0

    start = (date.fromisoformat(as_of) - timedelta(days=n)).isoformat()
    rows = _series_rows(db_path, config_path, currency_meta_path, timeline, start=start, end=as_of)
    if not rows:
        print(f"No priced trading days in window {start} → {as_of}")
        return 0
    if reverse:
        rows = list(reversed(rows))

    print(f"\nPortfolio performance series {start} → {as_of} (EUR, cumulative since inception)")
    print(_SERIES_HEADER)
    print("-" * _SERIES_RULE_WIDTH)
    for point in rows:
        print(_format_series_row(point))

    if any(point.total.short_history for point in rows):
        print("\n* < 1y or short history — annualized figures (Vol, CAGR) extrapolated")
    if any(point.total.estimated for point in rows):
        print(
            "\n~ MktVal carried forward from an earlier close on flagged days "
            "(no close on the day itself — fetch to refresh)."
        )
    if any(point.weighted_ter is not None for point in rows):
        print(
            "\nWTER = market-value-weighted TER; Fee€/yr = WTER × MktVal. "
            "Holdings without TER metadata contribute 0 (dilutes)."
        )
    return 0


# ---------------------------------------------------------------------------
# Metrics mode (ADR-0033, Phase A): a portfolio-level extended risk report.
# ---------------------------------------------------------------------------


_METRIC_LABEL = 21


def _metric_line(label: str, value: str, *, note: str = "") -> str:
    tail = f"   {note}" if note else ""
    return f"    {label:<{_METRIC_LABEL}} {value:>12}{tail}"


def _fmt_ratio(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.2f}"


def _fmt_signed_pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100.0:+.2f}%"


def _fmt_duration(days: int | None) -> str:
    return "n/a" if days is None else f"{days:,}d"


def _maxdd_duration_note(ext: ExtendedMetrics) -> str:
    if ext.max_dd_peak_date is None:  # no drawdown at all
        return "no drawdown"
    if ext.max_dd_ongoing:
        return f"peak {ext.max_dd_peak_date} → still underwater at {ext.max_dd_recovery_date}"
    return f"peak {ext.max_dd_peak_date} → recovery {ext.max_dd_recovery_date}"


def _render_metrics(as_of: str, total: PerformanceRow, ext: ExtendedMetrics) -> list[str]:
    """The portfolio metrics report as a list of printable lines (pure, testable)."""
    return [
        f"\nPortfolio metrics as of {as_of} (EUR)",
        "",
        "  Value",
        _metric_line("MktVal€", _fmt_money(total.market_value, flag=total.estimated)),
        _metric_line("Cost€", _fmt_money(total.cost)),
        _metric_line("P&L€", _fmt_money(total.pnl), note=_fmt_pct(total.pnl_pct, scaled=True)),
        "",
        "  Return",
        _metric_line("XIRR (money-weighted)", _fmt_pct(total.xirr)),
        _metric_line("TWR (time-weighted)", _fmt_pct(total.twr)),
        _metric_line("CAGR", _fmt_pct(total.cagr, flag=total.short_history)),
        "",
        "  Risk / drawdown",
        _metric_line("Volatility (ann.)", _fmt_pct(total.volatility, flag=total.short_history)),
        _metric_line("Max Drawdown", _fmt_pct(ext.max_drawdown)),
        _metric_line(
            "MaxDD Duration",
            _fmt_duration(ext.max_dd_duration_days),
            note=_maxdd_duration_note(ext),
        ),
        _metric_line(
            "Days Since High",
            _fmt_duration(ext.days_since_high),
            note="at high" if ext.days_since_high == 0 else "",
        ),
        _metric_line("Underwater (total)", _fmt_duration(ext.underwater_days)),
        _metric_line("Recovery Factor", _fmt_ratio(ext.recovery_factor)),
        "",
        "  Extremes",
        _metric_line("Best Day", _fmt_signed_pct(ext.best_day), note=ext.best_day_date or ""),
        _metric_line("Worst Day", _fmt_signed_pct(ext.worst_day), note=ext.worst_day_date or ""),
        _metric_line(
            "Best Month", _fmt_signed_pct(ext.best_month), note=ext.best_month_label or ""
        ),
        _metric_line(
            "Worst Month", _fmt_signed_pct(ext.worst_month), note=ext.worst_month_label or ""
        ),
        _metric_line("Max Gain / Max Loss", _fmt_ratio(ext.gain_loss_ratio)),
        "",
        "  Trailing returns (time-weighted)",
        _metric_line("1 Month", _fmt_signed_pct(ext.trailing_1m)),
        _metric_line("3 Months", _fmt_signed_pct(ext.trailing_3m)),
        _metric_line("6 Months", _fmt_signed_pct(ext.trailing_6m)),
    ]


def _metrics_snapshot(
    db_path: str,
    config_path: str,
    currency_meta_path: str,
    timeline: dict[str, list[PositionEvent]],
    day: str,
) -> tuple[list[PerformanceRow], PerformanceRow, ExtendedMetrics] | None:
    """The day's per-ISIN rows, portfolio TOTAL, and extended metrics — or None.

    None when no held ISIN is priceable on ``day`` (so a series skips it). The
    extended metrics measure exactly the value/contribution series ``total.twr``
    comes from — the same valuable set, first_day, and ``_aggregate_series`` call
    ``_total_row`` uses.
    """
    rows, holdings = _snapshot(db_path, config_path, currency_meta_path, timeline, day)
    if not any(row.valuable for row in rows):
        return None
    total = _total_row(rows, holdings, day, db_path)
    valuable = [series for series in holdings if _value_on(series, day, db_path) is not None]
    first_day = min((series.events[0].date for series in valuable), default=day)
    points = _aggregate_series(valuable, first_day, day, db_path)
    ext = extended_metrics(points, total.twr)
    return rows, total, ext


def _cmd_performance_metrics(
    db_path: str,
    config_path: str,
    *,
    as_of: str,
    currency_meta_path: str = DEFAULT_CURRENCY_META,
) -> int:
    timeline = position_timeline(load_trades(db_path))
    if not timeline:
        print("No ETF holdings in database")
        print("Ingest trades: e1f transactions trade-republic path/to/transactions.csv")
        return 0

    snapshot = _metrics_snapshot(db_path, config_path, currency_meta_path, timeline, as_of)
    if snapshot is None:
        print(f"No priceable holdings as of {as_of}")
        return 0
    rows, total, ext = snapshot

    for line in _render_metrics(as_of, total, ext):
        print(line)

    if total.short_history:
        print("\n* < 1y or short history — annualized figures (Vol, CAGR) extrapolated")
    if total.estimated:
        print(
            "\n~ MktVal carried forward from an earlier close (no price on the as-of "
            "day itself — fetch to refresh)."
        )
    excluded = [row.isin for row in rows if not row.valuable]
    if excluded:
        print(
            f"\n⚠ excluded (no price/FX on or before {as_of}): " + ", ".join(sorted(excluded))
        )
    print(
        "\nDurations are calendar days (peak → recovery); MaxDD Duration is the deepest "
        "drawdown's, Days Since High counts from the current running peak (0 = at a new "
        "high). Best/Worst Day are single time-weighted return periods on the "
        "gap-bridged daily series; Best/Worst Month are calendar-month buckets of it "
        "(partial first/last months included). Trailing returns are time-weighted over "
        "each window ending at the latest valued day; a window predating inception shows "
        "n/a."
    )
    return 0


# ---------------------------------------------------------------------------
# Return-contribution view (ADR-0033): --contrib — each holding's Cariño-linked
# share of the book's time-weighted return, summing exactly to the TOTAL TWR.
# ---------------------------------------------------------------------------


_CONTRIB_HEADER = f"\n{'ISIN':<14} {'Fund':<28} {'TWR':>8} {'Weight':>8} {'Ctr%':>8}"
_CONTRIB_RULE_WIDTH = len(_CONTRIB_HEADER.lstrip("\n"))


def _format_contrib_row(
    isin: str, name: str, twr: float | None, weight: float | None, contribution: float | None
) -> str:
    return (
        f"{isin:<14} {name[:28]:<28} {_fmt_signed_pct(twr):>8} "
        f"{_fmt_pct(weight, scaled=True):>8} {_fmt_signed_pct(contribution):>8}"
    )


def _cmd_performance_contrib(
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

    rows, holdings = _snapshot(db_path, config_path, currency_meta_path, timeline, as_of)
    if not any(row.valuable for row in rows):
        print(f"No priceable holdings as of {as_of}")
        return 0

    total = _total_row(rows, holdings, as_of, db_path)
    valuable_series = [s for s in holdings if _value_on(s, as_of, db_path) is not None]
    first_day = min((s.events[0].date for s in valuable_series), default=as_of)
    contributions = contribution_to_return(valuable_series, first_day, as_of, db_path)

    print(f"\nPer-holding return contribution as of {as_of} (EUR)")
    print(_CONTRIB_HEADER)
    print("-" * _CONTRIB_RULE_WIDTH)
    valuable_rows = [row for row in rows if row.valuable]
    for row in sort_rows(valuable_rows, sort_by=sort_by, reverse=reverse):
        weight = (
            100.0 * (row.market_value or 0.0) / total.market_value
            if total.market_value
            else None
        )
        contribution = None if contributions is None else contributions.get(row.isin)
        print(_format_contrib_row(row.isin, row.name, row.twr, weight, contribution))

    print("-" * _CONTRIB_RULE_WIDTH)
    total_contribution = None if contributions is None else sum(contributions.values())
    total_weight = 100.0 if total.market_value else None
    print(_format_contrib_row("TOTAL", "", total.twr, total_weight, total_contribution))

    excluded = [row.isin for row in rows if not row.valuable]
    if excluded:
        print(
            f"\n⚠ excluded (no price/FX on or before {as_of}): " + ", ".join(sorted(excluded))
        )
    if contributions is None:
        print("\nContribution unavailable: no priced sub-period, or a total loss (≤ −100%).")
    print(
        "\nCtr% is each holding's Cariño-linked share of the book's time-weighted return "
        "(ADR-0033); the column sums to the TOTAL TWR. TWR is each holding's own "
        "time-weighted return over its history; Weight is its current market-value share."
    )
    return 0


# ---------------------------------------------------------------------------
# Metrics series (ADR-0033): --metrics + --series N — one extended-metrics row
# per trading day, cumulative since inception (each row == --metrics that day).
# ---------------------------------------------------------------------------


_METRICS_SERIES_HEADER = (
    f"\n{'Date':<12} {'TWR':>7} {'MaxDD':>7} {'DDdur':>7} {'SinceHi':>8} {'Underwtr':>9} "
    f"{'RecFac':>7} {'Best':>8} {'Worst':>8} {'G/L':>6}"
)
_METRICS_SERIES_RULE_WIDTH = len(_METRICS_SERIES_HEADER.lstrip("\n"))


def _format_metrics_series_row(day: str, total: PerformanceRow, ext: ExtendedMetrics) -> str:
    return (
        f"{day:<12} {_fmt_pct(total.twr):>7} {_fmt_pct(ext.max_drawdown):>7} "
        f"{_fmt_duration(ext.max_dd_duration_days):>7} "
        f"{_fmt_duration(ext.days_since_high):>8} "
        f"{_fmt_duration(ext.underwater_days):>9} {_fmt_ratio(ext.recovery_factor):>7} "
        f"{_fmt_signed_pct(ext.best_day):>8} {_fmt_signed_pct(ext.worst_day):>8} "
        f"{_fmt_ratio(ext.gain_loss_ratio):>6}"
    )


def _cmd_performance_metrics_series(
    db_path: str,
    config_path: str,
    *,
    as_of: str,
    n: int,
    reverse: bool = False,
    currency_meta_path: str = DEFAULT_CURRENCY_META,
) -> int:
    timeline = position_timeline(load_trades(db_path))
    if not timeline:
        print("No ETF holdings in database")
        print("Ingest trades: e1f transactions trade-republic path/to/transactions.csv")
        return 0

    start = (date.fromisoformat(as_of) - timedelta(days=n)).isoformat()
    points: list[tuple[str, PerformanceRow, ExtendedMetrics]] = []
    for day in _trading_days(db_path, timeline, start, as_of):
        snapshot = _metrics_snapshot(db_path, config_path, currency_meta_path, timeline, day)
        if snapshot is not None:
            _rows, total, ext = snapshot
            points.append((day, total, ext))
    if not points:
        print(f"No priced trading days in window {start} → {as_of}")
        return 0
    if reverse:
        points = list(reversed(points))

    print(f"\nPortfolio metrics series {start} → {as_of} (EUR, cumulative since inception)")
    print(_METRICS_SERIES_HEADER)
    print("-" * _METRICS_SERIES_RULE_WIDTH)
    for day, total, ext in points:
        print(_format_metrics_series_row(day, total, ext))

    if any(total.estimated for _day, total, _ext in points):
        print(
            "\n~ some days' MktVal is carried forward from an earlier close "
            "(no price on the day itself — fetch to refresh)."
        )
    print(
        "\nDDdur/SinceHi/Underwtr are calendar days; a still-open drawdown's duration is "
        "measured to that day. SinceHi = days since the current running peak (0 = at a new "
        "high). RecFac = TWR/|MaxDD|. Best/Worst are single time-weighted returns."
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

--series N (ADR-0030) lists the portfolio TOTAL for each trading day over the
last N calendar days — one row per day, cumulative-since-inception metrics,
identical to running --as-of on each of those days. Trading days come from the
price data (weekends/holidays with no close drop out); --reverse shows newest
first. Mutually exclusive with --diff. Two trailing columns (ADR-0031) add the
market-value-weighted TER (WTER) and estimated annual fee at that day's MktVal
(Fee€/yr); holdings without TER metadata contribute 0 (dilutes).

--metrics (ADR-0033) replaces the per-holding table with a portfolio-level
extended risk report: XIRR/TWR/CAGR/Vol/MaxDD plus MaxDD duration, days since
the current high, total underwater time, recovery factor, the best/worst
single-period returns (day and calendar month), and trailing 1M/3M/6M
time-weighted returns (a window predating inception shows n/a). Durations are
calendar days. Composes with --as-of; add --series N for one metrics row per
trading day over the window (--reverse newest first); does not compose with --diff.

--contrib (ADR-0033) replaces the per-holding table with a return-contribution
table: each holding's own TWR, current market-value weight, and Cariño-linked
contribution (Ctr%) to the book's time-weighted return — the Ctr% column sums to
the TOTAL TWR. Composes with --as-of/--sort/--reverse; not with --diff/--series/--metrics.

Examples:
  e1f performance
  e1f performance --as-of 2025-12-31
  e1f performance --sort value --reverse
  e1f performance --show-status
  e1f performance --explain
  e1f performance --series 90
  e1f performance --as-of 2025-12-31 --series 30 --reverse
  e1f performance --metrics
  e1f performance --metrics --series 14
  e1f performance --contrib --sort pnl --reverse
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
    parser.add_argument(
        "--diff",
        metavar="N",
        default=None,
        help="Show signed change over the last N calendar days instead of a snapshot "
        "(composes with --as-of: window is [as_of − N, as_of]). N ≥ 1.",
    )
    parser.add_argument(
        "--series",
        metavar="N",
        default=None,
        help="List the portfolio TOTAL for each trading day over the last N calendar "
        "days (cumulative-since-inception metrics; composes with --as-of; --reverse "
        "shows newest first). Mutually exclusive with --diff. N ≥ 1.",
    )
    parser.add_argument(
        "--metrics",
        action="store_true",
        help="Portfolio-level extended risk report (MaxDD duration, underwater time, "
        "recovery factor, best/worst day) instead of the per-holding table. Composes "
        "with --as-of, and with --series N for one metrics row per trading day; not "
        "with --diff.",
    )
    parser.add_argument(
        "--contrib",
        action="store_true",
        help="Per-holding return-contribution table (each holding's Cariño-linked share "
        "of the book's time-weighted return; the Ctr%% column sums to the TOTAL TWR) "
        "instead of the default table. Composes with --as-of/--sort/--reverse; not with "
        "--diff/--series/--metrics.",
    )
    return parser


def _validate_as_of(as_of: str) -> None:
    try:
        date.fromisoformat(as_of)
    except ValueError as exc:
        raise ValueError(f"--as-of must be YYYY-MM-DD: {as_of}") from exc


def _validate_positive_int(raw: str | None, flag: str) -> int | None:
    """Parse an optional ``N`` window arg (``--diff`` / ``--series``); None when unset."""
    if raw is None:
        return None
    try:
        n = int(raw)
    except ValueError as exc:
        raise ValueError(f"{flag} must be a positive integer, got: {raw!r}") from exc
    if n < 1:
        raise ValueError(f"{flag} must be ≥ 1, got: {n}")
    return n


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        _validate_as_of(args.as_of)
        diff_n = _validate_positive_int(args.diff, "--diff")
        series_n = _validate_positive_int(args.series, "--series")
        if diff_n is not None and series_n is not None:
            raise ValueError("--diff and --series are mutually exclusive")
        if args.metrics and diff_n is not None:
            raise ValueError("--metrics does not compose with --diff")
        if args.contrib and (diff_n is not None or series_n is not None or args.metrics):
            raise ValueError("--contrib does not compose with --diff/--series/--metrics")
        if args.contrib:
            return _cmd_performance_contrib(
                args.db,
                args.config,
                as_of=args.as_of,
                sort_by=args.sort,
                reverse=args.reverse,
                currency_meta_path=args.currency_meta,
            )
        if args.metrics and series_n is not None:
            return _cmd_performance_metrics_series(
                args.db,
                args.config,
                as_of=args.as_of,
                n=series_n,
                reverse=args.reverse,
                currency_meta_path=args.currency_meta,
            )
        if args.metrics:
            return _cmd_performance_metrics(
                args.db,
                args.config,
                as_of=args.as_of,
                currency_meta_path=args.currency_meta,
            )
        if series_n is not None:
            return _cmd_performance_series(
                args.db,
                args.config,
                as_of=args.as_of,
                n=series_n,
                reverse=args.reverse,
                currency_meta_path=args.currency_meta,
            )
        if diff_n is not None:
            end = args.as_of
            start = (date.fromisoformat(end) - timedelta(days=diff_n)).isoformat()
            return _cmd_performance_diff(
                args.db,
                args.config,
                end=end,
                start=start,
                sort_by=args.sort,
                reverse=args.reverse,
                show_status=args.show_status,
                explain=args.explain,
                currency_meta_path=args.currency_meta,
            )
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
