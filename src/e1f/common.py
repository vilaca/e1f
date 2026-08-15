"""Shared primitives for the e1f ETF tooling.

Holds the pieces used by more than one command: default paths, the ETF
definition dataclass, the OpenFIGI resolver, and the YAML config manager.
The ``config`` command writes the universe; the ``fetch`` command reads it
back.
"""

import builtins
import logging
import os
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Self

import requests
import yaml

logger = logging.getLogger(__name__)

# Resolve default inputs/outputs against the project root (two levels above this
# package: src/e1f/common.py -> repo root), so an editable install works
# from any cwd. The --config / --db / --currency-meta flags remain the overrides.
_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = str(_ROOT / "data" / "etf_universe.yaml")
DEFAULT_DB = str(_ROOT / "data" / "e1f.db")
DEFAULT_CURRENCY_META = str(_ROOT / "data" / "currency_metadata.yaml")  # pinned ftgo resolution
DEFAULT_START_DATE = "2000-01-01"  # earlier than any ETF inception; fetch returns from inception

# OpenFIGI exchCode → XTB ticker suffix (e.g. GR → WEBN.DE). Used when indexing
# multi-listing ISINs for broker ingest and when building the XTB ticker map.
XTB_EXCHANGE_SUFFIX = {
    "LN": "UK",
    "L": "UK",
    "GR": "DE",
    "NA": "DE",
    "XETRA": "DE",
    "FR": "FR",
    "PA": "FR",
    "US": "US",
}


def _retry_after_seconds(response: requests.Response | None) -> float | None:
    """Parse a Retry-After header (delay-seconds or HTTP-date)."""
    if response is None:
        return None
    value = (response.headers.get('Retry-After') or '').strip()
    if not value:
        return None
    if value.isdigit():
        return float(value)
    try:
        return max(0.0, (parsedate_to_datetime(value)
                         - datetime.now(UTC)).total_seconds())
    except (TypeError, ValueError):
        return None


def call_with_retry(
    description: str,
    func: Callable[[], Any],
    *,
    retries: int = 3,
    base_delay: float = 2.0,
    max_delay: float = 300.0,
    is_retryable: Callable[[Exception], bool] | None = None,
) -> Any:
    """Call func(), retrying transient failures with backoff.

    Retryable by default: HTTP 429, HTTP 5xx, and requests connection
    errors. `is_retryable` extends this for libraries that raise
    non-requests exceptions (e.g. yfinance). When the server sends a
    Retry-After header it is honored; otherwise the wait grows
    exponentially (base_delay * 2**attempt, capped at max_delay).
    """
    for attempt in range(retries + 1):
        try:
            return func()
        except Exception as e:
            response: requests.Response | None = getattr(e, 'response', None)
            status = getattr(response, 'status_code', None)
            retryable = (
                status == 429
                or (status is not None and 500 <= status < 600)
                or (isinstance(e, requests.RequestException) and status is None)
                or (is_retryable is not None and is_retryable(e))
            )
            if not retryable or attempt == retries:
                raise
            wait = _retry_after_seconds(response)
            if wait is None:
                wait = min(max_delay, base_delay * 2 ** attempt)
            logger.info(
                f"{description}: attempt {attempt + 1}/{retries + 1} failed ({e}); "
                f"retrying in {wait:.0f}s"
            )
            time.sleep(wait)


@dataclass
class ETFDefinition:
    """Definition of an ETF from config."""
    isin: str
    name: str
    tickers: list[str]
    exchange: str = ""

    @classmethod
    def from_config(cls, isin: str, data: dict[str, Any]) -> Self:
        return cls(
            isin=isin,
            name=data.get('name', isin),
            tickers=data.get('tickers', []),
            exchange=data.get('exchange', ''),
        )


class OpenFIGIResolver:
    """Resolve ISIN to ETF metadata using OpenFIGI API."""

    BASE_URL = "https://api.openfigi.com/v3/mapping"

    def __init__(self, api_key: str | None = None):
        self.api_key = api_key or os.environ.get('OPENFIGI_API_KEY')
        self.headers: dict[str, str] = {}
        if self.api_key:
            self.headers['X-OPENFIGI-APIKEY'] = self.api_key
        self.session = requests.Session()

    def resolve(self, isin: str) -> dict[str, Any] | None:
        """Resolve ISIN to ETF metadata."""
        if not re.match(r'^[A-Z]{2}[A-Z0-9]{10}$', isin):
            print(f"✗ Invalid ISIN: {isin}")
            return None

        payload = [{"idType": "ID_ISIN", "idValue": isin}]

        def _post() -> requests.Response:
            r = self.session.post(
                self.BASE_URL,
                json=payload,
                headers=self.headers,
                timeout=10
            )
            r.raise_for_status()
            return r

        try:
            response = call_with_retry(f"OpenFIGI resolve {isin}", _post)
            data: list[dict[str, Any]] = response.json()

            listings_raw: list[dict[str, Any]] = data[0].get('data') or []
            if not listings_raw:
                print(f"✗ No data found for ISIN: {isin}")
                return None

            result: dict[str, Any] = listings_raw[0]
            listings: list[dict[str, str]] = []
            seen: set[tuple[str, str]] = set()
            tickers: list[str] = []
            for entry in listings_raw:
                ticker = str(entry.get('ticker') or '').strip().upper()
                exchange = str(entry.get('exchCode') or '').strip().upper()
                if not ticker or exchange not in XTB_EXCHANGE_SUFFIX:
                    continue
                key = (ticker, exchange)
                if key in seen:
                    continue
                seen.add(key)
                listings.append({'ticker': ticker, 'exchange': exchange})
                if ticker not in tickers:
                    tickers.append(ticker)

            if not tickers:
                primary_ticker = str(result.get('ticker') or '').strip().upper()
                tickers = [primary_ticker] if primary_ticker else []

            return {
                'name': result.get('name', f"ETF {isin}"),
                'tickers': tickers,
                'exchange': result.get('exchCode', ''),
                'figi': result.get('figi', ''),
                'listings': listings,
                'resolved_at': datetime.now(UTC).isoformat(),
                'source': 'OpenFIGI'
            }

        except requests.exceptions.RequestException as e:
            print(f"✗ API error for {isin}: {e}")
            return None
        except (KeyError, IndexError, ValueError, TypeError, AttributeError) as e:
            print(f"✗ Error parsing response for {isin}: {e}")
            return None


class ConfigManager:
    """Manage ETF configuration in YAML file."""

    def __init__(self, config_path: str = DEFAULT_CONFIG):
        self.config_path = config_path
        self._resolver: OpenFIGIResolver | None = None
        self.config = self._load_config()

    @property
    def resolver(self) -> OpenFIGIResolver:
        if self._resolver is None:
            self._resolver = OpenFIGIResolver()
        return self._resolver

    def _load_config(self) -> dict[str, Any]:
        if os.path.exists(self.config_path):
            with open(self.config_path) as f:
                return yaml.safe_load(f) or {'etfs': {}}
        return {'etfs': {}}

    def _save_config(self) -> None:
        with open(self.config_path, 'w') as f:
            yaml.dump(self.config, f, default_flow_style=False, sort_keys=False)

    def add(self, isin: str) -> bool:
        """Add ETF by ISIN (auto-resolves all fields)."""
        if isin in self.config.get('etfs', {}):
            print(f"⚠ ISIN {isin} already exists")
            return False

        print(f"🔍 Resolving {isin}...")
        info = self.resolver.resolve(isin)

        if not info:
            return False

        if 'etfs' not in self.config:
            self.config['etfs'] = {}

        self.config['etfs'][isin] = info
        self._save_config()

        print(f"✓ Added {isin}")
        print(f"  Name: {info['name']}")
        print(f"  Tickers: {', '.join(info['tickers'])}")
        print(f"  Exchange: {info['exchange']}")
        print(f"  FIGI: {info['figi']}")
        return True

    def list(self) -> builtins.list[tuple[str, dict[str, Any]]]:
        """List all ETFs in config."""
        return sorted(self.config.get('etfs', {}).items())

    def get(self, isin: str) -> dict[str, Any] | None:
        """Get ETF config by ISIN."""
        etfs: dict[str, Any] = self.config.get('etfs', {})
        return etfs.get(isin)

    def update(self, isin: str) -> bool:
        """Update ETF metadata from OpenFIGI."""
        if isin not in self.config.get('etfs', {}):
            print(f"✗ ISIN {isin} not found")
            return False

        print(f"🔍 Updating {isin}...")
        info = self.resolver.resolve(isin)

        if not info:
            return False

        self.config['etfs'][isin] = info
        self._save_config()

        print(f"✓ Updated {isin}")
        print(f"  Name: {info['name']}")
        return True
