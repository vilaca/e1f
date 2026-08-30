"""Daily EUR return series — the shared basis for correlation, performance, and
benchmark comparison (ADR-0033).

A fund's ``eur_return_series`` (graduated from ``correlation``, ADR-0015) and the
whole book's ``portfolio_return_series`` both speak the same currency: time-weighted
daily returns between consecutive *available* EUR closes, gap-bridged not filled.
The value/contribution aggregation and the wealth-index recurrence (graduated from
``performance``, ADR-0011) live here too, so ``performance``'s TOTAL and a benchmark
comparison never disagree on how a return is defined.
"""

import itertools
import math

from .defaults import UNSUPPORTED_FX_CURRENCIES
from .holdings import (
    HoldingSeries,
    PositionEvent,
    build_series,
    convert_to_eur,
    load_price_series,
    load_trades,
    pinned_quote_currency,
    position_asof,
    position_timeline,
    value_on,
)

_SHARE_EPSILON = 1e-9


def contribution_on(events: list[PositionEvent], day: str) -> float:
    """EUR contributed by the events dated exactly ``day`` (0.0 for none)."""
    return sum(event.cash_flow for event in events if event.date == day)


def aggregate_value_series(
    holdings: list[HoldingSeries], first_day: str, as_of: str, db_path: str
) -> list[tuple[str, float, float]]:
    """Portfolio value/contribution series: sum per-ISIN EUR values on shared days.

    A day is dropped when a currently-held ISIN cannot be valued on it (missing
    prior price/FX), rather than treating the gap as zero — which would spike the
    aggregate return when the price later appears (ADR-0011). Returns chronological
    ``(date, total_value, total_contribution)`` triples.
    """
    days: set[str] = set()
    for series in holdings:
        days.update(d for d in series.price_dates if first_day <= d <= as_of)
        days.update(e.date for e in series.events if first_day <= e.date <= as_of)

    points: list[tuple[str, float, float]] = []
    for day in sorted(days):
        total_value = 0.0
        total_contribution = 0.0
        valuable = True
        for series in holdings:
            shares, _cost = position_asof(series.events, day)
            if shares <= _SHARE_EPSILON:
                continue
            value = value_on(series, day, db_path)
            if value is None:
                valuable = False
                break
            total_value += value
            total_contribution += contribution_on(series.events, day)
        if valuable:
            points.append((day, total_value, total_contribution))
    return points


def wealth_and_returns(
    points: list[tuple[str, float, float]],
) -> tuple[list[tuple[str, float]], list[tuple[str, float]]]:
    """Dated wealth index and dated sub-period returns from a value/contribution series.

    Each sub-period return is ``r_t = V_t/(V_prev+CF_t) − 1`` (contribution treated as
    start-of-day), chain-linked into a wealth index seeded at 1.0. Returns
    ``(wealth_path, returns)``, each a list of ``(date, value)`` over the days where a
    return is defined. This is the single definition of the book's time-weighted daily
    return, shared by ``performance``'s risk metrics and any benchmark comparison.
    """
    returns: list[tuple[str, float]] = []
    wealth_path: list[tuple[str, float]] = []
    previous_value = 0.0
    wealth = 1.0
    for day, value, contribution in points:
        denominator = previous_value + contribution
        if denominator > 0.0:
            period_return = value / denominator - 1.0
            returns.append((day, period_return))
            wealth *= 1.0 + period_return
            wealth_path.append((day, wealth))
        previous_value = value
    return wealth_path, returns


def eur_close_series(
    db_path: str, isin: str, as_of: str, currency_meta_path: str
) -> list[tuple[str, float]]:
    """A fund's available EUR closes as ``(date, close)``, date-sorted, ``<= as_of``.

    A day with a local price but no FX rate on or before it is skipped (not filled).
    An ISIN with no pinned currency — or a currency with no EUR FX rule at all
    (GBX pence, ADR-0010) — has no EUR closes → ``[]``.
    """
    currency = pinned_quote_currency(isin, currency_meta_path)
    if currency is None or currency in UNSUPPORTED_FX_CURRENCIES:
        # No pinned currency, or a currency convert_to_eur permanently refuses (pence):
        # no EUR close ever exists, so there is no return series. Handling this up front
        # narrows the except below to the ONE transient case it is meant for.
        return []
    dates, closes = load_price_series(db_path, isin, as_of)
    eur_closes: list[tuple[str, float]] = []
    for day, close in zip(dates, closes, strict=True):
        try:
            eur_closes.append((day, convert_to_eur(close, currency, day, db_path)))
        except ValueError:
            continue  # the sole remaining ValueError: no FX rate on/before this day
    return eur_closes


def eur_return_series(
    db_path: str, isin: str, as_of: str, currency_meta_path: str
) -> list[tuple[str, float]]:
    """A fund's EUR returns as ``(date, return)`` between consecutive *available* EUR
    closes, date-sorted, ``<= as_of``.

    These are **not** necessarily calendar-daily: each return is
    ``close_t / close_prev − 1`` between consecutive *available* EUR closes and is
    dated at the later date ``t``, so a return may span more than one calendar day
    when a close in between is missing. A day with a local price but no FX rate on
    or before it has no EUR close; it is skipped and the adjacent return spans it
    (bridged, not filled). An ISIN with no pinned currency — or a currency with no
    EUR FX rule at all (GBX pence, ADR-0010) — has no EUR closes → ``[]``.

    ``load_price_series`` guarantees the ``(date, close)`` inputs are date-sorted and
    one-per-day, so the emitted returns are date-sorted with unique dates too.
    """
    return [
        (day, cur / prev - 1.0)
        for (_prev_day, prev), (day, cur) in itertools.pairwise(
            eur_close_series(db_path, isin, as_of, currency_meta_path)
        )
    ]


_CARINO_EPSILON = 1e-12


def _carino_coefficient(period_return: float) -> float:
    """Cariño log-linking coefficient ``ln(1+r)/r``, with the ``r → 0`` limit of 1.0."""
    if abs(period_return) < _CARINO_EPSILON:
        return 1.0
    return math.log1p(period_return) / period_return


def contribution_to_return(
    holdings: list[HoldingSeries], first_day: str, as_of: str, db_path: str
) -> dict[str, float] | None:
    """Each ISIN's Cariño-linked contribution to the book's total time-weighted return.

    Uses the same value/contribution days and sub-period-return definition as
    ``aggregate_value_series`` / ``wealth_and_returns`` (so the whole-book total equals
    ``performance``'s TOTAL TWR), decomposes each day's portfolio return into
    ``w_{i,prev} · r_{i,t}`` per holding — which sums to the day's portfolio return by
    construction — and Cariño-links (ADR-0033) the daily arithmetic contributions so the
    per-holding totals sum **exactly** to the multi-period portfolio return.

    Returns ``{isin: contribution}`` over the given ``holdings``, or ``None`` when the
    total return is undefined (no priced sub-period) or any sub-period wipes the book to
    zero (a −100% period, where ``ln(1+r)`` is singular). Days where a currently-held
    ISIN cannot be valued are dropped, exactly as ``aggregate_value_series`` drops them.
    """
    days: set[str] = set()
    for series in holdings:
        days.update(d for d in series.price_dates if first_day <= d <= as_of)
        days.update(e.date for e in series.events if first_day <= e.date <= as_of)

    raw: dict[str, float] = {series.isin: 0.0 for series in holdings}
    previous_value: dict[str, float] = {series.isin: 0.0 for series in holdings}
    previous_total = 0.0
    wealth = 1.0
    saw_return = False

    for day in sorted(days):
        current: dict[str, float] = {}
        cash_flow: dict[str, float] = {}
        total_value = 0.0
        total_contribution = 0.0
        valuable = True
        for series in holdings:
            shares, _cost = position_asof(series.events, day)
            if shares <= _SHARE_EPSILON:
                current[series.isin] = 0.0
                cash_flow[series.isin] = 0.0
                continue
            value = value_on(series, day, db_path)
            if value is None:
                valuable = False
                break
            current[series.isin] = value
            cash_flow[series.isin] = contribution_on(series.events, day)
            total_value += value
            total_contribution += cash_flow[series.isin]
        if not valuable:
            continue

        denominator = previous_total + total_contribution
        if denominator > 0.0:
            period_return = total_value / denominator - 1.0
            if period_return <= -1.0:  # a sub-period wipeout — ln(1+r) is singular
                return None
            coefficient = _carino_coefficient(period_return)
            for isin in raw:
                daily = (current[isin] - (previous_value[isin] + cash_flow[isin])) / denominator
                raw[isin] += coefficient * daily
            wealth *= 1.0 + period_return
            saw_return = True
        previous_value = current
        previous_total = total_value

    if not saw_return:
        return None
    scale = _carino_coefficient(wealth - 1.0)
    return {isin: value / scale for isin, value in raw.items()}


def portfolio_return_series(
    db_path: str, currency_meta_path: str, as_of: str
) -> list[tuple[str, float]]:
    """The whole book's time-weighted EUR daily returns ``(date, return)`` up to ``as_of``.

    Builds every held ISIN's series, keeps those valuable on ``as_of``, aggregates
    their EUR value/contribution per day, and chain-links the sub-period returns —
    the same path ``performance``'s TOTAL row takes, so the two agree. Empty when the
    book has nothing valuable on ``as_of``.
    """
    timeline = position_timeline(load_trades(db_path))
    holdings: list[HoldingSeries] = []
    for isin, events in timeline.items():
        capped = [event for event in events if event.date <= as_of]
        if not capped:
            continue
        holdings.append(build_series(db_path, isin, capped, as_of, currency_meta_path))

    valuable = [series for series in holdings if value_on(series, as_of, db_path) is not None]
    if not valuable:
        return []
    first_day = min(series.events[0].date for series in valuable)
    points = aggregate_value_series(valuable, first_day, as_of, db_path)
    _wealth_path, returns = wealth_and_returns(points)
    return returns
