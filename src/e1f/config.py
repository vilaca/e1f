#!/usr/bin/env python
"""e1f config builder — create/maintain the ETF universe YAML from ISINs.

Usage:
    e1f config add IE00BM67HK77
    e1f config add IE00BM67HK77 IE00BDBRDM35 IE00BKM4GZ66
    e1f config list
    e1f config update IE00BM67HK77 IE00BDBRDM35
    e1f config update
    e1f config trim

Each ISIN is resolved via OpenFIGI (name, tickers, exchange, FIGI) and written
to the YAML config consumed by the fetch command.
"""

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
    # format='mixed' parses per-value, so a date-only row alongside 'YYYY-MM-DD
    # HH:MM:SS' rows resolves to the same day (a real duplicate) rather than
    # coercing whole columns to NaT; errors='coerce' turns genuinely unparseable
    # or NULL dates into NaT instead of crashing validate. NaT rows are dropped
    # per-ISIN before the business-day math below.
    checked['date'] = pd.to_datetime(checked['date'], format='mixed', errors='coerce')
    checked = checked.sort_values(['isin', 'date'])

    def max_missing_business_days(dates: pd.Series) -> int:
        days = dates.dropna().to_numpy().astype('datetime64[D]')
        if len(days) < 2:
            return 0
        # Business days strictly between consecutive observations. Counting from
        # the day *after* the earlier date excludes both endpoints regardless of
        # whether either falls on a weekend (the data can carry weekend rows).
        missing = np.busday_count(days[:-1] + np.timedelta64(1, 'D'), days[1:])
        return max(int(missing.max()), 0)

    def max_abs_return(close: pd.Series) -> float:
        returns = close.pct_change(fill_method=None).abs().dropna()
        return float(returns.max()) if len(returns) else 0.0

    gaps = checked.groupby('isin')['date'].apply(max_missing_business_days)
    returns = checked.groupby('isin')['close'].apply(max_abs_return)
    invalid = checked['date'].isna()
    # Duplicates are keyed on (isin, date); compute them on rows with a real date
    # so NaT-vs-NaT doesn't masquerade as a key collision (those are invalid_dates).
    valid = checked[~invalid]
    duplicates = valid.duplicated(subset=['isin', 'date'], keep=False)
    nulls = checked['close'].isna()
    non_positive = checked['close'] <= 0
    weekends = checked['date'].dt.weekday >= 5
    return {
        'rows': len(checked),
        'duplicates': int(valid.duplicated(subset=['isin', 'date']).sum()),
        'nulls': int(nulls.sum()),
        'non_positive': int(non_positive.sum()),
        'weekend_rows': int(weekends.sum()),
        'invalid_dates': int(invalid.sum()),
        'max_missing_business_days': int(gaps.max()) if len(gaps) else 0,
        'max_abs_return': float(returns.max()) if len(returns) else 0.0,
        'duplicate_isins': sorted(valid.loc[duplicates, 'isin'].unique()),
        'null_isins': sorted(checked.loc[nulls, 'isin'].unique()),
        'non_positive_isins': sorted(checked.loc[non_positive, 'isin'].unique()),
        'weekend_isins': sorted(checked.loc[weekends, 'isin'].unique()),
        'invalid_date_isins': sorted(checked.loc[invalid, 'isin'].unique()),
        'missing_business_days_by_isin': {
            str(isin): int(days) for isin, days in gaps.items()
        },
        'abs_return_by_isin': {
            str(isin): float(change) for isin, change in returns.items()
        },
    }


def _affected(isins: list[str]) -> str:
    return f" [{', '.join(isins)}]" if isins else ""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="e1f config",
        description="Build the ETF universe YAML from ISINs (via OpenFIGI)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Add an ETF by ISIN (auto-resolves name, tickers, exchange)
  e1f config add IE00BM67HK77

  # Add multiple ETFs
  e1f config add IE00BM67HK77 IE00BDBRDM35 IE00BKM4GZ66

  # List all ETFs in the config
  e1f config list

  # Refresh one or more ETFs' metadata from OpenFIGI
  e1f config update IE00BM67HK77
  e1f config update IE00BM67HK77 IE00BDBRDM35
  e1f config update

  # Check config/DB sync, history depth, and data quality
  e1f config validate
        """
    )

    parser.add_argument('--config', '-c', default=DEFAULT_CONFIG, help='Config file path')

    subparsers = parser.add_subparsers(dest='command', help='Command')

    add_parser = subparsers.add_parser('add', help='Add ETF(s) by ISIN')
    add_parser.add_argument('isins', nargs='+', help='ISINs to add')

    subparsers.add_parser('list', help='List all ETFs')

    update_parser = subparsers.add_parser('update', help='Update ETF metadata')
    update_parser.add_argument(
        'isins',
        nargs='*',
        help='ISINs to update (default: all ETFs in config)',
    )

    remove_parser = subparsers.add_parser(
        'remove',
        help='Delete one or more ISINs from config, DB, and currency metadata',
    )
    remove_parser.add_argument('isins', nargs='+', help='ISINs to remove')
    remove_parser.add_argument('--db', default=DEFAULT_DB, help='SQLite DB path')
    remove_parser.add_argument('--currency-meta', default=DEFAULT_CURRENCY_META,
                               help='Currency metadata YAML path')

    trim_parser = subparsers.add_parser(
        'trim',
        help='Remove ISINs not present in both config and DB (keeps intersection)',
    )
    trim_parser.add_argument('--db', default=DEFAULT_DB, help='SQLite DB path')
    trim_parser.add_argument('--currency-meta', default=DEFAULT_CURRENCY_META,
                             help='Currency metadata YAML path')

    validate_parser = subparsers.add_parser(
        'validate',
        help='Check config/DB sync, history depth, and data quality',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Exit codes:
  0  clean, or warnings only
  1  errors found

Errors (exit 1)   — duplicate keys, null closes, non-positive closes, weekend
                    rows, invalid dates, or config/DB desync (missing or orphan
                    ISINs).
Warnings (exit 0) — missing-business-day gaps over the limit, large price moves,
                    short/sparse/cash-like history. Surfaced, never fatal.
        """,
    )
    validate_parser.add_argument('--db', default=DEFAULT_DB, help='SQLite DB path')
    validate_parser.add_argument('--min-years', type=float, default=3.0,
                                 help='Minimum history in years (default: 3)')
    validate_parser.add_argument('--min-fill', type=float, default=0.6,
                                 help='Minimum data fill rate 0-1 (default: 0.6)')
    validate_parser.add_argument('--max-vol', type=float, default=0.02,
                                 help='Ann vol below this flags as cash-like (default: 0.02)')

    args = parser.parse_args(argv)

    if not args.command:
        parser.print_help()
        return 1

    if args.command == 'list':
        config = ConfigManager(args.config)
        etfs = config.list()

        if not etfs:
            print("No ETFs in configuration")
            print("Add one: e1f config add IE00BM67HK77")
            return 0

        print(f"\n{'ISIN':<14} {'Name':<50} {'Ticker':<12} {'Exchange'}")
        print("-" * 90)
        for isin, data in etfs:
            name = data.get('name', 'Unknown')[:48]
            ticker = data.get('tickers', [''])[0] if data.get('tickers') else ''
            exchange = data.get('exchange', '')
            print(f"{isin:<14} {name:<50} {ticker:<12} {exchange}")
        print(f"\nTotal: {len(etfs)} ETFs")
        return 0

    if args.command == 'add':
        config = ConfigManager(args.config)
        success = 0
        for isin in args.isins:
            if config.add(isin):
                success += 1
            print()
        print(f"✓ Added {success}/{len(args.isins)} ETFs")
        return 0 if success == len(args.isins) else 1

    if args.command == 'update':
        config = ConfigManager(args.config)
        isins = args.isins or [isin for isin, _ in config.list()]
        if not isins:
            print("No ETFs in configuration")
            print("Add one: e1f config add IE00BM67HK77")
            return 0

        success = 0
        for isin in isins:
            if config.update(isin):
                success += 1
            print()
        print(f"✓ Updated {success}/{len(isins)} ETFs")
        return 0 if success == len(isins) else 1

    if args.command == 'remove':
        cm = ConfigManager(args.config)

        try:
            with open(args.currency_meta) as f:
                curr_meta = yaml.safe_load(f) or {}
        except FileNotFoundError:
            curr_meta = {}

        for isin in args.isins:
            removed_any = False

            if isin in cm.config.get('etfs', {}):
                del cm.config['etfs'][isin]
                removed_any = True
                print(f"{isin}: removed from config")

            if isin in curr_meta:
                del curr_meta[isin]
                removed_any = True
                print(f"{isin}: removed from currency metadata")

            if _db_has_prices(args.db):
                with closing(sqlite3.connect(args.db)) as conn:
                    n = conn.execute("DELETE FROM prices WHERE isin = ?", (isin,)).rowcount
                    conn.commit()
                if n:
                    removed_any = True
                    print(f"{isin}: removed {n} rows from DB")

            if not removed_any:
                print(f"{isin}: not found in any file")

        cm._save_config()
        with open(args.currency_meta, 'w') as f:
            yaml.dump(curr_meta, f, default_flow_style=False, sort_keys=True)
        return 0

    if args.command == 'trim':
        if not _db_has_prices(args.db):
            print(f"✗ No price data in {args.db} — run 'e1f fetch' first "
                  "(refusing to trim: intersection would be empty)")
            return 1

        cm = ConfigManager(args.config)
        config_isins = set(dict(cm.list()).keys())

        db_isins = set()
        if _db_has_prices(args.db):
            with closing(sqlite3.connect(args.db)) as conn:
                db_isins = {row[0] for row in conn.execute("SELECT DISTINCT isin FROM prices")}

        try:
            with open(args.currency_meta) as f:
                curr_meta = yaml.safe_load(f) or {}
        except FileNotFoundError:
            curr_meta = {}

        curr_isins = set(curr_meta.keys())

        kept = config_isins & db_isins & curr_isins
        all_isins = config_isins | db_isins | curr_isins

        if kept == all_isins:
            print("Nothing to trim — all three files are already in sync.")
            return 0

        to_remove_config = sorted(config_isins - kept)
        to_remove_db = sorted(db_isins - kept)
        to_remove_curr = sorted(curr_isins - kept)

        if to_remove_config:
            print(f"Removing from config:            {', '.join(to_remove_config)}")
            for isin in to_remove_config:
                del cm.config['etfs'][isin]
            cm._save_config()

        if to_remove_db:
            print(f"Removing from DB:                {', '.join(to_remove_db)}")
            with closing(sqlite3.connect(args.db)) as conn:
                conn.executemany("DELETE FROM prices WHERE isin = ?",
                                 [(isin,) for isin in to_remove_db])
                conn.commit()

        if to_remove_curr:
            print(f"Removing from currency metadata: {', '.join(to_remove_curr)}")
        curr_trimmed = {k: v for k, v in curr_meta.items() if k in kept}
        with open(args.currency_meta, 'w') as f:
            yaml.dump(curr_trimmed, f, default_flow_style=False, sort_keys=True)

        print(f"Done — {len(kept)} ISINs kept.")
        return 0

    if args.command == 'validate':
        TRADING_YEAR = 252

        cm = ConfigManager(args.config)
        config_meta = dict(cm.list())

        if not _db_has_prices(args.db):
            print(f"✗ No price data in {args.db} — run 'e1f fetch' first")
            return 1

        with closing(sqlite3.connect(args.db)) as conn:
            price_df = pd.read_sql(
                'SELECT isin, date, close FROM prices ORDER BY isin, date',
                conn,
            )
        # Parse per-value (mixed date-only / datetime strings) and coerce garbage
        # to NaT, matching quality_report so both agree on what a duplicate is.
        price_df['date'] = pd.to_datetime(
            price_df['date'], format='mixed', errors='coerce'
        )

        db_isins = set(price_df['isin'].unique())
        config_isin_set = set(config_meta.keys())

        # --- Price integrity ---
        quality = quality_report(price_df)
        gap_breakdown = sorted(
            (
                (isin, days)
                for isin, days in quality['missing_business_days_by_isin'].items()
                if days > MAX_MISSING_BUSINESS_DAYS
            ),
            key=lambda item: (-item[1], item[0]),
        )
        return_isins = sorted(
            isin for isin, change in quality['abs_return_by_isin'].items()
            if change >= MAX_ABS_RETURN
        )
        integrity_errors = (
            quality['duplicates'] > 0
            or quality['nulls'] > 0
            or quality['non_positive'] > 0
            or quality['weekend_rows'] > 0
            or quality['invalid_dates'] > 0
        )
        integrity_warnings = (
            quality['max_missing_business_days'] > MAX_MISSING_BUSINESS_DAYS
            or quality['max_abs_return'] >= MAX_ABS_RETURN
        )
        print("=== Data Integrity ===")
        print(f"  Rows: {quality['rows']}")
        print("  Errors:")
        print(f"    Duplicate keys:       {quality['duplicates']}"
              f"{_affected(quality['duplicate_isins'])}")
        print(f"    Null closes:          {quality['nulls']}"
              f"{_affected(quality['null_isins'])}")
        print(f"    Non-positive closes:  {quality['non_positive']}"
              f"{_affected(quality['non_positive_isins'])}")
        print(f"    Weekend rows:         {quality['weekend_rows']}"
              f"{_affected(quality['weekend_isins'])}")
        print(f"    Invalid dates:        {quality['invalid_dates']}"
              f"{_affected(quality['invalid_date_isins'])}")
        print("  Warnings:")
        print("    Largest missing-business-day gap: "
              f"{quality['max_missing_business_days']} days "
              f"(limit: {MAX_MISSING_BUSINESS_DAYS})")
        day_width = max((len(str(days)) for _, days in gap_breakdown), default=1)
        for isin, days in gap_breakdown:
            name = config_meta.get(isin, {}).get('name', 'Unknown')
            print(f"      {days:>{day_width}} days  {isin}  {name}")
        print(f"    Largest price change: {quality['max_abs_return']:.1%} "
              f"(limit: {MAX_ABS_RETURN:.0%}){_affected(return_isins)}")

        # --- Config vs DB sync ---
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

        # --- Per-ETF stats ---
        # Drop NaT dates and collapse duplicate (isin, date) keys so the pivot
        # below can't raise; both are surfaced as errors under Data Integrity.
        stats_df = (
            price_df.dropna(subset=['date'])
            .drop_duplicates(subset=['isin', 'date'], keep='last')
        )
        stats = stats_df.groupby('isin').agg(
            first_date=('date', 'min'),
            last_date=('date', 'max'),
            n_days=('date', 'count'),
        ).reset_index()
        stats['span_days'] = (stats['last_date'] - stats['first_date']).dt.days
        stats['expected'] = (stats['span_days'] * 5 / 7).clip(lower=1).astype(int)
        stats['fill_rate'] = stats['n_days'] / stats['expected']
        stats['years'] = stats['n_days'] / TRADING_YEAR

        wide = stats_df.pivot(index='date', columns='isin', values='close')
        ann_vol = wide.pct_change().std() * math.sqrt(TRADING_YEAR)
        stats = stats.merge(ann_vol.rename('ann_vol').reset_index(), on='isin', how='left')
        stats['name'] = stats['isin'].map(
            lambda x: config_meta.get(x, {}).get('name', 'Unknown')[:35]
        )

        # --- History breakdown ---
        print()
        print("=== History Breakdown ===")
        tiers = [
            ('>= 10yr', stats['years'] >= 10),
            ('5-10yr',  (stats['years'] >= 5) & (stats['years'] < 10)),
            ('3-5yr',   (stats['years'] >= 3) & (stats['years'] < 5)),
            ('1-3yr',   (stats['years'] >= 1) & (stats['years'] < 3)),
            ('< 1yr',   stats['years'] < 1),
        ]
        printed_tier = False
        for label, mask in tiers:
            n = int(mask.sum())
            if n:
                print(f"  {label:<10}  {n:>3} ETF{'s' if n != 1 else ''}")
                printed_tier = True
        if not printed_tier:
            print("  None — no dated price history to summarize.")

        # --- Issues ---
        flagged = []
        short  = stats[stats['n_days'] < TRADING_YEAR * args.min_years].sort_values('n_days')
        sparse = stats[(stats['fill_rate'] < args.min_fill) &
                       (stats['n_days'] >= TRADING_YEAR * args.min_years)].sort_values('fill_rate')
        cash   = stats[(stats['ann_vol'] < args.max_vol) &
                       (~stats['isin'].isin(short['isin']))].sort_values('ann_vol')

        print()
        print("=== Warnings ===")
        if not short.empty:
            print(f"  Short history (< {args.min_years:.0f}yr):")
            for _, r in short.iterrows():
                print(f"    {r['isin']}  {r['name']:<35}  "
                      f"{int(r['n_days']):>4} days  from {r['first_date'].date()}")
                flagged.append(r['isin'])

        if not sparse.empty:
            print(f"  Sparse data (fill < {args.min_fill:.0%}):")
            for _, r in sparse.iterrows():
                print(f"    {r['isin']}  {r['name']:<35}  "
                      f"{int(r['n_days'])}/{int(r['expected'])} days  ({r['fill_rate']:.0%})")
                flagged.append(r['isin'])

        if not cash.empty:
            print(f"  Cash-like (ann vol < {args.max_vol:.0%}):")
            for _, r in cash.iterrows():
                print(f"    {r['isin']}  {r['name']:<35}  vol {r['ann_vol']:.2%}")
                flagged.append(r['isin'])

        validation_errors = integrity_errors or bool(only_config) or bool(only_db)
        validation_warnings = integrity_warnings or bool(flagged)
        if not flagged:
            # flagged is empty here, so the only warnings left are integrity ones.
            if not validation_errors and not integrity_warnings:
                print("  None — all ETFs look good.")
            elif integrity_warnings:
                # This section only covers history/fill/vol; integrity warnings
                # (gaps, price jumps) are reported under Data Integrity above.
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

    return 0


if __name__ == "__main__":
    sys.exit(main())
