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
from dataclasses import dataclass
from datetime import UTC, date, datetime

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
from e1f.experimental.common import candidate_listing, crash_split, eur_series, price_catalog

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


class LimitBuyError(Exception):
    """A usage/data problem that stops a run with a message (never a stack trace)."""


@dataclass(frozen=True)
class LimitBuyRow:
    """One ``(expiry, below)`` cell; every statistic is a lower bound (ADR-0055 §3-4)."""

    below: float  # fraction under the start close (0.005 = 0.5%)
    expiry: int  # trading days the order stays open
    windows: int  # start days with a complete expiry window
    fill_rate: float | None  # the statistics are None when windows == 0
    win_rate: float | None  # share of start days with ΔShares ≥ 0
    mean: float | None  # ΔShares mean / median / nearest-rank P10 / minimum
    median: float | None
    p10: float | None
    worst: float | None

    @property
    def status(self) -> Status:
        return Status.BOUNDED if self.windows else Status.UNAVAILABLE

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


def summarize(closes: list[float], start: int, below: float, expiry: int) -> LimitBuyRow:
    """Summarise every start day ``t ≥ start`` whose expiry window fits the series."""
    outcomes = [
        limit_outcome(closes, t, below, expiry) for t in range(start, len(closes) - expiry)
    ]
    if not outcomes:
        return LimitBuyRow(below, expiry, 0, None, None, None, None, None, None)
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
    )


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


def _header(run: LimitBuyRun) -> list[str]:
    first, last, start = run.dates[0], run.dates[-1], run.dates[run.start]
    tested, absent = crash_split(start, last)
    lines = [
        f"\nLimit-buy bound — {run.name} ({run.isin}) · {BASE_CURRENCY}",
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
        "Order:    buy-limit Below under the start close; fills at the limit on the first",
        "          later close strictly below it, else buys at the expiry close",
    ]
    if run.limits:
        mapped = ", ".join(f"€{price:,.2f} → {_fmt_below(below)}" for price, below in run.limits)
        lines.append(f"Limits:   vs last close €{run.last_close:,.2f} ({last}): {mapped}")
    return lines


def _table(rows: list[LimitBuyRow], show_status: bool) -> list[str]:
    head = (
        f"{'Expiry':>6}  {'Below':>6}  {'Windows':>7}  {'Indep':>6}  {'Fill%≥':>6}"
        f"  {'Win%≥':>6}  {'Mean≥':>7}  {'Median≥':>7}  {'P10≥':>7}  {'Worst≥':>7}"
    )
    if show_status:
        head += f"  {'Status':<11}"
    lines = ["", head, "-" * len(head)]
    for row in rows:
        indep = f"{row.independent}{'*' if row.illustrative else ''}"
        line = (
            f"{row.expiry:>5}d  {_fmt_below(row.below):>6}  {row.windows:>7}  {indep:>6}"
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
    if any(row.illustrative for row in run.rows):
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
    ]


def render(run: LimitBuyRun, *, show_status: bool, explain: bool) -> list[str]:
    lines = _header(run) + _table(run.rows, show_status) + _footnotes(run)
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
) -> LimitBuyRun:
    """Every requested row over a loaded EUR close series (no IO)."""
    start = 0 if from_date is None else bisect.bisect_left(dates, from_date)
    if start > len(dates) - 1:
        raise LimitBuyError(f"--from {from_date} is after the last stored close ({dates[-1]})")
    limits = [(price, below_from_limit(price, closes[-1])) for price in limit_prices]
    depths = [pct / 100.0 for pct in below_pcts] + [below for _, below in limits]
    if not depths:
        depths = [pct / 100.0 for pct in DEFAULT_BELOW_PCT]
    rows = [
        summarize(closes, start, below, expiry)
        for expiry in sorted(set(expiries or DEFAULT_EXPIRY_DAYS))
        for below in sorted(set(depths))
    ]
    return LimitBuyRun(
        isin, name, currency, distributing, dates, closes[-1], start, limits, rows
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
            "Close-only worst-case bound on a buy-limit order vs buying at the close "
            "(ADR-0055)."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
For every start day in the stored history, a buy-limit Below under that day's close,
open for Expiry trading days, is compared with buying at that close. It fills at the
limit on the first later close strictly below it; if none is, it buys at the expiry
close. It reads only closes, so intraday fills are missed and every figure is a
worst case for the limit order (≥): a real order fills at least as often and ends
with at least as many shares on every start day.

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
