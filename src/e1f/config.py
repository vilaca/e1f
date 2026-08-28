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
import copy
import os
import sqlite3
import sys
from contextlib import closing

from e1f.common import (
    BASE_CURRENCY,
    DEFAULT_CONFIG,
    DEFAULT_CURRENCY_META,
    DEFAULT_DB,
    ConfigManager,
    CurrencyMetadata,
    live_isins_among,
)


def _connect_prices_database(db_path: str) -> sqlite3.Connection | None:
    """Open an existing prices database, or return None when it is absent."""
    if not os.path.exists(db_path):
        return None
    conn = sqlite3.connect(db_path)
    if conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='prices'"
    ).fetchone() is None:
        conn.close()
        return None
    return conn


def _db_has_prices(db_path: str) -> bool:
    """True when the SQLite DB exists and contains the prices table."""
    conn = _connect_prices_database(db_path)
    if conn is None:
        return False
    conn.close()
    return True


def _live_holdings_blocked(
    db_path: str,
    candidates: set[str] | list[str],
    *,
    force: bool,
    operation: str,
) -> bool:
    """Disclose live candidates and return whether a destructive operation must stop."""
    live = sorted(live_isins_among(db_path, candidates))
    if not live:
        return False
    if not force:
        print(
            f"✗ Refusing to {operation} live holding(s): "
            + ", ".join(live)
            + ". Sell them first or pass --force."
        )
        print("Transaction history is retained; forced removal makes valuation unavailable.")
        return True
    action = "removal" if operation == "remove" else operation
    print(
        f"⚠ forcing {action} of live holding(s): "
        + ", ".join(live)
        + "; transaction history will remain"
    )
    return False


def _build_parser() -> argparse.ArgumentParser:
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
    remove_parser.add_argument(
        '--force',
        '-f',
        action='store_true',
        help='Remove even when transactions still have a live position',
    )

    trim_parser = subparsers.add_parser(
        'trim',
        help='Remove ISINs not present in both config and DB (keeps intersection)',
    )
    trim_parser.add_argument('--db', default=DEFAULT_DB, help='SQLite DB path')
    trim_parser.add_argument('--currency-meta', default=DEFAULT_CURRENCY_META,
                             help='Currency metadata YAML path')
    trim_parser.add_argument(
        '--force',
        '-f',
        action='store_true',
        help='Trim even when transactions still have a live position',
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
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

        print(
            f"\n{'ISIN':<14} {'Name':<50} {'Asset class':<12} "
            f"{'Ticker':<12} {'Exchange'}"
        )
        print("-" * 103)
        for isin, data in etfs:
            name = data.get('name', 'Unknown')[:48]
            asset_class = str(data.get('asset_class') or '')[:12]
            ticker = data.get('tickers', [''])[0] if data.get('tickers') else ''
            exchange = data.get('exchange', '')
            print(
                f"{isin:<14} {name:<50} {asset_class:<12} "
                f"{ticker:<12} {exchange}"
            )
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
        if _live_holdings_blocked(
            args.db,
            args.isins,
            force=args.force,
            operation="remove",
        ):
            return 1
        cm = ConfigManager(args.config)
        curr_meta = CurrencyMetadata.load(args.currency_meta)
        original_config = copy.deepcopy(cm.config)
        original_meta = copy.deepcopy(curr_meta)
        conn = _connect_prices_database(args.db)

        try:
            for isin in args.isins:
                removed_any = False

                if isin in cm.config.get('etfs', {}):
                    del cm.config['etfs'][isin]
                    removed_any = True
                    print(f"{isin}: removed from config")

                if isin in curr_meta.funds:
                    del curr_meta.funds[isin]
                    removed_any = True
                    print(f"{isin}: removed from currency metadata")

                if conn is not None:
                    n = conn.execute("DELETE FROM prices WHERE isin = ?", (isin,)).rowcount
                    if n:
                        removed_any = True
                        print(f"{isin}: removed {n} rows from DB")

                if not removed_any:
                    print(f"{isin}: not found in any file")

            cm._save_config()
            curr_meta.save(args.currency_meta)
            if conn is not None:
                conn.commit()
        except Exception:
            if conn is not None:
                conn.rollback()
            cm.config = original_config
            cm._save_config()
            original_meta.save(args.currency_meta)
            raise
        finally:
            if conn is not None:
                conn.close()
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

        curr_meta = CurrencyMetadata.load(args.currency_meta)
        curr_isins = set(curr_meta.funds)

        kept = config_isins & db_isins & curr_isins
        all_isins = config_isins | db_isins | curr_isins

        if kept == all_isins:
            print("Nothing to trim — all three files are already in sync.")
            return 0

        to_remove_config = sorted(config_isins - kept)
        to_remove_db = sorted(db_isins - kept)
        to_remove_curr = sorted(curr_isins - kept)
        if _live_holdings_blocked(
            args.db,
            all_isins - kept,
            force=args.force,
            operation="trim",
        ):
            return 1
        original_config = copy.deepcopy(cm.config)
        original_meta = copy.deepcopy(curr_meta)

        if to_remove_config:
            print(f"Removing from config:            {', '.join(to_remove_config)}")
            for isin in to_remove_config:
                del cm.config['etfs'][isin]

        needed_quotes = {
            v['currency'] for k, v in curr_meta.funds.items()
            if k in kept and isinstance(v, dict)
            and v.get('currency') and v['currency'] != BASE_CURRENCY
        }
        needed_pairs = {f"{BASE_CURRENCY}{quote}" for quote in needed_quotes}
        curr_trimmed = CurrencyMetadata(
            funds={key: value for key, value in curr_meta.funds.items() if key in kept},
            fx_pairs={
                pair: value for pair, value in curr_meta.fx_pairs.items() if pair in needed_pairs
            },
        )
        if to_remove_curr:
            print(f"Removing from currency metadata: {', '.join(to_remove_curr)}")

        with closing(sqlite3.connect(args.db)) as conn:
            try:
                if to_remove_db:
                    print(f"Removing from DB:                {', '.join(to_remove_db)}")
                    conn.executemany(
                        "DELETE FROM prices WHERE isin = ?",
                        [(isin,) for isin in to_remove_db],
                    )

                has_snap = conn.execute(
                    "SELECT 1 FROM sqlite_master "
                    "WHERE type='table' AND name='holdings_snapshot'"
                ).fetchone()
                if has_snap and to_remove_db:
                    snap_ids = [
                        row[0]
                        for isin in to_remove_db
                        for row in conn.execute(
                            "SELECT id FROM holdings_snapshot WHERE fund_id = ?", (isin,)
                        )
                    ]
                    if snap_ids:
                        print(f"Removing from holdings_snapshot: {', '.join(to_remove_db)}")
                        conn.executemany(
                            "DELETE FROM holding WHERE snapshot_id = ?",
                            [(snapshot_id,) for snapshot_id in snap_ids],
                        )
                        conn.executemany(
                            "DELETE FROM holdings_snapshot WHERE id = ?",
                            [(snapshot_id,) for snapshot_id in snap_ids],
                        )

                has_alias = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='security_alias'"
                ).fetchone()
                has_holding = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='holding'"
                ).fetchone()
                if has_alias and has_holding:
                    removed_aliases = conn.execute(
                        "DELETE FROM security_alias WHERE raw_name NOT IN "
                        "(SELECT DISTINCT raw_name FROM holding)"
                    ).rowcount
                    if removed_aliases:
                        print(
                            "Removing from security_alias:    "
                            f"{removed_aliases} orphaned alias(es)"
                        )

                has_fx = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='fx_rates'"
                ).fetchone()
                fx_pairs = (
                    {
                        (row[0], row[1])
                        for row in conn.execute("SELECT DISTINCT base, quote FROM fx_rates")
                    }
                    if has_fx
                    else set()
                )
                stale_fx = sorted(fx_pairs - {(BASE_CURRENCY, quote) for quote in needed_quotes})
                if stale_fx:
                    pairs_str = ', '.join(f"{base}{quote}" for base, quote in stale_fx)
                    print(f"Removing from fx_rates:          {pairs_str}")
                    conn.executemany(
                        "DELETE FROM fx_rates WHERE base = ? AND quote = ?", stale_fx
                    )

                cm._save_config()
                curr_trimmed.save(args.currency_meta)
                conn.commit()
            except Exception:
                conn.rollback()
                cm.config = original_config
                cm._save_config()
                original_meta.save(args.currency_meta)
                raise

        print(f"Done — {len(kept)} ISINs kept.")
        return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
