"""Validate ETF configuration, currency metadata, and stored price data."""

import argparse
import math
import os
import sqlite3
import sys
from contextlib import closing
from typing import TypedDict

import numpy as np
import pandas as pd
import yaml

from e1f.common import DEFAULT_CONFIG, DEFAULT_CURRENCY_META, DEFAULT_DB, ConfigManager

MAX_MISSING_BUSINESS_DAYS = 5
MAX_ABS_RETURN = 0.5
TRADING_YEAR = 252
# Interior single-day gap detection, voted **within an exchange** (venue): a day is
# a "consensus trading day" when at least this share of same-venue funds spanning it
# have a close; a fund that spans the day but lacks it has an interior gap (a fetch
# that skipped a day — invisible to the ≤5-business-day gap check). Voting per venue
# means a real venue holiday (all its funds closed) is never flagged; MIN_COVERING is
# both the per-venue fund floor and the per-day covering floor (a thin venue or a
# series' own edges can't vote).
GAP_CONSENSUS = 0.8
MIN_COVERING_ISINS = 3


class QualityReport(TypedDict):
    rows: int
    duplicates: int
    nulls: int
    non_positive: int
    weekend_rows: int
    invalid_dates: int
    max_missing_business_days: int
    max_abs_return: float
    duplicate_isins: list[str]
    null_isins: list[str]
    non_positive_isins: list[str]
    weekend_isins: list[str]
    invalid_date_isins: list[str]
    missing_business_days_by_isin: dict[str, int]
    abs_return_by_isin: dict[str, float]


def _db_has_prices(db_path: str) -> bool:
    """True when the SQLite DB exists and contains the prices table."""
    if not os.path.exists(db_path):
        return False
    with closing(sqlite3.connect(db_path)) as conn:
        return conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='prices'"
        ).fetchone() is not None


def quality_report(prices: pd.DataFrame) -> QualityReport:
    """Return data-quality metrics for a long (isin, date, close) price frame."""
    checked = prices.copy()
    # Parse each value independently so mixed date/datetime strings resolve to
    # the same day, while genuinely invalid values become NaT.
    checked["date"] = pd.to_datetime(checked["date"], format="mixed", errors="coerce")
    checked = checked.sort_values(["isin", "date"])

    def max_missing_business_days(dates: pd.Series) -> int:
        days = dates.dropna().to_numpy().astype("datetime64[D]")
        if len(days) < 2:
            return 0
        missing = np.busday_count(days[:-1] + np.timedelta64(1, "D"), days[1:])
        return max(int(missing.max()), 0)

    def max_abs_return(close: pd.Series) -> float:
        returns = close.pct_change(fill_method=None).abs().dropna()
        return float(returns.max()) if len(returns) else 0.0

    gaps = checked.groupby("isin")["date"].apply(max_missing_business_days)
    returns = checked.groupby("isin")["close"].apply(max_abs_return)
    invalid = checked["date"].isna()
    valid = checked[~invalid]
    duplicates = valid.duplicated(subset=["isin", "date"], keep=False)
    nulls = checked["close"].isna()
    non_positive = checked["close"] <= 0
    weekends = checked["date"].dt.weekday >= 5
    return {
        "rows": len(checked),
        "duplicates": int(valid.duplicated(subset=["isin", "date"]).sum()),
        "nulls": int(nulls.sum()),
        "non_positive": int(non_positive.sum()),
        "weekend_rows": int(weekends.sum()),
        "invalid_dates": int(invalid.sum()),
        "max_missing_business_days": int(gaps.max()) if len(gaps) else 0,
        "max_abs_return": float(returns.max()) if len(returns) else 0.0,
        "duplicate_isins": sorted(valid.loc[duplicates, "isin"].unique()),
        "null_isins": sorted(checked.loc[nulls, "isin"].unique()),
        "non_positive_isins": sorted(checked.loc[non_positive, "isin"].unique()),
        "weekend_isins": sorted(checked.loc[weekends, "isin"].unique()),
        "invalid_date_isins": sorted(checked.loc[invalid, "isin"].unique()),
        "missing_business_days_by_isin": {
            str(isin): int(days) for isin, days in gaps.items()
        },
        "abs_return_by_isin": {
            str(isin): float(change) for isin, change in returns.items()
        },
    }


def consensus_gaps(
    prices: pd.DataFrame,
    venue_by_isin: dict[str, str],
    *,
    threshold: float = GAP_CONSENSUS,
) -> dict[str, list[str]]:
    """Per-ISIN interior gaps: days the ISIN lacks but its same-exchange peers have.

    A single skipped trading day is invisible to the business-day-gap check when the
    gap is under its limit, yet it distorts short-window return metrics. The vote is
    held **within an exchange** (from ``venue_by_isin``, e.g. LSE / GER) so a genuine
    venue holiday — when every fund on that exchange is closed — is never mistaken for
    a gap. Within a venue, a day is a *consensus trading day* when at least
    ``threshold`` of the funds whose history spans it have a close; a covering fund
    missing such a day has an interior gap (repair with ``e1f fetch <isin> --force``).
    A venue with fewer than ``MIN_COVERING_ISINS`` funds can't establish consensus and
    is skipped (under-reporting beats crying wolf). ``{isin: [YYYY-MM-DD, …]}``.
    """
    checked = prices.copy()
    checked["date"] = pd.to_datetime(checked["date"], format="mixed", errors="coerce")
    checked = checked.dropna(subset=["date"]).drop_duplicates(
        subset=["isin", "date"], keep="last"
    )
    if checked.empty:
        return {}
    present = checked.pivot(index="date", columns="isin", values="close").sort_index().notna()

    venues: dict[str, list[str]] = {}
    for isin in present.columns:
        venue = venue_by_isin.get(str(isin))
        if venue:
            venues.setdefault(venue, []).append(str(isin))

    gaps: dict[str, list[str]] = {}
    for isins in venues.values():
        if len(isins) < MIN_COVERING_ISINS:
            continue  # too few peers on this exchange to vote
        sub = present[isins]
        covering = pd.DataFrame(False, index=sub.index, columns=sub.columns)
        for isin in isins:
            valid = sub.index[sub[isin].to_numpy()]
            if len(valid):
                covering[isin] = (sub.index >= valid.min()) & (sub.index <= valid.max())
        covering_count = covering.sum(axis=1)
        ratio = sub.sum(axis=1).where(covering_count > 0).div(covering_count)
        consensus = (ratio >= threshold) & (covering_count >= MIN_COVERING_ISINS)
        for isin in isins:
            missing = (consensus & covering[isin] & ~sub[isin]).to_numpy()
            dates = sub.index[missing]
            if len(dates):
                gaps[isin] = [d.strftime("%Y-%m-%d") for d in dates]
    return gaps


def _affected(isins: list[str]) -> str:
    return f" [{', '.join(isins)}]" if isins else ""


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="e1f validate",
        description="Check config/DB sync, history depth, and data quality",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Exit codes:
  0  clean, or warnings only
  1  errors found

Errors (exit 1)   — duplicate keys, null closes, non-positive closes, weekend
                    rows, invalid dates, malformed pinned currency metadata, or
                    config/DB desync (missing or orphan ISINs).
Warnings (exit 0) — missing-business-day gaps over the limit, interior single-day
                    gaps (a day most funds have but one ETF lacks — a skipped
                    fetch; repair with 'e1f fetch <isin> --force'), large price
                    moves, short/sparse/cash-like history. Surfaced, never fatal.
        """,
    )
    parser.add_argument("--config", "-c", default=DEFAULT_CONFIG, help="Config file path")
    parser.add_argument("--db", default=DEFAULT_DB, help="SQLite DB path")
    parser.add_argument(
        "--currency-meta", default=DEFAULT_CURRENCY_META, help="Currency metadata YAML path"
    )
    parser.add_argument(
        "--min-years", type=float, default=3.0, help="Minimum history in years (default: 3)"
    )
    parser.add_argument(
        "--min-fill", type=float, default=0.6, help="Minimum data fill rate 0-1 (default: 0.6)"
    )
    parser.add_argument(
        "--max-vol",
        type=float,
        default=0.02,
        help="Ann vol below this flags as cash-like (default: 0.02)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    cm = ConfigManager(args.config)
    config_meta = dict(cm.list())

    if not _db_has_prices(args.db):
        print(f"✗ No price data in {args.db} — run 'e1f fetch' first")
        return 1

    with closing(sqlite3.connect(args.db)) as conn:
        price_df = pd.read_sql(
            "SELECT isin, date, close FROM prices ORDER BY isin, date",
            conn,
        )
    price_df["date"] = pd.to_datetime(
        price_df["date"], format="mixed", errors="coerce"
    )

    db_isins = set(price_df["isin"].unique())
    config_isin_set = set(config_meta.keys())

    try:
        with open(args.currency_meta) as f:
            currency_meta = yaml.safe_load(f) or {}
    except FileNotFoundError:
        currency_meta = {}

    currency_errors = []
    for isin in sorted(config_isin_set):
        pinned = currency_meta.get(isin)
        if not isinstance(pinned, dict):
            continue
        symbol = str(pinned.get("symbol") or "")
        currency = str(pinned.get("currency") or "")
        symbol_parts = symbol.split(":")
        if len(symbol_parts) < 3 or not currency or symbol_parts[-1] != currency:
            currency_errors.append((isin, currency or "(missing)", symbol))

    print("=== Currency Metadata ===")
    if currency_errors:
        print("  Errors:")
        for isin, currency, symbol in currency_errors:
            print(f"    {isin}: {currency} (malformed symbol: {symbol or '(missing)'})")
    else:
        print("  No malformed pinned quote currencies.")
    print()

    quality = quality_report(price_df)
    venue_by_isin = {
        isin: str(pinned.get("symbol", "")).split(":")[1]
        for isin, pinned in currency_meta.items()
        if isinstance(pinned, dict) and len(str(pinned.get("symbol", "")).split(":")) >= 3
    }
    consensus_gap_map = consensus_gaps(price_df, venue_by_isin)
    gap_breakdown = sorted(
        (
            (isin, days)
            for isin, days in quality["missing_business_days_by_isin"].items()
            if days > MAX_MISSING_BUSINESS_DAYS
        ),
        key=lambda item: (-item[1], item[0]),
    )
    return_isins = sorted(
        isin
        for isin, change in quality["abs_return_by_isin"].items()
        if change >= MAX_ABS_RETURN
    )
    integrity_errors = (
        quality["duplicates"] > 0
        or quality["nulls"] > 0
        or quality["non_positive"] > 0
        or quality["weekend_rows"] > 0
        or quality["invalid_dates"] > 0
    )
    integrity_warnings = (
        quality["max_missing_business_days"] > MAX_MISSING_BUSINESS_DAYS
        or quality["max_abs_return"] >= MAX_ABS_RETURN
        or bool(consensus_gap_map)
    )
    print("=== Data Integrity ===")
    print(f"  Rows: {quality['rows']}")
    print("  Errors:")
    print(
        f"    Duplicate keys:       {quality['duplicates']}"
        f"{_affected(quality['duplicate_isins'])}"
    )
    print(
        f"    Null closes:          {quality['nulls']}"
        f"{_affected(quality['null_isins'])}"
    )
    print(
        f"    Non-positive closes:  {quality['non_positive']}"
        f"{_affected(quality['non_positive_isins'])}"
    )
    print(
        f"    Weekend rows:         {quality['weekend_rows']}"
        f"{_affected(quality['weekend_isins'])}"
    )
    print(
        f"    Invalid dates:        {quality['invalid_dates']}"
        f"{_affected(quality['invalid_date_isins'])}"
    )
    print("  Warnings:")
    print(
        "    Largest missing-business-day gap: "
        f"{quality['max_missing_business_days']} days "
        f"(limit: {MAX_MISSING_BUSINESS_DAYS})"
    )
    day_width = max((len(str(days)) for _, days in gap_breakdown), default=1)
    for isin, days in gap_breakdown:
        name = config_meta.get(isin, {}).get("name", "Unknown")
        print(f"      {days:>{day_width}} days  {isin}  {name}")
    print(
        f"    Largest price change: {quality['max_abs_return']:.1%} "
        f"(limit: {MAX_ABS_RETURN:.0%}){_affected(return_isins)}"
    )
    if consensus_gap_map:
        total_gaps = sum(len(days) for days in consensus_gap_map.values())
        print(
            f"    Interior gaps (a trading day most funds have but this ETF lacks): "
            f"{total_gaps} across {len(consensus_gap_map)} ETF(s) "
            "— repair with 'e1f fetch <isin> --force'"
        )
        for isin, dates in sorted(consensus_gap_map.items()):
            name = config_meta.get(isin, {}).get("name", "Unknown")
            sample = ", ".join(dates[:5]) + (" …" if len(dates) > 5 else "")
            print(f"      {isin}  {name}  {len(dates)} day(s): {sample}")
    else:
        print("    Interior gaps: none")

    only_config = sorted(config_isin_set - db_isins)
    only_db = sorted(db_isins - config_isin_set)
    print()
    print("=== Config vs DB ===")
    if not only_config and not only_db:
        n = len(config_isin_set)
        print(f"  No errors — {n} ETF{'s' if n != 1 else ''}, config and DB in sync  ✓")
    else:
        print("  Errors:")
        if only_config:
            print(f"    In config, missing from DB (run fetch): {', '.join(only_config)}")
        if only_db:
            print(f"    In DB, not in config (orphans):         {', '.join(only_db)}")

    stats_df = (
        price_df.dropna(subset=["date"])
        .drop_duplicates(subset=["isin", "date"], keep="last")
    )
    stats = (
        stats_df.groupby("isin")
        .agg(
            first_date=("date", "min"),
            last_date=("date", "max"),
            n_days=("date", "count"),
        )
        .reset_index()
    )
    stats["span_days"] = (stats["last_date"] - stats["first_date"]).dt.days
    stats["expected"] = (stats["span_days"] * 5 / 7).clip(lower=1).astype(int)
    stats["fill_rate"] = stats["n_days"] / stats["expected"]
    stats["years"] = stats["n_days"] / TRADING_YEAR

    wide = stats_df.pivot(index="date", columns="isin", values="close")
    ann_vol = wide.pct_change().std() * math.sqrt(TRADING_YEAR)
    stats = stats.merge(ann_vol.rename("ann_vol").reset_index(), on="isin", how="left")
    stats["name"] = stats["isin"].map(
        lambda isin: config_meta.get(isin, {}).get("name", "Unknown")[:35]
    )

    print()
    print("=== History Breakdown ===")
    tiers = [
        (">= 10yr", stats["years"] >= 10),
        ("5-10yr", (stats["years"] >= 5) & (stats["years"] < 10)),
        ("3-5yr", (stats["years"] >= 3) & (stats["years"] < 5)),
        ("1-3yr", (stats["years"] >= 1) & (stats["years"] < 3)),
        ("< 1yr", stats["years"] < 1),
    ]
    printed_tier = False
    for label, mask in tiers:
        n = int(mask.sum())
        if n:
            print(f"  {label:<10}  {n:>3} ETF{'s' if n != 1 else ''}")
            printed_tier = True
    if not printed_tier:
        print("  None — no dated price history to summarize.")

    flagged = []
    short = stats[stats["n_days"] < TRADING_YEAR * args.min_years].sort_values(
        "n_days"
    )
    sparse = stats[
        (stats["fill_rate"] < args.min_fill)
        & (stats["n_days"] >= TRADING_YEAR * args.min_years)
    ].sort_values("fill_rate")
    cash = stats[
        (stats["ann_vol"] < args.max_vol) & (~stats["isin"].isin(short["isin"]))
    ].sort_values("ann_vol")

    print()
    print("=== Warnings ===")
    if not short.empty:
        print(f"  Short history (< {args.min_years:.0f}yr):")
        for _, row in short.iterrows():
            print(
                f"    {row['isin']}  {row['name']:<35}  "
                f"{int(row['n_days']):>4} days  from {row['first_date'].date()}"
            )
            flagged.append(row["isin"])

    if not sparse.empty:
        print(f"  Sparse data (fill < {args.min_fill:.0%}):")
        for _, row in sparse.iterrows():
            print(
                f"    {row['isin']}  {row['name']:<35}  "
                f"{int(row['n_days'])}/{int(row['expected'])} days  "
                f"({row['fill_rate']:.0%})"
            )
            flagged.append(row["isin"])

    if not cash.empty:
        print(f"  Cash-like (ann vol < {args.max_vol:.0%}):")
        for _, row in cash.iterrows():
            print(
                f"    {row['isin']}  {row['name']:<35}  vol {row['ann_vol']:.2%}"
            )
            flagged.append(row["isin"])

    validation_errors = (
        bool(currency_errors)
        or integrity_errors
        or bool(only_config)
        or bool(only_db)
    )
    validation_warnings = integrity_warnings or bool(flagged)
    if not flagged:
        if not validation_errors and not integrity_warnings:
            print("  None — all ETFs look good.")
        elif integrity_warnings:
            print("  None here — see Data Integrity warnings above.")
        else:
            print("  None.")

    if validation_errors:
        print()
        print("Validation failed — correct the errors above.")
    elif validation_warnings:
        print()
        print("Validation passed with warnings.")

    return 1 if validation_errors else 0


if __name__ == "__main__":
    sys.exit(main())
