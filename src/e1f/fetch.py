#!/usr/bin/env python
"""e1f data fetcher — populate the SQLite price DB for the ETF universe.

Usage:
    e1f fetch                 # fetch all ETFs in the config
    e1f fetch IE00BM67HK77    # fetch a single ISIN
    e1f fetch --force         # ignore the cache and re-download
    e1f fetch IE00BM67HK77 --replace  # atomically replace one stored series
    e1f fetch --fallback      # fall back to yfinance when ftgo has no data

Prices are sourced from ftgo (FT Markets), with an optional yfinance fallback
(enabled via --fallback), and stored in a SQLite DB.
"""

import argparse
import logging
import os
import sqlite3
import sys
import time
import warnings
from collections.abc import Mapping
from contextlib import closing
from typing import Any, cast

import pandas as pd
import requests
import yaml
import yfinance as yf
from ftgo import get_historical_prices, get_xid

from e1f.common import (
    DEFAULT_CONFIG,
    DEFAULT_CURRENCY_META,
    DEFAULT_DB,
    DEFAULT_START_DATE,
    ConfigManager,
    ETFDefinition,
    call_with_retry,
    fund_currency_from_name,
)

logger = logging.getLogger(__name__)


class DataExtractor:
    """Fetch historical ETF prices and persist them to SQLite."""

    def __init__(
        self,
        config_path: str = DEFAULT_CONFIG,
        db_path: str = DEFAULT_DB,
        start_date: str = DEFAULT_START_DATE,
        end_date: str | None = None,
        force_refresh: bool = False,
        replace: bool = False,
        allow_shrink: bool = False,
        fallback: bool = False,
        currency_meta_path: str = DEFAULT_CURRENCY_META
    ):
        self.config_path = config_path
        self.db_path = db_path
        self.start_date = pd.to_datetime(start_date)
        self.end_date = pd.to_datetime(end_date) if end_date else pd.Timestamp.now()
        self.force_refresh = force_refresh
        self.replace = replace
        self.allow_shrink = allow_shrink
        self.fallback = fallback

        # Load config
        self.config_manager = ConfigManager(config_path)
        self.etf_universe = self._load_universe()

        # Pinned ftgo resolution (isin -> {xid, symbol, currency}), so the
        # security we fetch can't drift as FT Markets search ordering changes.
        self.currency_meta_path = currency_meta_path
        self._ftgo_meta = self._load_currency_meta()

        self._init_database()

    @staticmethod
    def _summary(isin: str, name: str, source: str, df: pd.DataFrame,
                 ticker: str | None = None) -> str:
        """One-line result: ISIN, name, ticker, count, source and date range."""
        span = f"{df.index.min():%Y-%m-%d} to {df.index.max():%Y-%m-%d}"
        tag = f" ({ticker})" if ticker else ""
        return f"{isin} {name}{tag} — {len(df)} days - {source} - {span}"

    def _load_universe(self) -> dict[str, ETFDefinition]:
        """Load ETF universe from config."""
        config = self.config_manager.config
        universe = {}
        for isin, data in config.get('etfs', {}).items():
            if not data.get('tickers'):
                continue
            universe[isin] = ETFDefinition.from_config(isin, data)
        return universe

    def _load_currency_meta(self) -> dict[str, Any]:
        if os.path.exists(self.currency_meta_path):
            with open(self.currency_meta_path) as f:
                return yaml.safe_load(f) or {}
        return {}

    def _save_currency_meta(self) -> None:
        with open(self.currency_meta_path, 'w') as f:
            yaml.dump(self._ftgo_meta, f, default_flow_style=False, sort_keys=True)

    @staticmethod
    def _symbol_currency(symbol: str) -> str:
        """ftgo symbols look like "CSPX:LSE:USD"; currency is the last part."""
        parts = symbol.split(':')
        return parts[-1] if len(parts) >= 3 else ''

    def _resolve_ftgo(self, isin: str) -> dict[str, str]:
        """Resolve an ISIN to a pinned ftgo security {xid, symbol, currency}.

        Searches ftgo by ISIN (precise). Prefers the listing quoted in the
        fund's own share-class currency (from its name) so prices are the true
        NAV currency, not a venue FX overlay; falls back to the first match.
        The result is pinned and reused so the security can't drift as FT
        Markets search ordering changes. Raises ValueError if nothing matches.
        """
        if isin in self._ftgo_meta:
            return cast(dict[str, str], self._ftgo_meta[isin])

        matches = get_xid(isin, display_mode="all")  # raises if no matches
        currencies = matches['symbol'].map(self._symbol_currency)
        resolved = matches[currencies != '']
        if resolved.empty:
            raise ValueError(f"No currency-qualified ftgo match for {isin}")
        base = fund_currency_from_name(str(matches.iloc[0].get('name', '')))
        preferred = resolved[resolved['symbol'].map(self._symbol_currency) == base] \
            if base else matches.iloc[0:0]
        row = preferred.iloc[0] if not preferred.empty else resolved.iloc[0]

        symbol = str(row['symbol'])
        resolved = {'xid': str(row['xid']), 'symbol': symbol,
                    'currency': self._symbol_currency(symbol)}
        self._ftgo_meta[isin] = resolved
        self._save_currency_meta()
        logger.info(f"pinned ftgo resolution {isin} -> {symbol} (xid {resolved['xid']})")
        return resolved

    def _init_database(self) -> None:
        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS prices (
                    isin TEXT,
                    date TEXT,
                    close REAL,
                    PRIMARY KEY (isin, date)
                )
            """)
            conn.commit()

    @staticmethod
    def _read_series(conn: sqlite3.Connection, isin: str) -> pd.DataFrame:
        """Date-indexed stored close series for an ISIN.

        Parses dates per-value (format='mixed') so a date-only row next to a
        'YYYY-MM-DD HH:MM:SS' one resolves correctly, and drops rows whose date
        can't be parsed so a corrupt row can't skew cache-freshness math or crash
        strftime downstream. Repairing such rows is what `fetch --replace` is for.
        """
        df = pd.read_sql_query(
            "SELECT date, close FROM prices WHERE isin = ? ORDER BY date",
            conn,
            params=(isin,),
            index_col='date',
        )
        df.index = pd.to_datetime(df.index, format='mixed', errors='coerce')
        return df[df.index.notna()]

    def _is_cached(self, isin: str) -> tuple[bool, pd.DataFrame | None]:
        if self.force_refresh or self.replace:
            return False, None

        with closing(sqlite3.connect(self.db_path)) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM prices WHERE isin = ?", (isin,))
            count = cursor.fetchone()[0]

            if count == 0:
                return False, None

            df = self._read_series(conn, isin)

            if not df.empty:
                latest = df.index.max()
                days_behind = (self.end_date - latest).days
                # Refetch whenever we're not current; the DO NOTHING upsert
                # cheaply adds only the missing days.
                if days_behind > 0:
                    return False, df

            return True, df

    @staticmethod
    def _price_rows(isin: str, df: pd.DataFrame) -> list[tuple[str, str, float]]:
        """(isin, 'YYYY-MM-DD', close) rows; flattens yfinance's MultiIndex too."""
        prices = df[['Close']].copy()
        prices.columns = ['close']
        prices.index = pd.to_datetime(prices.index).strftime('%Y-%m-%d')
        return [(isin, str(date), float(close)) for date, close in prices['close'].items()]

    def _save_prices(self, isin: str, df: pd.DataFrame) -> None:
        rows = self._price_rows(isin, df)

        # By default keep already-stored closes and only add new dates; --force
        # overwrites existing rows with the freshly fetched values.
        on_conflict = (
            "DO UPDATE SET close = excluded.close" if self.force_refresh else "DO NOTHING"
        )
        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.executemany(
                "INSERT INTO prices (isin, date, close) VALUES (?, ?, ?) "
                f"ON CONFLICT(isin, date) {on_conflict}",
                rows,
            )
            conn.commit()

    def _replace_prices(self, isin: str, df: pd.DataFrame) -> None:
        """Atomically replace one ISIN's stored series after a successful fetch.

        Refuses to drop any stored date (a hallmark of a truncated upstream
        response) unless --allow-shrink is set, so a partial fetch can't silently
        wipe good history. The fetched series may add or overwrite dates, but by
        default must be a superset of what is stored — which also catches shorter
        ranges, narrower windows, and interior holes.
        """
        rows = self._price_rows(isin, df)

        with closing(sqlite3.connect(self.db_path)) as conn:
            if not self.allow_shrink:
                # Only *parseable* stored dates must be preserved. Unparseable or
                # NULL rows are corruption that --replace exists to clean out, so
                # they don't count against the fetched series' coverage (and can't
                # leak a literal 'None' into the message). Dates may carry a time
                # component; parse per-value and compare date parts only.
                stored_raw = [
                    row[0] for row in conn.execute(
                        "SELECT date FROM prices WHERE isin = ?", (isin,)
                    )
                ]
                stored_dates = {
                    d.strftime('%Y-%m-%d')
                    for d in pd.to_datetime(stored_raw, format='mixed', errors='coerce')
                    if not pd.isna(d)
                }
                new_dates = {str(date) for _, date, _ in rows}
                dropped = sorted(stored_dates - new_dates)
                if dropped:
                    raise RuntimeError(
                        f"Refusing to replace {isin}: fetched series does not "
                        f"cover {len(dropped)} valid stored date(s) (e.g. "
                        f"{dropped[0]}) — either a truncated response or stored "
                        f"rows that no longer exist upstream. Re-run with "
                        f"--allow-shrink to replace anyway."
                    )
            conn.execute("DELETE FROM prices WHERE isin = ?", (isin,))
            # Collapse any duplicate dates in the fetched batch (e.g. two intraday
            # timestamps that strftime to the same day) instead of tripping the
            # UNIQUE(isin, date) constraint, matching _save_prices.
            conn.executemany(
                "INSERT INTO prices (isin, date, close) VALUES (?, ?, ?) "
                "ON CONFLICT(isin, date) DO UPDATE SET close = excluded.close",
                rows,
            )
            conn.commit()

    def _fetch_ftgo(self, isin: str, start: pd.Timestamp | None = None) -> pd.DataFrame | None:
        start = start if start is not None else self.start_date
        try:
            xid = call_with_retry(
                f"ftgo resolve {isin}", lambda: self._resolve_ftgo(isin)
            )['xid']
            df = call_with_retry(
                f"ftgo prices {isin}",
                lambda: get_historical_prices(
                    xid,
                    start.strftime("%d%m%Y"),
                    self.end_date.strftime("%d%m%Y")
                ),
            )

            if df is not None and not df.empty:
                df = df.rename(columns={'date': 'Date', 'close': 'Close'})
                df['Date'] = pd.to_datetime(df['Date'])
                df = df.set_index('Date')
                return cast(pd.DataFrame, df[['Close']])
        except ValueError as e:
            # get_xid raises this when the ISIN isn't on FT Markets; fall back
            # to yfinance rather than aborting. Other ValueErrors propagate.
            if "No data found" not in str(e):
                raise
            logger.info(f"ftgo has no data for {isin}, falling back")
        except requests.RequestException as e:
            logger.warning(f"ftgo request failed for {isin} after retries: {e}")
        return None

    @staticmethod
    def _yf_rate_limited(e: Exception) -> bool:
        """yfinance raises non-requests errors for rate limits; match by text."""
        return '429' in str(e) or 'rate limit' in str(e).lower()

    def _fetch_yfinance(
        self, ticker: str, start: pd.Timestamp | None = None
    ) -> tuple[pd.DataFrame, str] | None:
        start = start if start is not None else self.start_date
        try:
            tickers_to_try = [ticker]
            if not any(ticker.endswith(suffix) for suffix in ['.L', '.DE']):
                for suffix in ['.L', '.DE']:
                    tickers_to_try.append(ticker + suffix)

            for t in tickers_to_try:
                with warnings.catch_warnings():
                    warnings.simplefilter('ignore')
                    df = call_with_retry(
                        f"yfinance {t}",
                        lambda t=t: yf.download(t, start=start, end=self.end_date, progress=False),  # type: ignore[misc]
                        retries=2,
                        is_retryable=self._yf_rate_limited,
                    )
                if df is not None and not df.empty:
                    if t != ticker:
                        logger.info(f"yfinance fallback ok: {ticker} -> {t}")
                    return df[['Close']], t
        except Exception as e:  # noqa: BLE001 — yfinance raises arbitrary exception types
            logger.warning(f"yfinance failed for {ticker} after retries: {e}")
        return None

    def _stored_series(self, isin: str) -> pd.DataFrame:
        """Full stored close series for an ISIN (date-indexed, 'close' column)."""
        with closing(sqlite3.connect(self.db_path)) as conn:
            return self._read_series(conn, isin)

    def fetch(self, isin: str | None = None) -> pd.DataFrame:
        """Fetch data for specific ISIN or all ETFs and persist to the DB."""
        # replace is destructive (delete-then-insert per ISIN); never let it run
        # across the whole universe. Enforced here, not just in the CLI, so
        # library callers can't wipe every series with a bare fetch().
        if self.replace and not isin:
            raise ValueError("replace requires a single ISIN")

        etfs: Mapping[str, ETFDefinition | None]
        if isin:
            etfs = {isin: self.etf_universe.get(isin)}
            if not etfs[isin]:
                raise ValueError(f"ISIN {isin} not in config")
        else:
            etfs = self.etf_universe

        data_dict = {}
        for etf_isin, etf in etfs.items():
            if not etf:
                continue

            cached, existing = self._is_cached(etf_isin)
            if cached and existing is not None and not existing.empty:
                logger.info(self._summary(etf_isin, etf.name, "cache", existing))
                data_dict[etf_isin] = existing['close']
                continue

            # Incremental: only pull dates after what we already have, unless
            # --force (re-download the full range and overwrite).
            have_existing = existing is not None and not existing.empty
            since = None
            if have_existing and not self.force_refresh:
                assert existing is not None
                since = existing.index.max() + pd.Timedelta(days=1)

            fetched = None  # (source, label) on success
            # ftgo resolves by ISIN (a single, pinned security), so try it once.
            df = self._fetch_ftgo(etf_isin, since)
            if df is not None and not df.empty:
                if self.replace:
                    self._replace_prices(etf_isin, df)
                else:
                    self._save_prices(etf_isin, df)
                fetched = ("ftgo", self._ftgo_meta.get(etf_isin, {}).get('symbol'))
            elif self.fallback:
                # yfinance is ticker-based; try each configured ticker.
                for i, ticker in enumerate(etf.tickers):
                    if i > 0:
                        time.sleep(0.5)
                    result = self._fetch_yfinance(ticker, since)
                    if result is not None:
                        df, actual_ticker = result
                        if self.replace:
                            self._replace_prices(etf_isin, df)
                        else:
                            self._save_prices(etf_isin, df)
                        fetched = ("yfinance", actual_ticker)
                        break

            if fetched:
                source, ticker = fetched
                full = self._stored_series(etf_isin)
                logger.info(self._summary(etf_isin, etf.name, source, full, ticker))
                data_dict[etf_isin] = full['close']
            elif have_existing:
                assert existing is not None
                # Nothing new upstream; keep what's already stored.
                logger.info(self._summary(etf_isin, etf.name, "cache", existing))
                data_dict[etf_isin] = existing['close']
            else:
                logger.warning(f"✗ {etf_isin} {etf.name} — all sources failed")

        if not data_dict:
            raise RuntimeError("No data fetched")

        combined = pd.DataFrame(data_dict)
        combined = combined.sort_index().ffill().dropna()

        dt_index = cast(pd.DatetimeIndex, combined.index)
        if dt_index.tz is not None:
            combined.index = dt_index.tz_localize(None)

        return combined


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s'
    )
    # ftgo logs its own progress using the DDMMYYYY strings it requires;
    # quiet it and emit our own yyyy-mm-dd lines instead.
    logging.getLogger("ftgo").setLevel(logging.WARNING)
    # yfinance emits ERROR-level messages for each failed ticker attempt in its
    # retry loop; these are expected when falling back to .L/.DE suffixes.
    logging.getLogger("yfinance").setLevel(logging.CRITICAL)

    parser = argparse.ArgumentParser(
        prog="e1f fetch",
        description="Populate the SQLite price DB for the ETF universe",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Fetch price data for all ETFs in the config
  e1f fetch

  # Fetch a single ETF
  e1f fetch IE00BM67HK77

  # Force a re-download, ignoring the cache
  e1f fetch --force

  # Repair one ETF by atomically replacing its complete stored series.
  # Deletes the stored rows first; refuses to shrink the series (a sign of a
  # truncated response) unless --allow-shrink is also given.
  e1f fetch IE00BM67HK77 --replace
        """
    )

    parser.add_argument('isin', nargs='?', help='ISIN to fetch (all if omitted)')
    parser.add_argument('--config', '-c', default=DEFAULT_CONFIG, help='Config file path')
    parser.add_argument('--db', '-d', default=DEFAULT_DB, help='Database file path')
    parser.add_argument('--start', '-s', default=DEFAULT_START_DATE, help='Start date')
    refresh_group = parser.add_mutually_exclusive_group()
    refresh_group.add_argument('--force', '-f', action='store_true', help='Force refresh')
    refresh_group.add_argument(
        '--replace',
        action='store_true',
        help="Replace one ISIN's complete stored series after a successful fetch",
    )
    parser.add_argument(
        '--allow-shrink',
        action='store_true',
        help='With --replace, permit dropping stored dates from the series',
    )
    parser.add_argument('--fallback', action='store_true',
                        help='Fall back to yfinance when ftgo has no data')
    parser.add_argument('--currency-meta', default=DEFAULT_CURRENCY_META,
                        help='Pinned ftgo resolution / currency sidecar path')

    args = parser.parse_args(argv)

    if args.replace and not args.isin:
        print("✗ Error: --replace requires an ISIN")
        return 1

    if args.allow_shrink and not args.replace:
        print("✗ Error: --allow-shrink only applies with --replace")
        return 1

    try:
        extractor = DataExtractor(
            config_path=args.config,
            db_path=args.db,
            start_date=args.start,
            force_refresh=args.force,
            replace=args.replace,
            allow_shrink=args.allow_shrink,
            fallback=args.fallback,
            currency_meta_path=args.currency_meta
        )
        prices = extractor.fetch(args.isin)
        logger.info(
            f"Fetched {len(prices.columns)} ETFs, {len(prices)} observations "
            f"({prices.index.min():%Y-%m-%d} to {prices.index.max():%Y-%m-%d})"
        )
    except Exception as e:  # noqa: BLE001 — CLI top-level; all errors become exit code 1
        print(f"✗ Error: {e}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
