"""Price-series quality primitives shared by ``validate`` and ``funds``.

Interior-gap detection is venue-voted (ADR-0042): a day most same-exchange peers
have, that one covering fund lacks, is a hole — not a holiday and not "the fund
is younger than the window."
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import closing

import pandas as pd

from .currency_metadata import CurrencyMetadata

# A day is a consensus trading day when at least this share of same-venue funds
# whose history spans it have a close. MIN_COVERING is both the per-venue fund
# floor and the per-day covering floor (a thin venue or a series' own edges
# cannot vote). Under-reporting beats crying wolf.
GAP_CONSENSUS = 0.8
MIN_COVERING_ISINS = 3


def venues_from_currency_meta(currency_meta: CurrencyMetadata) -> dict[str, str]:
    """``{isin: venue}`` from pinned symbols shaped ``TICKER:VENUE:CCY``."""
    venues: dict[str, str] = {}
    for isin, pinned in currency_meta.funds.items():
        parts = str(pinned.get("symbol") or "").split(":")
        if len(parts) >= 3 and parts[1]:
            venues[str(isin)] = parts[1]
    return venues


def load_price_frame(db_path: str) -> pd.DataFrame:
    """All stored ``(isin, date, close)`` rows, or an empty frame if none."""
    empty = pd.DataFrame(columns=["isin", "date", "close"])
    if not os.path.exists(db_path):
        return empty
    with closing(sqlite3.connect(db_path)) as conn:
        if (
            conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='prices'"
            ).fetchone()
            is None
        ):
            return empty
        return pd.read_sql("SELECT isin, date, close FROM prices", conn)


def clip_price_frame(
    prices: pd.DataFrame, *, start: str | None, as_of: str
) -> pd.DataFrame:
    """Keep rows with ``start ≤ date ≤ as_of`` (``start`` omitted → no floor)."""
    if prices.empty:
        return prices
    days = prices["date"].map(lambda value: str(value)[:10])
    mask = days <= as_of
    if start is not None:
        mask &= days >= start
    return prices.loc[mask].copy()


def consensus_gaps(
    prices: pd.DataFrame,
    venue_by_isin: dict[str, str],
    *,
    threshold: float = GAP_CONSENSUS,
) -> dict[str, list[str]]:
    """Per-ISIN interior gaps: days the ISIN lacks but its same-exchange peers have.

    A single skipped trading day is invisible to the business-day-gap check when the
    gap is under its limit, yet it distorts short-window return metrics. The vote is
    held **within an exchange** (from ``venue_by_isin``, e.g. LSE / GER) so a genuine
    venue holiday — when every fund on that exchange is closed — is never mistaken for
    a gap. Within a venue, a day is a *consensus trading day* when at least
    ``threshold`` of the funds whose history spans it have a close; a covering fund
    missing such a day has an interior gap (repair with ``e1f fetch <isin> --force``).
    A venue with fewer than ``MIN_COVERING_ISINS`` funds can't establish consensus and
    is skipped (under-reporting beats crying wolf). ``{isin: [YYYY-MM-DD, …]}``.

    A fund that listed after the frame's first day is not covering those earlier
    dates, so they are not gaps — the series is short, not gappy.
    """
    checked = prices.copy()
    checked["date"] = pd.to_datetime(checked["date"], format="mixed", errors="coerce")
    checked = checked.dropna(subset=["date"]).drop_duplicates(
        subset=["isin", "date"], keep="last"
    )
    if checked.empty:
        return {}
    present = checked.pivot(index="date", columns="isin", values="close").sort_index().notna()

    venues: dict[str, list[str]] = {}
    for isin in present.columns:
        venue = venue_by_isin.get(str(isin))
        if venue:
            venues.setdefault(venue, []).append(str(isin))

    gaps: dict[str, list[str]] = {}
    for isins in venues.values():
        if len(isins) < MIN_COVERING_ISINS:
            continue  # too few peers on this exchange to vote
        sub = present[isins]
        covering = pd.DataFrame(False, index=sub.index, columns=sub.columns)
        for isin in isins:
            valid = sub.index[sub[isin].to_numpy()]
            if len(valid):
                covering[isin] = (sub.index >= valid.min()) & (sub.index <= valid.max())
        covering_count = covering.sum(axis=1)
        ratio = sub.sum(axis=1).where(covering_count > 0).div(covering_count)
        consensus = (ratio >= threshold) & (covering_count >= MIN_COVERING_ISINS)
        for isin in isins:
            missing = (consensus & covering[isin] & ~sub[isin]).to_numpy()
            dates = sub.index[missing]
            if len(dates):
                gaps[str(isin)] = [d.strftime("%Y-%m-%d") for d in dates]
    return gaps


def interior_gaps(
    db_path: str,
    venue_by_isin: dict[str, str],
    *,
    start: str | None,
    as_of: str,
) -> dict[str, list[str]]:
    """``consensus_gaps`` over stored prices clipped to ``[start, as_of]``."""
    return consensus_gaps(
        clip_price_frame(load_price_frame(db_path), start=start, as_of=as_of),
        venue_by_isin,
    )
