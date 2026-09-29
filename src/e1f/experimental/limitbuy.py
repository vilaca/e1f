#!/usr/bin/env python
"""e1f limitbuy — close-only worst-case bound on a buy-limit order (ADR-0055).

For every start day in one ETF's EUR daily-close history, compares a buy-limit
placed a fixed percent under that day's close — open for a number of trading
days, bought at market on the expiry close if it never fills — with buying at
that close. It reads only closes, so a fill is detected when a later close is
strictly below the limit; a real order also fills on intraday touches the closes
miss. Every figure is therefore a lower bound for the limit order (Status
BOUNDED): on each start day a real order fills at least as often and ends with at
least as many shares.

Where daily bars are stored (ADR-0056), a second table estimates the same orders
from each day's open and low (ADR-0057, Status CALCULATED): a fill at the open when
the day opens below the limit, else at the limit when its low is strictly below.
On every window it covers, the estimate is at least the bound.

Usage:
    e1f limitbuy --isin IE0003XJA0J9
    e1f limitbuy --isin IE0003XJA0J9 --limit 13.00 --limit 12.90 --expiry 5
    e1f limitbuy --isin IE00B4L5Y983 --below 1 --below 2 --expiry 21 --explain
"""

import argparse
import bisect
import math
import statistics
import sys
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime
from enum import StrEnum

from e1f.common import (
    BASE_CURRENCY,
    DEFAULT_CONFIG,
    DEFAULT_CURRENCY_META,
    DEFAULT_DB,
    ConfigManager,
    MetricContract,
    Status,
    _explain_metric,
)
from e1f.experimental.common import (
    CRASH_WINDOWS,
    DailyBar,
    candidate_listing,
    crash_split,
    eur_series,
    load_daily_bars,
    price_catalog,
)

_TODAY = datetime.now(UTC).date().isoformat()

DEFAULT_BELOW_PCT = (0.5, 1.0, 2.0, 3.0, 5.0)
DEFAULT_EXPIRY_DAYS = (5, 21)

# A row with fewer non-overlapping windows is illustrative, not evidence (ADR-0055 §4).
ILLUSTRATIVE_INDEPENDENT = 24


LIMITBUY_CONTRACT = MetricContract(
    method_version="limit_buy_close_bound_v1",
    requires=("intraday lows (would turn each lower bound into a point estimate)",),
    does_not_require=(
        "return forecasts",
        "synthetic or proxy history",
        "a covariance estimate",
    ),
    supports=(
        "worst-case fill rate of a buy-limit",
        "worst-case share advantage vs buying at the close",
        "distribution over every start day",
    ),
    limitations=(
        "close-only: a real order fills at least as often — every figure is a lower bound",
        "a close strictly below the limit is taken as a fill at the limit (price priority); "
        "no fees, spread, queue position or partial fills",
        "accumulating series assumed — an ex-dividend drop reads as a dip and the buy-now "
        "dividend is not counted",
        "a converted (non-EUR) series only approximates a EUR-quoted listing",
        "overlapping windows are not independent; the non-overlapping count is disclosed",
    ),
)

LIMITBUY_ESTIMATE_CONTRACT = MetricContract(
    method_version="limit_buy_bar_estimate_v1",
    requires=(
        "the broker's own fills or quotes (a broker filling against its own bid/ask, "
        "not the listing's trades, can fill differently)",
    ),
    does_not_require=(
        "return forecasts",
        "synthetic or proxy history",
        "a covariance estimate",
    ),
    supports=(
        "fill rate of a buy-limit from each day's open and low",
        "share advantage vs buying at the close",
        "distribution over every start day whose window has usable bars",
    ),
    limitations=(
        "the order is assumed to rest on the listing whose bars ftgo reports",
        "fills at the open when a day opens below the limit, else at the limit when its "
        "low is strictly below it; a low exactly at the limit is not a fill (queue "
        "position); no fees, spread or partial fills",
        "a window with an open day lacking a usable bar (none stored, no volume, or "
        "low/high not bracketing open and close) is excluded; the count is disclosed",
        "accumulating series assumed — an ex-dividend drop reads as a dip and the buy-now "
        "dividend is not counted",
        "a converted (non-EUR) series only approximates a EUR-quoted listing; each bar "
        "converts at its close's FX rate",
        "overlapping windows are not independent; the non-overlapping count is disclosed",
    ),
)


class LimitBuyError(Exception):
    """A usage/data problem that stops a run with a message (never a stack trace)."""


class Gap(StrEnum):
    """Why a row has no figures (ADR-0057 §5)."""

    NO_WINDOW = "no start day has a full expiry window"
    NO_BARS = "no daily bars stored for this series"
    NO_USABLE_WINDOW = "every window has an open day without a usable bar"


@dataclass(frozen=True)
class LimitBuyRow:
    """One ``(expiry, below)`` cell of the bound (ADR-0055 §3-4) or the estimate (ADR-0057)."""

    below: float  # fraction under the start close (0.005 = 0.5%)
    expiry: int  # trading days the order stays open
    windows: int  # start days with a complete expiry window (estimate: with usable bars)
    fill_rate: float | None  # the statistics are None when windows == 0
    win_rate: float | None  # share of start days with ΔShares ≥ 0
    mean: float | None  # ΔShares mean / median / nearest-rank P10 / minimum
    median: float | None
    p10: float | None
    worst: float | None
    estimated: bool = False  # True: daily-bar estimate; False: close-only bound
    excluded: int = 0  # estimate only: windows left out for an unusable bar
    gap: Gap | None = None  # set exactly when windows == 0
    # Estimate only: start indices of the windows it covers, for its coverage lines.
    covered: tuple[int, ...] = field(default=(), repr=False, compare=False)

    @property
    def status(self) -> Status:
        if not self.windows:
            return Status.UNAVAILABLE
        return Status.CALCULATED if self.estimated else Status.BOUNDED

    @property
    def independent(self) -> int:
        """Non-overlapping windows: every ``expiry``-th start day."""
        return math.ceil(self.windows / self.expiry)

    @property
    def illustrative(self) -> bool:
        return 0 < self.independent < ILLUSTRATIVE_INDEPENDENT


@dataclass(frozen=True)
class LimitBuyRun:
    """Everything one run established, before rendering."""

    isin: str
    name: str
    currency: str  # listing currency; a non-EUR series was converted (ADR-0055 §5)
    distributing: bool
    dates: list[str]
    last_close: float  # EUR; the reference --limit prices convert against
    start: int  # index of the first start day (--from)
    limits: list[tuple[float, float]]  # (--limit EUR price, its fraction under the last close)
    rows: list[LimitBuyRow]
    estimates: list[LimitBuyRow]  # same order as rows (ADR-0057)
    bar_days: int  # days of the series with a stored bar
    usable_days: int  # of those, days whose bar passes the usability check

    @property
    def converted(self) -> bool:
        return self.currency != BASE_CURRENCY


# ---------------------------------------------------------------------------
# Pure core.
# ---------------------------------------------------------------------------


def limit_outcome(closes: list[float], t: int, below: float, expiry: int) -> tuple[bool, float]:
    """``(filled, Δshares)`` for a buy-limit placed at close ``t`` (ADR-0055 §1-2).

    The limit sits ``below`` under close ``t`` and is open for the next ``expiry``
    closes. It fills at the limit on the first of them strictly below it; otherwise
    it buys at the expiry close. Δshares is its share count over buying at close
    ``t``, minus 1.
    """
    limit = closes[t] * (1.0 - below)
    for k in range(t + 1, t + expiry + 1):
        if closes[k] < limit:
            return True, 1.0 / (1.0 - below) - 1.0
    return False, closes[t] / closes[t + expiry] - 1.0


def usable_bar(bar: DailyBar) -> bool:
    """A day whose open and low a fill can be read from (ADR-0057 §3).

    It traded, and its low and high bracket both its open and its close. A day with no
    volume carries a price nobody traded at; an unbracketed bar contradicts itself.
    """
    return (
        bar.volume > 0
        and bar.low <= min(bar.open, bar.close)
        and bar.high >= max(bar.open, bar.close)
    )


def align_bars(
    dates: list[str], closes: list[float], native: dict[str, DailyBar]
) -> list[DailyBar | None]:
    """EUR bar per series day, or None where no usable bar is stored (ADR-0057 §4).

    The check runs on the stored (native) bar, then open/high/low convert at the rate
    the day's EUR close used — ``eur_close / native_close`` — so a bar and its close
    never disagree on FX. EUR funds pass through unchanged.
    """
    aligned: list[DailyBar | None] = []
    for day, eur_close in zip(dates, closes, strict=True):
        bar = native.get(day)
        if bar is None or bar.close <= 0.0 or not usable_bar(bar):
            aligned.append(None)
            continue
        rate = eur_close / bar.close
        aligned.append(
            DailyBar(bar.open * rate, bar.high * rate, bar.low * rate, eur_close, bar.volume)
        )
    return aligned


def bar_outcome(
    closes: list[float], bars: list[DailyBar | None], t: int, below: float, expiry: int
) -> tuple[bool, float] | None:
    """``(filled, Δshares)`` from daily bars, or None if a window day lacks a usable bar.

    Same order as ``limit_outcome`` (ADR-0057 §1). On each open day ``t+1 … t+expiry``:
    an open below the limit fills at the open (the resting bid meets a lower opening
    price); otherwise a low strictly below the limit fills at the limit. Unfilled, it
    buys at the expiry close.
    """
    window: list[DailyBar] = []
    for k in range(t + 1, t + expiry + 1):
        bar = bars[k]
        if bar is None:
            return None
        window.append(bar)
    limit = closes[t] * (1.0 - below)
    for bar in window:
        if bar.open < limit:
            return True, closes[t] / bar.open - 1.0
        if bar.low < limit:
            return True, 1.0 / (1.0 - below) - 1.0
    return False, closes[t] / closes[t + expiry] - 1.0


def _row(
    below: float,
    expiry: int,
    outcomes: list[tuple[bool, float]],
    *,
    gap: Gap,
    estimated: bool = False,
    excluded: int = 0,
) -> LimitBuyRow:
    """Summary of per-start-day outcomes; ``gap`` names the reason when there are none."""
    if not outcomes:
        return LimitBuyRow(
            below, expiry, 0, None, None, None, None, None, None,
            estimated=estimated, excluded=excluded, gap=gap,
        )
    deltas = sorted(delta for _, delta in outcomes)
    n = len(deltas)
    return LimitBuyRow(
        below=below,
        expiry=expiry,
        windows=n,
        fill_rate=sum(filled for filled, _ in outcomes) / n,
        win_rate=sum(delta >= 0.0 for delta in deltas) / n,
        mean=statistics.fmean(deltas),
        median=statistics.median(deltas),
        p10=deltas[math.ceil(0.10 * n) - 1],  # nearest rank
        worst=deltas[0],
        estimated=estimated,
        excluded=excluded,
    )


def summarize(closes: list[float], start: int, below: float, expiry: int) -> LimitBuyRow:
    """Summarise every start day ``t ≥ start`` whose expiry window fits the series."""
    outcomes = [
        limit_outcome(closes, t, below, expiry) for t in range(start, len(closes) - expiry)
    ]
    return _row(below, expiry, outcomes, gap=Gap.NO_WINDOW)


def summarize_bars(
    closes: list[float],
    bars: list[DailyBar | None],
    start: int,
    below: float,
    expiry: int,
    *,
    bars_stored: bool = True,
) -> LimitBuyRow:
    """The daily-bar estimate over the same start days as ``summarize`` (ADR-0057).

    A start day whose window has an open day without a usable bar is excluded and
    counted, never filled in from closes. ``bars_stored`` is False when the series
    has no stored bar at all, which is the reason given for an empty row.
    """
    starts = range(start, len(closes) - expiry)
    found = [(t, bar_outcome(closes, bars, t, below, expiry)) for t in starts]
    covered = [(t, outcome) for t, outcome in found if outcome is not None]
    if not bars_stored:
        gap = Gap.NO_BARS
    elif not starts:
        gap = Gap.NO_WINDOW
    else:
        gap = Gap.NO_USABLE_WINDOW
    row = _row(
        below, expiry, [outcome for _, outcome in covered], gap=gap, estimated=True,
        excluded=len(found) - len(covered),
    )
    return replace(row, covered=tuple(t for t, _ in covered))


def below_from_limit(price: float, last_close: float) -> float:
    """Fraction a limit price sits under the last close; a price above it is refused."""
    if price > last_close:
        raise LimitBuyError(
            f"limit €{price:,.2f} is above the last close €{last_close:,.2f} — it would "
            "fill at once, which is buying now"
        )
    return 1.0 - price / last_close


# ---------------------------------------------------------------------------
# Rendering.
# ---------------------------------------------------------------------------


def _fmt_rate(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100.0:.0f}%"


def _fmt_signed_pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100.0:+.2f}%"


def _fmt_below(below: float) -> str:
    return f"{below * 100.0:.2f}%"


def _years(first: str, last: str) -> float:
    return (date.fromisoformat(last) - date.fromisoformat(first)).days / 365.25


def _estimate_coverage(run: LimitBuyRun) -> tuple[str | None, list[str], list[str]]:
    """The estimate's first covered start day, and the crashes a covered window overlaps.

    Excluded windows can sit anywhere in the series, so crash coverage is read from
    the covered windows themselves, not from a span.
    """
    spans = [
        (run.dates[t], run.dates[t + row.expiry]) for row in run.estimates for t in row.covered
    ]
    if not spans:
        return None, [], [name for name, _, _ in CRASH_WINDOWS]
    tested = [
        name for name, cstart, cend in CRASH_WINDOWS
        if any(first <= cend and last >= cstart for first, last in spans)
    ]
    absent = [name for name, _, _ in CRASH_WINDOWS if name not in tested]
    return min(first for first, _ in spans), tested, absent


def _header(run: LimitBuyRun) -> list[str]:
    first, last, start = run.dates[0], run.dates[-1], run.dates[run.start]
    tested, absent = crash_split(start, last)
    lines = [
        f"\nLimit-buy — {run.name} ({run.isin}) · {BASE_CURRENCY}",
        f"Data:     {first} → {last}  ({_years(first, last):.1f}y, {len(run.dates)} closes)",
    ]
    if run.converted:
        lines.append(
            f"Currency: {run.currency} closes → EUR at the nearest-prior FX rate; only "
            "approximates a EUR listing"
        )
    lines += [
        f"Starts:   {start} onward — every close with a full expiry window after it",
        f"Crashes:  tested: {', '.join(tested) or '—'}  ·  absent: {', '.join(absent) or '—'}",
        "Order:    buy-limit Below under the start close, open Expiry trading days; if it",
        "          has not filled by then, it buys at the expiry close",
        "Bound:    fills at the limit on the first later close strictly below it",
    ]
    if run.bar_days:
        lines += [
            "Estimate: fills at the open when a day opens below the limit, else at the limit",
            f"          when its low is strictly below it — bars on {run.bar_days} of "
            f"{len(run.dates)} days, {run.usable_days} usable",
        ]
        first_covered, est_tested, est_absent = _estimate_coverage(run)
        if first_covered is not None:
            lines.append(
                f"          covers start days from {first_covered}; tested: "
                f"{', '.join(est_tested) or '—'}  ·  absent: {', '.join(est_absent) or '—'}"
            )
    if run.limits:
        mapped = ", ".join(f"€{price:,.2f} → {_fmt_below(below)}" for price, below in run.limits)
        lines.append(f"Limits:   vs last close €{run.last_close:,.2f} ({last}): {mapped}")
    return lines


def _table(rows: list[LimitBuyRow], show_status: bool, *, estimate: bool = False) -> list[str]:
    """The bound's table (every figure ≥) or the estimate's (with an Excl column)."""
    mark = "" if estimate else "≥"
    excl_head = f"  {'Excl':>5}" if estimate else ""
    head = (
        f"{'Expiry':>6}  {'Below':>6}  {'Windows':>7}{excl_head}  {'Indep':>6}"
        f"  {'Fill%' + mark:>6}  {'Win%' + mark:>6}  {'Mean' + mark:>7}"
        f"  {'Median' + mark:>7}  {'P10' + mark:>7}  {'Worst' + mark:>7}"
    )
    if show_status:
        head += f"  {'Status':<11}"
    title = (
        "Daily-bar estimate (ADR-0057):" if estimate
        else "Close-only bound — worst case for the limit order (ADR-0055):"
    )
    lines = ["", title, head, "-" * len(head)]
    for row in rows:
        indep = f"{row.independent}{'*' if row.illustrative else ''}"
        excl = f"  {row.excluded:>5}" if estimate else ""
        line = (
            f"{row.expiry:>5}d  {_fmt_below(row.below):>6}  {row.windows:>7}{excl}  {indep:>6}"
            f"  {_fmt_rate(row.fill_rate):>6}  {_fmt_rate(row.win_rate):>6}"
            f"  {_fmt_signed_pct(row.mean):>7}  {_fmt_signed_pct(row.median):>7}"
            f"  {_fmt_signed_pct(row.p10):>7}  {_fmt_signed_pct(row.worst):>7}"
        )
        if show_status:
            line += f"  {row.status.value:<11}"
        lines.append(line)
    return lines


def _footnotes(run: LimitBuyRun) -> list[str]:
    lines = [
        "",
        "Mean/Median/P10/Worst are ΔShares: shares the limit order ends with vs buying at",
        "the start close (+0.50% = 0.5% more shares). Win% = start days where it ended",
        "with at least as many. ≥ = worst case for the limit order: closes miss intraday",
        "touches, so a real order fills at least as often and ends with at least as many",
        "shares on every start day (ADR-0055). Windows overlap; Indep counts",
        "non-overlapping ones.",
    ]
    if run.bar_days:
        lines += [
            "The estimate reads the same orders from each open day's bar and is at least",
            "the bound on every window it covers (ADR-0057). It assumes the order rests on",
            "the listing whose bars ftgo reports; a broker filling against its own quote",
            "can fill differently. Excl = windows left out because an open day has no",
            "usable bar (none stored, no volume, or low/high not bracketing open and close).",
            "The guarantee is per window: where Excl > 0 the estimate summarises fewer",
            "windows than the bound, so its figures can fall below the bound's.",
        ]
    if any(row.illustrative for row in run.rows + run.estimates):
        lines.append(
            f"* fewer than {ILLUSTRATIVE_INDEPENDENT} non-overlapping windows — "
            "illustrative, not evidence."
        )
    unavailable = sorted({row.expiry for row in run.rows if row.status is Status.UNAVAILABLE})
    if unavailable:
        days = ", ".join(f"{expiry}d" for expiry in unavailable)
        lines.append(
            f"n/a = UNAVAILABLE: no start day from {run.dates[run.start]} has a full "
            f"window for expiry {days}."
        )
    unusable = sorted(
        {row.expiry for row in run.estimates if row.gap is Gap.NO_USABLE_WINDOW}
    )
    if unusable:
        days = ", ".join(f"{expiry}d" for expiry in unusable)
        lines.append(
            f"n/a in the estimate = UNAVAILABLE: every window for expiry {days} has an "
            "open day without a usable bar."
        )
    lines.append(
        "\nNote: Below and Expiry are chosen by you and only tabulated — none is fitted or"
        "\nranked on this history."
    )
    return lines


def _explain_block(run: LimitBuyRun) -> list[str]:
    bounded = [row for row in run.rows if row.status is Status.BOUNDED]
    status = Status.BOUNDED if bounded else Status.UNAVAILABLE
    result = f"lower bounds for {len(bounded)} of {len(run.rows)} (Expiry, Below) rows"
    if len(bounded) < len(run.rows):
        result += f"; {len(run.rows) - len(bounded)} UNAVAILABLE (no complete window)"
    inputs = (
        f"{run.name} ({run.isin}) EUR closes — data {run.dates[0]}→{run.dates[-1]} "
        f"({len(run.dates)} closes), start days from {run.dates[run.start]}"
    )
    method = (
        "limit = close_t × (1 − Below), placed after close t; fills at the limit on the "
        "first of closes t+1…t+Expiry strictly below it, else buys at close t+Expiry; "
        "ΔShares = limit shares / shares bought at close t − 1; P10 nearest rank; "
        "Indep = ceil(Windows / Expiry); EUR valuation: native close × nearest-prior "
        "EUR/quote FX (EUR funds pass through)"
    )
    return [
        "\nProvenance (--explain) — reconstructed from source, not a log:",
        *_explain_metric(
            "Limit-buy close-only bound", status, result, inputs, method, LIMITBUY_CONTRACT
        ),
        *_explain_estimate(run),
    ]


def _explain_estimate(run: LimitBuyRun) -> list[str]:
    estimated = [row for row in run.estimates if row.status is Status.CALCULATED]
    status = Status.CALCULATED if estimated else Status.UNAVAILABLE
    result = f"estimates for {len(estimated)} of {len(run.estimates)} (Expiry, Below) rows"
    missing = [row for row in run.estimates if row.status is Status.UNAVAILABLE]
    if missing:
        reasons = "; ".join(sorted({row.gap.value for row in missing if row.gap}))
        result += f"; {len(missing)} UNAVAILABLE ({reasons})"
    excluded = {row.expiry: row.excluded for row in run.estimates}
    if run.bar_days and any(excluded.values()):
        result += "; windows excluded for an unusable bar: " + ", ".join(
            f"{expiry}d {count}" for expiry, count in sorted(excluded.items())
        )
    inputs = (
        f"{run.name} ({run.isin}) EUR closes and daily bars — bars stored on "
        f"{run.bar_days} of {len(run.dates)} days, {run.usable_days} usable"
    )
    method = (
        "limit = close_t × (1 − Below), placed after close t; on each day t+1…t+Expiry an "
        "open below the limit fills at the open, else a low strictly below it fills at the "
        "limit; unfilled, it buys at close t+Expiry; a start day is excluded if a window "
        "day's bar is missing or unusable (volume > 0, low ≤ min(open, close), high ≥ "
        "max(open, close)); bars convert at the day's EUR close / native close; ΔShares, "
        "P10 and Indep as for the bound"
    )
    return _explain_metric(
        "Limit-buy daily-bar estimate", status, result, inputs, method,
        LIMITBUY_ESTIMATE_CONTRACT,
    )


def _estimate_section(run: LimitBuyRun, show_status: bool) -> list[str]:
    if not run.bar_days:
        return [
            "",
            f"Daily-bar estimate: UNAVAILABLE — {Gap.NO_BARS.value}; run "
            "'e1f fetch --backfill' to store them (ADR-0056).",
        ]
    return _table(run.estimates, show_status, estimate=True)


def render(run: LimitBuyRun, *, show_status: bool, explain: bool) -> list[str]:
    lines = (
        _header(run)
        + _table(run.rows, show_status)
        + _estimate_section(run, show_status)
        + _footnotes(run)
    )
    if explain:
        lines += _explain_block(run)
    return lines


# ---------------------------------------------------------------------------
# Command.
# ---------------------------------------------------------------------------


def evaluate(
    isin: str,
    name: str,
    currency: str,
    distributing: bool,
    dates: list[str],
    closes: list[float],
    *,
    from_date: str | None,
    below_pcts: list[float],
    limit_prices: list[float],
    expiries: list[int],
    native_bars: dict[str, DailyBar] | None = None,
) -> LimitBuyRun:
    """Every requested row over a loaded EUR close series and its stored bars (no IO)."""
    start = 0 if from_date is None else bisect.bisect_left(dates, from_date)
    if start > len(dates) - 1:
        raise LimitBuyError(f"--from {from_date} is after the last stored close ({dates[-1]})")
    limits = [(price, below_from_limit(price, closes[-1])) for price in limit_prices]
    depths = [pct / 100.0 for pct in below_pcts] + [below for _, below in limits]
    if not depths:
        depths = [pct / 100.0 for pct in DEFAULT_BELOW_PCT]
    grid = [
        (expiry, below)
        for expiry in sorted(set(expiries or DEFAULT_EXPIRY_DAYS))
        for below in sorted(set(depths))
    ]
    native = native_bars or {}
    bars = align_bars(dates, closes, native)
    bar_days = sum(day in native for day in dates)
    rows = [summarize(closes, start, below, expiry) for expiry, below in grid]
    estimates = [
        summarize_bars(closes, bars, start, below, expiry, bars_stored=bar_days > 0)
        for expiry, below in grid
    ]
    return LimitBuyRun(
        isin, name, currency, distributing, dates, closes[-1], start, limits, rows,
        estimates, bar_days, sum(bar is not None for bar in bars),
    )


def _cmd_limitbuy(args: argparse.Namespace) -> int:
    config = ConfigManager(args.config)
    catalog_isins = {row[0] for row in price_catalog(args.db)}
    if not catalog_isins:
        raise LimitBuyError("no price series stored — run 'e1f fetch' first")
    if args.isin not in catalog_isins:
        raise LimitBuyError(
            f"no stored price series for {args.isin}. Available series:\n"
            f"{candidate_listing(args.db, config)}"
        )

    dates, closes, currency = eur_series(args.db, args.isin, args.to, args.currency_meta)
    if not dates:
        fx_hint = "" if currency == BASE_CURRENCY else f" — is the EUR/{currency} FX rate stored?"
        raise LimitBuyError(
            f"no close of {args.isin} on or before {args.to} can be valued in EUR{fx_hint}"
        )

    cfg = config.get(args.isin) or {}
    run = evaluate(
        args.isin,
        cfg.get("name") or args.isin,
        currency,
        (cfg.get("distribution") or "").lower().startswith("dist"),
        dates,
        closes,
        from_date=args.from_date,
        below_pcts=args.below or [],
        limit_prices=args.limit or [],
        expiries=args.expiry or [],
        native_bars=load_daily_bars(args.db, args.isin, args.to),
    )
    if run.distributing:
        print(
            f"⚠ {args.isin} is Distributing — an ex-dividend drop reads as a dip and the "
            "buy-now dividend is not counted, which flatters the limit order; prefer an "
            "Accumulating series.",
            file=sys.stderr,
        )
    show_status = args.show_status or args.explain
    print("\n".join(render(run, show_status=show_status, explain=args.explain)))
    return 0


def _below_pct(value: str) -> float:
    f = float(value)
    if not 0.0 <= f < 100.0:
        raise argparse.ArgumentTypeError(f"must be in [0, 100): {value}")
    return f


def _positive(value: str) -> float:
    f = float(value)
    if not (math.isfinite(f) and f > 0.0):
        raise argparse.ArgumentTypeError(f"must be > 0: {value}")
    return f


def _positive_int(value: str) -> int:
    i = int(value)
    if i <= 0:
        raise argparse.ArgumentTypeError(f"must be a positive integer: {value}")
    return i


def _valid_date(value: str) -> str:
    try:
        date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"must be YYYY-MM-DD: {value}") from exc
    return value


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="e1f limitbuy",
        description=(
            "Buy-limit order vs buying at the close: a close-only worst-case bound "
            "(ADR-0055) and a daily-bar estimate (ADR-0057)."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
For every start day in the stored history, a buy-limit Below under that day's close,
open for Expiry trading days, is compared with buying at that close; unfilled at
expiry, it buys at the expiry close.

Bound: it fills at the limit on the first later close strictly below it. Closes miss
intraday fills, so every figure is a worst case for the limit order (≥).

Estimate (needs daily bars, 'e1f fetch --backfill'): it fills at the open when a day
opens below the limit, else at the limit when the day's low is strictly below it.
Windows with a day lacking a usable bar are excluded and counted (Excl).

  e1f limitbuy --isin IE0003XJA0J9
  e1f limitbuy --isin IE0003XJA0J9 --limit 13.00 --limit 12.90 --expiry 5
  e1f limitbuy --isin IE00B4L5Y983 --below 1 --below 2 --expiry 21 --explain
""",
    )
    parser.add_argument("--isin", required=True, help="ETF to evaluate (required; no default)")
    parser.add_argument(
        "--below", type=_below_pct, action="append", metavar="PCT",
        help="Limit PCT percent under the start close, e.g. 0.5 (repeatable; "
             "default 0.5 1 2 3 5)",
    )
    parser.add_argument(
        "--limit", type=_positive, action="append", metavar="PRICE",
        help="Limit price in EUR, converted to a percent under the last close (repeatable)",
    )
    parser.add_argument(
        "--expiry", type=_positive_int, action="append", metavar="DAYS",
        help="Trading days the order stays open before buying at market (repeatable; "
             "default 5 21)",
    )
    parser.add_argument(
        "--from", dest="from_date", type=_valid_date, metavar="YYYY-MM-DD",
        help="Earliest start day (default: series start)",
    )
    parser.add_argument(
        "--to", type=_valid_date, default=_TODAY, metavar="YYYY-MM-DD",
        help="Last close used (default today)",
    )
    parser.add_argument("--db", "-d", default=DEFAULT_DB, help="Database file path")
    parser.add_argument("--config", "-c", default=DEFAULT_CONFIG, help="ETF universe config")
    parser.add_argument(
        "--currency-meta", default=DEFAULT_CURRENCY_META, help="Pinned currency metadata YAML",
    )
    parser.add_argument("--show-status", action="store_true", help="Add a Status column (ADR-0014)")
    parser.add_argument(
        "--explain", action="store_true", help="Add the provenance block (implies --show-status)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        return _cmd_limitbuy(args)
    except LimitBuyError as exc:
        print(f"✗ {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
