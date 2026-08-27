#!/usr/bin/env python
"""e1f correlation — return co-movement redundancy & clustering (ADR-0015).

Where ``overlap`` (ADR-0013) asks what the funds *hold* in common, ``correlation``
asks how the funds *move* in common — the second, statistical axis of portfolio
redundancy. It reports (a) highly-correlated fund pairs carrying meaningful
combined EUR weight, and (b) a hierarchical clustering of held funds into
move-together groups.

Two axes, kept separate on purpose (decision 1): a high ρ is *statistical*
evidence (funds moved together, for whatever reason); a resolved ``overlap`` floor
is *structural* evidence (a named security is demonstrably held in both). They are
never fused into one number.

Provenance for a correlation is the sample it was estimated from — its date window
and its length. Each pair therefore produces a typed ``PairwiseOverlap`` carrying a
``status`` and ``reason``, and — whenever an aligned sample exists — its window and
``n``. A pair below the ``--min-overlap`` threshold (default ``MIN_OVERLAP`` = 60),
or with a degenerate sample, is reported UNAVAILABLE with an explicit reason, never
as a deceptively precise point estimate. The one case that keeps neither window nor
sample is ``insufficient_overlap`` — it deliberately discards its (sub-threshold)
aligned vectors so the ``len(returns_a) == len(returns_b) == n`` invariant holds with
``n = 0`` (decision 3; the contrast that makes zero_variance/numerical_error's
*retention* of their vectors the load-bearing case).

Usage:
    e1f correlation                       # redundant pairs + clusters over held funds
    e1f correlation --explain             # reconstruct each flagged pair from source
    e1f correlation --rho-flag 0.95       # tighten the pairwise redundancy threshold
"""

import argparse
import hashlib
import math
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime

import numpy as np
from scipy.cluster.hierarchy import fcluster, linkage
from scipy.spatial.distance import squareform

from e1f.common import (
    DEFAULT_CONFIG,
    DEFAULT_CURRENCY_META,
    DEFAULT_DB,
    DEFAULT_SCENARIOS,
    ConfigManager,
    MetricContract,
    Status,
    _explain_metric,
    assemble_rebalance_valuations,
    compute_rebalance,
    eur_return_series,
    fund_eur_value,
    get_scenario,
    portfolio_isins,
    post_rebalance_weights,
)

# A return correlation below roughly one quarter of trading days is dominated by
# noise; ``MIN_OVERLAP`` is the DEFAULT minimum of aligned return observations below
# which a pair is reported UNAVAILABLE, never estimated. It is a configurable
# threshold, not an immutable floor: ``--min-overlap`` overrides it per run (≥ 2),
# so a caller may deliberately trade statistical strength for coverage.
MIN_OVERLAP = 60

# Post-alignment variance floor: a leg flatter than this over the *pair's* shared
# window (a stale/pinned price stretch, say) has no defined correlation. Evaluated
# on the aligned sample, never on a fund's global history (decision 3).
_VARIANCE_FLOOR = 1e-12

# Float noise near ±1 is clamped, but ONLY within this tolerance; anything further
# out of range is a numerical_error, never silently clamped (ADR-0013's "never
# clamp, never infer"). The ADR uses ``<=`` throughout.
_CLAMP_TOL = 1e-12

# Default thresholds (decision 5 / decision 6).
_DEFAULT_RHO_FLAG = 0.90
_DEFAULT_CLUSTER_RHO = 0.80
_DEFAULT_WEIGHT_FLAG = 0.20


CORRELATION_CONTRACT = MetricContract(
    method_version="pairwise_return_pearson_v1",
    requires=("overlapping EUR return history (≥ MIN_OVERLAP aligned observations)",),
    does_not_require=(
        "a common observation window across all pairs (pairwise overlap by design)",
        "look-through holdings (co-movement is independent of shared constituents)",
    ),
    supports=("pairwise redundancy flags", "co-movement clustering"),
    limitations=(
        "each ρ from its own shared window (n, start, end vary per pair)",
        "statistical co-movement, never established shared holdings (see `overlap`)",
    ),
)


# ---------------------------------------------------------------------------
# EUR return basis (decision 4). ``eur_return_series`` graduated to ``e1f.common``
# (ADR-0033) so ``benchmark`` can share it; imported above, semantics unchanged:
# returns between consecutive *available* EUR closes, a missing day bridged.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# The pair contract (decision 3) + its single construction site. Invariants are
# guaranteed by ``pairwise_overlap`` being the only builder and pinned by tests
# over every branch — no validating ``__post_init__`` (codebase convention).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PairwiseOverlap:
    """One fund pair's aligned return sample and its correlation, or why there is none.

    Invariants (all held by ``pairwise_overlap``): ``len(returns_a) ==
    len(returns_b) == n`` always; CALCULATED ⇒ ``n >= MIN_OVERLAP``, ``rho`` set,
    window set, ``reason`` None; ``zero_variance``/``numerical_error`` RETAIN the
    aligned vectors (they occur only after alignment succeeded).
    """

    status: Status          # CALCULATED | UNAVAILABLE
    returns_a: list[float]  # aligned, date-sorted (may be empty when insufficient)
    returns_b: list[float]  # aligned, date-sorted (len == len(returns_a))
    start: date | None      # first aligned date (None only for insufficient_overlap)
    end: date | None        # last aligned date
    n: int                  # len(returns_a) == len(returns_b), may be 0
    rho: float | None       # Pearson coefficient; not None iff CALCULATED
    reason: str | None      # None iff CALCULATED; else the UNAVAILABLE reason


def pearson_correlation(a: list[float], b: list[float]) -> float:
    """Pearson ρ over two equal-length, finite, positive-variance return vectors.

    Pure NumPy (``cov / √(var_x·var_y)``), deliberately not ``scipy.stats.pearsonr``:
    keeping it local keeps the numerical-branch tests deterministic and confines
    scipy to the clustering step. The variance guard in ``pairwise_overlap`` makes
    the denominator positive; an extreme-magnitude input may still yield a non-finite
    result (NaN or ±inf, e.g. from overflow), which ``pairwise_overlap``'s finite
    check turns into ``numerical_error`` rather than a spurious coefficient.
    """
    x = np.asarray(a, dtype=float)
    y = np.asarray(b, dtype=float)
    xc = x - x.mean()
    yc = y - y.mean()
    denominator = math.sqrt(float(np.sum(xc * xc)) * float(np.sum(yc * yc)))
    return float(np.sum(xc * yc)) / denominator


def pairwise_overlap(
    series_a: list[tuple[str, float]],
    series_b: list[tuple[str, float]],
    min_overlap: int = MIN_OVERLAP,
) -> PairwiseOverlap:
    """The frozen 8-step pair pipeline (decision 3) — the sole ``PairwiseOverlap`` builder.

    Exact-date inner join → ``min_overlap`` threshold → post-alignment variance guard →
    pure-NumPy Pearson → finite-first ρ validation with a ``±1`` clamp → CALCULATED.

    Precondition: ``series_a`` and ``series_b`` are each date-sorted with unique dates
    (as ``eur_return_series`` produces). ``start``/``end`` are read as the first/last
    aligned date, and the join keys ``series_b`` by date, both of which rely on that.
    Also ``min_overlap >= 2`` (the CLI enforces it): a smaller value is not rejected
    here — a 0/1-sample pair simply falls through to the variance/finite guards and
    resolves UNAVAILABLE (``zero_variance``/``numerical_error``), never CALCULATED.
    """
    b_by_date = dict(series_b)
    aligned = [(day, ret, b_by_date[day]) for day, ret in series_a if day in b_by_date]
    if len(aligned) < min_overlap:
        # An insufficient pair discards its aligned vectors — the contrast that makes
        # zero_variance/numerical_error RETAINING theirs the subtle trap. This resolves
        # a contradiction in ADR-0015's constructor example, which passes empty vectors
        # AND the join-count n: that pairing violates the ADR's own stated invariant
        # (len(returns_a) == len(returns_b) == n, always). We keep the invariant — empty
        # vectors ⇒ n = 0 — since no consumer reads an insufficient pair's n or vectors.
        return PairwiseOverlap(
            Status.UNAVAILABLE, [], [], None, None, 0, None, "insufficient_overlap"
        )

    n = len(aligned)
    r_a = [ret for _day, ret, _ in aligned]
    r_b = [other for _day, _ret, other in aligned]
    start = date.fromisoformat(aligned[0][0])
    end = date.fromisoformat(aligned[-1][0])

    if float(np.var(r_a)) < _VARIANCE_FLOOR or float(np.var(r_b)) < _VARIANCE_FLOOR:
        return PairwiseOverlap(Status.UNAVAILABLE, r_a, r_b, start, end, n, None, "zero_variance")

    rho = pearson_correlation(r_a, r_b)
    if not math.isfinite(rho):  # NaN/±inf FIRST — nan < -1 and nan > 1 are both false
        return PairwiseOverlap(Status.UNAVAILABLE, r_a, r_b, start, end, n, None, "numerical_error")
    if abs(rho - 1.0) <= _CLAMP_TOL:
        rho = 1.0
    elif abs(rho + 1.0) <= _CLAMP_TOL:
        rho = -1.0
    elif rho < -1.0 or rho > 1.0:
        return PairwiseOverlap(Status.UNAVAILABLE, r_a, r_b, start, end, n, None, "numerical_error")
    return PairwiseOverlap(Status.CALCULATED, r_a, r_b, start, end, n, rho, None)


# ---------------------------------------------------------------------------
# The correlation universe + clustering subset (decisions 6, 8). Pure over their
# inputs, tested in isolation like the pair math.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FundReturns:
    """A universe member: positive EUR value, weight (universe-normalized), and returns."""

    isin: str
    value: float
    weight: float
    returns: list[tuple[str, float]]


def _pair_key(a: str, b: str) -> tuple[str, str]:
    return (a, b) if a <= b else (b, a)


def clustering_eligible(
    universe: list[str], pairs: dict[tuple[str, str], PairwiseOverlap]
) -> list[str]:
    """Universe funds with ≥1 CALCULATED pair (decision 6); order preserved."""
    return [
        f
        for f in universe
        if any(
            g != f and pairs[_pair_key(f, g)].status == Status.CALCULATED
            for g in universe
        )
    ]


def all_peer_valid_subset(
    eligible: list[str], pairs: dict[tuple[str, str], PairwiseOverlap]
) -> list[str]:
    """The universal-vertex subset: funds CALCULATED against *every* other eligible fund.

    Deliberately NOT clique detection — the set of funds each individually
    connected to all others. Conservative by design (one sparse peer can shrink it
    to a singleton), so every distance fed to ``linkage`` is real (decision 6).

    A lone eligible fund is vacuously "all-peer-valid" (``all()`` over no peers is
    True) and stays in the subset; ``build_clusters`` then yields no cluster from it,
    and — correctly — it is *not* reported as excluded-from-clustering, since it has
    no peer edge to be missing. That taxonomy correctness is why the singleton is
    kept here rather than short-circuited away.
    """
    return [
        f
        for f in eligible
        if all(
            pairs[_pair_key(f, g)].status == Status.CALCULATED
            for g in eligible
            if g != f
        )
    ]


def _distance(rho: float) -> float:
    """Correlation distance ``√(½(1 − ρ))`` (clamped ≥0 against float noise)."""
    return math.sqrt(max(0.0, 0.5 * (1.0 - rho)))


def build_clusters(
    subset: list[str],
    rho_of: Callable[[str, str], float],
    cluster_rho: float,
) -> list[list[str]]:
    """Average-linkage clusters (size ≥2) over the all-peer-valid subset.

    ``D`` is complete and symmetric with a zero diagonal — a valid dissimilarity
    (PSD is not required for hierarchical clustering). Cut at the dendrogram
    *height* ``√(½(1 − cluster_rho))``; because average linkage merges on the mean
    inter-cluster distance, a resulting cluster may contain a pair below
    ``cluster_rho`` — that is standard behaviour, not enforced as a postcondition
    (decision 6). Singletons are omitted.
    """
    if len(subset) < 2:
        return []
    size = len(subset)
    dist = np.zeros((size, size))
    for i in range(size):
        for j in range(i + 1, size):
            dist[i][j] = dist[j][i] = _distance(rho_of(subset[i], subset[j]))
    linkage_matrix = linkage(squareform(dist, checks=False), method="average")
    labels = fcluster(linkage_matrix, _distance(cluster_rho), criterion="distance")

    groups: dict[int, list[str]] = {}
    for isin, label in zip(subset, labels, strict=True):
        groups.setdefault(int(label), []).append(isin)
    return [sorted(group) for group in groups.values() if len(group) >= 2]


# ---------------------------------------------------------------------------
# Report assembly — the full analysis in one immutable value the renderers read.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FlaggedPair:
    """A redundant pair: ρ ≥ rho_flag AND combined weight ≥ weight_flag (decision 5)."""

    a: str
    b: str
    overlap: PairwiseOverlap
    combined_weight: float

    @property
    def sort_key(self) -> float:
        """ρ × combined weight — the decision-5 *ordering* only, NOT a published metric.

        Ranks "these two funds are ~the same bet and you hold a lot of both" highest.
        Deliberately not exposed as a ``score`` so it is never mistaken for a
        statistically meaningful redundancy measure.
        """
        assert self.overlap.rho is not None
        return self.overlap.rho * self.combined_weight


@dataclass(frozen=True)
class Cluster:
    members: list[str]
    weight: float


@dataclass(frozen=True)
class CorrelationReport:
    as_of: str
    rho_flag: float
    cluster_rho: float
    weight_flag: float
    min_overlap: int
    names: dict[str, str]  # ISIN → fund name (from config; "" when unknown)
    universe: list[FundReturns]
    pairs: dict[tuple[str, str], PairwiseOverlap]
    flagged: list[FlaggedPair]
    clusters: list[Cluster]
    unavailable_for_correlation: list[str]  # in universe, no CALCULATED pair
    excluded_from_clustering: list[str]      # has pairs, missing an edge to a peer
    unvaluable: list[str]                    # held, no positive EUR value
    no_history: list[str]                    # positive value, no usable return series


def analyze(
    db_path: str,
    *,
    as_of: str,
    currency_meta_path: str,
    config_path: str,
    rho_flag: float,
    cluster_rho: float,
    weight_flag: float,
    min_overlap: int,
    isins: list[str] | None = None,
    weights: dict[str, float] | None = None,
) -> CorrelationReport:
    """Build the whole report: universe → pairs → flags → clusters → taxonomy.

    By default the universe is the held portfolio, weighted by each fund's current
    EUR value.  A scenario (ADR-0017) overrides both: ``isins`` and ``weights``
    describe the *post-rebalance* portfolio the scenario implies (targeted funds
    at their targets, untargeted funds diluted), so the report answers "how
    correlated is the portfolio I'd hold after this rebalance?".  A fund not yet
    held is included as long as it has a usable return series; the held-value gate
    is skipped when ``weights`` is supplied.
    """
    source = sorted(isins) if isins is not None else sorted(portfolio_isins(db_path))
    config_entries = dict(ConfigManager(config_path).list())
    names = {
        isin: str((config_entries.get(isin) or {}).get("name", "")) for isin in source
    }

    unvaluable: list[str] = []
    no_history: list[str] = []
    valued: list[tuple[str, float, list[tuple[str, float]]]] = []
    for isin in source:
        if weights is None:
            value = fund_eur_value(isin, as_of, db_path, currency_meta_path)
            if value is None or value <= 0.0:
                unvaluable.append(isin)
                continue
        else:
            value = weights.get(isin, 0.0)  # scenario weight; no held-value gate
        returns = eur_return_series(db_path, isin, as_of, currency_meta_path)
        if not returns:
            no_history.append(isin)  # value but no usable history — a distinct category
            continue
        valued.append((isin, value, returns))

    total = sum(value for _isin, value, _returns in valued)
    universe = [
        FundReturns(isin=isin, value=value, weight=value / total, returns=returns)
        for isin, value, returns in valued
    ]
    weight_of = {fund.isin: fund.weight for fund in universe}
    universe_isins = [fund.isin for fund in universe]

    pairs: dict[tuple[str, str], PairwiseOverlap] = {}
    by_isin = {fund.isin: fund.returns for fund in universe}
    for index, a in enumerate(universe_isins):
        for b in universe_isins[index + 1:]:
            pairs[(a, b)] = pairwise_overlap(by_isin[a], by_isin[b], min_overlap)

    flagged = [
        FlaggedPair(a, b, overlap, weight_of[a] + weight_of[b])
        for (a, b), overlap in pairs.items()
        if overlap.status == Status.CALCULATED
        and overlap.rho is not None
        and overlap.rho >= rho_flag
        and weight_of[a] + weight_of[b] >= weight_flag
    ]
    flagged.sort(key=lambda p: (-p.sort_key, p.a, p.b))

    eligible = clustering_eligible(universe_isins, pairs)
    eligible_set = set(eligible)
    subset = all_peer_valid_subset(eligible, pairs)
    subset_set = set(subset)

    def rho_of(a: str, b: str) -> float:
        rho = pairs[_pair_key(a, b)].rho
        assert rho is not None  # subset pairs are CALCULATED by construction
        return rho

    clusters = [
        Cluster(members=members, weight=sum(weight_of[m] for m in members))
        for members in build_clusters(subset, rho_of, cluster_rho)
    ]
    clusters.sort(key=lambda c: (-c.weight, c.members))

    return CorrelationReport(
        as_of=as_of,
        rho_flag=rho_flag,
        cluster_rho=cluster_rho,
        weight_flag=weight_flag,
        min_overlap=min_overlap,
        names=names,
        universe=universe,
        pairs=pairs,
        flagged=flagged,
        clusters=clusters,
        unavailable_for_correlation=[f for f in universe_isins if f not in eligible_set],
        excluded_from_clustering=[f for f in eligible if f not in subset_set],
        unvaluable=unvaluable,
        no_history=no_history,
    )


# ---------------------------------------------------------------------------
# Rendering — the compact report and the --explain reconstruction.
# ---------------------------------------------------------------------------


_NAME_WIDTH = 40  # truncation for a fund name shown beside its ISIN


def _pct(fraction: float) -> str:
    return f"{fraction * 100.0:.1f}%"


def _labeled(report: CorrelationReport, isin: str) -> str:
    """``ISIN  Name`` (name truncated), or bare ISIN when the name is unknown."""
    name = report.names.get(isin, "")[:_NAME_WIDTH]
    return f"{isin}  {name}".rstrip()


def _paren(report: CorrelationReport, isin: str) -> str:
    """``ISIN (Name)`` for inline use, or bare ISIN when the name is unknown."""
    name = report.names.get(isin, "")[:_NAME_WIDTH]
    return f"{isin} ({name})" if name else isin


def _tag() -> str:
    return f"[{Status.UNAVAILABLE.value}]"


def _window(overlap: PairwiseOverlap, *, month: bool) -> str:
    assert overlap.start is not None and overlap.end is not None
    fmt = "%Y-%m" if month else "%Y-%m-%d"
    return f"[{overlap.start.strftime(fmt)} … {overlap.end.strftime(fmt)}, n={overlap.n}]"


def _flagged_lines(report: CorrelationReport) -> list[str]:
    header = (
        f"\nRedundant pairs (ρ ≥ {report.rho_flag:.2f}, "
        f"combined weight ≥ {_pct(report.weight_flag)}):"
    )
    if not report.flagged:
        return [header, "  (none)"]
    lines = [header]
    for pair in report.flagged:
        assert pair.overlap.rho is not None
        lines.append(
            f"  {_paren(report, pair.a)}  ×  {_paren(report, pair.b)}"
            f"    ρ {pair.overlap.rho:.2f}   combined {_pct(pair.combined_weight)}"
            f"   {_window(pair.overlap, month=True)}"
        )
    return lines


def _cluster_lines(report: CorrelationReport) -> list[str]:
    header = f"\nClusters (average-linkage cut, nominal ρ ≈ {report.cluster_rho:.2f}):"
    if not report.clusters:
        return [header, "  (no move-together group of ≥2 funds)"]
    lines = [header]
    for index, cluster in enumerate(report.clusters, start=1):
        lines.append(f"  Cluster {index}  — {_pct(cluster.weight)} of correlation universe")
        for member in cluster.members:
            lines.append(f"    {_labeled(report, member)}")
    lines.append("  (singletons omitted)")
    return lines


def _unavailable_lines(report: CorrelationReport) -> list[str]:
    lines: list[str] = []
    if report.unavailable_for_correlation:
        lines.append(
            f"\nUnavailable for correlation (no pair produced a CALCULATED "
            f"correlation)   {_tag()}:"
        )
        for isin in report.unavailable_for_correlation:
            lines.append(f"  {_labeled(report, isin)}   (no CALCULATED pair with any peer)")
    if report.excluded_from_clustering:
        lines.append(
            f"\nExcluded from clustering (incomplete distances to all peers)   {_tag()}:"
        )
        for isin in report.excluded_from_clustering:
            lines.append(f"  {_labeled(report, isin)}   (missing an edge to some peer)")
    return lines


def _excluded_universe_lines(report: CorrelationReport) -> list[str]:
    if not report.unvaluable and not report.no_history:
        return []
    lines = ["\nExcluded from the universe (disclosed, not correlated):"]
    for isin in report.unvaluable:
        lines.append(f"  {_labeled(report, isin)}   (no positive EUR value)")
    for isin in report.no_history:
        lines.append(
            f"  {_labeled(report, isin)}   (positive value, but no usable return series yet)"
        )
    return lines


_NOTES = (
    "\nNotes (ADR-0015):",
    "  • `correlation` measures how funds MOVE together; `overlap` establishes what "
    "they HOLD in common. A high ρ is statistical evidence, never established "
    "shared holdings — the two are never fused into one number.",
    "  • Each ρ is estimated over that pair's own shared window (its n, start, end); "
    "a short window is weaker evidence than a long one, and is shown so.",
    "  • Clusters are cut at a dendrogram height (average linkage); a cluster may "
    "contain a pair below the cut ρ. Hard per-pair guarantees live in the flags.",
    "  • Funds outside the universe are disclosed, never silently dropped "
    "(unvaluable, or valued but without a usable return series).",
    "  • Weights are normalized over the correlation universe (held funds with a "
    "usable EUR return series), NOT the whole portfolio — an excluded holding is in "
    "neither the weights nor any pair (ADR-0015 decision 8). So a shown % is a share "
    "of the correlated sub-portfolio, which can exceed the fund's true portfolio %.",
)


def _digest(a: list[float], b: list[float]) -> str:
    """A display fingerprint of the aligned return *sample*, for eyeballing that two
    runs saw the same vectors — NOT a source-data identifier (distinct histories can
    round to the same vector). The authoritative reproduction path is re-running
    ``--explain``; serialization is an implementation detail (ADR-0015 decision 8).
    """
    payload = repr([round(v, 12) for v in a]) + "|" + repr([round(v, 12) for v in b])
    return hashlib.sha256(payload.encode()).hexdigest()[:12]


def _preview(vector: list[float]) -> str:
    if len(vector) <= 6:
        body = ", ".join(f"{v:.4f}" for v in vector)
    else:
        head = ", ".join(f"{v:.4f}" for v in vector[:3])
        tail = ", ".join(f"{v:.4f}" for v in vector[-3:])
        body = f"{head}, …, {tail}"
    return f"[{body}]"


_PEARSON_METHOD = (
    "ρ = Σ(xᵢ−x̄)(yᵢ−ȳ) / √(Σ(xᵢ−x̄)²·Σ(yᵢ−ȳ)²) over EUR returns between consecutive "
    "available closes on the pair's shared window (recomputed from source; no persisted result)"
)


def _pair_inputs(a: str, b: str, overlap: PairwiseOverlap) -> str:
    if not overlap.returns_a:
        return "no aligned sample retained (below the minimum overlap)"
    return (
        f"{a} returns {_preview(overlap.returns_a)} · "
        f"{b} returns {_preview(overlap.returns_b)} · "
        f"n={overlap.n} · sample digest {_digest(overlap.returns_a, overlap.returns_b)}"
    )


def _explain_pair(
    report: CorrelationReport, a: str, b: str, overlap: PairwiseOverlap, combined_weight: float
) -> list[str]:
    """Reconstruct one pair from source, at whatever status it resolved to.

    Handles CALCULATED and every UNAVAILABLE reason, so a caller may explain a
    flagged pair (always CALCULATED) or an on-demand named pair (any status).
    """
    if overlap.status is Status.CALCULATED:
        assert overlap.rho is not None
        result = (
            f"ρ {overlap.rho:.4f} · combined {_pct(combined_weight)} · "
            f"{_window(overlap, month=False)}"
        )
    elif overlap.start is not None:  # zero_variance / numerical_error retain their window
        result = (
            f"UNAVAILABLE ({overlap.reason}) · combined {_pct(combined_weight)} · "
            f"{_window(overlap, month=False)}"
        )
    else:  # insufficient_overlap — no sufficiently overlapping sample
        result = f"UNAVAILABLE ({overlap.reason}) · combined {_pct(combined_weight)}"
    title = f"{_paren(report, a)} × {_paren(report, b)}"
    return _explain_metric(
        title, overlap.status, result, _pair_inputs(a, b, overlap),
        _PEARSON_METHOD, CORRELATION_CONTRACT,
    )


def _matrix_lines(report: CorrelationReport) -> list[str]:
    universe = report.universe
    if len(universe) < 2:
        return []
    n = len(universe)
    isins = [fund.isin for fund in universe]

    col_w = 6  # " -0.45" or "  0.87" or "  1.00" or "     —"
    idx_w = 3
    lines = ["\nPairwise correlation matrix (ρ):"]
    lines.append(" " * idx_w + "".join(f"{i + 1:>{col_w}}" for i in range(n)))
    for i, isin_a in enumerate(isins):
        row = f"{i + 1:>{idx_w}}"
        for j, isin_b in enumerate(isins):
            if i == j:
                row += f"{'1.00':>{col_w}}"
            else:
                overlap = report.pairs[_pair_key(isin_a, isin_b)]
                if overlap.status == Status.CALCULATED and overlap.rho is not None:
                    row += f"{overlap.rho:>{col_w}.2f}"
                else:
                    row += f"{'—':>{col_w}}"
        lines.append(row)
    lines.append("")
    for i, fund in enumerate(universe):
        name = report.names.get(fund.isin, "")[:_NAME_WIDTH]
        lines.append(f"  {i + 1:2d}  {fund.isin}  {name}".rstrip())
    return lines


def render(report: CorrelationReport, *, matrix: bool = False) -> list[str]:
    lines = [
        f"\nReturn co-movement — ADR-0015 (as of {report.as_of})",
        f"Window policy: pairwise overlap · min {report.min_overlap} return "
        f"observations · returns in EUR",
        f"Correlation universe: {len(report.universe)} funds",
    ]
    lines.extend(_flagged_lines(report))
    lines.extend(_cluster_lines(report))
    lines.extend(_unavailable_lines(report))
    lines.extend(_excluded_universe_lines(report))
    if matrix:
        lines.extend(_matrix_lines(report))
    lines.extend(_NOTES)
    return lines


def render_explain(report: CorrelationReport) -> list[str]:
    lines = [
        f"\nReturn co-movement — ADR-0015 (as of {report.as_of}) · --explain",
        f"Window policy: pairwise overlap · min {report.min_overlap} return "
        f"observations · returns in EUR",
    ]
    if not report.flagged:
        lines.append("\nNo redundant pair to reconstruct (none crossed both thresholds).")
        return lines
    lines.append("\nReconstructed redundant pairs (recomputed from source data):")
    for pair in report.flagged:
        lines.extend(_explain_pair(report, pair.a, pair.b, pair.overlap, pair.combined_weight))
    return lines


def _fund_membership(report: CorrelationReport, isin: str) -> str:
    """Where a requested ISIN sits: universe / unvaluable / no_history / not_held."""
    if any(fund.isin == isin for fund in report.universe):
        return "universe"
    if isin in report.unvaluable:
        return "unvaluable"
    if isin in report.no_history:
        return "no_history"
    return "not_held"


_MEMBERSHIP_BLOCKER = {
    "not_held": "not a held fund",
    "unvaluable": "excluded from the universe (no positive EUR value)",
    "no_history": "excluded from the universe (no usable return series)",
}


def render_pair_explain(report: CorrelationReport, a: str, b: str) -> list[str]:
    """Reconstruct one named pair on demand, ignoring the flag thresholds."""
    lines = [
        f"\nReturn co-movement — ADR-0015 (as of {report.as_of}) · --explain {a} {b}",
        f"Window policy: pairwise overlap · min {report.min_overlap} return "
        f"observations · returns in EUR",
    ]
    if a == b:
        lines.append("\nCannot reconstruct a pair of one fund — give two distinct ISINs.")
        return lines
    blockers = [
        f"  {_labeled(report, isin)}: {_MEMBERSHIP_BLOCKER[status]}"
        for isin in (a, b)
        if (status := _fund_membership(report, isin)) != "universe"
    ]
    if blockers:
        lines.append("\nCannot reconstruct this pair — a fund is not in the correlation universe:")
        lines.extend(blockers)
        return lines
    weight_of = {fund.isin: fund.weight for fund in report.universe}
    overlap = report.pairs[_pair_key(a, b)]
    lines.append("\nReconstructed pair (recomputed from source data):")
    lines.extend(_explain_pair(report, a, b, overlap, weight_of[a] + weight_of[b]))
    return lines


# ---------------------------------------------------------------------------
# Command.
# ---------------------------------------------------------------------------


def _cmd_correlation(
    db_path: str,
    *,
    as_of: str,
    currency_meta_path: str,
    config_path: str,
    rho_flag: float,
    cluster_rho: float,
    weight_flag: float,
    min_overlap: int,
    explain: bool,
    matrix: bool,
    pair: list[str],
    scenario_name: str | None = None,
    scenario_targets: dict[str, float] | None = None,
) -> int:
    isins: list[str] | None = None
    weights: dict[str, float] | None = None

    if scenario_targets is not None:
        # Correlate the POST-rebalance portfolio the scenario implies (ADR-0017):
        # run the buy-only plan, then weight by each fund's final EUR value.
        values, held_set, _untargeted, _price_dates = assemble_rebalance_valuations(
            db_path, currency_meta_path, scenario_targets, as_of
        )
        plan = compute_rebalance(scenario_targets, values, held_set)
        if not plan.feasible:
            print(f"\nReturn co-movement — ADR-0015 (as of {as_of})")
            print(
                f"\nUNAVAILABLE — cannot build the post-rebalance portfolio for scenario "
                f"{scenario_name!r}: rebalance is infeasible ({plan.reason}). "
                f"Run 'e1f rebalance --scenario {scenario_name}' for the full diagnosis."
            )
            return 0
        weights = post_rebalance_weights(plan, values)
        isins = sorted(weights)
        print(
            f"Universe: portfolio after applying scenario {scenario_name!r} "
            f"(post-rebalance weights, {len(isins)} funds)"
        )
    else:  # held-portfolio mode
        held = portfolio_isins(db_path)
        if not held:
            print("No ETF holdings in database")
            print("Ingest trades: e1f transactions trade-republic path/to/transactions.csv")
            return 0

    report = analyze(
        db_path,
        as_of=as_of,
        currency_meta_path=currency_meta_path,
        config_path=config_path,
        rho_flag=rho_flag,
        cluster_rho=cluster_rho,
        weight_flag=weight_flag,
        min_overlap=min_overlap,
        isins=isins,
        weights=weights,
    )

    if pair:  # two named ISINs → reconstruct just that pair, at any status
        for line in render_pair_explain(report, pair[0], pair[1]):
            print(line)
        return 0

    if not report.universe:
        print(f"\nReturn co-movement — ADR-0015 (as of {as_of})")
        print("\nNo held fund has a usable EUR return series — nothing to correlate.")
        for line in _excluded_universe_lines(report):
            print(line)
        return 0

    for line in (render_explain(report) if explain else render(report, matrix=matrix)):
        print(line)
    return 0


def _bounded_float(low: float, high: float) -> Callable[[str], float]:
    def parse(text: str) -> float:
        try:
            value = float(text)
        except ValueError:
            raise argparse.ArgumentTypeError(f"{text!r} is not a number") from None
        if not low <= value <= high:
            raise argparse.ArgumentTypeError(f"must be in [{low}, {high}] (got {value})")
        return value

    return parse


def _int_at_least(minimum: int) -> Callable[[str], int]:
    def parse(text: str) -> int:
        try:
            value = int(text)
        except ValueError:
            raise argparse.ArgumentTypeError(f"{text!r} is not an integer") from None
        if value < minimum:
            raise argparse.ArgumentTypeError(f"must be ≥ {minimum} (got {value})")
        return value

    return parse


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="e1f correlation",
        description="Return co-movement redundancy — highly-correlated fund pairs "
        "carrying real weight, plus a hierarchical clustering of held funds",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
`overlap` asks what the funds HOLD in common; `correlation` asks how they MOVE in
common. A high ρ is statistical evidence (funds moved together), never established
shared holdings — the two are kept as separate commands with separate vocabularies.

Each pair is correlated over its own shared window (an exact-date inner join of the
two funds' EUR returns between consecutive available closes — bridged over missing
days, so an observation is not necessarily one calendar day); a pair below the
minimum overlap, or with a degenerate sample, is reported UNAVAILABLE with a reason,
never as a point estimate.
Clustering runs only on funds with a valid distance to every peer (no fabricated
distances), so one sparse fund can shrink the clustered set.

Examples:
  e1f correlation
  e1f correlation --explain
  e1f correlation --rho-flag 0.95 --weight-flag 0.10
  e1f correlation --explain IE00B4L5Y983 IE00BK5BQT80   # reconstruct one named pair
  e1f correlation --scenario core            # correlate the post-rebalance book (ADR-0017)
        """,
    )
    parser.add_argument(
        "pair",
        nargs="*",
        metavar="ISIN",
        help="Two held-fund ISINs to reconstruct just that pair, at any status "
        "(ignores the flag thresholds); omit for the full report",
    )
    parser.add_argument("--db", "-d", default=DEFAULT_DB, help="Database file path")
    parser.add_argument(
        "--config", "-c", default=DEFAULT_CONFIG, help="ETF universe config for fund names"
    )
    parser.add_argument(
        "--currency-meta",
        default=DEFAULT_CURRENCY_META,
        help="Pinned ftgo resolution / currency sidecar path",
    )
    parser.add_argument(
        "--as-of",
        default=datetime.now(UTC).date().isoformat(),
        metavar="YYYY-MM-DD",
        help="Value the portfolio and bound the return history as of this date (default: today)",
    )
    parser.add_argument(
        "--rho-flag",
        type=_bounded_float(-1.0, 1.0),
        default=_DEFAULT_RHO_FLAG,
        metavar="ρ",
        help="Flag a pair when its correlation is ≥ this (∈ [-1, 1]; default 0.90)",
    )
    parser.add_argument(
        "--cluster-rho",
        type=_bounded_float(-1.0, 1.0),
        default=_DEFAULT_CLUSTER_RHO,
        metavar="ρ",
        help="Dendrogram cut height, as a correlation (∈ [-1, 1]; default 0.80)",
    )
    parser.add_argument(
        "--weight-flag",
        type=_bounded_float(0.0, 1.0),
        default=_DEFAULT_WEIGHT_FLAG,
        metavar="W",
        help="Flag a pair only when combined EUR weight is ≥ this (∈ [0, 1]; default 0.20)",
    )
    parser.add_argument(
        "--min-overlap",
        type=_int_at_least(2),
        default=MIN_OVERLAP,
        metavar="N",
        help="Minimum aligned return observations to correlate a pair (≥ 2; default 60)",
    )
    parser.add_argument(
        "--scenario",
        "-s",
        metavar="NAME",
        help="Correlate the POST-rebalance portfolio a saved scenario implies "
        "(ADR-0017) instead of the held portfolio — targeted funds at their "
        "targets, untargeted funds diluted, weighted by final EUR value "
        "(see 'e1f scenario').",
    )
    parser.add_argument(
        "--scenarios-file",
        default=DEFAULT_SCENARIOS,
        help="Scenarios YAML file path (used with --scenario).",
    )
    parser.add_argument(
        "--explain",
        action="store_true",
        help="Reconstruct the flagged pairs from source — pair flags only, not clusters "
        "(aligned-sample preview + digest); combine with two ISINs to reconstruct one pair",
    )
    parser.add_argument(
        "--matrix",
        action="store_true",
        help="Append the full N×N pairwise ρ table to the report (— for UNAVAILABLE pairs)",
    )
    return parser


def _validate_as_of(as_of: str) -> None:
    try:
        date.fromisoformat(as_of)
    except ValueError as exc:
        raise ValueError(f"--as-of must be YYYY-MM-DD: {as_of}") from exc


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.pair and len(args.pair) != 2:
        parser.error("give exactly two held-fund ISINs to reconstruct a pair (or none)")

    scenario_targets: dict[str, float] | None = None
    if args.scenario:
        try:
            scenario = get_scenario(args.scenario, args.scenarios_file)
        except Exception as e:  # noqa: BLE001 — surface as a clean parser error
            parser.error(str(e))
        scenario_targets = {isin: pct / 100.0 for isin, pct in scenario.targets.items()}

    try:
        _validate_as_of(args.as_of)
        return _cmd_correlation(
            args.db,
            as_of=args.as_of,
            currency_meta_path=args.currency_meta,
            config_path=args.config,
            rho_flag=args.rho_flag,
            cluster_rho=args.cluster_rho,
            weight_flag=args.weight_flag,
            min_overlap=args.min_overlap,
            explain=args.explain,
            matrix=args.matrix,
            pair=args.pair,
            scenario_name=args.scenario,
            scenario_targets=scenario_targets,
        )
    except Exception as e:  # noqa: BLE001 — CLI top-level; all errors become exit code 1
        print(f"✗ Error: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
