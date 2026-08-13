#!/usr/bin/env python
"""e1f data fetcher — populate the SQLite price DB for the ETF universe.

Usage:
    e1f fetch                 # fetch all ETFs in the config
    e1f fetch IE00BM67HK77    # fetch a single ISIN
    e1f fetch --force         # ignore the cache and re-download

Prices are sourced from ftgo (FT Markets) with a yfinance fallback, and stored
in a SQLite DB.
"""

import argparse
import logging
import os
import re
import sqlite3
import sys
import time
import warnings
from typing import Dict, Optional, Tuple

import pandas as pd
import requests
import yaml
import yfinance as yf
from ftgo import get_xid, get_historical_prices

from e1f.common import (
    ConfigManager,
    ETFDefinition,
    DEFAULT_CONFIG,
    DEFAULT_CURRENCY_META,
    DEFAULT_DB,
    DEFAULT_START_DATE,
    call_with_retry,
)

logger = logging.getLogger(__name__)


class DataExtractor:
    """Fetch historical ETF prices and persist them to SQLite."""

    def __init__(
        self,
        config_path: str = DEFAULT_CONFIG,
        db_path: str = DEFAULT_DB,
        start_date: str = DEFAULT_START_DATE,
        end_date: Optional[str] = None,
        force_refresh: bool = False,
        currency_meta_path: str = DEFAULT_CURRENCY_META
    ):
        self.config_path = config_path
        self.db_path = db_path
        self.start_date = pd.to_datetime(start_date)
        self.end_date = pd.to_datetime(end_date) if end_date else pd.Timestamp.now()
        self.force_refresh = force_refresh

        # Load config
        self.config_manager = ConfigManager(config_path)
        self.etf_universe = self._load_universe()

        # Pinned ftgo resolution (isin -> {xid, symbol, currency}), so the
        # security we fetch can't drift as FT Markets search ordering changes.
        self.currency_meta_path = currency_meta_path
        self._ftgo_meta = self._load_currency_meta()

        # Cache
        self._data_cache = {}

        self._init_database()

    @staticmethod
    def _summary(isin: str, name: str, source: str, df: pd.DataFrame,
                 ticker: str = None) -> str:
        """One-line result: ISIN, name, ticker, count, source and date range."""
        span = f"{df.index.min():%Y-%m-%d} to {df.index.max():%Y-%m-%d}"
        tag = f" ({ticker})" if ticker else ""
        return f"{isin} {name}{tag} — {len(df)} days - {source} - {span}"

    def _load_universe(self) -> Dict[str, ETFDefinition]:
        """Load ETF universe from config."""
        config = self.config_manager.config
        universe = {}
        for isin, data in config.get('etfs', {}).items():
            if not data.get('tickers'):
                continue
            universe[isin] = ETFDefinition.from_config(isin, data)
        return universe

    def _load_currency_meta(self) -> dict:
        if os.path.exists(self.currency_meta_path):
            with open(self.currency_meta_path) as f:
                return yaml.safe_load(f) or {}
        return {}

    def _save_currency_meta(self):
        with open(self.currency_meta_path, 'w') as f:
            yaml.dump(self._ftgo_meta, f, default_flow_style=False, sort_keys=True)

    # Quote currencies we might see in ftgo symbols / fund names.
    _KNOWN_CCYS = {'USD', 'EUR', 'GBP', 'GBX', 'CHF', 'JPY', 'CAD',
                   'AUD', 'SEK', 'NOK', 'DKK', 'HKD', 'SGD'}

    @classmethod
    def _base_currency(cls, name: str) -> Optional[str]:
        """The fund's share-class currency, parsed from its name.

        e.g. "iShares Core S&P 500 UCITS ETF USD (Acc)" -> "USD".
        """
        for tok in reversed(re.findall(r'\b[A-Z]{3}\b', name or '')):
            if tok in cls._KNOWN_CCYS:
                return tok
        return None

    @staticmethod
    def _symbol_currency(symbol: str) -> str:
        """ftgo symbols look like "CSPX:LSE:USD"; currency is the last part."""
        return symbol.split(':')[-1] if ':' in symbol else ''

    def _resolve_ftgo(self, isin: str) -> dict:
        """Resolve an ISIN to a pinned ftgo security {xid, symbol, currency}.

        Searches ftgo by ISIN (precise). Prefers the listing quoted in the
        fund's own share-class currency (from its name) so prices are the true
        NAV currency, not a venue FX overlay; falls back to the first match.
        The result is pinned and reused so the security can't drift as FT
        Markets search ordering changes. Raises ValueError if nothing matches.
        """
        if isin in self._ftgo_meta:
            return self._ftgo_meta[isin]

        matches = get_xid(isin, display_mode="all")  # raises if no matches
        base = self._base_currency(matches.iloc[0].get('name', ''))
        preferred = matches[matches['symbol'].map(self._symbol_currency) == base] \
            if base else matches.iloc[0:0]
        row = preferred.iloc[0] if not preferred.empty else matches.iloc[0]

        symbol = str(row['symbol'])
        resolved = {'xid': str(row['xid']), 'symbol': symbol,
                    'currency': self._symbol_currency(symbol)}
        self._ftgo_meta[isin] = resolved
        self._save_currency_meta()
        logger.info(f"pinned ftgo resolution {isin} -> {symbol} (xid {resolved['xid']})")
        return resolved

    def _init_database(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS prices (
                    isin TEXT,
                    date TEXT,
                    close REAL,
                    PRIMARY KEY (isin, date)
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_prices_isin_date
                ON prices (isin, date)
            """)
            conn.commit()

    def _is_cached(self, isin: str) -> Tuple[bool, Optional[pd.DataFrame]]:
        if self.force_refresh:
            return False, None

        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM prices WHERE isin = ?", (isin,))
            count = cursor.fetchone()[0]

            if count == 0:
                return False, None

            df = pd.read_sql_query(
                "SELECT date, close FROM prices WHERE isin = ? ORDER BY date",
                conn,
                params=(isin,),
                index_col='date',
                parse_dates=['date']
            )

            if not df.empty:
                latest = df.index.max()
                days_behind = (self.end_date - latest).days
                # Refetch whenever we're not current; the DO NOTHING upsert
                # cheaply adds only the missing days.
                if days_behind > 0:
                    return False, df

            return True, df

    def _save_prices(self, isin: str, df: pd.DataFrame):
        prices = df[['Close']].copy()
        prices.columns = ['close']            # flattens yfinance's MultiIndex too
        prices.index = pd.to_datetime(prices.index).strftime('%Y-%m-%d %H:%M:%S')
        rows = [(isin, date, float(close)) for date, close in prices['close'].items()]

        # By default keep already-stored closes and only add new dates; --force
        # overwrites existing rows with the freshly fetched values.
        on_conflict = (
            "DO UPDATE SET close = excluded.close" if self.force_refresh else "DO NOTHING"
        )
        with sqlite3.connect(self.db_path) as conn:
            conn.executemany(
                "INSERT INTO prices (isin, date, close) VALUES (?, ?, ?) "
                f"ON CONFLICT(isin, date) {on_conflict}",
                rows,
            )
            conn.commit()

    def _fetch_ftgo(self, isin: str, start: Optional[pd.Timestamp] = None) -> Optional[pd.DataFrame]:
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
                return df[['Close']]
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

    def _fetch_yfinance(self, ticker: str, start: Optional[pd.Timestamp] = None) -> Optional[Tuple[pd.DataFrame, str]]:
        start = start if start is not None else self.start_date
        try:
            tickers_to_try = [ticker]
            if not any(ticker.endswith(suffix) for suffix in ['.L', '.DE']):
                for suffix in ['.L', '.DE']:
                    tickers_to_try.append(ticker + suffix)

            for t in tickers_to_try:
                df = call_with_retry(
                    f"yfinance {t}",
                    lambda: yf.download(t, start=start, end=self.end_date, progress=False),
                    retries=2,
                    is_retryable=self._yf_rate_limited,
                )
                if df is not None and not df.empty:
                    if t != ticker:
                        logger.info(f"yfinance fallback ok: {ticker} -> {t}")
                    return df[['Close']], t
        except Exception as e:
            logger.warning(f"yfinance failed for {ticker} after retries: {e}")
        return None

    def _stored_series(self, isin: str) -> pd.DataFrame:
        """Full stored close series for an ISIN (date-indexed, 'close' column)."""
        with sqlite3.connect(self.db_path) as conn:
            return pd.read_sql_query(
                "SELECT date, close FROM prices WHERE isin = ? ORDER BY date",
                conn,
                params=(isin,),
                index_col='date',
                parse_dates=['date']
            )

    def fetch(self, isin: Optional[str] = None) -> pd.DataFrame:
        """Fetch data for specific ISIN or all ETFs and persist to the DB."""
        if isin:
            etfs = {isin: self.etf_universe.get(isin)}
            if not etfs[isin]:
                raise ValueError(f"ISIN {isin} not in config")
        else:
            etfs = self.etf_universe

        data_dict = {}
        for isin, etf in etfs.items():
            if not etf:
                continue

            cached, existing = self._is_cached(isin)
            if cached and existing is not None and not existing.empty:
                logger.info(self._summary(isin, etf.name, "cache", existing))
                data_dict[isin] = existing['close']
                continue

            # Incremental: only pull dates after what we already have, unless
            # --force (re-download the full range and overwrite).
            have_existing = existing is not None and not existing.empty
            since = None
            if have_existing and not self.force_refresh:
                since = existing.index.max() + pd.Timedelta(days=1)

            fetched = None  # (source, label) on success
            # ftgo resolves by ISIN (a single, pinned security), so try it once.
            df = self._fetch_ftgo(isin, since)
            if df is not None and not df.empty:
                self._save_prices(isin, df)
                fetched = ("ftgo", self._ftgo_meta.get(isin, {}).get('symbol'))
            else:
                # yfinance is ticker-based; try each configured ticker.
                for ticker in etf.tickers:
                    result = self._fetch_yfinance(ticker, since)
                    if result is not None:
                        df, actual_ticker = result
                        self._save_prices(isin, df)
                        fetched = ("yfinance", actual_ticker)
                        break
                    time.sleep(0.5)

            if fetched:
                source, ticker = fetched
                full = self._stored_series(isin)
                logger.info(self._summary(isin, etf.name, source, full, ticker))
                data_dict[isin] = full['close']
            elif have_existing:
                # Nothing new upstream; keep what's already stored.
                logger.info(self._summary(isin, etf.name, "cache", existing))
                data_dict[isin] = existing['close']
            else:
                logger.warning(f"✗ {isin} {etf.name} — all sources failed")

        if not data_dict:
            raise RuntimeError("No data fetched")

        combined = pd.DataFrame(data_dict)
        combined = combined.sort_index().ffill().dropna()

        if combined.index.tz is not None:
            combined.index = combined.index.tz_localize(None)

        self._data_cache = data_dict
        return combined


def main(argv=None):
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s'
    )
    warnings.filterwarnings('ignore')
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
        """
    )

    parser.add_argument('isin', nargs='?', help='ISIN to fetch (all if omitted)')
    parser.add_argument('--config', '-c', default=DEFAULT_CONFIG, help='Config file path')
    parser.add_argument('--db', '-d', default=DEFAULT_DB, help='Database file path')
    parser.add_argument('--start', '-s', default=DEFAULT_START_DATE, help='Start date')
    parser.add_argument('--force', '-f', action='store_true', help='Force refresh')
    parser.add_argument('--currency-meta', default=DEFAULT_CURRENCY_META,
                        help='Pinned ftgo resolution / currency sidecar path')

    args = parser.parse_args(argv)

    try:
        extractor = DataExtractor(
            config_path=args.config,
            db_path=args.db,
            start_date=args.start,
            force_refresh=args.force,
            currency_meta_path=args.currency_meta
        )
        prices = extractor.fetch(args.isin)
        logger.info(
            f"Fetched {len(prices.columns)} ETFs, {len(prices)} observations "
            f"({prices.index.min():%Y-%m-%d} to {prices.index.max():%Y-%m-%d})"
        )
    except Exception as e:
        print(f"✗ Error: {e}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
