#!/usr/bin/env python
"""e1f look-through fetcher (experimental, ADR-0024).

Cache each held fund's yfinance composition (top holdings, sector and
asset-class weightings) as an immutable look-through snapshot, so the offline
`concentration` and `overlap` commands have something to read.

Split out of `fetch` when the experimental tier was isolated (ADR-0024): stable
`e1f fetch` no longer refreshes look-through, so run this on demand — typically
right after a bulk `e1f fetch`. yfinance is the only look-through source, and the
refresh is best-effort: a fund with no composition data is logged and skipped.

    e1f lookthrough                 # refresh look-through for every held fund
    e1f lookthrough --db path.db    # against a specific DB
"""

import argparse
import logging
import sys
import warnings
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import yfinance as yf

from e1f.common import (
    DEFAULT_CONFIG,
    DEFAULT_DB,
    ConfigManager,
    ETFDefinition,
    call_with_retry,
    portfolio_isins,
)
from e1f.experimental.common import (
    DIMENSION_ASSET_CLASS,
    DIMENSION_SECTOR,
    DIMENSION_SECURITY,
    HoldingRow,
    insert_lookthrough_snapshot,
    normalize_security_name,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class LookthroughRefreshSummary:
    """Exhaustive semantic outcome for one held-fund refresh."""

    held_isins: tuple[str, ...]
    created_isins: tuple[str, ...]
    unchanged_isins: tuple[str, ...]
    unavailable_isins: tuple[str, ...]
    skipped_isins: tuple[str, ...]


def _yf_rate_limited(e: Exception) -> bool:
    """yfinance raises non-requests errors for rate limits; match by text."""
    return "429" in str(e) or "rate limit" in str(e).lower()


def _ticker_candidates(ticker: str) -> list[str]:
    candidates = [ticker]
    if not any(ticker.endswith(suffix) for suffix in (".L", ".DE")):
        candidates += [ticker + ".L", ticker + ".DE"]
    return candidates


def _scale_weights(pairs: list[tuple[str, float]]) -> list[tuple[str, float]]:
    """Normalize a weight group to fractions; scale down if it looks like percent."""
    values = [w for _, w in pairs]
    if values and max(values) > 1.5:
        return [(name, w / 100.0) for name, w in pairs]
    return pairs


def _security_rows(funds_data: Any) -> list[HoldingRow]:
    try:
        df = funds_data.top_holdings
    except Exception:  # noqa: BLE001 — yfinance raises arbitrary types on missing data
        return []
    if df is None or getattr(df, "empty", True):
        return []
    pairs: list[tuple[str, float]] = []
    for index, row in df.iterrows():
        name = str(row.get("Name") or index or "").strip()
        pct = row.get("Holding Percent")
        if not name or pct is None:
            continue
        pairs.append((name, float(pct)))
    pairs = _scale_weights(pairs)
    pairs.sort(key=lambda p: p[1], reverse=True)
    return [
        HoldingRow(DIMENSION_SECURITY, name, normalize_security_name(name), weight, rank)
        for rank, (name, weight) in enumerate(pairs, start=1)
    ]


def _weight_rows(funds_data: Any, attribute: str, dimension: str) -> list[HoldingRow]:
    try:
        mapping = getattr(funds_data, attribute)
    except Exception:  # noqa: BLE001 — yfinance raises arbitrary types on missing data
        return []
    if not isinstance(mapping, Mapping):
        return []
    pairs = [(str(k), float(v)) for k, v in mapping.items() if v is not None]
    pairs = _scale_weights(pairs)
    return [HoldingRow(dimension, name, None, weight, None) for name, weight in pairs]


def _lookthrough_rows(funds_data: Any) -> list[HoldingRow]:
    return [
        *_security_rows(funds_data),
        *_weight_rows(funds_data, "sector_weightings", DIMENSION_SECTOR),
        *_weight_rows(funds_data, "asset_classes", DIMENSION_ASSET_CLASS),
    ]


def _fetch_lookthrough(tickers: list[str]) -> list[HoldingRow] | None:
    for ticker in tickers:
        for candidate in _ticker_candidates(ticker):
            try:
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore")
                    funds_data = call_with_retry(
                        f"yfinance funds_data {candidate}",
                        lambda c=candidate: yf.Ticker(c).funds_data,  # type: ignore[misc]
                        retries=2,
                        is_retryable=_yf_rate_limited,
                    )
            except Exception as e:  # noqa: BLE001 — yfinance raises arbitrary types
                logger.debug("yfinance funds_data failed for %s: %s", candidate, e)
                continue
            rows = _lookthrough_rows(funds_data)
            if rows:
                return rows
    return None


def refresh_lookthrough(db_path: str, config_path: str) -> LookthroughRefreshSummary:
    """Refresh look-through snapshots for every held fund (best-effort).

    ``insert_lookthrough_snapshot`` creates the look-through schema on first
    write and skips content-identical re-observations (ADR-0012 decision 5).
    """
    held = sorted(portfolio_isins(db_path))
    if not held:
        print("No ETF holdings in database")
        print("Ingest trades: e1f transactions trade-republic path/to/transactions.csv")
        return LookthroughRefreshSummary((), (), (), (), ())

    universe = {
        isin: ETFDefinition.from_config(isin, data)
        for isin, data in ConfigManager(config_path).config.get("etfs", {}).items()
    }
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    retrieved_at = datetime.now(UTC).isoformat()
    created: list[str] = []
    unchanged: list[str] = []
    unavailable: list[str] = []
    skipped: list[str] = []
    for isin in held:
        etf = universe.get(isin)
        if etf is None:
            logger.warning("✗ %s — held fund is absent from the ETF config; skipped", isin)
            skipped.append(isin)
            continue
        if not etf.tickers:
            logger.warning("✗ %s %s — no configured ticker; skipped", isin, etf.name)
            skipped.append(isin)
            continue
        rows = _fetch_lookthrough(etf.tickers)
        if not rows:
            logger.warning(f"✗ {isin} {etf.name} — no yfinance look-through")
            unavailable.append(isin)
            continue
        snapshot_id = insert_lookthrough_snapshot(
            db_path,
            fund_id=isin,
            as_of=today,
            source="yfinance",
            tier="provider",
            retrieved_at=retrieved_at,
            reported_holding_count=None,
            holdings=rows,
        )
        if snapshot_id is None:
            logger.info(f"{isin} look-through — unchanged")
            unchanged.append(isin)
        else:
            logger.info(
                f"{isin} look-through — snapshot #{snapshot_id} ({len(rows)} rows)"
            )
            created.append(isin)
    summary = LookthroughRefreshSummary(
        held_isins=tuple(held),
        created_isins=tuple(created),
        unchanged_isins=tuple(unchanged),
        unavailable_isins=tuple(unavailable),
        skipped_isins=tuple(skipped),
    )
    logger.info(
        "Look-through refresh: held=%d; new=%d; unchanged=%d; unavailable=%d; skipped=%d",
        len(summary.held_isins),
        len(summary.created_isins),
        len(summary.unchanged_isins),
        len(summary.unavailable_isins),
        len(summary.skipped_isins),
    )
    return summary


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="e1f lookthrough",
        description="Refresh cached yfinance look-through snapshots for held funds "
        "(experimental; read by `concentration` and `overlap`)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Experimental (ADR-0024). Split out of `e1f fetch`, which no longer refreshes
look-through; run this on demand, typically right after a bulk `e1f fetch`.

Examples:
  e1f fetch
  e1f lookthrough
        """,
    )
    parser.add_argument("--db", "-d", default=DEFAULT_DB, help="Database file path")
    parser.add_argument(
        "--config", "-c", default=DEFAULT_CONFIG, help="ETF universe config for tickers"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
    )
    # yfinance emits ERROR-level messages for each failed ticker attempt in its
    # retry loop; these are expected when trying .L/.DE suffixes.
    logging.getLogger("yfinance").setLevel(logging.CRITICAL)
    args = _build_parser().parse_args(argv)
    try:
        refresh_lookthrough(args.db, args.config)
        return 0
    except Exception as e:  # noqa: BLE001 — CLI top-level; all errors become exit code 1
        print(f"✗ Error: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
