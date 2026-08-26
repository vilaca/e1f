"""Universe YAML, OpenFIGI resolution, and fund-metadata enrichment."""

import builtins
import logging
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Self

import requests
import yaml
from ftgo import get_fund_stats, get_xid

from .defaults import DEFAULT_CONFIG, KNOWN_FUND_CURRENCIES, XTB_EXCHANGE_SUFFIX
from .retry import call_with_retry

logger = logging.getLogger(__name__)

_FTGO_TER_FIELDS = ("Ongoing charge", "Net expense ratio")
_JUSTETF_PROFILE_URL = "https://www.justetf.com/en/etf-profile.html?isin={isin}"


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
                print(f"⚠ ter {isin}: ftgo has no expense ratio; used justETF ({ter:.2f}%)")

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
            name=data.get("name", isin),
            tickers=data.get("tickers", []),
            exchange=data.get("exchange", ""),
        )


class OpenFIGIResolver:
    """Resolve ISIN to ETF metadata using OpenFIGI API."""

    BASE_URL = "https://api.openfigi.com/v3/mapping"

    def __init__(self, api_key: str | None = None):
        self.api_key = api_key or os.environ.get("OPENFIGI_API_KEY")
        self.headers: dict[str, str] = {}
        if self.api_key:
            self.headers["X-OPENFIGI-APIKEY"] = self.api_key
        self.session = requests.Session()

    def resolve(self, isin: str) -> dict[str, Any] | None:
        """Resolve ISIN to ETF metadata."""
        if not re.match(r"^[A-Z]{2}[A-Z0-9]{10}$", isin):
            print(f"✗ Invalid ISIN: {isin}")
            return None

        payload = [{"idType": "ID_ISIN", "idValue": isin}]

        def _post() -> requests.Response:
            r = self.session.post(self.BASE_URL, json=payload, headers=self.headers, timeout=10)
            r.raise_for_status()
            return r

        try:
            response = call_with_retry(f"OpenFIGI resolve {isin}", _post)
            data: list[dict[str, Any]] = response.json()

            listings_raw: list[dict[str, Any]] = data[0].get("data") or []
            if not listings_raw:
                print(f"✗ No data found for ISIN: {isin}")
                return None

            result: dict[str, Any] = listings_raw[0]
            listings: list[dict[str, str]] = []
            seen: set[tuple[str, str]] = set()
            tickers: list[str] = []
            for entry in listings_raw:
                ticker = str(entry.get("ticker") or "").strip().upper()
                exchange = str(entry.get("exchCode") or "").strip().upper()
                if not ticker or exchange not in XTB_EXCHANGE_SUFFIX:
                    continue
                key = (ticker, exchange)
                if key in seen:
                    continue
                seen.add(key)
                listings.append({"ticker": ticker, "exchange": exchange})
                if ticker not in tickers:
                    tickers.append(ticker)

            if not tickers:
                primary_ticker = str(result.get("ticker") or "").strip().upper()
                tickers = [primary_ticker] if primary_ticker else []

            return {
                "name": result.get("name", f"ETF {isin}"),
                "tickers": tickers,
                "exchange": result.get("exchCode", ""),
                "figi": result.get("figi", ""),
                "listings": listings,
                "resolved_at": datetime.now(UTC).isoformat(),
                "source": "OpenFIGI",
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
                return yaml.safe_load(f) or {"etfs": {}}
        return {"etfs": {}}

    def _save_config(self) -> None:
        with open(self.config_path, "w") as f:
            yaml.dump(self.config, f, default_flow_style=False, sort_keys=False)

    def add(self, isin: str) -> bool:
        """Add ETF by ISIN (auto-resolves all fields)."""
        if isin in self.config.get("etfs", {}):
            print(f"⚠ ISIN {isin} already exists")
            return False

        print(f"🔍 Resolving {isin}...")
        info = self.resolver.resolve(isin)

        if not info:
            return False

        info = enrich_fund_metadata(isin, info)

        if "etfs" not in self.config:
            self.config["etfs"] = {}

        self.config["etfs"][isin] = info
        self._save_config()

        print(f"✓ Added {isin}")
        print(f"  Name: {info['name']}")
        print(f"  Tickers: {', '.join(info['tickers'])}")
        print(f"  Exchange: {info['exchange']}")
        print(f"  FIGI: {info['figi']}")
        if info.get("fund_currency"):
            print(f"  Fund currency: {info['fund_currency']}")
        if info.get("distribution"):
            print(f"  Distribution: {info['distribution']}")
        if info.get("ter") is not None:
            print(f"  TER: {info['ter']:.2f}%")
        if info.get("asset_class"):
            print(f"  Asset class: {info['asset_class']}")
        return True

    def list(self) -> builtins.list[tuple[str, dict[str, Any]]]:
        """List all ETFs in config."""
        return sorted(self.config.get("etfs", {}).items())

    def get(self, isin: str) -> dict[str, Any] | None:
        """Get ETF config by ISIN."""
        etfs: dict[str, Any] = self.config.get("etfs", {})
        return etfs.get(isin)

    def update(self, isin: str) -> bool:
        """Update ETF metadata from OpenFIGI."""
        if isin not in self.config.get("etfs", {}):
            print(f"✗ ISIN {isin} not found")
            return False

        print(f"🔍 Updating {isin}...")
        info = self.resolver.resolve(isin)

        if not info:
            return False

        info = enrich_fund_metadata(isin, info)

        self.config["etfs"][isin] = info
        self._save_config()

        print(f"✓ Updated {isin}")
        print(f"  Name: {info['name']}")
        if info.get("fund_currency"):
            print(f"  Fund currency: {info['fund_currency']}")
        if info.get("distribution"):
            print(f"  Distribution: {info['distribution']}")
        if info.get("ter") is not None:
            print(f"  TER: {info['ter']:.2f}%")
        if info.get("asset_class"):
            print(f"  Asset class: {info['asset_class']}")
        return True
