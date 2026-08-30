#!/usr/bin/env python
"""e1f benchmark — portfolio vs benchmark return comparison (ADR-0033, Phase B).

Regresses the book's time-weighted daily EUR returns on one or more benchmark
funds' returns and reports, per benchmark, the beta, R², annualized tracking
error, information ratio, and relative strength over their shared window — plus
that window's outperformance. Benchmarks are investable ETFs (net of their TER),
not raw indices; each comparison spans only the overlap of the two histories,
disclosed per row (ADR-0033). Metrics needing a risk-free rate (Sharpe, Treynor,
Jensen alpha) are deliberately out of scope until €STR is fetched.

Usage:
    e1f benchmark                                    # vs the seven broad benchmarks
    e1f benchmark --against IE00B5BMR087,IE00B4K48X80
    e1f benchmark --all                              # vs every ISIN in the price DB
    e1f benchmark --as-of 2025-12-31 --explain
"""

import argparse
import os
import sqlite3
import statistics
import sys
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, date, datetime

import numpy as np

from e1f.common import (
    DEFAULT_CONFIG,
    DEFAULT_CURRENCY_META,
    DEFAULT_DB,
    ConfigManager,
    MetricContract,
    Status,
    _explain_metric,
    eur_return_series,
    portfolio_isins,
    portfolio_return_series,
)

_TRADING_DAYS = 252
# The default is the *mathematical* floor: variance/correlation/sample-stdev need at
# least two shared returns. There is no quarter-year minimum by default — a caller who
# wants a stricter bar raises it with --min-overlap (a short window's n is always shown).
_MIN_OVERLAP = 2
_VARIANCE_FLOOR = 1e-12

# The default benchmark set: seven broad indices, each an accumulating (≈ total
# return) UCITS fund already in the universe (all USD/EUR, EUR/USD FX present),
# shown by its complete fund name (the raw config names are cryptic — "SS SPD MS
# AL CO WO UC" etc.). Order is preserved in the output.
_DEFAULT_BENCHMARK_LABELS = {
    "IE00B4L5Y983": "iShares Core MSCI World (Acc)",        # SWDA / IWDA
    "IE00B4K48X80": "iShares Core MSCI Europe (Acc)",       # IMAE / SMEA
    "IE0003XJA0J9": "Amundi Prime All Country World (Acc)",  # WEBN
    "IE00B5BMR087": "iShares Core S&P 500 (Acc)",           # CSPX / SXR8
    "IE00B44Z5B48": "SPDR MSCI ACWI (Acc)",                 # SPYY / ACWI
    "IE00B3YLTY66": "SPDR MSCI ACWI IMI (Acc)",             # SPYI / IMID
    "IE00BK5BQT80": "Vanguard FTSE All-World (Acc)",        # VWCE / VWRA
}
_DEFAULT_BENCHMARK = ",".join(_DEFAULT_BENCHMARK_LABELS)
_NAME_WIDTH = 38  # fits the longest complete fund name plus the held '*' marker
SORT_FIELDS = (
    "isin", "name", "n", "beta", "r2", "te", "ir", "vol", "maxdd", "twr",
    "relstr", "out",
)


BENCHMARK_CONTRACT = MetricContract(
    method_version="return_regression_v1",
    requires=(
        "at least two shared trading days (more shared history tightens the estimate)",
        "a benchmark price + FX series (an accumulating ETF ≈ total return)",
    ),
    does_not_require=("a risk-free rate", "look-through holdings"),
    supports=(
        "beta", "R²", "tracking error", "information ratio",
        "TWR", "Vol", "MaxDD", "relative strength",
    ),
    limitations=(
        "returns are time-weighted daily on the gap-bridged EUR series; every "
        "×√252 annualization treats them as uniform daily",
        "the benchmark is an investable ETF net of its TER, not the raw index",
        "each comparison spans only the overlap of the two histories",
        "risk-adjusted metrics needing €STR (Sharpe, Treynor, alpha) are out of scope",
    ),
)


# ---------------------------------------------------------------------------
# Pure stats over two aligned return vectors (no DB) — the tested core.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BenchmarkStats:
    """One benchmark's regression stats vs the book over their shared window."""

    isin: str
    name: str
    status: Status
    reason: str | None
    n: int
    start: str | None
    end: str | None
    beta: float | None
    r_squared: float | None
    tracking_error: float | None  # annualized
    information_ratio: float | None  # annualized
    relative_strength: float | None  # (1+port_twr)/(1+bench_twr) over the window
    port_twr: float | None  # overlap book TWR — feeds Out% / RelStr, not a column
    bench_twr: float | None  # benchmark cumulative TWR over the same window
    bench_vol: float | None  # benchmark annualized vol over the shared window
    bench_maxdd: float | None  # benchmark wealth-index MaxDD over the same window

    @property
    def outperformance(self) -> float | None:
        """ETF minus book cumulative return over the shared window."""
        if self.port_twr is None or self.bench_twr is None:
            return None
        return self.bench_twr - self.port_twr


def _align(
    port: list[tuple[str, float]], bench: list[tuple[str, float]]
) -> tuple[list[float], list[float], list[str]]:
    """Portfolio/benchmark return vectors over their shared dates, date-sorted.

    ``port`` is already date-sorted (from ``portfolio_return_series``), so the
    intersection preserves order. A return dated ``t`` is kept only when both
    series define one on ``t`` — the same date-intersection alignment ``correlation``
    uses; boundary returns may span unequal intervals (the bridged-series caveat).
    """
    bench_by_day = dict(bench)
    shared = [(day, r, bench_by_day[day]) for day, r in port if day in bench_by_day]
    return (
        [p for _d, p, _b in shared],
        [b for _d, _p, b in shared],
        [day for day, _p, _b in shared],
    )


def _cumulative(returns: list[float]) -> float:
    """Chain-linked cumulative return of a sub-period return series."""
    wealth = 1.0
    for r in returns:
        wealth *= 1.0 + r
    return wealth - 1.0


def _ann_vol(returns: list[float]) -> float | None:
    """Sample stdev ×√252; None unless there are at least two returns."""
    if len(returns) < 2:
        return None
    return float(statistics.stdev(returns) * (_TRADING_DAYS ** 0.5))


def _max_drawdown(returns: list[float]) -> float | None:
    """Wealth-index peak-to-trough; 0.0 if never below the running peak."""
    if not returns:
        return None
    wealth = 1.0
    peak = 1.0
    max_dd = 0.0
    for period_return in returns:
        wealth *= 1.0 + period_return
        peak = max(peak, wealth)
        max_dd = min(max_dd, wealth / peak - 1.0)
    return max_dd


@dataclass(frozen=True)
class BookSummary:
    """The book's own return/risk over its full history (not a row overlap)."""

    n: int
    start: str
    end: str
    twr: float
    volatility: float | None
    max_drawdown: float | None


def book_summary(port: list[tuple[str, float]]) -> BookSummary:
    """TWR / Vol / MaxDD of the portfolio return series (ADR-0045)."""
    returns = [r for _day, r in port]
    return BookSummary(
        n=len(returns),
        start=port[0][0],
        end=port[-1][0],
        twr=_cumulative(returns),
        volatility=_ann_vol(returns),
        max_drawdown=_max_drawdown(returns),
    )


def _unavailable(isin: str, name: str, reason: str, n: int = 0) -> BenchmarkStats:
    return BenchmarkStats(
        isin=isin, name=name, status=Status.UNAVAILABLE, reason=reason, n=n,
        start=None, end=None, beta=None, r_squared=None, tracking_error=None,
        information_ratio=None, relative_strength=None, port_twr=None, bench_twr=None,
        bench_vol=None, bench_maxdd=None,
    )


def benchmark_stats(
    port_returns: list[tuple[str, float]],
    bench_returns: list[tuple[str, float]],
    isin: str,
    name: str,
    *,
    min_overlap: int = _MIN_OVERLAP,
) -> BenchmarkStats:
    """Beta / R² / tracking error / information ratio / relative strength vs a benchmark.

    UNAVAILABLE (never a misleading point estimate) when the shared sample is below
    ``min_overlap``. Beta needs the benchmark's returns to vary; R² needs both legs
    to vary; the information ratio needs the active-return series to vary — each is
    ``None`` (not 0, not NaN) when its denominator is degenerate, the rest still
    reported.
    """
    a, b, dates = _align(port_returns, bench_returns)
    n = len(a)
    if n < min_overlap:
        return _unavailable(
            isin, name, f"insufficient overlap (n={n} < {min_overlap})", n=n
        )

    ap = np.asarray(a, dtype=float)
    bp = np.asarray(b, dtype=float)
    var_a = float(np.var(ap))
    var_b = float(np.var(bp))

    beta = None if var_b <= _VARIANCE_FLOOR else float(np.cov(ap, bp, ddof=0)[0, 1]) / var_b
    if var_a <= _VARIANCE_FLOOR or var_b <= _VARIANCE_FLOOR:
        r_squared: float | None = None
    else:
        corr = float(np.corrcoef(ap, bp)[0, 1])
        r_squared = corr * corr

    active = ap - bp
    active_sd = float(np.std(active, ddof=1))
    tracking_error = active_sd * (_TRADING_DAYS ** 0.5)
    information_ratio = (
        float(np.mean(active)) / active_sd * (_TRADING_DAYS ** 0.5)
        if active_sd > 0.0
        else None
    )

    port_twr = _cumulative(a)
    bench_twr = _cumulative(b)
    relative_strength = (
        (1.0 + port_twr) / (1.0 + bench_twr) if bench_twr != -1.0 else None
    )

    return BenchmarkStats(
        isin=isin, name=name, status=Status.CALCULATED, reason=None,
        n=n, start=dates[0], end=dates[-1],
        beta=beta, r_squared=r_squared, tracking_error=tracking_error,
        information_ratio=information_ratio, relative_strength=relative_strength,
        port_twr=port_twr, bench_twr=bench_twr,
        bench_vol=_ann_vol(b), bench_maxdd=_max_drawdown(b),
    )


# ---------------------------------------------------------------------------
# Rendering.
# ---------------------------------------------------------------------------


def _priced_isins(db_path: str) -> list[str]:
    """Distinct ISINs in ``prices``, sorted; empty when the table is missing."""
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


def _bench_name(config_path: str, isin: str) -> str:
    """Complete fund name for a default benchmark; else the config name; else the ISIN."""
    if isin in _DEFAULT_BENCHMARK_LABELS:
        return _DEFAULT_BENCHMARK_LABELS[isin]
    data = ConfigManager(config_path).get(isin)
    return str((data or {}).get("name", ""))[:_NAME_WIDTH - 1] or isin


def _fmt_ratio(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.2f}"


def _fmt_pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100.0:.1f}%"


_HEADER = (
    f"\n{'Benchmark':<{_NAME_WIDTH}} {'n':>4} {'Beta':>6} {'R2':>6} {'TE':>7} {'IR':>6} "
    f"{'Vol':>7} {'MaxDD':>7} {'TWR':>7} {'RelStr':>7} {'Out%':>7}"
)
_RULE_WIDTH = len(_HEADER.lstrip("\n"))


def _format_row(stats: BenchmarkStats) -> str:
    return (
        f"{stats.name:<{_NAME_WIDTH}} {stats.n:>4} {_fmt_ratio(stats.beta):>6} "
        f"{_fmt_ratio(stats.r_squared):>6} {_fmt_pct(stats.tracking_error):>7} "
        f"{_fmt_ratio(stats.information_ratio):>6} {_fmt_pct(stats.bench_vol):>7} "
        f"{_fmt_pct(stats.bench_maxdd):>7} {_fmt_pct(stats.bench_twr):>7} "
        f"{_fmt_ratio(stats.relative_strength):>7} {_fmt_pct(stats.outperformance):>7}"
    )


def _sort_key(stats: BenchmarkStats, sort_by: str) -> str | float:
    if sort_by == "isin":
        return stats.isin
    if sort_by == "name":
        return stats.name.lower()
    value = {
        "n": float(stats.n),
        "beta": stats.beta,
        "r2": stats.r_squared,
        "te": stats.tracking_error,
        "ir": stats.information_ratio,
        "vol": stats.bench_vol,
        "maxdd": stats.bench_maxdd,
        "twr": stats.bench_twr,
        "relstr": stats.relative_strength,
        "out": stats.outperformance,
    }[sort_by]
    return float("-inf") if value is None else value


def sort_stats(
    rows: list[BenchmarkStats], *, sort_by: str, reverse: bool = False
) -> list[BenchmarkStats]:
    return sorted(rows, key=lambda s: _sort_key(s, sort_by), reverse=reverse)


def _format_book_summary(summary: BookSummary) -> str:
    return (
        f"Book {summary.start} → {summary.end}  n={summary.n}  "
        f"TWR={_fmt_pct(summary.twr)}  Vol={_fmt_pct(summary.volatility)}  "
        f"MaxDD={_fmt_pct(summary.max_drawdown)}"
    )


def _render_explain(rows: list[BenchmarkStats]) -> list[str]:
    lines = ["\nProvenance (--explain) — reconstructed from source, not a log:"]
    for stats in rows:
        if stats.status is Status.CALCULATED:
            lines.append(
                f"  {stats.isin}  {stats.name}: {stats.start} → {stats.end}, n={stats.n} ; "
                f"β={_fmt_ratio(stats.beta)} R²={_fmt_ratio(stats.r_squared)} "
                f"TE={_fmt_pct(stats.tracking_error)} IR={_fmt_ratio(stats.information_ratio)} "
                f"Vol={_fmt_pct(stats.bench_vol)} MaxDD={_fmt_pct(stats.bench_maxdd)} "
                f"TWR={_fmt_pct(stats.bench_twr)} overlap-book-TWR={_fmt_pct(stats.port_twr)} "
                f"RelStr={_fmt_ratio(stats.relative_strength)} Out={_fmt_pct(stats.outperformance)}"
            )
        else:
            lines.append(f"  {stats.isin}  {stats.name}: UNAVAILABLE — {stats.reason}")
    status = (
        Status.CALCULATED
        if any(s.status is Status.CALCULATED for s in rows)
        else Status.UNAVAILABLE
    )
    lines.extend(_explain_metric(
        "Benchmark comparison",
        status,
        "per-benchmark figures + windows listed above",
        "portfolio TWR daily returns + benchmark EUR daily returns, aligned on shared dates",
        "β = cov(rp,rb)/var(rb) ; R² = corr(rp,rb)² ; TE = stdev(rp−rb)×√252 ; "
        "IR = mean(rp−rb)/stdev(rp−rb)×√252 ; RelStr = (1+rp)/(1+rb) ; "
        "Vol = stdev(rb)×√252 ; MaxDD = wealth-index peak-to-trough of rb",
        BENCHMARK_CONTRACT,
    ))
    return lines


def _cmd_benchmark(
    db_path: str,
    config_path: str,
    *,
    as_of: str,
    benchmarks: list[str],
    min_overlap: int = _MIN_OVERLAP,
    explain: bool = False,
    sort_by: str | None = None,
    reverse: bool = False,
    currency_meta_path: str = DEFAULT_CURRENCY_META,
    all_priced: bool = False,
) -> int:
    port = portfolio_return_series(db_path, currency_meta_path, as_of)
    if not port:
        print(f"No priceable portfolio return history as of {as_of}")
        print("Ingest trades and fetch prices first: e1f transactions … && e1f fetch")
        return 0

    held = portfolio_isins(db_path)
    rows: list[BenchmarkStats] = []
    for isin in benchmarks:
        name = _bench_name(config_path, isin) + ("*" if isin in held else "")
        bench = eur_return_series(db_path, isin, as_of, currency_meta_path)
        if not bench:
            rows.append(_unavailable(isin, name, f"no return series (fetch {isin}?)"))
            continue
        rows.append(benchmark_stats(port, bench, isin, name, min_overlap=min_overlap))

    if sort_by is not None:
        rows = sort_stats(rows, sort_by=sort_by, reverse=reverse)
    elif reverse:
        rows = list(reversed(rows))

    subject = "all priced ETFs" if all_priced else "benchmarks"
    print(f"\nPortfolio vs {subject} as of {as_of} (EUR, time-weighted)")
    print()
    print(_format_book_summary(book_summary(port)))
    print(_HEADER)
    print("-" * _RULE_WIDTH)
    for stats in rows:
        print(_format_row(stats))

    # Only surface benchmarks that need attention — an UNAVAILABLE one carries a
    # reason; a fully-calculated table prints no legend at all.
    problems = [stats for stats in rows if stats.status is not Status.CALCULATED]
    if problems:
        print()
        for stats in sorted(problems, key=lambda s: s.isin):
            print(f"  {stats.isin}  {stats.name} — UNAVAILABLE: {stats.reason}")

    if any(stats.isin in held for stats in rows):
        print("\n* also a current portfolio holding.")

    print(
        "\nBeta/R2/TE/IR vs the benchmark's daily EUR returns over each pair's shared "
        "window (n); Vol / MaxDD / TWR are the benchmark over that window; "
        "Out% = table TWR − overlap book TWR, RelStr = growth ratio. The Book line is the "
        "portfolio's own full history (not a row overlap). Benchmarks are investable "
        "ETFs net of TER, not raw indices. Metrics "
        "needing €STR (Sharpe, Treynor, alpha) are out of scope (ADR-0033). --explain "
        "lists each benchmark's window."
    )
    if all_priced:
        print("--all: every ISIN in the prices table, not the default seven.")
    if explain:
        for line in _render_explain(rows):
            print(line)
    return 0


# ---------------------------------------------------------------------------
# CLI.
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="e1f benchmark",
        description="Compare the portfolio's time-weighted returns against one or more "
        "benchmark ETFs (beta, R², tracking error, information ratio, relative strength)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Metrics (EUR, time-weighted daily returns aligned on shared trading days):
  Beta    sensitivity of the book to the benchmark: cov(rp,rb)/var(rb)
  R²      share of the book's variance the benchmark explains: corr(rp,rb)²
  TE      tracking error — stdev(rp − rb), annualized ×√252
  IR      information ratio — mean(rp − rb)/stdev(rp − rb), annualized
  Vol     this ETF's annualized volatility over the shared window (stdev ×√252)
  MaxDD   this ETF's wealth-index peak-to-trough over the shared window
  TWR     this ETF's cumulative time-weighted return over the shared window
  RelStr  relative strength — (1+book TWR)/(1+TWR) over the window
  Out%    TWR − book TWR over the window (overlap book TWR, not the Book line)

A Book line above the table is the portfolio's own full-history TWR / Vol / MaxDD
(not a row overlap). Book-side overlap TWR / Vol / MaxDD are not table columns.
Benchmarks are investable ETFs (net of their TER, ≈ total
return for accumulating share classes), not raw indices. There is no
minimum-history floor by default: a benchmark computes over whatever days it
shares with the book (the sample size n is always shown, so a thin window is
visible) — raise --min-overlap to demand more.
A benchmark not yet fetched is reported UNAVAILABLE with the reason, never estimated.
--all scores the book against every ISIN in the prices table instead of the default
seven (mutually exclusive with --against). Metrics needing a risk-free rate (Sharpe,
Treynor, Jensen alpha) are out of scope until €STR is fetched (ADR-0033).

Examples:
  e1f benchmark
  e1f benchmark --against IE00B5BMR087,IE00B4K48X80
  e1f benchmark --all
  e1f benchmark --as-of 2025-12-31
  e1f benchmark --explain
  e1f benchmark --sort out --reverse
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
        "--against",
        default=None,
        metavar="ISIN[,ISIN...]",
        help="Comma-separated benchmark ISINs (default: the seven broad benchmarks — "
        "MSCI World, MSCI Europe, WEBN, S&P 500, MSCI ACWI, MSCI ACWI IMI, FTSE All-World). "
        "Mutually exclusive with --all.",
    )
    parser.add_argument(
        "--all",
        dest="all_priced",
        action="store_true",
        help="Compare against every ISIN in the prices table instead of the default "
        "seven (ADR-0044). Mutually exclusive with --against.",
    )
    parser.add_argument(
        "--as-of",
        default=datetime.now(UTC).date().isoformat(),
        metavar="YYYY-MM-DD",
        help="Compare as of this date (default: today)",
    )
    parser.add_argument(
        "--min-overlap",
        type=int,
        default=_MIN_OVERLAP,
        help=f"Minimum shared trading days to estimate (default: {_MIN_OVERLAP}, >= 2)",
    )
    parser.add_argument(
        "--explain",
        action="store_true",
        help="Add a provenance block (method/contract/limited-by; ADR-0014)",
    )
    parser.add_argument(
        "--sort",
        choices=SORT_FIELDS,
        default=None,
        help="Sort rows by column (default: listed order)",
    )
    parser.add_argument(
        "--reverse", "-r", action="store_true", help="Descending sort order"
    )
    return parser


def _parse_benchmarks(raw: str) -> list[str]:
    isins = [token.strip() for token in raw.split(",") if token.strip()]
    if not isins:
        raise ValueError("--against needs at least one ISIN")
    return isins


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        date.fromisoformat(args.as_of)
    except ValueError:
        print(f"✗ Error: --as-of must be YYYY-MM-DD: {args.as_of}")
        return 1
    try:
        if args.min_overlap < 2:
            raise ValueError("--min-overlap must be >= 2")
        if args.all_priced and args.against is not None:
            raise ValueError("--all and --against are mutually exclusive")
        if args.all_priced:
            benchmarks = _priced_isins(args.db)
            if not benchmarks:
                print("No price series in database")
                print("Fetch prices: e1f fetch")
                return 0
        else:
            benchmarks = _parse_benchmarks(args.against or _DEFAULT_BENCHMARK)
        return _cmd_benchmark(
            args.db,
            args.config,
            as_of=args.as_of,
            benchmarks=benchmarks,
            min_overlap=args.min_overlap,
            explain=args.explain,
            sort_by=args.sort,
            reverse=args.reverse,
            currency_meta_path=args.currency_meta,
            all_priced=args.all_priced,
        )
    except Exception as e:  # noqa: BLE001 — CLI top-level; all errors become exit code 1
        print(f"✗ Error: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
