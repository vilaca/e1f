"""Buy-only rebalance core (ADR-0016), graduated so correlation can consume it."""

from dataclasses import dataclass

from .holdings import (
    _SHARE_EPSILON,
    build_series,
    load_trades,
    position_asof,
    position_timeline,
    price_date_asof,
    value_on,
)


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
    reason: str | None  # UNAVAILABLE reason, or None if feasible
    unvaluable_targets: list[str]  # ISINs triggering target_unvaluable (sorted)
    v: float  # Σ valued held v_f
    v_prime: float  # V'_min (0.0 if infeasible)
    c_min: float  # total cash injection (0.0 if infeasible)
    buys: dict[str, float]  # ISIN → EUR buy
    binders: list[str]  # sorted pinned ISINs binding V'_min
    residual_bound_binds: bool  # True if v_rest/R == V'_min


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
            feasible=False,
            reason="target_unvaluable",
            unvaluable_targets=unvaluable_targets,
            v=0.0,
            v_prime=0.0,
            c_min=0.0,
            buys={},
            binders=[],
            residual_bound_binds=False,
        )

    # V = Σ valued held v_f (untargeted unvaluables already excluded by caller)
    v_held: dict[str, float] = {
        isin: v for isin, v in values.items() if v is not None and isin in held
    }
    V = sum(v_held.values())

    # empty_portfolio: no valued anchor
    if V <= 0.0:
        return RebalancePlan(
            feasible=False,
            reason="empty_portfolio",
            unvaluable_targets=[],
            v=0.0,
            v_prime=0.0,
            c_min=0.0,
            buys={},
            binders=[],
            residual_bound_binds=False,
        )

    R = 1.0 - sum(targets.values())
    v_rest = sum(v for isin, v in v_held.items() if isin not in targets)

    # residual feasibility checks
    if R < _FLOAT_CLAMP and v_rest > 0.0:
        return RebalancePlan(
            feasible=False,
            reason="residual_full",
            unvaluable_targets=[],
            v=V,
            v_prime=0.0,
            c_min=0.0,
            buys={},
            binders=[],
            residual_bound_binds=False,
        )
    if R >= _FLOAT_CLAMP and v_rest <= 0.0:
        return RebalancePlan(
            feasible=False,
            reason="residual_unallocable",
            unvaluable_targets=[],
            v=V,
            v_prime=0.0,
            c_min=0.0,
            buys={},
            binders=[],
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

    binders = sorted(isin for b, isin in pin_bounds if abs(b - v_prime) <= _FLOAT_CLAMP)
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
        feasible=True,
        reason=None,
        unvaluable_targets=[],
        v=V,
        v_prime=v_prime,
        c_min=c_min,
        buys=buys,
        binders=binders,
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
    frozenset[str],  # held (positive shares as-of)
    list[str],  # untargeted_unvaluable (sorted)
    dict[str, str | None],  # price_dates per ISIN
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
    finals = {isin: (values.get(isin) or 0.0) + buy for isin, buy in plan.buys.items()}
    return {isin: v for isin, v in finals.items() if v > 0.0}
