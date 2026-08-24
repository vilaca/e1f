#!/usr/bin/env python
"""e1f concentration — coverage-aware within-fund concentration (ADR-0012 v1a).

Reports, per held ETF, its internal concentration by security (rank-constrained
bounds on the top-10-only tail), by sector, and by position type (yfinance
positionType buckets, not a conventional asset allocation) — each with an
explicit coverage denominator and a four-state status, so no figure ever implies
information its provenance does not establish (ADR-0012's governing invariant).

Coverage is per-dimension: the security tail is top-10-only, whereas sector and
position-type weightings are whole-fund (they sum to 100%). A whole-fund sector
HHI is therefore not "observed-only" — its denominator is the fund, not the
observed top-10.

Reads the immutable look-through snapshots cached by ``fetch`` (yfinance
``funds_data``), so it runs offline. Cross-fund single-name overlap is *not*
asserted here — matching names surface only as an unresolved candidate signal
that points at where the deferred ``overlap`` command (v1b) would pay off.

Usage:
    e1f concentration                 # every held fund + overlap candidates
    e1f concentration VWCE            # one fund by ticker, ISIN, or name
    e1f concentration IE00BK5BQT80 --explain
"""

import argparse
import re
import sys
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from e1f.common import (
    DEFAULT_CONFIG,
    DEFAULT_DB,
    DIMENSION_ASSET_CLASS,
    DIMENSION_SECTOR,
    DIMENSION_SECURITY,
    ConfigManager,
    LookthroughSnapshot,
    latest_lookthrough_snapshot,
    normalize_security_name,
    portfolio_isins,
)

# Cumulative-curve rungs (ADR-0012 decision 3). Rungs deeper than the observed
# named holdings render as unknown (—), never 0 — a yfinance-only source stops
# at the top 10, so Top-25 / Top-50 are unobserved, not empty.
_CURVE_RUNGS = (1, 5, 10, 25, 50)
_ISIN_RE = re.compile(r"^[A-Z]{2}[A-Z0-9]{10}$")

# A complete weighting (sector / asset-class) is CALCULATED *because* it sums to
# 100% with non-negative parts. When the source violates that — negative or
# >100% components, or a sum far from 1.0 (seen on swap / synthetic funds) — the
# premise fails, so the figure is flagged suspect and downgraded rather than
# stamped CALCULATED over data its provenance does not support.
_WEIGHT_EPS = 0.005       # 0.5% slack on any single weight
_WEIGHT_SUM_TOL = 0.02    # 2% slack on the 100% total


def dimension_issue(entries: list[tuple[str, float]]) -> str | None:
    """Why a complete weighting is untrustworthy, or None when it is sound."""
    if not entries:
        return "no weights"
    if any(w < -_WEIGHT_EPS for _, w in entries):
        return "contains negative weights"
    if any(w > 1.0 + _WEIGHT_EPS for _, w in entries):
        return "a weight exceeds 100%"
    total = sum(w for _, w in entries)
    if abs(total - 1.0) > _WEIGHT_SUM_TOL:
        return f"weights sum to {total * 100:.0f}%"
    return None


# ---------------------------------------------------------------------------
# Metric status + data contracts (ADR-0012 decision 7). The contract is the
# single source for a metric's method id, its --explain limited-by / not-limited-by
# split, and its limitation prose — kept in code so they cannot drift from the calc.
# ---------------------------------------------------------------------------


class Status(StrEnum):
    """Four-state per-metric status — the single status vocabulary (decision 7)."""

    CALCULATED = "CALCULATED"   # enough evidence for a point value
    BOUNDED = "BOUNDED"         # no exact value, but defensible math bounds exist
    UNAVAILABLE = "UNAVAILABLE"  # not enough reliable info for even a useful bound
    UNRESOLVED = "UNRESOLVED"   # identity is the blocker, not coverage (v1b)


@dataclass(frozen=True)
class MetricContract:
    """A metric's data requirements — drives method id + limited-by / not-limited-by."""

    method_version: str
    requires: tuple[str, ...]         # what, if improved, would tighten/unblock it
    does_not_require: tuple[str, ...]  # what would not help (or is refused)
    supports: tuple[str, ...]         # what the metric enables
    limitations: tuple[str, ...]      # standing caveats that travel with the figure


SECURITY_CONTRACT = MetricContract(
    method_version="hhi_rank_capped_tail_v1",
    requires=("full holdings (top-10 named only)", "reported holding count"),
    does_not_require=("sector classification", "canonical security identity"),
    supports=("security HHI bounds", "effective-holdings range", "cumulative curve"),
    limitations=(
        "top-10 named holdings only",
        "unobserved tail is bounded by the rank cap, not observed",
    ),
)
SECTOR_CONTRACT = MetricContract(
    method_version="hhi_full_weights_v1",
    requires=(),
    does_not_require=("canonical security identity", "holdings look-through"),
    supports=("sector HHI", "sector weights"),
    limitations=("source sector taxonomy (Yahoo) used verbatim, not canonicalized",),
)
ASSET_CLASS_CONTRACT = MetricContract(
    method_version="asset_class_split_v1",
    requires=(),
    does_not_require=("canonical security identity",),
    supports=("position-type split",),
    limitations=(
        "yfinance positionType buckets (stock / bond / cash / …), a reported "
        "exposure mix — not a conventional asset allocation; negative or >100% "
        "values reflect shorts / derivatives / financing and are shown verbatim, "
        "never normalized into a stocks/bonds/cash picture",
    ),
)
REGION_CONTRACT = MetricContract(
    method_version="region_unavailable_v1",
    requires=("region/country look-through (no reliable free source)",),
    does_not_require=(
        "swap collateral (refused: collateral is not underlying index exposure)",
    ),
    supports=(),
    limitations=("no free automated region source; never inferred from swap collateral",),
)
OVERLAP_CONTRACT = MetricContract(
    method_version="overlap_unresolved_v1",
    requires=("canonical security identity across funds (v1b security_alias)",),
    does_not_require=(),
    supports=("unresolved overlap candidates",),
    limitations=("a name/ticker match is a hint, never established identity",),
)


# ---------------------------------------------------------------------------
# Pure concentration math (no DB) — the false-precision-prone core, tested in
# isolation like performance's return math. Weights are NAV fractions (0..1).
# ---------------------------------------------------------------------------


def hhi(weights: list[float]) -> float:
    """Herfindahl-Hirschman index Σ wᵢ² over the given weights (fractions)."""
    return float(sum(w * w for w in weights))


def remainder(weights: list[float]) -> float:
    """Unobserved NAV remainder ``R = 1 - Σ wᵢ`` (clamped at 0)."""
    return max(0.0, 1.0 - float(sum(weights)))


def hhi_bounds(
    weights: list[float], reported_holding_count: int | None
) -> tuple[float, float]:
    """Rank-constrained ``(HHI_min, HHI_max)`` for the unobserved tail (decision 3).

    With observed weights ``w₁..w_k`` (rank-ordered, so every unobserved security
    weighs ``≤ w_k``), remainder ``R`` and reported count ``N``:

    - ``HHI_max = HHI_observed + R·w_k`` — packing the tail at the rank cap is a
      hard upper bound, not an estimate (the naïve ``R²`` is a valid but vacuous
      supremum, so it is not used).
    - ``HHI_min = HHI_observed + R²/(N-k)`` when ``N`` is known — spreading ``R``
      evenly minimises Σ of squares; without ``N`` the infimum degrades gracefully
      to ``HHI_observed`` (never a fabricated value).
    """
    observed = hhi(weights)
    if not weights:
        return observed, observed
    r = remainder(weights)
    smallest_observed = weights[-1]  # w_k, the rank cap on every unobserved name
    hhi_max = observed + r * smallest_observed
    if reported_holding_count is not None and reported_holding_count > len(weights):
        hhi_min = observed + (r * r) / (reported_holding_count - len(weights))
    else:
        hhi_min = observed
    return hhi_min, hhi_max


def effective_holdings(hhi_value: float) -> float | None:
    """Effective number of equal-weight holdings ``1/HHI`` (None when undefined)."""
    return None if hhi_value <= 0.0 else 1.0 / hhi_value


def cumulative_share(weights: list[float], rung: int) -> float | None:
    """NAV share of the largest ``rung`` holdings, or None when unobserved.

    Known only when the rung falls within the observed named holdings; a deeper
    rung is *unknown*, not zero (decision 2) — the caller renders it as ``—``.
    """
    if rung <= len(weights):
        return float(sum(weights[:rung]))
    return None


def coverage(weights: list[float]) -> float:
    """Observed NAV fraction — the concentration denominator (decision 3)."""
    return float(sum(weights))


# ---------------------------------------------------------------------------
# Snapshot → per-fund analysis assembly.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DimensionWeights:
    """A complete (CALCULATED) weighting — sector or asset-class."""

    entries: list[tuple[str, float]]  # (label, weight), descending

    @property
    def hhi(self) -> float:
        return hhi([w for _, w in self.entries])

    @property
    def issue(self) -> str | None:
        return dimension_issue(self.entries)

    @property
    def is_valid(self) -> bool:
        return self.issue is None


@dataclass(frozen=True)
class FundConcentration:
    """Everything the renderers need for one fund, derived from its snapshot."""

    isin: str
    name: str
    snapshot: LookthroughSnapshot | None
    security_weights: list[float]          # observed, rank-descending
    security_names: list[str]              # parallel to security_weights
    sector: DimensionWeights | None
    asset_class: DimensionWeights | None

    @property
    def has_lookthrough(self) -> bool:
        return self.snapshot is not None

    @property
    def security_status(self) -> Status:
        return Status.BOUNDED if self.security_weights else Status.UNAVAILABLE

    @property
    def sector_status(self) -> Status:
        return Status.CALCULATED if (self.sector and self.sector.is_valid) else Status.UNAVAILABLE

    @property
    def asset_class_status(self) -> Status:
        return (
            Status.CALCULATED
            if (self.asset_class and self.asset_class.is_valid)
            else Status.UNAVAILABLE
        )


def _security_series(snapshot: LookthroughSnapshot) -> tuple[list[float], list[str]]:
    """Observed security weights (rank-descending) and their raw names."""
    rows = snapshot.by_dimension(DIMENSION_SECURITY)
    rows.sort(key=lambda h: h.weight, reverse=True)
    return [h.weight for h in rows], [h.raw_name for h in rows]


def _dimension_weights(
    snapshot: LookthroughSnapshot, dimension: str
) -> DimensionWeights | None:
    rows = snapshot.by_dimension(dimension)
    if not rows:
        return None
    entries = sorted(((h.raw_name, h.weight) for h in rows), key=lambda e: e[1], reverse=True)
    return DimensionWeights(entries=entries)


def build_fund_concentration(
    isin: str, name: str, snapshot: LookthroughSnapshot | None
) -> FundConcentration:
    """Assemble a fund's concentration view from its analysis snapshot (or none)."""
    if snapshot is None:
        return FundConcentration(isin, name, None, [], [], None, None)
    weights, names = _security_series(snapshot)
    return FundConcentration(
        isin=isin,
        name=name,
        snapshot=snapshot,
        security_weights=weights,
        security_names=names,
        sector=_dimension_weights(snapshot, DIMENSION_SECTOR),
        asset_class=_dimension_weights(snapshot, DIMENSION_ASSET_CLASS),
    )


def overlap_candidates(funds: list[FundConcentration]) -> list[tuple[str, int]]:
    """Raw security names in ≥2 funds' top holdings — the *unresolved* signal.

    Grouped by normalized name (a hint), reported with a representative raw name
    and the fund count. Never summed into an exposure figure (decision 2): its
    only job is to point at where v1b's reviewed canonical resolution would pay off.
    """
    by_norm: dict[str, tuple[str, set[str]]] = {}
    for fund in funds:
        if fund.snapshot is None:
            continue
        seen_here: set[str] = set()
        for row in fund.snapshot.by_dimension(DIMENSION_SECURITY):
            norm = row.normalized_name or normalize_security_name(row.raw_name)
            if norm in seen_here:
                continue
            seen_here.add(norm)
            display, funds_seen = by_norm.get(norm, (row.raw_name, set()))
            funds_seen.add(fund.isin)
            by_norm[norm] = (display, funds_seen)

    candidates = [
        (display, len(funds_seen))
        for display, funds_seen in by_norm.values()
        if len(funds_seen) >= 2
    ]
    candidates.sort(key=lambda c: (-c[1], c[0].lower()))
    return candidates


# ---------------------------------------------------------------------------
# Fund resolution (arg → ISIN).
# ---------------------------------------------------------------------------


def resolve_fund(arg: str, entries: list[tuple[str, dict[str, Any]]]) -> str:
    """Resolve a CLI fund argument to a single ISIN by ISIN, ticker, or name.

    Raises ValueError with the ambiguous / unknown matches so ``main`` can turn
    it into a clean exit-1 message rather than a stack trace.
    """
    if _ISIN_RE.match(arg):
        return arg
    needle = arg.strip().lower()
    matches: list[str] = []
    for isin, data in entries:
        tickers = [str(t).lower() for t in (data.get("tickers") or [])]
        name = str(data.get("name") or "").lower()
        if needle in tickers or needle in name:
            matches.append(isin)
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise ValueError(f"no fund matches {arg!r} (try an ISIN, ticker, or name fragment)")
    raise ValueError(f"{arg!r} is ambiguous — matches {', '.join(sorted(matches))}")


# ---------------------------------------------------------------------------
# Rendering — compact table (default) and --explain provenance chain.
# ---------------------------------------------------------------------------


def _pct(value: float | None) -> str:
    return "—" if value is None else f"{value * 100.0:.1f}%"


def _tag(status: Status) -> str:
    return f"[{status.value}]"


def _top_entries(dim: DimensionWeights, limit: int = 3) -> str:
    return ", ".join(f"{label} {w * 100.0:.1f}%" for label, w in dim.entries[:limit])


def _coverage_line(fund: FundConcentration) -> str:
    weights = fund.security_weights
    cov = coverage(weights)
    unobserved = remainder(weights)
    count = len(weights)
    reported = fund.snapshot.reported_holding_count if fund.snapshot else None
    scope = f"top {count} of {reported}" if reported else f"top {count}"
    return (
        f"  {'Coverage':<14} {_pct(cov)} NAV ({scope})   "
        f"Unobserved {_pct(unobserved)}"
    )


def _security_lines(fund: FundConcentration) -> list[str]:
    weights = fund.security_weights
    if not weights:
        # No named holdings came back (bot-walled issuer, swap fund, etc.).
        # Coverage is 0% / 100% unobserved — the honest degrade — so no HHI,
        # bound, or curve is emitted: a 0.0000 here would read as "fully
        # observed", the opposite of the truth (status is UNAVAILABLE).
        return [
            f"  {'Coverage':<14} 0.0% NAV - no named holdings from source",
            f"  {'Security HHI':<14} unavailable (no named holdings from source)"
            f"   {_tag(Status.UNAVAILABLE)}",
        ]
    lines = [_coverage_line(fund)]
    for rung in _CURVE_RUNGS:
        share = cumulative_share(weights, rung)
        suffix = "" if share is not None else "   (unknown: top-10 source)"
        lines.append(f"  {'Top ' + str(rung):<14} {_pct(share)}{suffix}")

    observed = hhi(weights)
    reported = fund.snapshot.reported_holding_count if fund.snapshot else None
    hhi_min, hhi_max = hhi_bounds(weights, reported)
    if hhi_min == hhi_max == observed:
        bounded = f"{observed:.4f} observed (fully observed — no unobserved tail)"
    else:
        bounded = f"{observed:.4f} observed / {hhi_min:.4f}-{hhi_max:.4f} bounded"
    lines.append(f"  {'Security HHI':<14} {bounded}   {_tag(fund.security_status)}")

    eff_low = effective_holdings(hhi_max)
    eff_high = effective_holdings(hhi_min)
    if eff_low is not None and eff_high is not None:
        # A bound (1/HHI_max .. 1/HHI_min), not an estimate — so it carries the
        # BOUNDED tag and no "~", keeping the status vocabulary consistent.
        lines.append(
            f"  {'Eff. holdings':<14} {eff_low:.0f}-{eff_high:.0f}   "
            f"{_tag(fund.security_status)}"
        )
    return lines


def _sector_line(fund: FundConcentration) -> str:
    dim = fund.sector
    if dim is None:
        return f"  {'Sector HHI':<14} unavailable   {_tag(Status.UNAVAILABLE)}"
    if dim.issue is not None:
        # Flag, never suppress: show the raw source weights but withhold an HHI
        # over them and downgrade the status (ADR-0012 governing invariant).
        return (
            f"  {'Sector HHI':<14} suspect ({dim.issue})   {_tag(Status.UNAVAILABLE)}"
            f"   (source: {_top_entries(dim)})"
        )
    # "whole-fund": the sector weighting's denominator is the fund, not the
    # observed top-10 securities — so this HHI is not an observed-only figure.
    return (
        f"  {'Sector HHI':<14} {dim.hhi:.4f}   {_tag(Status.CALCULATED)} whole-fund"
        f"   (top: {_top_entries(dim)})"
    )


def _asset_class_line(fund: FundConcentration) -> str:
    # Labelled "Position types", not "Asset class": these are yfinance positionType
    # buckets (a reported exposure mix that can go negative / over 100% on
    # swap/synthetic funds), not a conventional stocks/bonds/cash allocation.
    dim = fund.asset_class
    if dim is None:
        return f"  {'Position types':<14} unavailable   {_tag(Status.UNAVAILABLE)}"
    split = " / ".join(f"{label} {w * 100.0:.1f}%" for label, w in dim.entries)
    if dim.issue is not None:
        tag = _tag(Status.UNAVAILABLE)
        return f"  {'Position types':<14} {split}   {tag}   (suspect: {dim.issue})"
    return f"  {'Position types':<14} {split}   {_tag(Status.CALCULATED)}"


def _region_line() -> str:
    return (
        f"  {'Region':<14} unavailable (no reliable free source)   "
        f"{_tag(Status.UNAVAILABLE)}"
    )


def _fund_header(fund: FundConcentration) -> str:
    name = fund.name[:40]
    return f"\n{fund.isin}  {name}".rstrip()


def render_fund(fund: FundConcentration) -> list[str]:
    lines = [_fund_header(fund)]
    if not fund.has_lookthrough:
        lines.append(
            "  look-through unavailable — run `e1f fetch` to cache it, "
            "or yfinance could not resolve this fund"
        )
        lines.append(_region_line())
        return lines
    lines.extend(_security_lines(fund))
    lines.append(_sector_line(fund))
    lines.append(_asset_class_line(fund))
    lines.append(_region_line())
    return lines


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


def _snapshot_provenance(snapshot: LookthroughSnapshot) -> str:
    return (
        f"snapshot #{snapshot.id}, source {snapshot.source}/{snapshot.tier}, "
        f"as_of {snapshot.as_of}, retrieved {snapshot.retrieved_at}"
    )


def render_fund_explain(fund: FundConcentration) -> list[str]:
    """Reconstruct each metric's provenance chain from the snapshot + contract.

    Nothing here is read from a persisted audit log — the chain is recomputed
    from the immutable snapshot, so it is always what the code actually did
    (ADR-0012 decision 7).
    """
    lines = [_fund_header(fund)]
    if not fund.has_lookthrough:
        lines.append("  look-through unavailable — run `e1f fetch` to cache it")
        return lines
    assert fund.snapshot is not None
    provenance = _snapshot_provenance(fund.snapshot)

    weights = fund.security_weights
    observed = hhi(weights)
    hhi_min, hhi_max = hhi_bounds(weights, fund.snapshot.reported_holding_count)
    ranked = ", ".join(f"{w * 100.0:.2f}%" for w in weights) or "none"
    lines.extend(_explain_metric(
        "Security concentration",
        fund.security_status,
        f"HHI = {observed:.4f} observed ∈ [{hhi_min:.4f}, {hhi_max:.4f}] bounded ; "
        f"coverage {_pct(coverage(weights))} NAV",
        f"ranked top weights [{ranked}] ; {provenance}",
        "HHI_observed = Σwᵢ² ; HHI_max = +R·w_k (rank cap) ; "
        "HHI_min = +R²/(N-k) when N known, else observed",
        SECURITY_CONTRACT,
    ))

    if fund.sector is not None:
        sector_issue = fund.sector.issue
        sector_result = (
            f"suspect ({sector_issue}); source weights: {_top_entries(fund.sector)}"
            if sector_issue is not None
            else f"HHI = {fund.sector.hhi:.4f} (top: {_top_entries(fund.sector)})"
        )
        lines.extend(_explain_metric(
            "Sector concentration",
            fund.sector_status,
            sector_result,
            f"{len(fund.sector.entries)} sector weights ; {provenance}",
            "HHI = Σwᵢ² over the source's own sector taxonomy (only when weights are sound)",
            SECTOR_CONTRACT,
        ))

    if fund.asset_class is not None:
        split = " / ".join(f"{label} {w * 100.0:.1f}%" for label, w in fund.asset_class.entries)
        asset_issue = fund.asset_class.issue
        asset_result = f"{split} (suspect: {asset_issue})" if asset_issue is not None else split
        lines.extend(_explain_metric(
            "Position-type split",
            fund.asset_class_status,
            asset_result,
            f"position-type weights ; {provenance}",
            "verbatim yfinance positionType weightings (sound only if they sum to 100%)",
            ASSET_CLASS_CONTRACT,
        ))

    lines.extend(_explain_metric(
        "Region concentration",
        Status.UNAVAILABLE,
        "unavailable — not enough reliable info for even a useful bound",
        "none available for free ; swap collateral available but refused",
        "no claim emitted (unknown ≠ zero)",
        REGION_CONTRACT,
    ))
    return lines


def render_overlap(candidates: list[tuple[str, int]]) -> list[str]:
    if not candidates:
        return []
    lines = ["\nPotential overlap candidates — identity unresolved  [UNRESOLVED]:"]
    width = max(len(name) for name, _ in candidates)
    for name, count in candidates:
        lines.append(f"  {name:<{width}}  {count} funds")
    return lines


# A concise coverage caveat printed *above* the per-fund blocks (the detailed
# _NOTES stay at the bottom), so the reader knows each dimension's denominator
# before reading a single figure and never mistakes a whole-fund sector HHI for
# an observed-top-10 one.
_DATA_STATUS = (
    "  Data status (full caveats below):",
    "    • Security holdings — top-10 only where available; deeper tail bounded",
    "    • Sector / position types — whole-fund weightings (denominator is the fund)",
    "    • Region — unavailable",
    "    • Cross-fund overlap — unresolved name matches only",
)


_NOTES = (
    "\nNotes (why a figure is bounded or absent — decision 6):",
    "  • Look-through is a periodic yfinance snapshot, not live.",
    "  • Security dimension is top-10 named holdings only; deeper rungs and the "
    "tail are bounded by the rank cap, not observed.",
    "  • Sector taxonomy is Yahoo's, used verbatim (not canonicalized).",
    "  • No region/country dimension (no reliable free source; never inferred "
    "from swap collateral).",
    "  • Overlap candidates are matching names, not established identity — never "
    "summed into an exposure figure (v1b resolves identity).",
)


# ---------------------------------------------------------------------------
# Command.
# ---------------------------------------------------------------------------


def _cmd_concentration(
    db_path: str,
    config_path: str,
    *,
    fund: str | None = None,
    explain: bool = False,
) -> int:
    held = portfolio_isins(db_path)
    entries = ConfigManager(config_path).list()

    if fund is not None:
        isin = resolve_fund(fund, entries)
        targets = [isin]
        cross_fund = False
    else:
        targets = sorted(held)
        cross_fund = True

    if not targets:
        print("No ETF holdings in database")
        print("Ingest trades: e1f transactions trade-republic path/to/transactions.csv")
        return 0

    names = dict(entries)
    funds = [
        build_fund_concentration(
            isin,
            str((names.get(isin) or {}).get("name", "")),
            latest_lookthrough_snapshot(db_path, isin),
        )
        for isin in targets
    ]

    scope = "held funds" if cross_fund else funds[0].isin
    print(f"\nWithin-fund concentration — {scope} (ADR-0012 v1a)")
    for line in _DATA_STATUS:
        print(line)
    render = render_fund_explain if explain else render_fund
    for concentration in funds:
        for line in render(concentration):
            print(line)

    if cross_fund:
        for line in render_overlap(overlap_candidates(funds)):
            print(line)

    for line in _NOTES:
        print(line)
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="e1f concentration",
        description="Coverage-aware within-fund concentration (security / sector / "
        "asset-class) with rank-constrained bounds on the unobserved tail",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Each figure carries a four-state status:
  CALCULATED  point value from complete evidence (sector, asset-class)
  BOUNDED     defensible math bounds, no point value (security HHI, top-10 only)
  UNAVAILABLE not enough reliable info for even a bound (region)
  UNRESOLVED  identity is the blocker, not coverage (cross-fund overlap — v1b)

Coverage is the observed NAV fraction; deeper curve rungs and the tail are
bounded, never fabricated. Names shared across funds surface only as an
unresolved overlap signal — never summed into an exposure figure.

Look-through is cached by `e1f fetch` (yfinance funds_data); this command runs
offline against that cache.

Examples:
  e1f concentration
  e1f concentration VWCE
  e1f concentration IE00BK5BQT80 --explain
        """,
    )
    parser.add_argument(
        "fund",
        nargs="?",
        help="ISIN, ticker, or name fragment (all held funds if omitted)",
    )
    parser.add_argument("--db", "-d", default=DEFAULT_DB, help="Database file path")
    parser.add_argument(
        "--config", "-c", default=DEFAULT_CONFIG, help="ETF universe config for names"
    )
    parser.add_argument(
        "--explain",
        action="store_true",
        help="Show each metric's reconstructed provenance chain (Result / Inputs / "
        "Method / limited-by)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        return _cmd_concentration(
            args.db,
            args.config,
            fund=args.fund,
            explain=args.explain,
        )
    except Exception as e:  # noqa: BLE001 — CLI top-level; all errors become exit code 1
        print(f"✗ Error: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
