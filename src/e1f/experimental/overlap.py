#!/usr/bin/env python
"""e1f overlap — cross-fund single-name exposure floor (ADR-0013 v1b).

Where ``concentration`` (v1a) reports within-fund concentration one fund at a
time and deliberately refuses to sum a name across funds, ``overlap`` establishes
*cross-fund* single-name exposure: "how much Apple do I really hold when several
funds each carry it?" It sums a security across funds only once a human has
asserted a canonical identity for it (``security_alias``); a string match never
establishes identity (ADR-0012's governing invariant).

Two qualifiers always travel with the number: identity is *resolved*, and
coverage is *partial* (yfinance top-10 only). The second makes the headline a
**floor** (``≥``), never a point value — an unobserved tail can only hold more of
the same security, so the observed aggregate is a strict lower bound.

Subcommands:
    e1f overlap                              # the floor report (held & valued funds)
    e1f overlap --explain                    # + per-security Vf·w reconstruction
    e1f overlap candidates                   # resolution worklist (two tiers)
    e1f overlap resolve "<raw-name>" <key>   # assert a reviewed canonical identity
"""

import argparse
import sys
from dataclasses import dataclass, field
from datetime import UTC, date, datetime

from e1f.common import (
    DEFAULT_CURRENCY_META,
    DEFAULT_DB,
    MetricContract,
    Status,
    _explain_metric,
    fund_eur_value,
    portfolio_isins,
)
from e1f.experimental.common import (
    DIMENSION_SECURITY,
    LookthroughSnapshot,
    NavWeightIssue,
    latest_lookthrough_snapshot,
    load_security_aliases,
    nav_weight_issue,
    normalize_security_name,
    overlap_candidates,
    upsert_security_alias,
)

# A weight enters the floor only as a valid non-negative long portfolio weight
# ``0 ≤ w ≤ 1+ε`` (ADR-0013 decision 5). ``ε`` is upper-bound source-rounding
# slack; the zero lower bound is exact. Never clamp, net, infer the cause, or
# classify a negative as a short — exclude and disclose it so one bad
# observation cannot contaminate the others.


OVERLAP_FLOOR_CONTRACT = MetricContract(
    method_version="overlap_floor_observed_v1",
    requires=(
        "complete holdings (top-10 named only — an unobserved tail may hold more)",
    ),
    does_not_require=(
        "an upper bound (a monotonic single-name floor needs none)",
        "clamping / netting of invalid weights (refused — exclude and disclose)",
    ),
    supports=("cross-fund single-name exposure floor (≥ €, ≥ % of valued portfolio)",),
    limitations=(
        "identity summed only via reviewed security_alias (never a string match)",
        "top-10 observed slice → a floor, never an exact exposure",
        "snapshot as_of (weights) and valuation as_of (€ value) differ by nature",
    ),
)


# ---------------------------------------------------------------------------
# Observations — the pure inputs the floor math groups by canonical_key.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Observation:
    """One fund's observed top-10 weight of one security, with its snapshot date."""

    fund_isin: str
    raw_name: str
    normalized_name: str
    weight: float
    snapshot_as_of: str


@dataclass(frozen=True)
class LookthroughObservationLoad:
    """Snapshot assembly with an exhaustive partition of every requested fund."""

    observations: list[Observation]
    requested: frozenset[str]
    available: frozenset[str]
    missing: frozenset[str]

    def __post_init__(self) -> None:
        if self.available & self.missing or self.available | self.missing != self.requested:
            raise ValueError("available and missing snapshots must partition requested funds")
        observation_funds = {observation.fund_isin for observation in self.observations}
        if not observation_funds <= self.available:
            raise ValueError("observations must belong to snapshot-backed funds")

    @property
    def coverage(self) -> float | None:
        return None if not self.requested else len(self.available) / len(self.requested)


def _snapshot_observations(
    fund_isin: str, snapshot: LookthroughSnapshot | None
) -> list[Observation]:
    if snapshot is None:
        return []
    return [
        Observation(
            fund_isin=fund_isin,
            raw_name=row.raw_name,
            normalized_name=row.normalized_name or normalize_security_name(row.raw_name),
            weight=row.weight,
            snapshot_as_of=snapshot.as_of,
        )
        for row in snapshot.by_dimension(DIMENSION_SECURITY)
    ]


def _weight_issue(weight: float) -> str | None:
    """Why a weight may not enter the floor, or None when it is a valid long weight."""
    issue = nav_weight_issue(weight)
    return {
        NavWeightIssue.NON_FINITE: "non-finite weight",
        NavWeightIssue.NEGATIVE: "negative weight",
        NavWeightIssue.ABOVE_NAV: "weight exceeds 100% (not a NAV portfolio weight)",
        None: None,
    }[issue]


# ---------------------------------------------------------------------------
# Pure floor math (no DB) — resolve → group_by(canonical_key) → eligibility
# filter → Σ Vf·w → floor + valued-% denominator, gated ≥2 funds *after* grouping
# (ADR-0013 decisions 1, 2, 5, 6, 7). Tested in isolation like v1a's bounds math.
# ---------------------------------------------------------------------------


@dataclass
class _KeyGroup:
    canonical_key: str
    display: str
    resolved_funds: set[str] = field(default_factory=set)
    # Valid contributions: (fund, raw_name, weight, Vf, Vf*w, snapshot_as_of).
    contributions: list[tuple[str, str, float, float, float, str]] = field(default_factory=list)
    # Excluded observations: (fund, raw_name, weight, reason).
    excluded: list[tuple[str, str, float, str]] = field(default_factory=list)


@dataclass(frozen=True)
class OverlapFloor:
    """A resolved cross-fund security's exposure floor over the valued sub-portfolio."""

    canonical_key: str
    display: str
    floor_eur: float
    resolved_in: int  # distinct valued funds the identity is resolved in
    floor_from: int   # distinct valued funds contributing a valid weight
    excluded: int     # observations dropped for an invalid weight
    contributions: list[tuple[str, str, float, float, float, str]]
    excluded_rows: list[tuple[str, str, float, str]]

    def pct_of(self, denominator: float) -> float | None:
        return None if denominator <= 0.0 else 100.0 * self.floor_eur / denominator


def compute_floors(
    observations: list[Observation],
    fund_values: dict[str, float],
    aliases: dict[str, tuple[str, str]],
) -> list[OverlapFloor]:
    """Resolved cross-fund floors, gated ≥2 valued funds and sorted € descending.

    ``fund_values`` holds only the *valued* funds (``Vf`` each); observations from
    unvalued funds never reach here. An observation contributes to a key's floor
    only when its ``raw_name`` resolves (``aliases``) *and* its weight is a valid
    long weight; a resolved-but-invalid observation is disclosed as an exclusion
    while its fund's ``Vf`` still counts in the denominator (decision 6). The ≥2
    gate is applied over distinct *resolved* funds, after grouping (decision 7).
    """
    groups: dict[str, _KeyGroup] = {}
    for obs in observations:
        if obs.fund_isin not in fund_values:
            continue  # unvalued fund — excluded from numerator and denominator
        resolved = aliases.get(obs.raw_name)
        if resolved is None:
            continue  # unresolved identity — a worklist entry, never a floor
        key, display = resolved
        group = groups.get(key)
        if group is None:
            group = groups[key] = _KeyGroup(canonical_key=key, display=display)
        # A stable display independent of iteration order.
        group.display = min(group.display, display) if group.resolved_funds else display
        group.resolved_funds.add(obs.fund_isin)
        issue = _weight_issue(obs.weight)
        if issue is not None:
            group.excluded.append((obs.fund_isin, obs.raw_name, obs.weight, issue))
        else:
            vf = fund_values[obs.fund_isin]
            group.contributions.append(
                (obs.fund_isin, obs.raw_name, obs.weight, vf, vf * obs.weight, obs.snapshot_as_of)
            )

    floors = [
        OverlapFloor(
            canonical_key=group.canonical_key,
            display=group.display,
            floor_eur=sum(contribution for *_, contribution, _ in group.contributions),
            resolved_in=len(group.resolved_funds),
            floor_from=len({fund for fund, *_ in group.contributions}),
            excluded=len(group.excluded),
            contributions=group.contributions,
            excluded_rows=group.excluded,
        )
        for group in groups.values()
        if len(group.resolved_funds) >= 2  # cross-fund gate, after GROUP BY key
    ]
    floors.sort(key=lambda f: (-f.floor_eur, f.display.lower()))
    return floors


@dataclass(frozen=True)
class UnresolvedName:
    """A normalized co-occurrence group still awaiting a reviewed identity."""

    raw_names: list[str]  # distinct source names, sorted
    fund_count: int


def unresolved_worklist(
    observations: list[Observation], aliases: dict[str, tuple[str, str]]
) -> list[UnresolvedName]:
    """Normalized names co-occurring in ≥2 funds with an unresolved source name.

    v1a's UNRESOLVED signal, reframed as "resolve these to establish overlap"
    (decision 7). A group drops off once every source name in it is resolved; a
    group with any unresolved name stays, so the progression from candidate to
    calculated floor is always visible. No floor is ever guessed here.
    """
    by_norm: dict[str, tuple[set[str], set[str]]] = {}
    for obs in observations:
        raw_names, funds = by_norm.setdefault(obs.normalized_name, (set(), set()))
        raw_names.add(obs.raw_name)
        funds.add(obs.fund_isin)

    worklist = [
        UnresolvedName(raw_names=sorted(raw_names), fund_count=len(funds))
        for raw_names, funds in by_norm.values()
        if len(funds) >= 2 and any(name not in aliases for name in raw_names)
    ]
    worklist.sort(key=lambda u: (-u.fund_count, u.raw_names[0].lower()))
    return worklist


# ---------------------------------------------------------------------------
# Data assembly (reads the immutable snapshots + aliases; values via common).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ValuationView:
    """Held funds split into valued (with ``Vf``) and unvaluable, per ``as_of``."""

    as_of: str
    fund_values: dict[str, float]  # valued funds only
    unvalued: list[str]            # held but unvaluable (excluded + disclosed)

    @property
    def valued_total(self) -> float:
        return sum(self.fund_values.values())

    @property
    def held_count(self) -> int:
        return len(self.fund_values) + len(self.unvalued)

    @property
    def coverage(self) -> float | None:
        return None if self.held_count == 0 else len(self.fund_values) / self.held_count


def _valuation_view(
    held: list[str], as_of: str, db_path: str, currency_meta_path: str
) -> ValuationView:
    fund_values: dict[str, float] = {}
    unvalued: list[str] = []
    for isin in held:
        value = fund_eur_value(isin, as_of, db_path, currency_meta_path)
        if value is None:
            unvalued.append(isin)
        else:
            fund_values[isin] = value
    return ValuationView(as_of=as_of, fund_values=fund_values, unvalued=unvalued)


def _observations_for(isins: list[str], db_path: str) -> LookthroughObservationLoad:
    observations: list[Observation] = []
    available: set[str] = set()
    missing: set[str] = set()
    for isin in isins:
        snapshot = latest_lookthrough_snapshot(db_path, isin)
        if snapshot is None:
            missing.add(isin)
        else:
            available.add(isin)
            observations.extend(_snapshot_observations(isin, snapshot))
    return LookthroughObservationLoad(
        observations=observations,
        requested=frozenset(isins),
        available=frozenset(available),
        missing=frozenset(missing),
    )


# ---------------------------------------------------------------------------
# Rendering — the floor report, --explain chain, and the candidates worklist.
# ---------------------------------------------------------------------------


def _fmt_eur(value: float) -> str:
    return f"€{value:,.0f}"


def _basis_label(coverage: float | None) -> str:
    # Collapses to "portfolio" only at full valuation coverage (decision 6).
    return "portfolio" if coverage is not None and coverage >= 1.0 else "valued portfolio"


def _coverage_line(view: ValuationView) -> str:
    coverage = view.coverage
    pct = "n/a" if coverage is None else f"{coverage * 100.0:.1f}%"
    return f"Valued portfolio: {_fmt_eur(view.valued_total)} · valuation coverage {pct}"


def _lookthrough_coverage_lines(load: LookthroughObservationLoad) -> list[str]:
    valued_count = len(load.requested)
    pct = "n/a" if load.coverage is None else f"{load.coverage * 100.0:.1f}%"
    lines = [
        f"Look-through coverage: {pct} of valued funds "
        f"({len(load.available)}/{valued_count} snapshots)"
    ]
    if load.missing:
        lines.append(
            "⚠ valued funds with no look-through snapshot: "
            + ", ".join(sorted(load.missing))
        )
    return lines


def _floor_lines(floors: list[OverlapFloor], view: ValuationView) -> list[str]:
    basis = _basis_label(view.coverage)
    width = max((len(f.display) for f in floors), default=0)
    lines: list[str] = []
    for floor in floors:
        pct = floor.pct_of(view.valued_total)
        pct_text = "n/a" if pct is None else f"≥ {pct:.1f}% of {basis}"
        lines.append(
            f"  {floor.display:<{width}}  ≥ {_fmt_eur(floor.floor_eur)} "
            f"({pct_text})   [{Status.CALCULATED.value} floor]"
        )
        lines.append(
            f"    Identity resolved in {floor.resolved_in} funds · "
            f"floor from {floor.floor_from} · excluded {floor.excluded}"
        )
    return lines


def _explain_floor(floor: OverlapFloor, view: ValuationView) -> list[str]:
    basis = _basis_label(view.coverage)
    pct = floor.pct_of(view.valued_total)
    pct_text = "n/a" if pct is None else f"≥ {pct:.1f}% of {basis}"
    chain = "; ".join(
        f"{fund} {_fmt_eur(vf)}·{weight * 100.0:.2f}%={_fmt_eur(contribution)} "
        f"(snapshot as_of {snap_as_of})"
        for fund, _raw, weight, vf, contribution, snap_as_of in floor.contributions
    ) or "no eligible observations"
    excluded = "; ".join(
        f"{fund} {raw} {weight * 100.0:.2f}% ({reason})"
        for fund, raw, weight, reason in floor.excluded_rows
    ) or "none"
    inputs = (
        f"Vf·w: {chain}   |   excluded: {excluded}   |   "
        f"valued denominator {_fmt_eur(view.valued_total)} across "
        f"{len(view.fund_values)} funds ; valuation as_of {view.as_of}"
    )
    return _explain_metric(
        f"{floor.display}  [{floor.canonical_key}]",
        Status.CALCULATED,
        f"≥ {_fmt_eur(floor.floor_eur)} ({pct_text}) floor · "
        f"resolved in {floor.resolved_in} · floor from {floor.floor_from} · "
        f"excluded {floor.excluded}",
        inputs,
        "E_floor = Σ Vf·w^obs over resolved + eligible observations ; "
        "% = E_floor / V_valued (same valued-fund set on both sides)",
        OVERLAP_FLOOR_CONTRACT,
    )


def _worklist_lines(worklist: list[UnresolvedName]) -> list[str]:
    if not worklist:
        return []
    lines = [
        f"\nUnresolved co-occurring names — resolve to establish overlap   "
        f"[{Status.UNRESOLVED.value}]:"
    ]
    for entry in worklist:
        source_count = len(entry.raw_names)
        noun = "name" if source_count == 1 else "names"
        lines.append(
            f"  {' / '.join(entry.raw_names)}   "
            f"({source_count} source {noun} · {entry.fund_count} funds)"
        )
    return lines


def _unvalued_line(view: ValuationView) -> list[str]:
    if not view.unvalued:
        return []
    return [
        f"\n⚠ excluded from overlap (no price/FX on or before {view.as_of}): "
        + ", ".join(sorted(view.unvalued))
    ]


_NOTES = (
    "\nNotes (ADR-0013):",
    "  • Floor, not point value: an unobserved top-10 tail can only hold more of "
    "the same security, so ≥ is the honest type.",
    "  • Two qualifiers always: identity is resolved (reviewed security_alias) and "
    "coverage is partial (yfinance top-10).",
    "  • Only names resolved in ≥2 valued funds appear; a single-fund resolved "
    "name is visible via `e1f concentration`, not here.",
    "  • Unvalued funds are excluded from both the floor and its denominator, and "
    "disclosed above (unknown ≠ €0).",
    "  • Valued funds without a look-through snapshot remain in the denominator but "
    "cannot contribute an observed weight; look-through coverage is disclosed separately.",
)


# ---------------------------------------------------------------------------
# Subcommands.
# ---------------------------------------------------------------------------


def _cmd_report(
    db_path: str,
    *,
    as_of: str,
    currency_meta_path: str,
    explain: bool,
) -> int:
    held = sorted(portfolio_isins(db_path))
    if not held:
        print("No ETF holdings in database")
        print("Ingest trades: e1f transactions trade-republic path/to/transactions.csv")
        return 0

    view = _valuation_view(held, as_of, db_path, currency_meta_path)
    loaded = _observations_for(sorted(view.fund_values), db_path)
    observations = loaded.observations
    aliases = load_security_aliases(db_path)
    floors = compute_floors(observations, view.fund_values, aliases)
    worklist = unresolved_worklist(observations, aliases)

    print(f"\nCross-fund security overlap — ADR-0013 v1b (as of {as_of})")
    print(_coverage_line(view))
    for line in _lookthrough_coverage_lines(loaded):
        print(line)

    if not view.fund_values:
        print("\nNo held fund could be valued — nothing to establish overlap against.")
        for line in _unvalued_line(view):
            print(line)
        return 0

    if floors:
        if explain:
            for floor in floors:
                for line in _explain_floor(floor, view):
                    print(line)
        else:
            for line in _floor_lines(floors, view):
                print(line)
    else:
        print("\nNo cross-fund overlap established yet — resolve co-occurring names below.")

    for line in _worklist_lines(worklist):
        print(line)
    for line in _unvalued_line(view):
        print(line)
    for line in _NOTES:
        print(line)
    return 0


def _cmd_resolve(
    db_path: str, raw_name: str, canonical_key: str, *, display: str | None
) -> int:
    reviewed_at = upsert_security_alias(
        db_path, raw_name, canonical_key, canonical_name=display
    )
    print(
        f"✓ Resolved {raw_name!r} → {canonical_key} "
        f"(display: {display or raw_name}) · reviewed_at {reviewed_at}"
    )
    return 0


def _cmd_candidates(db_path: str) -> int:
    held = sorted(portfolio_isins(db_path))
    if not held:
        print("No ETF holdings in database")
        return 0

    aliases = load_security_aliases(db_path)
    snapshots = [(isin, latest_lookthrough_snapshot(db_path, isin)) for isin in held]
    observations = [
        obs for isin, snapshot in snapshots for obs in _snapshot_observations(isin, snapshot)
    ]

    print("\nResolution worklist — ADR-0013 v1b (every observed security across held funds)")

    print("\nTier 1 — co-occurrence seed (normalized name in ≥2 funds):")
    seed = overlap_candidates(snapshots)
    if seed:
        width = max(len(name) for name, _ in seed)
        for name, count in seed:
            tag = "resolved" if name in aliases else "unresolved"
            print(f"  {name:<{width}}  {count} funds   [{tag}]")
    else:
        print("  (none)")

    print("\nTier 2 — complete observed-name roster:")
    _print_roster(observations, aliases)
    return 0


def _print_roster(
    observations: list[Observation], aliases: dict[str, tuple[str, str]]
) -> None:
    by_name: dict[str, set[str]] = {}
    for obs in observations:
        by_name.setdefault(obs.raw_name, set()).add(obs.fund_isin)

    resolved_groups: dict[str, tuple[set[str], set[str]]] = {}
    unresolved: list[tuple[str, int]] = []
    for raw_name, funds in by_name.items():
        alias = aliases.get(raw_name)
        if alias is None:
            unresolved.append((raw_name, len(funds)))
        else:
            key = alias[0]
            names, all_funds = resolved_groups.setdefault(key, (set(), set()))
            names.add(raw_name)
            all_funds.update(funds)

    print("  Resolved (collapsed to one line per canonical identity):")
    if resolved_groups:
        for key in sorted(resolved_groups):
            names, funds = resolved_groups[key]
            noun = "name" if len(names) == 1 else "names"
            print(f"    {key}   ({len(names)} {noun} · {len(funds)} funds)")
    else:
        print("    (none)")

    print("  Unresolved:")
    if unresolved:
        unresolved.sort(key=lambda u: (-u[1], u[0].lower()))
        width = max(len(name) for name, _ in unresolved)
        for name, count in unresolved:
            noun = "fund" if count == 1 else "funds"
            print(f"    {name:<{width}}  {count} {noun}")
    else:
        print("    (none)")


# ---------------------------------------------------------------------------
# Parser + entry point.
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="e1f overlap",
        description="Cross-fund single-name exposure floor — sums a security across "
        "funds only via a reviewed canonical identity, and always as a ≥ floor",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
The headline is a floor (≥), never a point value: identity is resolved via a
reviewed security_alias, and coverage is the yfinance top-10 slice, so the true
exposure can only be greater. Only names resolved in ≥2 valued funds appear;
unresolved co-occurring names are listed as a worklist beneath the report.

  resolve   assert a reviewed canonical identity (idempotent; stamps reviewed_at)
  candidates  the resolution worklist — Tier 1 co-occurrence seed + Tier 2 roster
  (bare)    the floor report; add --explain for the per-security Vf·w chain

Examples:
  e1f overlap
  e1f overlap --explain
  e1f overlap candidates
  e1f overlap resolve "Apple Inc." apple-ord --name "Apple Inc."
        """,
    )
    parser.add_argument("--db", "-d", default=DEFAULT_DB, help="Database file path")
    parser.add_argument(
        "--currency-meta",
        default=DEFAULT_CURRENCY_META,
        help="Pinned ftgo resolution / currency sidecar path",
    )
    parser.add_argument(
        "--as-of",
        default=datetime.now(UTC).date().isoformat(),
        metavar="YYYY-MM-DD",
        help="Value the portfolio as of this date (default: today)",
    )
    parser.add_argument(
        "--explain",
        action="store_true",
        help="Reconstruct each floor's per-fund Vf·w chain, exclusions, and both as_of dates",
    )

    sub = parser.add_subparsers(dest="subcommand")
    # ``--db`` is repeated on each subparser (SUPPRESS default) so it is accepted
    # either before or after the subcommand without the top-level default
    # clobbering a value parsed before it (the argparse subparser-default trap).
    resolve = sub.add_parser(
        "resolve", help="Assert a reviewed canonical identity for a source name"
    )
    resolve.add_argument("raw_name", help="The observed source name (as it appears in a snapshot)")
    resolve.add_argument("canonical_key", help="The canonical security identity (e.g. apple-ord)")
    resolve.add_argument("--name", dest="display", help="Display name (defaults to the raw name)")
    resolve.add_argument("--db", "-d", default=argparse.SUPPRESS, help="Database file path")
    candidates = sub.add_parser("candidates", help="Print the two-tier resolution worklist")
    candidates.add_argument("--db", "-d", default=argparse.SUPPRESS, help="Database file path")
    return parser


def _validate_as_of(as_of: str) -> None:
    try:
        date.fromisoformat(as_of)
    except ValueError as exc:
        raise ValueError(f"--as-of must be YYYY-MM-DD: {as_of}") from exc


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        if args.subcommand == "resolve":
            return _cmd_resolve(
                args.db, args.raw_name, args.canonical_key, display=args.display
            )
        if args.subcommand == "candidates":
            return _cmd_candidates(args.db)
        _validate_as_of(args.as_of)
        return _cmd_report(
            args.db,
            as_of=args.as_of,
            currency_meta_path=args.currency_meta,
            explain=args.explain,
        )
    except Exception as e:  # noqa: BLE001 — CLI top-level; all errors become exit code 1
        print(f"✗ Error: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
