"""e1f funds — configured-universe candidate table (ADR-0042).

One row per configured ISIN: static metadata plus TWR / Vol / MaxDD over a
user-chosen window (``--from`` / ``--as-of``). Held funds are marked ``*``.
A fund younger than ``--from`` stays in the table; ``From`` is the first EUR
close actually used, and pre-listing days are not ``Gap``.
"""

from __future__ import annotations

import argparse
import itertools
import statistics
import sys
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

from e1f.common import (
    DEFAULT_CONFIG,
    DEFAULT_CURRENCY_META,
    DEFAULT_DB,
    ConfigManager,
    CurrencyMetadata,
    MetricContract,
    Status,
    _explain_metric,
    eur_close_series,
    interior_gaps,
    pinned_quote_currency,
    portfolio_isins,
    venues_from_currency_meta,
)

_TRADING_DAYS = 252
_NAME_WIDTH = 28
SORT_FIELDS = (
    "isin", "name", "class", "dist", "ccy", "ter", "from", "n", "gap", "twr", "vol", "maxdd",
)

FUNDS_CONTRACT = MetricContract(
    method_version="fund_window_v1",
    requires=(
        "a pinned quote currency and EUR closes in the window",
        "same-venue peers to vote interior gaps (thin venues report Gap = 0)",
    ),
    does_not_require=("a holding", "a risk-free rate", "look-through holdings"),
    supports=("TWR", "Vol", "MaxDD", "n", "Gap", "From"),
    limitations=(
        "returns are gap-bridged EUR closes, not calendar-daily; Vol ×√252 "
        "treats each return as one day",
        "Gap counts venue-consensus holes the fund spans, never days before listing",
        "each fund's From may be later than --from when the series is shorter",
    ),
)


# ---------------------------------------------------------------------------
# Pure window / risk math (no DB) — the tested core.
# ---------------------------------------------------------------------------


def clip_closes(
    closes: list[tuple[str, float]], *, start: str | None, as_of: str
) -> list[tuple[str, float]]:
    """EUR closes with ``start ≤ date ≤ as_of`` (``start`` omitted → no floor)."""
    return [
        (day, close)
        for day, close in closes
        if day <= as_of and (start is None or day >= start)
    ]


def returns_from_closes(closes: list[tuple[str, float]]) -> list[float]:
    """Gap-bridged returns between consecutive remaining closes."""
    return [cur / prev - 1.0 for (_d0, prev), (_d1, cur) in itertools.pairwise(closes)]


def risk_from_returns(
    returns: list[float],
) -> tuple[float | None, float | None, float | None]:
    """``(TWR, Vol, MaxDD)`` from a sub-period return list.

    TWR is the chain-linked product. Vol is sample stdev ×√252 (needs ≥2
    returns). MaxDD is the deepest peak-to-trough of the wealth index.
    """
    if not returns:
        return None, None, None
    wealth = 1.0
    peak = 1.0
    max_drawdown = 0.0
    for period_return in returns:
        wealth *= 1.0 + period_return
        peak = max(peak, wealth)
        max_drawdown = min(max_drawdown, wealth / peak - 1.0)
    volatility = (
        statistics.stdev(returns) * (_TRADING_DAYS ** 0.5) if len(returns) >= 2 else None
    )
    return wealth - 1.0, volatility, max_drawdown


def _distribution_label(distribution: str) -> str:
    if distribution == "Accumulating":
        return "ACC"
    if distribution == "Distributing":
        return "Dist"
    return distribution[:4] if distribution else ""


def _dist_matches(distribution: str, wanted: str) -> bool:
    token = wanted.strip().lower()
    if token in {"acc", "accumulating"}:
        return distribution == "Accumulating"
    if token in {"dist", "distributing"}:
        return distribution == "Distributing"
    return distribution.lower() == token


@dataclass(frozen=True)
class FundRow:
    """One configured fund over the analysis window."""

    isin: str
    name: str
    held: bool
    asset_class: str
    distribution: str
    currency: str
    ter: float | None
    start: str | None
    n: int
    gap: int
    twr: float | None
    volatility: float | None
    max_drawdown: float | None
    status: Status
    reason: str | None
    gap_dates: tuple[str, ...]


def build_row(
    isin: str,
    data: dict[str, Any],
    *,
    held: bool,
    closes: list[tuple[str, float]],
    gap_dates: list[str],
    start: str | None,
    as_of: str,
    currency: str,
) -> FundRow:
    """Assemble one row from metadata + windowed EUR closes + interior gaps."""
    name = str(data.get("name") or isin)
    ter_raw = data.get("ter")
    ter = float(ter_raw) if isinstance(ter_raw, (int, float)) else None
    window = clip_closes(closes, start=start, as_of=as_of)
    from_day = window[0][0] if window else None
    returns = returns_from_closes(window)
    twr, vol, maxdd = risk_from_returns(returns)
    if not window:
        status = Status.UNAVAILABLE
        reason = "no EUR close in the window (fetch this ISIN?)"
    elif not returns:
        status = Status.UNAVAILABLE
        reason = "only one EUR close in the window"
    else:
        status = Status.CALCULATED
        reason = None
    return FundRow(
        isin=isin,
        name=name,
        held=held,
        asset_class=str(data.get("asset_class") or ""),
        distribution=str(data.get("distribution") or ""),
        currency=currency,
        ter=ter,
        start=from_day,
        n=len(returns),
        gap=len(gap_dates),
        twr=twr,
        volatility=vol,
        max_drawdown=maxdd,
        status=status,
        reason=reason,
        gap_dates=tuple(gap_dates),
    )


# ---------------------------------------------------------------------------
# Rendering.
# ---------------------------------------------------------------------------


def _display_name(row: FundRow) -> str:
    marker = "*" if row.held else ""
    return (row.name + marker)[:_NAME_WIDTH]


def _fmt_pct(value: float | None) -> str:
    return "—" if value is None else f"{value * 100.0:.1f}%"


def _fmt_ter(value: float | None) -> str:
    return "—" if value is None else f"{value:.2f}%"


_HEADER = (
    f"\n{'ISIN':<12} {'Name':<{_NAME_WIDTH}} {'Class':<8} {'Dist':<4} {'Ccy':<3} "
    f"{'TER':>6} {'From':<10} {'n':>5} {'Gap':>3} {'TWR':>7} {'Vol':>7} {'MaxDD':>7}"
)
_RULE_WIDTH = len(_HEADER.lstrip("\n"))


def _format_row(row: FundRow) -> str:
    return (
        f"{row.isin:<12} {_display_name(row):<{_NAME_WIDTH}} "
        f"{row.asset_class[:8]:<8} {_distribution_label(row.distribution):<4} "
        f"{(row.currency or '—'):<3} {_fmt_ter(row.ter):>6} "
        f"{(row.start or '—'):<10} {row.n:>5} {row.gap:>3} "
        f"{_fmt_pct(row.twr):>7} {_fmt_pct(row.volatility):>7} "
        f"{_fmt_pct(row.max_drawdown):>7}"
    )


def _sort_key(row: FundRow, sort_by: str) -> str | float:
    if sort_by == "isin":
        return row.isin
    if sort_by == "name":
        return row.name.lower()
    if sort_by == "class":
        return row.asset_class.lower()
    if sort_by == "dist":
        return row.distribution.lower()
    if sort_by == "ccy":
        return row.currency.lower()
    if sort_by == "from":
        return row.start or ""
    value = {
        "ter": row.ter,
        "n": float(row.n),
        "gap": float(row.gap),
        "twr": row.twr,
        "vol": row.volatility,
        "maxdd": row.max_drawdown,
    }[sort_by]
    return float("-inf") if value is None else value


def sort_rows(
    rows: list[FundRow], *, sort_by: str, reverse: bool = False
) -> list[FundRow]:
    return sorted(rows, key=lambda row: _sort_key(row, sort_by), reverse=reverse)


def _matches_filters(
    row: FundRow,
    *,
    unheld: bool,
    asset_class: str | None,
    distribution: str | None,
) -> bool:
    if unheld and row.held:
        return False
    if asset_class and row.asset_class.lower() != asset_class.strip().lower():
        return False
    if distribution is None:
        return True
    return _dist_matches(row.distribution, distribution)


def _window_banner(start: str | None, as_of: str) -> str:
    if start is None:
        return (
            f"Configured universe as of {as_of} (EUR, each fund from its first close)"
        )
    return f"Configured universe {start} → {as_of} (EUR; a later From means a shorter series)"


def _render_explain(rows: list[FundRow], *, start: str | None, as_of: str) -> list[str]:
    lines = ["\nProvenance (--explain) — reconstructed from source, not a log:"]
    for row in rows:
        window = f"{row.start or '—'} → {as_of}, n={row.n}, Gap={row.gap}"
        if row.gap_dates:
            sample = ", ".join(row.gap_dates[:5])
            extra = " …" if len(row.gap_dates) > 5 else ""
            window += f" ({sample}{extra})"
        if row.status is Status.CALCULATED:
            lines.append(
                f"  {row.isin}  {row.name}: {window} ; "
                f"TWR={_fmt_pct(row.twr)} Vol={_fmt_pct(row.volatility)} "
                f"MaxDD={_fmt_pct(row.max_drawdown)}"
            )
        else:
            lines.append(f"  {row.isin}  {row.name}: UNAVAILABLE — {row.reason} ({window})")
    status = (
        Status.CALCULATED
        if any(row.status is Status.CALCULATED for row in rows)
        else Status.UNAVAILABLE
    )
    requested = f"{start or '(first close)'} → {as_of}"
    lines.extend(_explain_metric(
        "Fund window",
        status,
        "per-fund TWR/Vol/MaxDD + From/n/Gap listed above",
        f"EUR closes in {requested}; interior gaps from same-venue consensus",
        "clip closes to the window, pairwise returns (bridged), "
        "TWR = Π(1+r)−1, Vol = stdev(r)×√252, MaxDD on the wealth index; "
        "Gap = consensus days the fund spans but lacks",
        FUNDS_CONTRACT,
    ))
    return lines


def _cmd_funds(
    db_path: str,
    config_path: str,
    *,
    as_of: str,
    start: str | None,
    unheld: bool,
    asset_class: str | None,
    distribution: str | None,
    explain: bool,
    sort_by: str,
    reverse: bool,
    currency_meta_path: str,
) -> int:
    config = ConfigManager(config_path)
    etfs = config.list()
    if not etfs:
        print("No ETFs in configuration")
        print("Add one: e1f config add IE00BM67HK77")
        return 0

    held = portfolio_isins(db_path)
    currency_meta = CurrencyMetadata.load(currency_meta_path)
    venues = venues_from_currency_meta(currency_meta)
    gaps = interior_gaps(db_path, venues, start=start, as_of=as_of)

    rows: list[FundRow] = []
    for isin, data in etfs:
        currency = pinned_quote_currency(isin, currency_meta_path) or str(
            data.get("fund_currency") or ""
        )
        closes = eur_close_series(db_path, isin, as_of, currency_meta_path)
        rows.append(
            build_row(
                isin,
                data,
                held=isin in held,
                closes=closes,
                gap_dates=gaps.get(isin, []),
                start=start,
                as_of=as_of,
                currency=currency,
            )
        )

    rows = [row for row in rows if _matches_filters(
        row, unheld=unheld, asset_class=asset_class, distribution=distribution,
    )]
    if not rows:
        print("No funds match the filters")
        return 0

    rows = sort_rows(rows, sort_by=sort_by, reverse=reverse)

    print(f"\n{_window_banner(start, as_of)}")
    print(_HEADER)
    print("-" * _RULE_WIDTH)
    for row in rows:
        print(_format_row(row))
    print(f"\nTotal: {len(rows)} ETF{'s' if len(rows) != 1 else ''}")

    if any(row.held for row in rows):
        print("* also a current portfolio holding.")

    problems = [row for row in rows if row.status is not Status.CALCULATED]
    if problems:
        print()
        for row in problems:
            print(f"  {row.isin}  {row.name} — UNAVAILABLE: {row.reason}")

    print(
        "\nn is gap-bridged EUR returns (not calendar days). Gap is venue-consensus "
        "interior holes this fund spans — days before listing are not Gap. "
        "TWR/Vol/MaxDD use the closes from From through --as-of. "
        "Repair holes with 'e1f fetch <isin> --force'."
    )
    if explain:
        for line in _render_explain(rows, start=start, as_of=as_of):
            print(line)
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="e1f funds",
        description="List configured ETFs with cost, windowed return/risk, and data coverage",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
One row per configured ISIN (not just holdings). TWR / Vol / MaxDD are the
fund's own EUR series over --from → --as-of, not your cash-flow return.

  From   first EUR close actually used (later than --from if the fund is younger)
  n      gap-bridged EUR returns that feed TWR/Vol/MaxDD
  Gap    interior missing trading days (same-exchange peers have a close; this
         fund spans the day but lacks it). Pre-listing days are not Gap.

A fund that does not go back to --from stays in the table with a later From
and a shorter n. --from is optional: omitted, each fund starts at its first close.

Examples:
  e1f funds
  e1f funds --from 2020-01-01
  e1f funds --from 2020-01-01 --as-of 2024-12-31
  e1f funds --unheld --sort ter
  e1f funds --class Equity --dist acc
        """,
    )
    parser.add_argument("--db", "-d", default=DEFAULT_DB, help="Database file path")
    parser.add_argument(
        "--config", "-c", default=DEFAULT_CONFIG, help="ETF universe config path",
    )
    parser.add_argument(
        "--currency-meta",
        default=DEFAULT_CURRENCY_META,
        help="Pinned ftgo resolution / currency sidecar path",
    )
    parser.add_argument(
        "--from",
        dest="start",
        default=None,
        metavar="YYYY-MM-DD",
        help="Window start (default: each fund's first EUR close). Younger funds stay listed.",
    )
    parser.add_argument(
        "--as-of",
        default=datetime.now(UTC).date().isoformat(),
        metavar="YYYY-MM-DD",
        help="Window end (default: today)",
    )
    parser.add_argument(
        "--unheld",
        action="store_true",
        help="Only funds that are not a current holding",
    )
    parser.add_argument(
        "--class",
        dest="asset_class",
        default=None,
        metavar="CLASS",
        help="Filter by asset class (e.g. Equity, Bonds)",
    )
    parser.add_argument(
        "--dist",
        default=None,
        metavar="ACC|DIST",
        help="Filter by distribution (acc / dist / accumulating / distributing)",
    )
    parser.add_argument(
        "--explain",
        action="store_true",
        help="Add a provenance block (method/contract/limited-by; ADR-0014)",
    )
    parser.add_argument(
        "--sort",
        choices=SORT_FIELDS,
        default="isin",
        help="Sort rows by column (default: isin)",
    )
    parser.add_argument(
        "--reverse", "-r", action="store_true", help="Descending sort order",
    )
    return parser


def _parse_day(value: str, flag: str) -> str | None:
    try:
        date.fromisoformat(value)
    except ValueError:
        print(f"✗ Error: {flag} must be YYYY-MM-DD: {value}")
        return None
    return value


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    as_of = _parse_day(args.as_of, "--as-of")
    if as_of is None:
        return 1
    start = None
    if args.start is not None:
        start = _parse_day(args.start, "--from")
        if start is None:
            return 1
        if start > as_of:
            print(f"✗ Error: --from {start} is after --as-of {as_of}")
            return 1
    try:
        return _cmd_funds(
            args.db,
            args.config,
            as_of=as_of,
            start=start,
            unheld=args.unheld,
            asset_class=args.asset_class,
            distribution=args.dist,
            explain=args.explain,
            sort_by=args.sort,
            reverse=args.reverse,
            currency_meta_path=args.currency_meta,
        )
    except Exception as exc:  # noqa: BLE001 — CLI top-level; all errors become exit code 1
        print(f"✗ Error: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
