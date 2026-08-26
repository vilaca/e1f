"""Shared primitives for the e1f ETF tooling.

Holds the pieces used by more than one command: default paths, the ETF
definition dataclass, the OpenFIGI resolver, and the YAML config manager.
The ``config`` command writes the universe; the ``fetch`` command reads it
back.
"""

import bisect
import builtins
import logging
import os
import re
import sqlite3
import time
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, date, datetime
from enum import StrEnum
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Self

import requests
import yaml
from ftgo import get_fund_stats, get_xid

logger = logging.getLogger(__name__)

# Resolve default inputs/outputs against the project root (two levels above this
# package: src/e1f/common.py -> repo root), so an editable install works
# from any cwd. The --config / --db / --currency-meta flags remain the overrides.
_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = str(_ROOT / "data" / "etf_universe.yaml")
DEFAULT_DB = str(_ROOT / "data" / "e1f.db")
DEFAULT_CURRENCY_META = str(_ROOT / "data" / "currency_metadata.yaml")  # pinned ftgo resolution
DEFAULT_SCENARIOS = str(_ROOT / "data" / "scenarios.yaml")  # named ISIN:pct baskets (ADR-0017)
DEFAULT_START_DATE = "2000-01-01"  # earlier than any ETF inception; fetch returns from inception

# Portfolio is valued in a single base currency (ADR-0010). GBX/GBp (pence) is
# not an ISO currency ftgo quotes an FX spot pair for — it needs a ÷100 GBP
# normalization first, so conversion refuses it rather than mis-scaling.
BASE_CURRENCY = "EUR"
UNSUPPORTED_FX_CURRENCIES = frozenset({"GBX", "GBp"})

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

KNOWN_FUND_CURRENCIES = frozenset(
    {"USD", "EUR", "GBP", "GBX", "CHF", "JPY", "CAD", "AUD", "SEK", "NOK", "DKK", "HKD", "SGD"}
)

_FTGO_TER_FIELDS = ("Ongoing charge", "Net expense ratio")
_JUSTETF_PROFILE_URL = "https://www.justetf.com/en/etf-profile.html?isin={isin}"


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


def fund_currency_from_name(name: str) -> str | None:
    """Share-class currency parsed from a fund or listing name."""
    upper = (name or "").upper()
    match = re.search(r"\b(USD|EUR|GBP|CHF|JPY|CAD|AUD|SEK|NOK|DKK|HKD|SGD)(A|D)\b", upper)
    if match:
        return match.group(1)
    for token in reversed(re.findall(r"\b[A-Z]{3}\b", upper)):
        if token in KNOWN_FUND_CURRENCIES:
            return str(token)
    return None


def distribution_from_name(name: str) -> str | None:
    """Accumulating vs distributing, parsed from fund naming conventions."""
    upper = (name or "").upper()
    if re.search(r"\bUSDD\b|\bEURD\b|\bGBPD\b", upper):
        return "Distributing"
    if re.search(r"\bUSDA\b|\bEURA\b|\bGBPA\b", upper):
        return "Accumulating"
    if re.search(r"\(ACC\)|\bACC\b|\bACCUMULATING\b", upper):
        return "Accumulating"
    if re.search(r"\(DIST\)|\bDIST\b|\bDISTRIBUTING\b|\bDISTRIBUTION\b", upper):
        return "Distributing"
    return None


def _asset_class_from_investment_focus(value: str) -> str | None:
    """Extract justETF's broad asset class from its detailed investment focus."""
    asset_class = (value or "").partition(",")[0].strip()
    return asset_class or None


def _parse_percent_value(value: str) -> float | None:
    text = (value or "").strip()
    if not text or text in {"--", "-"}:
        return None
    match = re.search(r"([\d.]+)\s*%", text)
    return float(match.group(1)) if match else None


def _short_lookup_error(exc: Exception) -> str:
    message = str(exc).strip()
    lower = message.lower()
    if "404" in message or "not found" in lower:
        return "quote not found"
    if "429" in message or "rate limit" in lower:
        return "rate limited"
    first_line = message.splitlines()[0]
    return first_line[:100] if len(first_line) > 100 else first_line


def _best_ftgo_name(names: list[str], hint: str) -> str:
    """Pick the FT Markets name that best matches the ISIN's OpenFIGI share-class hint."""
    hint_dist = distribution_from_name(hint)
    hint_ccy = fund_currency_from_name(hint)
    if hint_dist:
        for name in names:
            if distribution_from_name(name) == hint_dist:
                return name
    if hint_ccy:
        for name in names:
            if fund_currency_from_name(name) == hint_ccy:
                return name
    return names[0]


def _ftgo_load(isin: str) -> tuple[Any, str | None]:
    try:
        matches = get_xid(isin, display_mode="all")
        if matches is None or matches.empty:
            return None, "no FT Markets listing"
        return matches, None
    except ValueError as exc:
        if "No data found" in str(exc):
            return None, "no FT Markets listing"
        return None, _short_lookup_error(exc)
    except Exception as exc:  # noqa: BLE001 — ftgo/network failures are optional enrichment
        return None, _short_lookup_error(exc)


def _names_from_ftgo_matches(matches: Any) -> list[str]:
    names: list[str] = []
    seen: set[str] = set()
    for _, row in matches.iterrows():
        raw = row.get("name")
        if not raw:
            continue
        name = str(raw).strip()
        if name and name not in seen:
            seen.add(name)
            names.append(name)
    return names


def _ftgo_xid_for_hint(matches: Any, hint: str) -> str:
    names = _names_from_ftgo_matches(matches)
    best = _best_ftgo_name(names, hint) if names else ""
    for _, row in matches.iterrows():
        if str(row.get("name") or "").strip() == best:
            return str(row["xid"])
    return str(matches.iloc[0]["xid"])


def _ftgo_ter(matches: Any, hint: str) -> float | None:
    stats = get_fund_stats(_ftgo_xid_for_hint(matches, hint))
    for field in _FTGO_TER_FIELDS:
        if field in stats:
            ter = _parse_percent_value(str(stats[field]))
            if ter is not None:
                return ter
    return None


def _justetf_field(html: str, field: str) -> str | None:
    match = re.search(
        rf'data-testid="tl_etf-basics_value_{field}">([^<]+)',
        html,
        flags=re.IGNORECASE,
    )
    if not match:
        return None
    value = match.group(1).strip()
    return value or None


def _fetch_justetf_html(isin: str) -> str | None:
    def _get() -> str:
        response = requests.get(
            _JUSTETF_PROFILE_URL.format(isin=isin),
            timeout=15,
            headers={"User-Agent": "e1f/0.1"},
        )
        response.raise_for_status()
        return response.text

    try:
        return str(call_with_retry(f"justETF profile {isin}", _get, retries=2))
    except requests.RequestException as exc:
        logger.debug("justETF profile failed for %s: %s", isin, exc)
        return None


def _ftgo_listing_names(isin: str) -> tuple[list[str], str | None]:
    matches, error = _ftgo_load(isin)
    if error:
        return [], error
    names = _names_from_ftgo_matches(matches)
    if not names:
        return [], "FT Markets listing has no name"
    return names, None


def _fund_currency_from_names(names: list[str]) -> str | None:
    for name in names:
        currency = fund_currency_from_name(name)
        if currency:
            return currency
    return None


def _ftgo_fund_name(isin: str, hint: str = "") -> tuple[str | None, str | None]:
    names, error = _ftgo_listing_names(isin)
    if error:
        return None, error
    return _best_ftgo_name(names, hint), None


def enrich_fund_metadata(isin: str, info: dict[str, Any]) -> dict[str, Any]:
    """Augment OpenFIGI resolution with fund currency, distribution, TER, and asset class.

    justETF structured fields are the primary source for currency and distribution.
    ftgo supplies TER and is the primary source for that. Name parsing (OpenFIGI,
    then ftgo listing names) is a last resort and always triggers a warning.
    """
    openfigi_name = str(info.get("name") or "")
    fund_currency: str | None = None
    distribution: str | None = None
    ter: float | None = None
    asset_class: str | None = None

    # justETF first: structured fields for currency, distribution, asset class
    justetf_html = _fetch_justetf_html(isin)
    if justetf_html:
        raw_ccy = _justetf_field(justetf_html, "fund-currency")
        if raw_ccy in KNOWN_FUND_CURRENCIES:
            fund_currency = raw_ccy

        raw_dist = _justetf_field(justetf_html, "distribution-policy")
        if raw_dist:
            dist_lower = raw_dist.lower()
            if "accum" in dist_lower:
                distribution = "Accumulating"
            elif "distrib" in dist_lower:
                distribution = "Distributing"

        raw_focus = _justetf_field(justetf_html, "investment-focus")
        if raw_focus:
            asset_class = _asset_class_from_investment_focus(raw_focus)

    # ftgo: primary source for TER; names kept for last-resort fallback only
    matches, ftgo_error = _ftgo_load(isin)
    ftgo_names = _names_from_ftgo_matches(matches) if matches is not None else []
    ftgo_name = _best_ftgo_name(ftgo_names, openfigi_name) if ftgo_names else None
    if matches is not None:
        ter = _ftgo_ter(matches, openfigi_name)

    if ter is None and justetf_html:
        raw_ter = _justetf_field(justetf_html, "ter")
        if raw_ter:
            ter = _parse_percent_value(raw_ter)
            if ter is not None:
                print(
                    f"⚠ ter {isin}: ftgo has no expense ratio; "
                    f"used justETF ({ter:.2f}%)"
                )

    # Last resort: parse currency/distribution from listing names with a warning
    if not fund_currency:
        parsed = fund_currency_from_name(openfigi_name) or _fund_currency_from_names(ftgo_names)
        if parsed:
            fund_currency = parsed
            print(f"⚠ fund currency {isin}: justETF missing; inferred from name ({fund_currency})")

    if not distribution:
        parsed_dist = distribution_from_name(openfigi_name) or (
            distribution_from_name(ftgo_name) if ftgo_name else None
        )
        if parsed_dist:
            distribution = parsed_dist
            print(f"⚠ distribution {isin}: justETF missing; inferred from name ({distribution})")

    if ftgo_error and (fund_currency or distribution):
        print(f"⚠ ftgo {isin}: {ftgo_error}")

    if fund_currency:
        info["fund_currency"] = fund_currency
    if distribution:
        info["distribution"] = distribution
    if ter is not None:
        info["ter"] = ter
    if asset_class:
        info["asset_class"] = asset_class
    return info


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

        info = enrich_fund_metadata(isin, info)

        if 'etfs' not in self.config:
            self.config['etfs'] = {}

        self.config['etfs'][isin] = info
        self._save_config()

        print(f"✓ Added {isin}")
        print(f"  Name: {info['name']}")
        print(f"  Tickers: {', '.join(info['tickers'])}")
        print(f"  Exchange: {info['exchange']}")
        print(f"  FIGI: {info['figi']}")
        if info.get('fund_currency'):
            print(f"  Fund currency: {info['fund_currency']}")
        if info.get('distribution'):
            print(f"  Distribution: {info['distribution']}")
        if info.get('ter') is not None:
            print(f"  TER: {info['ter']:.2f}%")
        if info.get('asset_class'):
            print(f"  Asset class: {info['asset_class']}")
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

        info = enrich_fund_metadata(isin, info)

        self.config['etfs'][isin] = info
        self._save_config()

        print(f"✓ Updated {isin}")
        print(f"  Name: {info['name']}")
        if info.get('fund_currency'):
            print(f"  Fund currency: {info['fund_currency']}")
        if info.get('distribution'):
            print(f"  Distribution: {info['distribution']}")
        if info.get('ter') is not None:
            print(f"  TER: {info['ter']:.2f}%")
        if info.get('asset_class'):
            print(f"  Asset class: {info['asset_class']}")
        return True


_SHARE_EPSILON = 1e-9
_BUY_SIDES = frozenset({"BUY", "SAVINGS_PLAN"})


def load_trades(
    db_path: str,
) -> list[tuple[str, str, str, str, float, float, float]]:
    """Chronological trade rows ``(broker, datetime, symbol, side, shares, price, fee)``.

    The shared read behind holdings and performance: ordered by ``datetime`` then
    ``transaction_id`` so average-cost accounting is deterministic. Empty when the
    ``transactions`` table is absent.
    """

    with closing(sqlite3.connect(db_path)) as conn:
        if conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='transactions'"
        ).fetchone() is None:
            return []
        return conn.execute(
            """
            SELECT broker, datetime, symbol, side, shares, price, fee
            FROM transactions
            ORDER BY datetime, transaction_id
            """
        ).fetchall()


@dataclass(frozen=True)
class PositionEvent:
    """One trade's effect on a per-ISIN position, with running totals.

    ``cash_flow`` is the EUR contributed by this event (``shares * price + fee``);
    it is ``0.0`` for a SELL, since buy-and-hold return math treats cash flows as
    contributions only (ADR-0011). ``shares_held`` and ``cost_basis`` are the
    cumulative average-cost totals *after* the event.
    """

    date: str
    cash_flow: float
    shares_held: float
    cost_basis: float


def position_timeline(
    rows: list[tuple[str, str, str, str, float, float, float]],
) -> dict[str, list[PositionEvent]]:
    """Per-ISIN chronological position events, netted across brokers (ADR-0011).

    Shares are keyed on the ISIN alone (value is broker-agnostic), so contributions
    to the same fund at different brokers accumulate into one series. Share/cost
    accounting mirrors ``portfolio.compute_holdings`` average-cost, including SELL
    reduction, so the two agree on the final snapshot. Dates are the ``YYYY-MM-DD``
    prefix of the trade datetime.
    """
    running: dict[str, tuple[float, float]] = {}
    timeline: dict[str, list[PositionEvent]] = {}

    for _broker, dt, symbol, side, shares, price, fee in rows:
        qty = shares or 0.0
        if qty <= 0:
            continue
        unit_price = price or 0.0
        trade_fee = fee or 0.0
        held, cost = running.get(symbol, (0.0, 0.0))

        if side in _BUY_SIDES:
            cash_flow = qty * unit_price + trade_fee
            held += qty
            cost += cash_flow
        elif side == "SELL":
            if held <= _SHARE_EPSILON:
                continue
            sell_qty = min(qty, held)
            avg = cost / held
            held -= sell_qty
            cost -= avg * sell_qty
            cash_flow = 0.0
        else:
            continue

        running[symbol] = (held, cost)
        timeline.setdefault(symbol, []).append(
            PositionEvent(
                date=str(dt)[:10],
                cash_flow=cash_flow,
                shares_held=held,
                cost_basis=cost,
            )
        )

    return timeline


def pinned_quote_currency(
    isin: str, currency_meta_path: str = DEFAULT_CURRENCY_META
) -> str | None:
    """Currency the stored ``prices.close`` for ``isin`` is denominated in.

    Read from the pinned ftgo resolution sidecar (ADR-0002) — the only trustworthy
    statement of a stored price's currency, never ``fund_currency`` (ADR-0010).
    ``None`` when the ISIN is not pinned, so a caller can treat it as unvaluable.
    """
    if not os.path.exists(currency_meta_path):
        return None
    with open(currency_meta_path) as f:
        meta = yaml.safe_load(f) or {}
    entry = meta.get(isin)
    if not isinstance(entry, dict):
        return None
    currency = entry.get("currency")
    return str(currency) if currency else None


def portfolio_isins(db_path: str) -> frozenset[str]:
    """ISINs with a net-positive position derived from stored transactions."""

    with closing(sqlite3.connect(db_path)) as conn:
        if conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='transactions'"
        ).fetchone() is None:
            return frozenset()
        rows = conn.execute(
            "SELECT symbol, side, shares FROM transactions ORDER BY datetime, transaction_id"
        ).fetchall()

    held: dict[str, float] = {}
    for symbol, side, shares in rows:
        qty = (shares or 0.0)
        if qty <= 0:
            continue
        if side in _BUY_SIDES:
            held[symbol] = held.get(symbol, 0.0) + qty
        elif side == "SELL":
            prev = held.get(symbol, 0.0)
            held[symbol] = max(0.0, prev - qty)

    return frozenset(sym for sym, qty in held.items() if qty > _SHARE_EPSILON)


def fx_rate_asof(db_path: str, quote: str, date: str, base: str = BASE_CURRENCY) -> float:
    """Nearest-prior FX rate (``quote`` units per 1 ``base``) on or before ``date``.

    Forward-fill / nearest-prior per ADR-0010: the most recent stored rate dated
    on or before ``date``. Never interpolates and never uses a later rate, so an
    as-of valuation can't depend on future information. Returns ``1.0`` for the
    identity ``quote == base``. Raises ValueError when no rate exists on or before
    ``date`` — an unfetched pair, or a date preceding the series — so a caller can
    never silently value with a missing rate.
    """

    if quote == base:
        return 1.0

    with closing(sqlite3.connect(db_path)) as conn:
        row = conn.execute(
            "SELECT rate FROM fx_rates "
            "WHERE base = ? AND quote = ? AND date <= ? "
            "ORDER BY date DESC LIMIT 1",
            (base, quote, date),
        ).fetchone()

    if row is None:
        raise ValueError(
            f"no {base}/{quote} FX rate on or before {date} "
            f"(pair unfetched, or date precedes the series)"
        )
    return float(row[0])


def convert_to_eur(amount: float, quote: str, date: str, db_path: str) -> float:
    """Convert ``amount`` (in ``quote`` currency) to EUR using the as-of daily rate.

    Applies ``amount / rate`` where ``rate`` is quote-per-EUR (ADR-0010). Refuses
    currencies with no EUR FX rule (e.g. GBX pence) rather than mis-converting.
    """
    if quote == BASE_CURRENCY:
        return amount
    if quote in UNSUPPORTED_FX_CURRENCIES:
        raise ValueError(
            f"currency {quote} (pence) has no EUR FX rule — needs GBP "
            f"normalization; not supported (ADR-0010)"
        )
    return amount / fx_rate_asof(db_path, quote, date)


# ---------------------------------------------------------------------------
# Point-in-time EUR valuation core (ADR-0013 decision 4, graduated down from
# ``performance``). ``performance`` re-imports these for its return metrics;
# ``overlap`` consumes ``fund_eur_value`` for a held fund's ``Vf``. The move is a
# clean downward relocation — every dependency (PositionEvent, convert_to_eur,
# pinned_quote_currency, load_trades, position_timeline) already lives here.
# ---------------------------------------------------------------------------


@dataclass
class HoldingSeries:
    """Everything needed to value and measure one held ISIN as of a date."""

    isin: str
    events: list[PositionEvent]  # filtered to date <= as_of, chronological
    price_dates: list[str]       # sorted, <= as_of
    price_closes: list[float]    # parallel to price_dates, native currency
    currency: str | None


def position_asof(events: list[PositionEvent], day: str) -> tuple[float, float]:
    """Shares held and average-cost basis after the last event on or before ``day``."""
    shares, cost = 0.0, 0.0
    for event in events:
        if event.date > day:
            break
        shares, cost = event.shares_held, event.cost_basis
    return shares, cost


def price_index_asof(series: HoldingSeries, day: str) -> int:
    """Index of the nearest-prior priced day on or before ``day`` (-1 if none)."""
    return bisect.bisect_right(series.price_dates, day) - 1


def close_asof(series: HoldingSeries, day: str) -> float | None:
    """Nearest-prior close on or before ``day``; None if the day precedes history."""
    index = price_index_asof(series, day)
    return None if index < 0 else series.price_closes[index]


def price_date_asof(series: HoldingSeries, day: str) -> str | None:
    """Date of the close ``close_asof`` would use for ``day`` (None if none)."""
    index = price_index_asof(series, day)
    return None if index < 0 else series.price_dates[index]


def value_on(series: HoldingSeries, day: str, db_path: str) -> float | None:
    """EUR market value of the position on ``day``; None when it cannot be valued.

    None means: no pinned currency, no price on or before the day, or no FX rate
    on or before the day (``convert_to_eur`` raising) — every path that would
    otherwise force a silent or wrong number.
    """
    if series.currency is None:
        return None
    shares, _cost = position_asof(series.events, day)
    if shares <= _SHARE_EPSILON:
        return 0.0
    close = close_asof(series, day)
    if close is None:
        return None
    try:
        return convert_to_eur(shares * close, series.currency, day, db_path)
    except ValueError:
        return None


def load_price_series(db_path: str, isin: str, as_of: str) -> tuple[list[str], list[float]]:
    """Sorted ``(dates, closes)`` for an ISIN, deduped to one close per day, <= as_of."""
    with closing(sqlite3.connect(db_path)) as conn:
        if conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='prices'"
        ).fetchone() is None:
            return [], []
        rows = conn.execute(
            "SELECT date, close FROM prices WHERE isin = ? ORDER BY date", (isin,)
        ).fetchall()

    by_day: dict[str, float] = {}
    for raw_date, close in rows:
        day = str(raw_date)[:10]
        if close is None or day > as_of:
            continue
        by_day[day] = float(close)  # last write wins if a day repeats
    dates = sorted(by_day)
    return dates, [by_day[d] for d in dates]


def build_series(
    db_path: str,
    isin: str,
    events: list[PositionEvent],
    as_of: str,
    currency_meta_path: str,
) -> HoldingSeries:
    """A fund's price/position series as of ``as_of``, ready to value."""
    dates, closes = load_price_series(db_path, isin, as_of)
    return HoldingSeries(
        isin=isin,
        events=events,
        price_dates=dates,
        price_closes=closes,
        currency=pinned_quote_currency(isin, currency_meta_path),
    )


def fund_eur_value(
    isin: str,
    as_of: str,
    db_path: str,
    currency_meta_path: str = DEFAULT_CURRENCY_META,
) -> float | None:
    """EUR value of a held fund on ``as_of`` (``Vf`` for ADR-0013's overlap floor).

    Wraps ``load_trades → position_timeline → build_series → value_on``. Returns
    ``None`` when the fund cannot be valued — never held on/before ``as_of``, or
    ``value_on``'s own None (no pinned currency, no price, or no FX). A
    ``None``-valued fund is excluded from the overlap floor and disclosed
    (ADR-0013 decision 4), never treated as €0.
    """
    events = [
        event
        for event in position_timeline(load_trades(db_path)).get(isin, [])
        if event.date <= as_of
    ]
    if not events:
        return None
    return value_on(build_series(db_path, isin, events, as_of, currency_meta_path), as_of, db_path)



# ---------------------------------------------------------------------------
# Provenance vocabulary (ADR-0013 decision 8), graduated down from
# ``concentration``. The four-state status, the metric-contract shape, and the
# ``--explain`` rendering helpers live here so ``concentration`` and ``overlap``
# share one home; per-metric contract *instances* stay in the command modules.
# This relocates the mechanism only — it does not retrofit ``performance`` /
# ``portfolio`` onto the model (that generalization is a future ADR, 0014+).
# ---------------------------------------------------------------------------


class Status(StrEnum):
    """Four-state per-metric status — the single status vocabulary (ADR-0012 decision 7)."""

    CALCULATED = "CALCULATED"    # enough evidence for a point value
    BOUNDED = "BOUNDED"          # no exact value, but defensible math bounds exist
    UNAVAILABLE = "UNAVAILABLE"  # not enough reliable info for even a useful bound
    UNRESOLVED = "UNRESOLVED"    # identity is the blocker, not coverage (v1b)


@dataclass(frozen=True)
class MetricContract:
    """A metric's data requirements — drives method id + limited-by / not-limited-by."""

    method_version: str
    requires: tuple[str, ...]          # what, if improved, would tighten/unblock it
    does_not_require: tuple[str, ...]  # what would not help (or is refused)
    supports: tuple[str, ...]          # what the metric enables
    limitations: tuple[str, ...]       # standing caveats that travel with the figure


def _limited_by(contract: MetricContract) -> list[str]:
    limited = "; ".join(contract.requires) if contract.requires else "nothing (complete)"
    not_limited = "; ".join(contract.does_not_require) if contract.does_not_require else "—"
    lines = [f"    Limited by:     {limited}", f"    Not limited by: {not_limited}"]
    if contract.supports:
        lines.append(f"    Supports:       {'; '.join(contract.supports)}")
    if contract.limitations:
        lines.append(f"    Limitations:    {'; '.join(contract.limitations)}")
    return lines


def _explain_metric(
    title: str, status: Status, result: str, inputs: str, method: str,
    contract: MetricContract,
) -> list[str]:
    return [
        f"  {title}",
        f"    Status:         {status.value}   (method = {contract.method_version})",
        f"    Result:         {result}",
        f"    Inputs:         {inputs}",
        f"    Method:         {method}",
        *_limited_by(contract),
    ]



# ---------------------------------------------------------------------------
# Scenarios (ADR-0017): named ISIN→percent baskets persisted in one YAML file,
# managed by the `scenario` command and consumed by `rebalance` / `correlation`.
# Held here so both consumers and the manager share one on-disk shape without
# importing each other (ADR-0003).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Scenario:
    """A named basket: ISIN → target percent (of the whole book), plus an optional
    default DCA horizon in months (consumed by ``rebalance``; ignored by
    ``correlation``).  ``targets`` percents are the raw stored values in (0, 100];
    validation of the set (dupes, Σ ≤ 100) lives with the writers/consumers.
    """

    name: str
    targets: dict[str, float]
    months: int | None = None


class ScenarioError(Exception):
    """Raised for a missing scenario or a malformed scenarios file."""


def load_scenarios(path: str = DEFAULT_SCENARIOS) -> dict[str, Scenario]:
    """Load every scenario from ``path`` (missing file → empty dict)."""
    if not os.path.exists(path):
        return {}
    with open(path) as f:
        raw = yaml.safe_load(f) or {}
    entries = raw.get("scenarios") or {}
    if not isinstance(entries, dict):
        raise ScenarioError(f"{path}: 'scenarios' must be a mapping of name → definition")
    return {str(name): _parse_scenario(str(name), body, path) for name, body in entries.items()}


def _parse_scenario(name: str, body: Any, path: str) -> Scenario:
    if not isinstance(body, dict):
        raise ScenarioError(f"{path}: scenario {name!r} must be a mapping")
    targets_raw = body.get("targets")
    if not isinstance(targets_raw, dict) or not targets_raw:
        raise ScenarioError(f"{path}: scenario {name!r} needs a non-empty 'targets' mapping")
    targets: dict[str, float] = {}
    for isin, pct in targets_raw.items():
        try:
            targets[str(isin)] = float(pct)
        except (TypeError, ValueError):
            raise ScenarioError(
                f"{path}: scenario {name!r} target {isin!r} has non-numeric percent {pct!r}"
            ) from None
    months = body.get("months")
    if months is not None and not isinstance(months, int):
        raise ScenarioError(f"{path}: scenario {name!r} 'months' must be an integer")
    return Scenario(name=name, targets=targets, months=months)


def get_scenario(name: str, path: str = DEFAULT_SCENARIOS) -> Scenario:
    """Fetch one scenario by name, or raise ``ScenarioError`` listing what exists."""
    scenarios = load_scenarios(path)
    if name not in scenarios:
        known = ", ".join(sorted(scenarios)) or "(none saved)"
        raise ScenarioError(f"no scenario named {name!r} in {path} — saved: {known}")
    return scenarios[name]


def save_scenario(scenario: Scenario, path: str = DEFAULT_SCENARIOS) -> bool:
    """Upsert one scenario, preserving the others.  Returns True if it already existed."""
    scenarios = load_scenarios(path)
    existed = scenario.name in scenarios
    scenarios[scenario.name] = scenario
    _write_scenarios(scenarios, path)
    return existed


def delete_scenario(name: str, path: str = DEFAULT_SCENARIOS) -> None:
    """Remove one scenario, or raise ``ScenarioError`` if it is not present."""
    scenarios = load_scenarios(path)
    if name not in scenarios:
        known = ", ".join(sorted(scenarios)) or "(none saved)"
        raise ScenarioError(f"no scenario named {name!r} in {path} — saved: {known}")
    del scenarios[name]
    _write_scenarios(scenarios, path)


def _write_scenarios(scenarios: dict[str, Scenario], path: str) -> None:
    body = {
        "scenarios": {name: _scenario_to_yaml(scenarios[name]) for name in sorted(scenarios)}
    }
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w") as f:
        yaml.dump(body, f, default_flow_style=False, sort_keys=False)


def _scenario_to_yaml(scenario: Scenario) -> dict[str, Any]:
    entry: dict[str, Any] = {}
    if scenario.months is not None:
        entry["months"] = scenario.months
    entry["targets"] = dict(scenario.targets)
    return entry


# ---------------------------------------------------------------------------
# Buy-only rebalance core (ADR-0016): the pure-math plan and its DB-level
# valuation assembly.  Graduated into common (ADR-0017) so `correlation` can run
# on the *post-rebalance* portfolio a scenario implies without importing
# `rebalance` (ADR-0003).  The `rebalance` command re-exports these and keeps its
# own rendering.
# ---------------------------------------------------------------------------

_SHARE_EPSILON = 1e-9
_FLOAT_CLAMP = 1e-9  # clamp for binder-fund buy rounding and bound equality checks


@dataclass(frozen=True)
class RebalancePlan:
    """Result of the buy-only rebalance math (ADR-0016 decisions 3/5).

    ``feasible`` is True when a finite V'_min exists.  ``buys`` maps universe
    ISIN → EUR buy amount (0.0 for the binder(s)); covers pinned funds and
    valued untargeted funds.  Empty when infeasible.

    ``binders``: pinned ISIN(s) whose bound v_i/t_i equals V'_min (sorted).
    ``residual_bound_binds``: True when the residual bound v_rest/R also equals
    V'_min (so the whole residual bucket gets zero buys).
    """

    feasible: bool
    reason: str | None            # UNAVAILABLE reason, or None if feasible
    unvaluable_targets: list[str] # ISINs triggering target_unvaluable (sorted)
    v: float                      # Σ valued held v_f
    v_prime: float                 # V'_min (0.0 if infeasible)
    c_min: float                   # total cash injection (0.0 if infeasible)
    buys: dict[str, float]        # ISIN → EUR buy
    binders: list[str]             # sorted pinned ISINs binding V'_min
    residual_bound_binds: bool    # True if v_rest/R == V'_min


def compute_rebalance(
    targets: dict[str, float],
    values: dict[str, float | None],
    held: frozenset[str],
) -> RebalancePlan:
    """Buy-only minimum-cash rebalance (ADR-0016 decisions 3/5).

    ``targets``: ISIN → fraction in (0, 1] (already validated; non-empty; Σ ≤ 1).
    ``values``: universe ISIN → EUR value.  ``None`` = held-but-unvaluable;
    ``0.0`` = not held (open-a-position if targeted); ``>0`` = valued position.
    ``held``: ISINs with positive shares as-of (needed to distinguish held-unvaluable
    from unheld).

    Feasibility check order (first match wins, ADR-0016 decision 5):
      target_unvaluable → empty_portfolio → residual_full / residual_unallocable.
    """
    # target_unvaluable: held targeted fund with no price/FX
    unvaluable_targets = sorted(
        isin for isin in targets if isin in held and values.get(isin) is None
    )
    if unvaluable_targets:
        return RebalancePlan(
            feasible=False, reason="target_unvaluable",
            unvaluable_targets=unvaluable_targets,
            v=0.0, v_prime=0.0, c_min=0.0, buys={}, binders=[],
            residual_bound_binds=False,
        )

    # V = Σ valued held v_f (untargeted unvaluables already excluded by caller)
    v_held: dict[str, float] = {
        isin: v for isin, v in values.items()
        if v is not None and isin in held
    }
    V = sum(v_held.values())

    # empty_portfolio: no valued anchor
    if V <= 0.0:
        return RebalancePlan(
            feasible=False, reason="empty_portfolio",
            unvaluable_targets=[],
            v=0.0, v_prime=0.0, c_min=0.0, buys={}, binders=[],
            residual_bound_binds=False,
        )

    R = 1.0 - sum(targets.values())
    v_rest = sum(v for isin, v in v_held.items() if isin not in targets)

    # residual feasibility checks
    if R < _FLOAT_CLAMP and v_rest > 0.0:
        return RebalancePlan(
            feasible=False, reason="residual_full",
            unvaluable_targets=[],
            v=V, v_prime=0.0, c_min=0.0, buys={}, binders=[],
            residual_bound_binds=False,
        )
    if R >= _FLOAT_CLAMP and v_rest <= 0.0:
        return RebalancePlan(
            feasible=False, reason="residual_unallocable",
            unvaluable_targets=[],
            v=V, v_prime=0.0, c_min=0.0, buys={}, binders=[],
            residual_bound_binds=False,
        )

    # Compute V'_min = max over pin bounds [and residual bound]
    # Pin bound for fund i: v_i / t_i. v_i = 0 for unheld targets → bound = 0, never binds.
    pin_bounds: list[tuple[float, str]] = [
        (_v_for_bound(values.get(isin)) / t_i, isin) for isin, t_i in targets.items()
    ]

    v_prime = max(b for b, _ in pin_bounds)
    residual_bound: float | None = None
    if R >= _FLOAT_CLAMP and v_rest > 0.0:
        residual_bound = v_rest / R
        v_prime = max(v_prime, residual_bound)

    c_min = v_prime - V

    binders = sorted(
        isin for b, isin in pin_bounds if abs(b - v_prime) <= _FLOAT_CLAMP
    )
    residual_bound_binds = (
        residual_bound is not None and abs(residual_bound - v_prime) <= _FLOAT_CLAMP
    )

    # Per-fund buys
    buys: dict[str, float] = {}
    for isin, t_i in targets.items():
        v_i = _v_for_bound(values.get(isin))
        c_i = t_i * v_prime - v_i
        # Clamp analytically-zero negatives on the binder fund (float noise)
        if -_FLOAT_CLAMP <= c_i < 0.0:
            c_i = 0.0
        buys[isin] = c_i

    if R >= _FLOAT_CLAMP and v_rest > 0.0:
        c_rest = R * v_prime - v_rest
        for isin, v_j in v_held.items():
            if isin not in targets and v_j > 0.0:
                buys[isin] = c_rest * v_j / v_rest

    return RebalancePlan(
        feasible=True, reason=None, unvaluable_targets=[],
        v=V, v_prime=v_prime, c_min=c_min,
        buys=buys, binders=binders,
        residual_bound_binds=residual_bound_binds,
    )


def _v_for_bound(v: float | None) -> float:
    """Return the EUR value for a bound computation: None → 0.0 (unheld)."""
    return 0.0 if v is None else v


def assemble_rebalance_valuations(
    db_path: str,
    currency_meta_path: str,
    targets: dict[str, float],
    as_of: str,
) -> tuple[
    dict[str, float | None],  # values
    frozenset[str],           # held (positive shares as-of)
    list[str],                # untargeted_unvaluable (sorted)
    dict[str, str | None],    # price_dates per ISIN
]:
    """Load universe valuations using position_timeline (as-of-aware, ADR-0016 decision 7).

    Seeds the held set from position_timeline capped at as_of — NOT portfolio_isins(),
    which is current-net and as-of-blind (a fund sold after as_of would be wrongly dropped).
    """
    timeline = position_timeline(load_trades(db_path))

    held_isins: set[str] = set()
    for isin, events in timeline.items():
        capped = [e for e in events if e.date <= as_of]
        if not capped:
            continue
        shares, _ = position_asof(capped, as_of)
        if shares > _SHARE_EPSILON:
            held_isins.add(isin)

    universe = held_isins | set(targets)
    values: dict[str, float | None] = {}
    price_dates: dict[str, str | None] = {}
    untargeted_unvaluable: list[str] = []

    for isin in universe:
        events_all = timeline.get(isin, [])
        capped = [e for e in events_all if e.date <= as_of]
        shares, _ = position_asof(capped, as_of) if capped else (0.0, 0.0)
        is_held = shares > _SHARE_EPSILON

        if not is_held:
            values[isin] = 0.0
            price_dates[isin] = None
            continue

        series = build_series(db_path, isin, capped, as_of, currency_meta_path)
        val = value_on(series, as_of, db_path)

        if val is None:
            values[isin] = None
            price_dates[isin] = None
            if isin not in targets:
                untargeted_unvaluable.append(isin)
        else:
            values[isin] = val
            price_dates[isin] = price_date_asof(series, as_of)

    return values, frozenset(held_isins), sorted(untargeted_unvaluable), price_dates


def post_rebalance_weights(
    plan: RebalancePlan, values: dict[str, float | None]
) -> dict[str, float]:
    """Final EUR value per fund after a feasible buy-only plan: current + buy.

    Keyed over ``plan.buys`` — the post-rebalance portfolio (targeted funds at
    their targets, valued untargeted funds diluted).  Empty if infeasible or if
    no fund ends with a positive value.  ``correlation --scenario`` (ADR-0017)
    uses these as the correlation weights.
    """
    if not plan.feasible:
        return {}
    finals = {
        isin: (values.get(isin) or 0.0) + buy for isin, buy in plan.buys.items()
    }
    return {isin: v for isin, v in finals.items() if v > 0.0}


# ---------------------------------------------------------------------------
# Money-weighted return solver (XIRR), graduated down from ``performance``
# (ADR-0019 decision 9). ``performance`` imports ``xirr`` from here for its
# return metrics; the contribution-timing backtest core below uses it for
# per-strategy IRR. Newton-Raphson with a bisection fallback, Actual/365.
# ---------------------------------------------------------------------------


def _npv(rate: float, flows: list[tuple[float, float]]) -> float:
    return float(sum(amount / (1.0 + rate) ** t for t, amount in flows))


def _npv_derivative(rate: float, flows: list[tuple[float, float]]) -> float:
    return float(sum(-t * amount / (1.0 + rate) ** (t + 1.0) for t, amount in flows))


def _newton(
    flows: list[tuple[float, float]],
    *,
    guess: float = 0.1,
    tol: float = 1e-9,
    iterations: int = 100,
) -> float | None:
    """Newton-Raphson root of NPV(rate); None if it leaves the domain or stalls."""
    rate = guess
    for _ in range(iterations):
        try:
            derivative = _npv_derivative(rate, flows)
            if derivative == 0.0:
                return None
            step = _npv(rate, flows) / derivative
        except (OverflowError, ZeroDivisionError):
            return None
        rate -= step
        if rate <= -1.0:  # (1+rate) must stay positive for fractional powers
            return None
        if abs(step) < tol:
            return rate if abs(_npv(rate, flows)) < 1e-6 else None
    return None


def _bisect(
    flows: list[tuple[float, float]],
    *,
    low: float = -0.9999,
    high: float = 100.0,
    iterations: int = 500,
) -> float | None:
    """Bisection fallback on a bracket with a guaranteed sign change."""
    f_low = _npv(low, flows)
    f_high = _npv(high, flows)
    if f_low == 0.0:
        return low
    if f_high == 0.0:
        return high
    if (f_low > 0.0) == (f_high > 0.0):
        return None  # no sign change in the bracket — no root to find
    mid = low
    for _ in range(iterations):
        mid = (low + high) / 2.0
        f_mid = _npv(mid, flows)
        if abs(f_mid) < 1e-9 or (high - low) / 2.0 < 1e-12:
            return mid
        if (f_mid > 0.0) == (f_low > 0.0):
            low, f_low = mid, f_mid
        else:
            high = mid
    return mid


def xirr(cash_flows: list[tuple[str, float]]) -> float | None:
    """Money-weighted annualized return over dated cash flows (Actual/365).

    ``cash_flows`` are ``(YYYY-MM-DD, amount)`` with contributions negative
    (money out) and the terminal value positive (money notionally back). Solves
    ``sum(amount / (1+r)^(days/365)) = 0`` by Newton with a bisection fallback.
    Returns ``None`` (never a wrong number) when there is no sign change (all
    same-sign flows) or neither method converges (ADR-0011 guards this to
    ``n/a``).
    """
    if len(cash_flows) < 2:
        return None
    amounts = [amount for _, amount in cash_flows]
    if not (any(a > 0 for a in amounts) and any(a < 0 for a in amounts)):
        return None

    start = min(date.fromisoformat(d) for d, _ in cash_flows)
    flows = [
        ((date.fromisoformat(d) - start).days / 365.0, amount)
        for d, amount in cash_flows
    ]
    return _newton(flows) or _bisect(flows)
