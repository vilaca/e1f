"""Buy-only rebalance core (ADR-0016), graduated so correlation can consume it."""

import math
from dataclasses import dataclass, field

from .holdings import (
    _SHARE_EPSILON,
    build_series,
    load_trades,
    position_asof,
    position_timeline,
    price_date_asof,
    value_on,
)


_FLOAT_CLAMP = 1e-9  # clamp for binder/residual buy rounding and bound equality checks


@dataclass(frozen=True)
class RebalancePlan:
    """Result of the buy-only rebalance math (ADR-0016 decisions 3/5).

    ``feasible`` is True when a finite V'_min exists.  ``buys`` maps universe
    ISIN → EUR buy amount (0.0 for the binder(s)); covers pinned funds and
    valued untargeted funds.  Empty when infeasible.

    ``binders``: pinned ISIN(s) whose bound v_i/t_i equals V'_min (sorted).
    ``residual_bound_binds``: True when the residual bound v_rest/R also equals
    V'_min (so the whole residual bucket gets zero buys).

    A sleeve plan (ADR-0060) sets ``sleeve``: targets are fractions of the
    non-frozen sleeve, ``frozen`` held funds get 0.0 buys, ``sleeve_v`` is the
    sleeve's current value S and ``level`` its final value L.  ``budget`` is the
    cash to invest when one was given, else the plan invests ``c_min``.
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
    sleeve: bool = False
    frozen: frozenset[str] = frozenset()  # held frozen ISINs (sleeve plans)
    unclassified: list[str] = field(default_factory=list)  # held, neither targeted nor frozen
    budget: float | None = None  # cash to invest (sleeve plans); None = c_min
    sleeve_v: float = 0.0  # S: current value of the non-frozen sleeve
    level: float = 0.0  # L: final value of the non-frozen sleeve

    @property
    def injection(self) -> float:
        """Cash the plan invests: the budget when given, else ``c_min``."""
        return self.c_min if self.budget is None else self.budget

    @property
    def v_final(self) -> float:
        """Book value after the plan's buys."""
        return self.v + self.injection

    @property
    def shortfall(self) -> float:
        """How far a budget falls short of ``c_min`` (0.0 when it reaches targets)."""
        return 0.0 if self.budget is None else max(0.0, self.c_min - self.budget)


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
    if not targets:
        raise ValueError("targets must be non-empty")
    if any(
        isinstance(target, bool)
        or not math.isfinite(target)
        or not 0.0 < target <= 1.0
        for target in targets.values()
    ):
        raise ValueError("target fractions must be finite and in (0, 1]")
    if sum(targets.values()) > 1.0 + _FLOAT_CLAMP:
        raise ValueError("target fractions must sum to at most 1")

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
        # Same clamp when the residual bound binds: c_rest is analytically zero
        if -_FLOAT_CLAMP <= c_rest < 0.0:
            c_rest = 0.0
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


def compute_sleeve_rebalance(
    targets: dict[str, float],
    values: dict[str, float | None],
    held: frozenset[str],
    frozen: frozenset[str],
    budget: float | None = None,
) -> RebalancePlan:
    """Buy-only rebalance of the non-frozen sleeve (ADR-0060).

    ``targets``: ISIN → fraction of the sleeve in (0, 1], Σ = 1, disjoint from
    ``frozen``.  ``values`` / ``held`` as in ``compute_rebalance``.  Frozen funds
    are never bought; every other held fund must be targeted.

    The sleeve's final value L solves Σ max(0, s_i·L − v_i) = B, and fund i buys
    c_i = max(0, s_i·L − v_i).  Without a budget B is the minimum ``c_min`` =
    max(v_i/s_i) − S, where every fund reaches its target; a smaller budget
    closes the largest gaps first.

    Feasibility check order (first match wins):
      target_unvaluable → unclassified → empty_sleeve.
    """
    if not targets:
        raise ValueError("targets must be non-empty")
    if any(
        isinstance(target, bool)
        or not math.isfinite(target)
        or not 0.0 < target <= 1.0
        for target in targets.values()
    ):
        raise ValueError("target fractions must be finite and in (0, 1]")
    if abs(sum(targets.values()) - 1.0) > _FLOAT_CLAMP:
        raise ValueError("sleeve target fractions must sum to 1")
    if frozen & set(targets):
        raise ValueError("a fund cannot be both targeted and frozen")
    if budget is not None and (not math.isfinite(budget) or budget < 0.0):
        raise ValueError("budget must be finite and >= 0")

    def unavailable(
        reason: str,
        *,
        unvaluable: list[str] | None = None,
        unclassified: list[str] | None = None,
        v: float = 0.0,
        frozen_held: frozenset[str] = frozenset(),
    ) -> RebalancePlan:
        return RebalancePlan(
            feasible=False, reason=reason, unvaluable_targets=unvaluable or [], v=v,
            v_prime=0.0, c_min=0.0, buys={}, binders=[], residual_bound_binds=False,
            sleeve=True, frozen=frozen_held, unclassified=unclassified or [], budget=budget,
        )

    unvaluable_targets = sorted(
        isin for isin in targets if isin in held and values.get(isin) is None
    )
    if unvaluable_targets:
        return unavailable("target_unvaluable", unvaluable=unvaluable_targets)
    unclassified = sorted(held - set(targets) - frozen)
    if unclassified:
        return unavailable("unclassified", unclassified=unclassified)

    v_held: dict[str, float] = {
        isin: v for isin, v in values.items() if v is not None and isin in held
    }
    V = sum(v_held.values())
    frozen_held = frozenset(isin for isin in v_held if isin in frozen)
    sleeve = {isin: _v_for_bound(values.get(isin)) for isin in targets}
    S = sum(sleeve.values())
    if S <= 0.0 and not budget:
        return unavailable("empty_sleeve", v=V, frozen_held=frozen_held)

    # Fund i starts buying once L passes its bound v_i/s_i; walk the bounds upward.
    bounds = sorted((sleeve[isin] / s_i, isin) for isin, s_i in targets.items())
    level_min = bounds[-1][0]
    c_min = level_min - S
    if -_FLOAT_CLAMP <= c_min < 0.0:
        c_min = 0.0
    cash = c_min if budget is None else budget
    if cash >= c_min:
        level = S + cash
    else:
        s_sum = v_sum = 0.0
        level = 0.0
        for k, (_, isin) in enumerate(bounds):
            s_sum += targets[isin]
            v_sum += sleeve[isin]
            level = (cash + v_sum) / s_sum
            if k + 1 == len(bounds) or level <= bounds[k + 1][0]:
                break

    buys: dict[str, float] = {}
    for isin, s_i in targets.items():
        c_i = s_i * level - sleeve[isin]
        buys[isin] = c_i if c_i > _FLOAT_CLAMP else 0.0
    buys.update({isin: 0.0 for isin in frozen_held})
    binders = sorted(isin for b, isin in bounds if b >= level - _FLOAT_CLAMP)

    return RebalancePlan(
        feasible=True,
        reason=None,
        unvaluable_targets=[],
        v=V,
        v_prime=V + c_min,
        c_min=c_min,
        buys=buys,
        binders=binders,
        residual_bound_binds=False,
        sleeve=True,
        frozen=frozen_held,
        budget=budget,
        sleeve_v=S,
        level=level,
    )


def plan_rebalance(
    targets: dict[str, float],
    values: dict[str, float | None],
    held: frozenset[str],
    frozen: frozenset[str] | None = None,
    budget: float | None = None,
) -> RebalancePlan:
    """A sleeve plan when a frozen set is given (ADR-0060), else the residual plan."""
    if frozen is None:
        if budget is not None:
            raise ValueError("a budget needs a frozen set (sleeve mode)")
        return compute_rebalance(targets, values, held)
    return compute_sleeve_rebalance(targets, values, held, frozen, budget)


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
