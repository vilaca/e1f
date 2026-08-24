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
import sqlite3
import time
from collections.abc import Callable
from contextlib import closing
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
        from ftgo import get_xid

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
    from ftgo import get_fund_stats

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
    import sqlite3
    from contextlib import closing

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
    import sqlite3
    from contextlib import closing

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
    import sqlite3
    from contextlib import closing

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
# Look-through snapshots (ADR-0012): immutable, append-only observations of a
# fund's composition, split header (``holdings_snapshot``) / children
# (``holding``). Shared here so ``fetch`` can populate them and ``concentration``
# can read them without the two command modules importing each other (ADR-0003).
# ---------------------------------------------------------------------------

# The three look-through dimensions stored per snapshot. ``security`` rows are
# rank-ordered named holdings (top-10 from yfinance); ``sector`` / ``asset_class``
# rows are complete weightings and carry no rank.
DIMENSION_SECURITY = "security"
DIMENSION_SECTOR = "sector"
DIMENSION_ASSET_CLASS = "asset_class"

# Source tier priority: higher wins when several snapshots exist for one fund
# (ADR-0012 decision 5). ``provider`` is yfinance; the rest are v1b territory.
_TIER_RANK = {"inferred": 0, "provider": 1, "curated": 2, "issuer": 3}

_SECURITY_SUFFIXES = frozenset(
    {"inc", "corp", "co", "plc", "ltd", "ag", "sa", "nv", "se", "the", "class"}
)


@dataclass(frozen=True)
class HoldingRow:
    """One child row of a look-through snapshot (one dimension, one key)."""

    dimension: str
    raw_name: str
    normalized_name: str | None
    weight: float
    rank: int | None


@dataclass(frozen=True)
class LookthroughSnapshot:
    """One immutable observation of one fund's composition from one source."""

    id: int
    fund_id: str
    as_of: str
    source: str
    tier: str
    retrieved_at: str
    reported_holding_count: int | None
    holdings: list[HoldingRow]

    def by_dimension(self, dimension: str) -> list[HoldingRow]:
        return [h for h in self.holdings if h.dimension == dimension]

    @property
    def tier_rank(self) -> int:
        return _TIER_RANK.get(self.tier, _TIER_RANK["provider"])


def normalize_security_name(name: str) -> str:
    """Fold a holding name to a coarse match key — a *hint*, never identity.

    Lower-cases, drops punctuation and common corporate suffixes, and collapses
    whitespace so ``"Apple Inc."`` and ``"APPLE INC"`` co-occur in the unresolved
    overlap-candidate signal (ADR-0012 decision 2). It deliberately does not
    resolve share classes, dual listings, or ADRs — that is the reviewed
    ``security_alias`` work of v1b, not a string algorithm.
    """
    tokens = re.findall(r"[a-z0-9]+", (name or "").lower())
    kept = [t for t in tokens if t not in _SECURITY_SUFFIXES]
    return " ".join(kept or tokens)


def init_lookthrough_schema(conn: sqlite3.Connection) -> None:
    """Create the ADR-0012 look-through tables if absent (idempotent).

    ``holdings_snapshot`` is the immutable header (one observation of one fund
    from one source/tier); ``holding`` holds its children across all three
    dimensions; ``security_alias`` is the deliberately-empty v1a resolution table
    that v1b fills incrementally from the overlap-candidate report.
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS holdings_snapshot (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fund_id TEXT NOT NULL,
            as_of TEXT NOT NULL,
            source TEXT NOT NULL,
            tier TEXT NOT NULL,
            retrieved_at TEXT NOT NULL,
            reported_holding_count INTEGER
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS holding (
            snapshot_id INTEGER NOT NULL,
            dimension TEXT NOT NULL,
            raw_name TEXT NOT NULL,
            normalized_name TEXT,
            weight REAL NOT NULL,
            rank INTEGER,
            FOREIGN KEY (snapshot_id) REFERENCES holdings_snapshot(id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS security_alias (
            raw_name TEXT PRIMARY KEY,
            canonical_name TEXT,
            canonical_key TEXT,
            reviewed_at TEXT
        )
        """
    )
    conn.commit()


def _snapshot_signature(
    reported_holding_count: int | None, holdings: list[HoldingRow]
) -> tuple[Any, ...]:
    """Content fingerprint for identical-observation dedupe (as_of excluded).

    Two observations with the same composition are the *same* snapshot even if
    re-fetched on a later day, so the auto-refresh never becomes a fetch log
    (ADR-0012 decision 5). Weights are rounded to absorb float noise.
    """
    return (
        reported_holding_count,
        tuple(
            sorted(
                (h.dimension, h.raw_name, round(h.weight, 8), h.rank) for h in holdings
            )
        ),
    )


def _load_snapshot(conn: sqlite3.Connection, header: tuple[Any, ...]) -> LookthroughSnapshot:
    snapshot_id, fund_id, as_of, source, tier, retrieved_at, reported = header
    rows = conn.execute(
        "SELECT dimension, raw_name, normalized_name, weight, rank "
        "FROM holding WHERE snapshot_id = ? ORDER BY rank IS NULL, rank, raw_name",
        (snapshot_id,),
    ).fetchall()
    holdings = [
        HoldingRow(
            dimension=str(dim),
            raw_name=str(raw),
            normalized_name=None if norm is None else str(norm),
            weight=float(weight),
            rank=None if rank is None else int(rank),
        )
        for dim, raw, norm, weight, rank in rows
    ]
    return LookthroughSnapshot(
        id=int(snapshot_id),
        fund_id=str(fund_id),
        as_of=str(as_of),
        source=str(source),
        tier=str(tier),
        retrieved_at=str(retrieved_at),
        reported_holding_count=None if reported is None else int(reported),
        holdings=holdings,
    )


_SNAPSHOT_COLUMNS = "id, fund_id, as_of, source, tier, retrieved_at, reported_holding_count"


def _latest_for_source_tier(
    conn: sqlite3.Connection, fund_id: str, source: str, tier: str
) -> LookthroughSnapshot | None:
    header = conn.execute(
        f"SELECT {_SNAPSHOT_COLUMNS} FROM holdings_snapshot "
        "WHERE fund_id = ? AND source = ? AND tier = ? ORDER BY id DESC LIMIT 1",
        (fund_id, source, tier),
    ).fetchone()
    return None if header is None else _load_snapshot(conn, header)


def insert_lookthrough_snapshot(
    db_path: str,
    *,
    fund_id: str,
    as_of: str,
    source: str,
    tier: str,
    retrieved_at: str,
    reported_holding_count: int | None,
    holdings: list[HoldingRow],
) -> int | None:
    """Append one immutable snapshot, skipping an identical re-observation.

    Returns the new snapshot id, or ``None`` when the latest snapshot for the
    same ``(fund, source, tier)`` is content-identical (ADR-0012 decision 5:
    corrections append, identical re-observations do not). Never mutates an
    existing snapshot.
    """
    with closing(sqlite3.connect(db_path)) as conn:
        init_lookthrough_schema(conn)
        latest = _latest_for_source_tier(conn, fund_id, source, tier)
        if latest is not None and _snapshot_signature(
            latest.reported_holding_count, latest.holdings
        ) == _snapshot_signature(reported_holding_count, holdings):
            return None

        cursor = conn.execute(
            "INSERT INTO holdings_snapshot "
            "(fund_id, as_of, source, tier, retrieved_at, reported_holding_count) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (fund_id, as_of, source, tier, retrieved_at, reported_holding_count),
        )
        snapshot_id = int(cursor.lastrowid or 0)
        conn.executemany(
            "INSERT INTO holding "
            "(snapshot_id, dimension, raw_name, normalized_name, weight, rank) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [
                (snapshot_id, h.dimension, h.raw_name, h.normalized_name, h.weight, h.rank)
                for h in holdings
            ],
        )
        conn.commit()
        return snapshot_id


def latest_lookthrough_snapshot(db_path: str, fund_id: str) -> LookthroughSnapshot | None:
    """The analysis snapshot for a fund: highest tier, then latest as_of, then id.

    Prior snapshots are retained as evidence (immutable append-only); this picks
    the one analysis should read (ADR-0012 decision 5). ``None`` when the fund has
    no look-through observation yet.
    """
    with closing(sqlite3.connect(db_path)) as conn:
        if conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='holdings_snapshot'"
        ).fetchone() is None:
            return None
        headers = conn.execute(
            f"SELECT {_SNAPSHOT_COLUMNS} FROM holdings_snapshot WHERE fund_id = ?",
            (fund_id,),
        ).fetchall()
        if not headers:
            return None
        snapshots = [_load_snapshot(conn, header) for header in headers]

    return max(snapshots, key=lambda s: (s.tier_rank, s.as_of, s.id))
