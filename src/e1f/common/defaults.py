"""Default paths and shared constants (ADR-0002, ADR-0010, ADR-0017).

Resolved against the project root for a checkout, or the wheel's installed
``share/e1f`` data directory for a non-editable install.  The --config / --db /
--currency-meta flags remain the overrides.
"""

import sys
from pathlib import Path


def _default_data_dir(module_file: str = __file__, prefix: str = sys.prefix) -> Path:
    """Runtime data beside a checkout, otherwise wheel-installed shared data."""
    # src/e1f/common/defaults.py -> repo root (the common package adds one parent).
    repo_data = Path(module_file).resolve().parents[3] / "data"
    if (repo_data / "etf_universe.yaml").is_file():
        return repo_data
    return Path(prefix) / "share" / "e1f"


_DATA_DIR = _default_data_dir()
DEFAULT_CONFIG = str(_DATA_DIR / "etf_universe.yaml")
DEFAULT_DB = str(_DATA_DIR / "e1f.db")
DEFAULT_CURRENCY_META = str(_DATA_DIR / "currency_metadata.yaml")  # pinned ftgo resolution
DEFAULT_SCENARIOS = str(_DATA_DIR / "scenarios.yaml")  # named ISIN:pct baskets (ADR-0017)
DEFAULT_GLOSSARY = str(_DATA_DIR / "glossary.md")  # metric glossary read by `glossary`
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
