#!/usr/bin/env python
"""e1f config builder — create/maintain the ETF universe YAML from ISINs.

Usage:
    e1f config add IE00BM67HK77
    e1f config add IE00BM67HK77 IE00BDBRDM35 IE00BKM4GZ66
    e1f config list
    e1f config update IE00BM67HK77
    e1f config trim

Each ISIN is resolved via OpenFIGI (name, tickers, exchange, FIGI) and written
to the YAML config consumed by the fetch command.
"""

import argparse
import os
import sqlite3
import sys

import numpy as np
import pandas as pd
import yaml

from e1f.common import DEFAULT_CONFIG, DEFAULT_CURRENCY_META, DEFAULT_DB, ConfigManager


def _db_has_prices(db_path: str) -> bool:
    """True when the SQLite DB exists and contains the prices table."""
    if not os.path.exists(db_path):
        return False
    with sqlite3.connect(db_path) as conn:
        return conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='prices'"
        ).fetchone() is not None


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

  # Refresh an ETF's metadata from OpenFIGI
  e1f config update IE00BM67HK77

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
    update_parser.add_argument('isin', help='ISIN to update')

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
        success = config.update(args.isin)
        return 0 if success else 1

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
                with sqlite3.connect(args.db) as conn:
                    n = conn.execute("DELETE FROM prices WHERE isin = ?", (isin,)).rowcount
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
            with sqlite3.connect(args.db) as conn:
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
            with sqlite3.connect(args.db) as conn:
                conn.executemany("DELETE FROM prices WHERE isin = ?",
                                 [(isin,) for isin in to_remove_db])

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

        with sqlite3.connect(args.db) as conn:
            price_df = pd.read_sql(
                'SELECT isin, date, close FROM prices ORDER BY isin, date',
                conn, parse_dates=['date'],
            )

        db_isins = set(price_df['isin'].unique())
        config_isin_set = set(config_meta.keys())

        # --- Config vs DB sync ---
        only_config = sorted(config_isin_set - db_isins)
        only_db = sorted(db_isins - config_isin_set)
        print("=== Config vs DB ===")
        if not only_config and not only_db:
            print(f"  {len(config_isin_set)} ETFs — config and DB in sync  ✓")
        else:
            if only_config:
                print(f"  In config, missing from DB (run fetch): {', '.join(only_config)}")
            if only_db:
                print(f"  In DB, not in config (orphans):         {', '.join(only_db)}")

        # --- Per-ETF stats ---
        stats = price_df.groupby('isin').agg(
            first_date=('date', 'min'),
            last_date=('date', 'max'),
            n_days=('date', 'count'),
        ).reset_index()
        stats['span_days'] = (stats['last_date'] - stats['first_date']).dt.days
        stats['expected'] = (stats['span_days'] * 5 / 7).clip(lower=1).astype(int)
        stats['fill_rate'] = stats['n_days'] / stats['expected']
        stats['years'] = stats['n_days'] / TRADING_YEAR

        wide = price_df.pivot(index='date', columns='isin', values='close')
        ann_vol = wide.pct_change().std() * np.sqrt(TRADING_YEAR)
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
        for label, mask in tiers:
            n = int(mask.sum())
            if n:
                print(f"  {label:<10}  {n:>3} ETF{'s' if n != 1 else ''}")

        # --- Issues ---
        flagged = []
        short  = stats[stats['n_days'] < TRADING_YEAR * args.min_years].sort_values('n_days')
        sparse = stats[(stats['fill_rate'] < args.min_fill) &
                       (stats['n_days'] >= TRADING_YEAR * args.min_years)].sort_values('fill_rate')
        cash   = stats[(stats['ann_vol'] < args.max_vol) &
                       (~stats['isin'].isin(short['isin']))].sort_values('ann_vol')

        print()
        print("=== Issues ===")
        if not short.empty:
            print(f"Short history (< {args.min_years:.0f}yr):")
            for _, r in short.iterrows():
                print(f"  {r['isin']}  {r['name']:<35}  "
                      f"{int(r['n_days']):>4} days  from {r['first_date'].date()}")
                flagged.append(r['isin'])

        if not sparse.empty:
            print(f"Sparse data (fill < {args.min_fill:.0%}):")
            for _, r in sparse.iterrows():
                print(f"  {r['isin']}  {r['name']:<35}  "
                      f"{int(r['n_days'])}/{int(r['expected'])} days  ({r['fill_rate']:.0%})")
                flagged.append(r['isin'])

        if not cash.empty:
            print(f"Cash-like (ann vol < {args.max_vol:.0%}):")
            for _, r in cash.iterrows():
                print(f"  {r['isin']}  {r['name']:<35}  vol {r['ann_vol']:.2%}")
                flagged.append(r['isin'])

        if not flagged:
            print("  None — all ETFs look good.")

        return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
