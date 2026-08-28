"""Default paths and shared constants (ADR-0002, ADR-0010, ADR-0017).

Resolved against the project root so an editable install works from any cwd.
The --config / --db / --currency-meta flags remain the overrides.
"""

from pathlib import Path

# src/e1f/common/defaults.py -> repo root (package adds one extra parent vs the
# old src/e1f/common.py location).
_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_CONFIG = str(_ROOT / "data" / "etf_universe.yaml")
DEFAULT_DB = str(_ROOT / "data" / "e1f.db")
DEFAULT_CURRENCY_META = str(_ROOT / "data" / "currency_metadata.yaml")  # pinned ftgo resolution
DEFAULT_SCENARIOS = str(_ROOT / "data" / "scenarios.yaml")  # named ISIN:pct baskets (ADR-0017)
DEFAULT_GLOSSARY = str(_ROOT / "data" / "glossary.md")  # metric glossary read by `glossary`
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
