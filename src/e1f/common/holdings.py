"""Trades, position timeline, FX conversion, and point-in-time EUR valuation."""

import bisect
import sqlite3
from contextlib import closing
from dataclasses import dataclass

from .currency_metadata import CurrencyMetadata
from .defaults import BASE_CURRENCY, DEFAULT_CURRENCY_META, UNSUPPORTED_FX_CURRENCIES


_SHARE_EPSILON = 1e-9
_BUY_SIDES = frozenset({"BUY", "SAVINGS_PLAN"})


def load_trades(
    db_path: str,
) -> list[tuple[str, str, str, str, float, float, float]]:
    """Chronological trade rows ``(broker, datetime, symbol, side, shares, price, fee)``.

    The shared read behind holdings and performance: ordered by ``datetime`` then
    ``transaction_id`` so average-cost accounting is deterministic. Empty when the
    ``transactions`` table is absent.
    """

    with closing(sqlite3.connect(db_path)) as conn:
        if (
            conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='transactions'"
            ).fetchone()
            is None
        ):
            return []
        return conn.execute(
            """
            SELECT broker, datetime, symbol, side, shares, price, fee
            FROM transactions
            ORDER BY datetime, transaction_id
            """
        ).fetchall()


@dataclass(frozen=True)
class PositionEvent:
    """One trade's effect on a per-ISIN position, with running totals.

    ``cash_flow`` is the EUR contributed by this event (``shares * price + fee``);
    it is ``0.0`` for a SELL, since buy-and-hold return math treats cash flows as
    contributions only (ADR-0011). ``shares_held`` and ``cost_basis`` are the
    cumulative average-cost totals *after* the event.
    """

    date: str
    cash_flow: float
    shares_held: float
    cost_basis: float


def position_timeline(
    rows: list[tuple[str, str, str, str, float, float, float]],
) -> dict[str, list[PositionEvent]]:
    """Per-ISIN chronological position events, netted across brokers (ADR-0011).

    Shares are keyed on the ISIN alone (value is broker-agnostic), so contributions
    to the same fund at different brokers accumulate into one series. Share/cost
    accounting mirrors ``portfolio.compute_holdings`` average-cost, including SELL
    reduction, so the two agree on the final snapshot. Dates are the ``YYYY-MM-DD``
    prefix of the trade datetime.
    """
    running: dict[str, tuple[float, float]] = {}
    timeline: dict[str, list[PositionEvent]] = {}

    for _broker, dt, symbol, side, shares, price, fee in rows:
        qty = shares or 0.0
        if qty <= 0:
            continue
        unit_price = price or 0.0
        trade_fee = fee or 0.0
        held, cost = running.get(symbol, (0.0, 0.0))

        if side in _BUY_SIDES:
            cash_flow = qty * unit_price + trade_fee
            held += qty
            cost += cash_flow
        elif side == "SELL":
            if held <= _SHARE_EPSILON:
                continue
            sell_qty = min(qty, held)
            avg = cost / held
            held -= sell_qty
            cost -= avg * sell_qty
            cash_flow = 0.0
        else:
            continue

        running[symbol] = (held, cost)
        timeline.setdefault(symbol, []).append(
            PositionEvent(
                date=str(dt)[:10],
                cash_flow=cash_flow,
                shares_held=held,
                cost_basis=cost,
            )
        )

    return timeline


def pinned_quote_currency(isin: str, currency_meta_path: str = DEFAULT_CURRENCY_META) -> str | None:
    """Currency the stored ``prices.close`` for ``isin`` is denominated in.

    Read from the pinned ftgo resolution sidecar (ADR-0002) — the only trustworthy
    statement of a stored price's currency, never ``fund_currency`` (ADR-0010).
    ``None`` when the ISIN is not pinned, so a caller can treat it as unvaluable.
    """
    entry = CurrencyMetadata.load(currency_meta_path).funds.get(isin)
    if entry is None:
        return None
    currency = entry.get("currency")
    return str(currency) if currency else None


def portfolio_isins(db_path: str) -> frozenset[str]:
    """ISINs with a net-positive position derived from stored transactions."""

    with closing(sqlite3.connect(db_path)) as conn:
        if (
            conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='transactions'"
            ).fetchone()
            is None
        ):
            return frozenset()
        rows = conn.execute(
            "SELECT symbol, side, shares FROM transactions ORDER BY datetime, transaction_id"
        ).fetchall()

    held: dict[str, float] = {}
    for symbol, side, shares in rows:
        qty = shares or 0.0
        if qty <= 0:
            continue
        if side in _BUY_SIDES:
            held[symbol] = held.get(symbol, 0.0) + qty
        elif side == "SELL":
            prev = held.get(symbol, 0.0)
            held[symbol] = max(0.0, prev - qty)

    return frozenset(sym for sym, qty in held.items() if qty > _SHARE_EPSILON)


def fx_rate_asof(db_path: str, quote: str, date: str, base: str = BASE_CURRENCY) -> float:
    """Nearest-prior FX rate (``quote`` units per 1 ``base``) on or before ``date``.

    Forward-fill / nearest-prior per ADR-0010: the most recent stored rate dated
    on or before ``date``. Never interpolates and never uses a later rate, so an
    as-of valuation can't depend on future information. Returns ``1.0`` for the
    identity ``quote == base``. Raises ValueError when no rate exists on or before
    ``date`` — an unfetched pair, or a date preceding the series — so a caller can
    never silently value with a missing rate.
    """

    if quote == base:
        return 1.0

    with closing(sqlite3.connect(db_path)) as conn:
        row = conn.execute(
            "SELECT rate FROM fx_rates "
            "WHERE base = ? AND quote = ? AND date <= ? "
            "ORDER BY date DESC LIMIT 1",
            (base, quote, date),
        ).fetchone()

    if row is None:
        raise ValueError(
            f"no {base}/{quote} FX rate on or before {date} "
            f"(pair unfetched, or date precedes the series)"
        )
    return float(row[0])


def convert_to_eur(amount: float, quote: str, date: str, db_path: str) -> float:
    """Convert ``amount`` (in ``quote`` currency) to EUR using the as-of daily rate.

    Applies ``amount / rate`` where ``rate`` is quote-per-EUR (ADR-0010). Refuses
    currencies with no EUR FX rule (e.g. GBX pence) rather than mis-converting.
    """
    if quote == BASE_CURRENCY:
        return amount
    if quote in UNSUPPORTED_FX_CURRENCIES:
        raise ValueError(
            f"currency {quote} (pence) has no EUR FX rule — needs GBP "
            f"normalization; not supported (ADR-0010)"
        )
    return amount / fx_rate_asof(db_path, quote, date)


# ---------------------------------------------------------------------------
# Point-in-time EUR valuation core (ADR-0013 decision 4, graduated down from
# ``performance``). ``performance`` re-imports these for its return metrics;
# ``overlap`` consumes ``fund_eur_value`` for a held fund's ``Vf``. The move is a
# clean downward relocation — every dependency (PositionEvent, convert_to_eur,
# pinned_quote_currency, load_trades, position_timeline) already lives here.
# ---------------------------------------------------------------------------


@dataclass
class HoldingSeries:
    """Everything needed to value and measure one held ISIN as of a date."""

    isin: str
    events: list[PositionEvent]  # filtered to date <= as_of, chronological
    price_dates: list[str]  # sorted, <= as_of
    price_closes: list[float]  # parallel to price_dates, native currency
    currency: str | None


def position_asof(events: list[PositionEvent], day: str) -> tuple[float, float]:
    """Shares held and average-cost basis after the last event on or before ``day``."""
    shares, cost = 0.0, 0.0
    for event in events:
        if event.date > day:
            break
        shares, cost = event.shares_held, event.cost_basis
    return shares, cost


def price_index_asof(series: HoldingSeries, day: str) -> int:
    """Index of the nearest-prior priced day on or before ``day`` (-1 if none)."""
    return bisect.bisect_right(series.price_dates, day) - 1


def close_asof(series: HoldingSeries, day: str) -> float | None:
    """Nearest-prior close on or before ``day``; None if the day precedes history."""
    index = price_index_asof(series, day)
    return None if index < 0 else series.price_closes[index]


def price_date_asof(series: HoldingSeries, day: str) -> str | None:
    """Date of the close ``close_asof`` would use for ``day`` (None if none)."""
    index = price_index_asof(series, day)
    return None if index < 0 else series.price_dates[index]


def value_on(series: HoldingSeries, day: str, db_path: str) -> float | None:
    """EUR market value of the position on ``day``; None when it cannot be valued.

    None means: no pinned currency, no price on or before the day, or no FX rate
    on or before the day (``convert_to_eur`` raising) — every path that would
    otherwise force a silent or wrong number.
    """
    if series.currency is None:
        return None
    shares, _cost = position_asof(series.events, day)
    if shares <= _SHARE_EPSILON:
        return 0.0
    unit_value = unit_value_on(series, day, db_path)
    return None if unit_value is None else shares * unit_value


def unit_value_on(series: HoldingSeries, day: str, db_path: str) -> float | None:
    """EUR value of one share on ``day`` using the canonical close and FX rules."""
    if series.currency is None:
        return None
    close = close_asof(series, day)
    if close is None:
        return None
    try:
        return convert_to_eur(close, series.currency, day, db_path)
    except ValueError:
        return None


def load_price_series(db_path: str, isin: str, as_of: str) -> tuple[list[str], list[float]]:
    """Sorted ``(dates, closes)`` for an ISIN, deduped to one close per day, <= as_of."""
    with closing(sqlite3.connect(db_path)) as conn:
        if (
            conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='prices'"
            ).fetchone()
            is None
        ):
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


def build_series(
    db_path: str,
    isin: str,
    events: list[PositionEvent],
    as_of: str,
    currency_meta_path: str,
) -> HoldingSeries:
    """A fund's price/position series as of ``as_of``, ready to value."""
    dates, closes = load_price_series(db_path, isin, as_of)
    return HoldingSeries(
        isin=isin,
        events=events,
        price_dates=dates,
        price_closes=closes,
        currency=pinned_quote_currency(isin, currency_meta_path),
    )


def fund_eur_value(
    isin: str,
    as_of: str,
    db_path: str,
    currency_meta_path: str = DEFAULT_CURRENCY_META,
) -> float | None:
    """EUR value of a held fund on ``as_of`` (``Vf`` for ADR-0013's overlap floor).

    Wraps ``load_trades → position_timeline → build_series → value_on``. Returns
    ``None`` when the fund cannot be valued — never held on/before ``as_of``, or
    ``value_on``'s own None (no pinned currency, no price, or no FX). A
    ``None``-valued fund is excluded from the overlap floor and disclosed
    (ADR-0013 decision 4), never treated as €0.
    """
    events = [
        event
        for event in position_timeline(load_trades(db_path)).get(isin, [])
        if event.date <= as_of
    ]
    if not events:
        return None
    return value_on(build_series(db_path, isin, events, as_of, currency_meta_path), as_of, db_path)
