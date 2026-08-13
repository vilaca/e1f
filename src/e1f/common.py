"""Shared primitives for the e1f ETF tooling.

Holds the pieces used by more than one command: default paths, the ETF
definition dataclass, the OpenFIGI resolver, and the YAML config manager.
The ``config`` command writes the universe; the ``fetch`` command reads it
back.
"""

import logging
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Callable, List, Optional, Tuple

import requests
import yaml

logger = logging.getLogger(__name__)

# Resolve default inputs/outputs against the project root (two levels above this
# package: src/e1f/common.py -> repo root), so an editable install works
# from any cwd. The --config / --db / --currency-meta flags remain the overrides.
_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = str(_ROOT / "config" / "etf_universe.yaml")
DEFAULT_DB = str(_ROOT / "data" / "e1f.db")
DEFAULT_CURRENCY_META = str(_ROOT / "data" / "currency_metadata.yaml")  # pinned ftgo resolution per ISIN
DEFAULT_START_DATE = "2000-01-01"  # earlier than any UCITS ETF; sources return from inception


def _retry_after_seconds(response) -> Optional[float]:
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
                         - datetime.now(timezone.utc)).total_seconds())
    except (TypeError, ValueError):
        return None


def call_with_retry(
    description: str,
    func: Callable[[], Any],
    *,
    retries: int = 3,
    base_delay: float = 2.0,
    max_delay: float = 300.0,
    is_retryable: Optional[Callable[[Exception], bool]] = None,
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
            response = getattr(e, 'response', None)
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
    tickers: List[str]
    exchange: str = ""
    figi: str = ""

    @classmethod
    def from_config(cls, isin: str, data: dict) -> "ETFDefinition":
        return cls(
            isin=isin,
            name=data.get('name', isin),
            tickers=data.get('tickers', []),
            exchange=data.get('exchange', ''),
            figi=data.get('figi', '')
        )


class OpenFIGIResolver:
    """Resolve ISIN to ETF metadata using OpenFIGI API."""

    BASE_URL = "https://api.openfigi.com/v3/mapping"

    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key
        self.headers = {}
        if api_key:
            self.headers['X-OPENFIGI-APIKEY'] = api_key
        self.session = requests.Session()

    def resolve(self, isin: str) -> Optional[dict]:
        """Resolve ISIN to ETF metadata."""
        if not re.match(r'^[A-Z]{2}[A-Z0-9]{10}$', isin):
            print(f"✗ Invalid ISIN: {isin}")
            return None

        payload = [{"idType": "ID_ISIN", "idValue": isin}]

        def _post():
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
            data = response.json()

            if not data or not data[0].get('data'):
                print(f"✗ No data found for ISIN: {isin}")
                return None

            result = data[0]['data'][0]

            return {
                'name': result.get('name', f"ETF {isin}"),
                'tickers': [result.get('ticker')] if result.get('ticker') else [],
                'exchange': result.get('exchCode', ''),
                'figi': result.get('figi', ''),
                'resolved_at': datetime.now().isoformat(),
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
        self.resolver = OpenFIGIResolver()
        self.config = self._load_config()

    def _load_config(self) -> dict:
        if os.path.exists(self.config_path):
            with open(self.config_path, 'r') as f:
                return yaml.safe_load(f) or {'etfs': {}}
        return {'etfs': {}}

    def _save_config(self):
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

    def list(self) -> List[Tuple[str, dict]]:
        """List all ETFs in config."""
        return sorted(self.config.get('etfs', {}).items())

    def get(self, isin: str) -> Optional[dict]:
        """Get ETF config by ISIN."""
        return self.config.get('etfs', {}).get(isin)

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
