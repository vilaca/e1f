#!/usr/bin/env python
"""e1f pick — tilt this month's rebalance buys toward funds below average cost (ADR-0062).

Starts from the scenario's buy-only rebalance plan for one month (``rebalance``'s
Monthly€) and moves a user-chosen share of the money planned for the other funds to
the funds at least ``--min-depth`` below their average cost, in proportion to plan
buy × depth below cost. A fund can pull forward at most ``--pull`` months of its own
plan buys, so a fund the plan does not buy (frozen or overweight) is never picked,
and the month's total is unchanged. The next ``rebalance`` run sees the extra and
buys less of it later.

``--record`` stores the month's plan and pick with the prices used; ``--review``
reports what each recorded tilt has gained or lost against its plain plan since.

It is an untested timing tilt: the average cost records when the user bought, not
where the price goes, and it is not backtested.

Usage:
    e1f pick --scenario core-70 --tilt 30
    e1f pick --scenario core-70 --tilt 50 --pull 3 --min-depth 3 --record
    e1f pick --review
"""

import argparse
import math
import sqlite3
import sys
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, date, datetime
from enum import StrEnum

from e1f.common import (
    _FLOAT_CLAMP,
    DEFAULT_CONFIG,
    DEFAULT_CURRENCY_META,
    DEFAULT_DB,
    DEFAULT_SCENARIOS,
    ConfigManager,
    MetricContract,
    PositionEvent,
    ScenarioError,
    Status,
    _explain_metric,
    assemble_rebalance_valuations,
    build_series,
    get_scenario,
    load_trades,
    plan_rebalance,
    position_asof,
    position_timeline,
    price_date_asof,
    unit_value_on,
)

_NAME_W = 24
_EUR_W = 10
_PCT_W = 8
DEFAULT_PULL_MONTHS = 1
DEFAULT_MIN_DEPTH_PCT = 2.0
DEFAULT_BUFFER_PCT = 0.5
# Fewer recorded months than this are an anecdote, not evidence (same bar as ADR-0055 §4).
ILLUSTRATIVE_MONTHS = 24

PICK_CONTRACT = MetricContract(
    method_version="below_cost_tilt_v2",
    requires=("evidence that buying below one's own average cost improves returns",),
    does_not_require=("return forecasts", "a covariance estimate", "synthetic history"),
    supports=("one month's buy amounts per fund", "the cash moved by the tilt"),
    limitations=(
        "untested timing signal: average cost reflects when you bought, not future returns",
        "the tilt share, pull and minimum depth are user-chosen, not fitted or validated",
        "money moves only from funds not below the minimum depth; if every bought fund is "
        "below it, nothing moves",
        "a fund the plan does not buy this month (frozen or overweight) is never picked",
        "price is the latest stored close (ftgo, official exchange close), not the broker's "
        "quote; average cost is from fill prices, so it includes the spread paid",
        "average cost is in EUR at trade time, so for a non-EUR listing it includes FX moves",
    ),
)

REVIEW_CONTRACT = MetricContract(
    method_version="below_cost_tilt_review_v1",
    requires=("the actual fills of each month's buys",),
    does_not_require=("a backtest", "synthetic history"),
    supports=("each recorded tilt's gain against its plain plan since the record",),
    limitations=(
        "both the plan and the pick are taken as bought at the recorded close; the user's "
        "real fills, timing and spread are not used",
        f"fewer than {ILLUSTRATIVE_MONTHS} recorded months is an anecdote, not evidence",
        "months overlap in time and share market moves, so they are not independent",
    ),
)


class PickError(Exception):
    """A usage or data problem the CLI reports as a clean error."""


class Pick(StrEnum):
    """What the tilt did with one fund's plan buy."""

    BELOW = "below cost"          # received extra money
    CAPPED = "below, capped"      # received extra up to the pull limit
    DONOR = "donor"               # gave part of its plan buy (not below the minimum depth)
    NO_COST = "not held"          # no average cost yet: plan buy kept
    NO_BUY = "no buy"             # the plan buys nothing, so the tilt cannot either


@dataclass(frozen=True)
class PickRow:
    isin: str
    name: str
    avg_cost: float | None    # EUR per share; None when not held
    price: float | None       # EUR per share at the latest stored close
    price_date: str | None
    estimated: bool           # price_date before as_of
    plan: float               # this month's rebalance buy
    pick: float               # this month's buy after the tilt
    outcome: Pick
    plan_limit: float | None = None  # max unit price for the Plan€ order (ADR-0062 §4)
    limit: float | None = None  # max unit price for the extra Δ€; None = no extra

    @property
    def vs_cost(self) -> float | None:
        """Price against average cost, as a fraction (negative = below cost)."""
        if self.avg_cost is None or self.price is None or self.avg_cost <= 0.0:
            return None
        return self.price / self.avg_cost - 1.0


@dataclass(frozen=True)
class PickRun:
    scenario: str
    as_of: str
    months: int
    tilt: float               # fraction of donor plan buys offered, in [0, 1]
    pull: int                 # months a fund may pull forward, after the months cap
    min_depth: float          # fraction below cost a recipient must be
    rows: list[PickRow]
    reason: str | None        # rebalance UNAVAILABLE reason, else None
    offered: float = 0.0      # tilt × Σ donor plan buys
    moved: float = 0.0        # cash actually moved (≤ offered when pull limits bind)
    pull_requested: int = DEFAULT_PULL_MONTHS
    buffer: float = DEFAULT_BUFFER_PCT / 100.0  # plan limit above the last close

    @property
    def feasible(self) -> bool:
        return self.reason is None

    @property
    def plan_total(self) -> float:
        return sum(row.plan for row in self.rows)


@dataclass(frozen=True)
class ReviewRow:
    month: str
    scenario: str
    as_of: str                # the day the pick was computed
    moved: float              # Σ positive Δ€
    gain: float | None        # Σ Δ€ × (price now / price then − 1); None when unpriced
    unpriced: tuple[str, ...]  # funds with a Δ€ and no price now


@dataclass(frozen=True)
class Review:
    as_of: str
    rows: list[ReviewRow]

    @property
    def priced(self) -> list[ReviewRow]:
        return [r for r in self.rows if r.gain is not None]


# ---------------------------------------------------------------------------
# Pure tilt math.
# ---------------------------------------------------------------------------


def tilt_buys(
    plan: dict[str, float],
    vs_cost: dict[str, float | None],
    tilt: float,
    pull: int,
    min_depth: float = 0.0,
) -> tuple[dict[str, float], dict[str, Pick], float, float]:
    """Move ``tilt`` of the donors' plan buys to the funds below cost (ADR-0062 §2).

    ``plan``: ISIN → this month's plan buy (≥ 0).  ``vs_cost``: ISIN → price / average
    cost − 1, or None when the fund has no average cost.  Returns the tilted buys,
    each fund's outcome, the cash offered and the cash moved.

    Recipients (plan > 0, vs_cost ≤ −min_depth) share the moved cash in proportion
    to plan × depth, where depth = −vs_cost, each up to pull × plan; the cash a
    capped fund cannot take goes to the others, and what no recipient can take stays
    with the donors.  Donors (plan > 0, any other vs_cost) each give the same
    fraction of their plan buy.  Σ tilted buys = Σ plan buys.
    """
    if not 0.0 <= tilt <= 1.0:
        raise ValueError("tilt must be in [0, 1]")
    if pull < 0:
        raise ValueError("pull must be ≥ 0")
    if not 0.0 <= min_depth < 1.0:
        raise ValueError("min_depth must be in [0, 1)")

    outcome: dict[str, Pick] = {}
    for isin, buy in plan.items():
        gap = vs_cost.get(isin)
        if buy <= _FLOAT_CLAMP:
            outcome[isin] = Pick.NO_BUY
        elif gap is None:
            outcome[isin] = Pick.NO_COST
        elif gap < 0.0 and -gap >= min_depth:
            outcome[isin] = Pick.BELOW
        else:
            outcome[isin] = Pick.DONOR

    donors = [isin for isin, o in outcome.items() if o is Pick.DONOR]
    recipients = [isin for isin, o in outcome.items() if o is Pick.BELOW]
    donor_total = sum(plan[isin] for isin in donors)
    offered = tilt * donor_total if recipients else 0.0
    headroom = {isin: pull * plan[isin] for isin in recipients}
    moved = min(offered, sum(headroom.values()))

    extra = {isin: 0.0 for isin in recipients}
    remaining = moved
    active = list(recipients)
    while active and remaining > _FLOAT_CLAMP:
        weight = {isin: plan[isin] * -(vs_cost[isin] or 0.0) for isin in active}
        total_w = sum(weight.values())
        share = {isin: remaining * weight[isin] / total_w for isin in active}
        full = [isin for isin in active if share[isin] >= headroom[isin] - _FLOAT_CLAMP]
        if not full:
            for isin in active:
                extra[isin] = share[isin]
            break
        for isin in full:
            extra[isin] = headroom[isin]
            remaining -= headroom[isin]
            outcome[isin] = Pick.CAPPED
            active.remove(isin)
    if moved <= _FLOAT_CLAMP and pull == 0:
        for isin in recipients:
            outcome[isin] = Pick.CAPPED

    give = moved / donor_total if donor_total > 0.0 else 0.0
    tilted = dict(plan)
    for isin in donors:
        tilted[isin] = plan[isin] * (1.0 - give)
    for isin in recipients:
        tilted[isin] = plan[isin] + extra[isin]
    return tilted, outcome, offered, moved


def tilt_gain(deltas: list[tuple[float, float, float | None]]) -> float | None:
    """Σ Δ€ × (price now / price then − 1) over ``(Δ€, price then, price now)``.

    The tilt's gain against its plain plan when both are bought at the recorded close:
    a euro moved into fund i and out of fund j earns r_i − r_j.  None when a fund with
    a non-zero Δ€ has no price now (or no positive price then).
    """
    gain = 0.0
    for delta, then, now in deltas:
        if abs(delta) <= _FLOAT_CLAMP:
            continue
        if now is None or then <= 0.0:
            return None
        gain += delta * (now / then - 1.0)
    return gain


def plan_limit(price: float | None, buffer: float) -> float | None:
    """Max unit price for a Plan€ order: the last close plus ``buffer``.

    It fills at once in a normal market and only guards against a bad quote or a jump
    since the close; it is never under the price to catch a lower one, which
    ``limitbuy`` found loses on average (ADR-0055, ADR-0057).
    """
    if price is None or price <= 0.0:
        return None
    return price * (1.0 + buffer)


def extra_limit(
    avg_cost: float | None, min_depth: float, plan_max: float | None
) -> float | None:
    """Max unit price for a fund's extra Δ€: still ``min_depth`` below cost, and no
    higher than the Plan€ order's limit.

    The first bound keeps the tilt's own rule at execution: the extra fills only while
    the fund is still a recipient.  The second keeps the extra from being a more
    eager order than the plan buy.  For a recipient the last close is at or below
    both, so the order is marketable when placed.
    """
    if avg_cost is None or avg_cost <= 0.0 or plan_max is None:
        return None
    return min(avg_cost * (1.0 - min_depth), plan_max)


# ---------------------------------------------------------------------------
# Data assembly.
# ---------------------------------------------------------------------------


def _eur_price(
    db_path: str, isin: str, events: list[PositionEvent], as_of: str, currency_meta_path: str
) -> tuple[float | None, str | None]:
    series = build_series(db_path, isin, events, as_of, currency_meta_path)
    return unit_value_on(series, as_of, db_path), price_date_asof(series, as_of)


def evaluate(
    db_path: str,
    config_path: str,
    currency_meta_path: str,
    *,
    scenario_name: str,
    scenarios_path: str,
    as_of: str,
    tilt: float,
    pull: int = DEFAULT_PULL_MONTHS,
    min_depth: float = DEFAULT_MIN_DEPTH_PCT / 100.0,
    buffer: float = DEFAULT_BUFFER_PCT / 100.0,
    months: int | None = None,
    monthly: float | None = None,
) -> PickRun:
    """This month's plan for ``scenario_name`` and its below-cost tilt."""
    try:
        scenario = get_scenario(scenario_name, scenarios_path)
    except ScenarioError as exc:
        raise PickError(str(exc)) from exc
    months = months if months is not None else (scenario.months or 1)
    monthly = monthly if monthly is not None else scenario.monthly
    frozen = None if scenario.frozen is None else frozenset(scenario.frozen)
    if frozen is None and monthly is not None:
        raise PickError("--monthly needs a sleeve scenario (one with 'frozen')")
    budget = None if monthly is None else monthly * months
    # A fund cannot pull forward more months than the plan has after this one.
    effective_pull = min(pull, months - 1)

    targets = {isin: pct / 100.0 for isin, pct in scenario.targets.items()}
    values, held, _unvaluable, _dates = assemble_rebalance_valuations(
        db_path, currency_meta_path, targets, as_of
    )
    plan = plan_rebalance(targets, values, held, frozen, budget)
    if not plan.feasible:
        return PickRun(scenario_name, as_of, months, tilt, effective_pull, min_depth, [],
                       plan.reason, pull_requested=pull, buffer=buffer)
    base = {isin: buy / months for isin, buy in plan.buys.items()
            if frozen is None or isin not in frozen}

    timeline = position_timeline(load_trades(db_path))
    config = ConfigManager(config_path)
    prices: dict[str, tuple[float | None, float | None, str | None]] = {}
    for isin in base:
        events = [e for e in timeline.get(isin, []) if e.date <= as_of]
        shares, cost = position_asof(events, as_of) if events else (0.0, 0.0)
        price, pd = _eur_price(db_path, isin, events, as_of, currency_meta_path)
        prices[isin] = (cost / shares if shares > _FLOAT_CLAMP else None, price, pd)

    vs_cost: dict[str, float | None] = {}
    for isin, (avg, price, _pd) in prices.items():
        vs_cost[isin] = None if avg is None or price is None or avg <= 0.0 else price / avg - 1.0
    tilted, outcome, offered, moved = tilt_buys(base, vs_cost, tilt, effective_pull, min_depth)

    rows = []
    for isin in base:
        avg, price, pd = prices[isin]
        plan_max = plan_limit(price, buffer) if tilted[isin] > _FLOAT_CLAMP else None
        rows.append(PickRow(
            isin=isin,
            name=str((config.get(isin) or {}).get("name", ""))[:_NAME_W],
            avg_cost=avg,
            price=price,
            price_date=pd,
            estimated=pd is not None and pd < as_of,
            plan=base[isin],
            pick=tilted[isin],
            outcome=outcome[isin],
            plan_limit=plan_max,
            limit=extra_limit(avg, min_depth, plan_max)
            if tilted[isin] > base[isin] + _FLOAT_CLAMP else None,
        ))
    rows.sort(key=lambda r: (-r.plan, r.isin))
    return PickRun(scenario_name, as_of, months, tilt, effective_pull, min_depth, rows, None,
                   offered, moved, pull_requested=pull, buffer=buffer)


# ---------------------------------------------------------------------------
# Record and review (ADR-0062 §5).
# ---------------------------------------------------------------------------

_PICKS_DDL = """
    CREATE TABLE IF NOT EXISTS picks (
        month TEXT NOT NULL,
        scenario TEXT NOT NULL,
        isin TEXT NOT NULL,
        as_of TEXT NOT NULL,
        plan REAL NOT NULL,
        pick REAL NOT NULL,
        price REAL,
        tilt REAL NOT NULL,
        pull INTEGER NOT NULL,
        min_depth REAL NOT NULL,
        PRIMARY KEY (month, scenario, isin)
    )
"""


def record(db_path: str, run: PickRun, *, replace: bool = False) -> int:
    """Store ``run`` as its month's pick; returns the rows written.

    Refuses an UNAVAILABLE run, and a month already recorded for the scenario unless
    ``replace``.  The old month is deleted and the new one inserted in one
    transaction, so a failure leaves the previous record in place.
    """
    if not run.feasible:
        raise PickError(f"nothing to record: the rebalance plan is {run.reason}")
    month = run.as_of[:7]
    with closing(sqlite3.connect(db_path)) as conn:
        conn.execute(_PICKS_DDL)
        existing = conn.execute(
            "SELECT as_of FROM picks WHERE month = ? AND scenario = ? LIMIT 1",
            (month, run.scenario),
        ).fetchone()
        if existing is not None and not replace:
            raise PickError(
                f"{run.scenario} already has a pick recorded for {month} (computed "
                f"{existing[0]}); pass --replace to overwrite it"
            )
        with conn:
            conn.execute("DELETE FROM picks WHERE month = ? AND scenario = ?",
                         (month, run.scenario))
            conn.executemany(
                "INSERT INTO picks VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [(month, run.scenario, r.isin, run.as_of, r.plan, r.pick, r.price,
                  run.tilt, run.pull, run.min_depth) for r in run.rows],
            )
    return len(run.rows)


def review(db_path: str, currency_meta_path: str, *, as_of: str) -> Review:
    """Each recorded month's tilt gain against its plain plan, priced as of ``as_of``."""
    with closing(sqlite3.connect(db_path)) as conn:
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='picks'"
                        ).fetchone() is None:
            return Review(as_of, [])
        stored = conn.execute(
            "SELECT month, scenario, as_of, isin, plan, pick, price FROM picks "
            "WHERE as_of <= ? ORDER BY month, scenario, isin", (as_of,)
        ).fetchall()

    grouped: dict[tuple[str, str, str], list[tuple[str, float, float, float | None]]] = {}
    for month, scenario, day, isin, plan_buy, pick_buy, price in stored:
        grouped.setdefault((month, scenario, day), []).append(
            (isin, pick_buy - plan_buy, plan_buy, price)
        )

    now_cache: dict[str, float | None] = {}
    rows = []
    for (month, scenario, day), funds in grouped.items():
        deltas = []
        unpriced = []
        for isin, delta, _plan, then in funds:
            if abs(delta) <= _FLOAT_CLAMP:
                continue
            if isin not in now_cache:
                now_cache[isin] = _eur_price(db_path, isin, [], as_of, currency_meta_path)[0]
            now = now_cache[isin]
            if now is None or then is None:
                unpriced.append(isin)
            deltas.append((delta, then or 0.0, now))
        gain = None if unpriced else tilt_gain(deltas)
        moved = sum(d for d, _t, _n in deltas if d > 0.0)
        rows.append(ReviewRow(month, scenario, day, moved, gain, tuple(unpriced)))
    return Review(as_of, rows)


# ---------------------------------------------------------------------------
# Rendering.
# ---------------------------------------------------------------------------


def _money(value: float | None, *, stale: bool = False) -> str:
    return "—" if value is None else f"{value:,.2f}" + ("~" if stale else "")


def _signed_pct(value: float | None) -> str:
    return "—" if value is None else f"{value * 100:+.1f}%"


def render(run: PickRun, *, explain: bool) -> list[str]:
    lines = [
        f"\nPick — this month's plan tilted toward funds below average cost, "
        f"as of {run.as_of} (EUR)",
        f"Scenario {run.scenario} · {run.months}-month plan · tilt {run.tilt * 100:g}% · "
        f"pull ≤ {run.pull} month(s) · below cost by ≥ {run.min_depth * 100:g}%",
        "Experimental (ADR-0062): an untested timing tilt, not a forecast.",
    ]
    if not run.feasible:
        lines.append(
            f"\nUNAVAILABLE — the rebalance plan is {run.reason}; "
            f"run 'e1f rebalance --scenario {run.scenario}' for the reason."
        )
        return lines

    header = (
        f"{'ISIN':<12} {'Name':<{_NAME_W}} {'Avg€':>{_EUR_W}} {'Price€':>{_EUR_W}} "
        f"{'vsCost':>{_PCT_W}} {'Plan€':>{_EUR_W}} {'Pick€':>{_EUR_W}} {'Δ€':>{_EUR_W}} "
        f"{'MaxPlan€':>{_EUR_W}} {'MaxΔ€':>{_EUR_W}}  Pick"
    )
    rule = "-" * (len(header) + 12)
    lines += ["", header, rule]
    for r in run.rows:
        lines.append(
            f"{r.isin:<12} {r.name:<{_NAME_W}} {_money(r.avg_cost):>{_EUR_W}} "
            f"{_money(r.price, stale=r.estimated):>{_EUR_W}} {_signed_pct(r.vs_cost):>{_PCT_W}} "
            f"{_money(r.plan):>{_EUR_W}} {_money(r.pick):>{_EUR_W}} "
            f"{r.pick - r.plan:>+{_EUR_W},.2f} {_money(r.plan_limit):>{_EUR_W}} "
            f"{_money(r.limit):>{_EUR_W}}  {r.outcome.value}"
        )
    lines += [rule, f"{'TOTAL':<{12 + 1 + _NAME_W + 3 * (_EUR_W + 1) - 1}} "
                    f"{_money(run.plan_total):>{_EUR_W}} {_money(run.plan_total):>{_EUR_W}}"]
    lines += ["", *_summary(run)]
    stale = [f"{r.isin} ({r.price_date})" for r in run.rows if r.estimated]
    if stale:
        lines.append(f"~ latest close before {run.as_of}: {', '.join(stale)}")
    if explain:
        lines += _explain(run)
    return lines


def _summary(run: PickRun) -> list[str]:
    below = [r for r in run.rows if r.outcome in (Pick.BELOW, Pick.CAPPED)]
    donors = [r for r in run.rows if r.outcome is Pick.DONOR]
    capped = [r for r in run.rows if r.outcome is Pick.CAPPED]
    depth = f"{run.min_depth * 100:g}%"
    lines: list[str] = []
    if not below:
        lines.append(f"No fund the plan buys is at least {depth} below its average cost "
                     f"— Pick€ = Plan€.")
    elif not donors:
        lines.append(f"Every fund the plan buys is at least {depth} below cost; the tilt "
                     f"moves money only from the others, so Pick€ = Plan€.")
    elif run.pull == 0:
        lines.append(f"The pull limit is 0 months (a {run.months}-month plan has no later "
                     f"buys to pull forward), so Pick€ = Plan€.")
    else:
        lines.append(f"Moved €{_money(run.moved)} from {len(donors)} donor fund(s) to "
                     f"{len(below)} at least {depth} below cost (offered "
                     f"€{_money(run.offered)}).")
        if run.moved < run.offered - 0.005:
            lines.append(f"Pull limit bound: {', '.join(r.isin for r in capped)} at "
                         f"{run.pull} month(s) of plan; €{_money(run.offered - run.moved)} "
                         f"stayed with the donors.")
    if any(r.plan_limit is not None for r in run.rows):
        lines.append(f"Orders are buy-limits at a max unit price: Plan€ at MaxPlan€ (last close "
                     f"+ {run.buffer * 100:g}%), and Δ€ at MaxΔ€ (still ≥ {depth} below cost, "
                     f"never above MaxPlan€); where they are equal, one order of Pick€.")
        if any(r.limit is not None for r in run.rows):
            lines.append("If the Δ€ order does not fill, the fund has recovered and the extra "
                         "is not needed.")
        if any(r.estimated for r in run.rows if r.plan_limit is not None):
            lines.append("Some limits use a close before the as-of day (~): run 'e1f fetch' "
                         "before placing orders.")
    if run.pull < run.pull_requested:
        lines.append(f"--pull {run.pull_requested} reduced to {run.pull}: the plan has "
                     f"{run.months} month(s).")
    no_buy = [r for r in run.rows if r.outcome is Pick.NO_BUY
              and r.vs_cost is not None and r.vs_cost <= -run.min_depth]
    if no_buy:
        lines.append(f"Below cost but not bought by the plan (overweight): "
                     f"{', '.join(r.isin for r in no_buy)}.")
    return lines


def _explain(run: PickRun) -> list[str]:
    result = (f"€{_money(run.moved)} of €{_money(run.plan_total)} moved; "
              f"Σ Pick€ = Σ Plan€")
    inputs = (f"scenario {run.scenario} rebalance plan ÷ {run.months} months; "
              f"average cost = Σ fill cost / shares across brokers; latest close × "
              f"nearest-prior EUR FX as of {run.as_of}")
    method = (f"recipients: vsCost ≤ −{run.min_depth:g}; offered = {run.tilt:g} × Σ Plan€ "
              f"of the other bought funds; recipients share it ∝ Plan€ × depth below cost, "
              f"each ≤ {run.pull} × Plan€; donors each give moved / Σ donor Plan€ of their buy")
    return [
        "\nProvenance (--explain) — reconstructed from source, not a log:",
        *_explain_metric("Below-cost tilt", Status.CALCULATED, result, inputs, method,
                         PICK_CONTRACT),
    ]


def render_review(rev: Review, *, explain: bool) -> list[str]:
    lines = [
        f"\nPick review — each recorded tilt against its plain plan, priced as of "
        f"{rev.as_of} (EUR)",
        "Both buys are taken at the recorded close; Gain€ = Σ Δ€ × (price now / then − 1).",
    ]
    if not rev.rows:
        return [*lines, "\nNo picks recorded — run 'e1f pick ... --record' each month."]
    header = (f"{'Month':<8} {'Scenario':<20} {'Computed':<10} {'Moved€':>{_EUR_W}} "
              f"{'Gain€':>{_EUR_W}} {'Gain%':>{_PCT_W}}")
    rule = "-" * len(header)
    lines += ["", header, rule]
    for r in rev.rows:
        pct = None if r.gain is None or r.moved <= 0.0 else r.gain / r.moved
        lines.append(f"{r.month:<8} {r.scenario[:20]:<20} {r.as_of:<10} "
                     f"{_money(r.moved):>{_EUR_W}} {_money(r.gain):>{_EUR_W}} "
                     f"{_signed_pct(pct):>{_PCT_W}}")
    priced = rev.priced
    moved = sum(r.moved for r in priced)
    gain = sum(r.gain or 0.0 for r in priced)
    lines += [rule, f"{'TOTAL':<{8 + 1 + 20 + 1 + 10}} {_money(moved):>{_EUR_W}} "
                    f"{_money(gain):>{_EUR_W}} "
                    f"{_signed_pct(gain / moved if moved > 0.0 else None):>{_PCT_W}}", ""]
    unpriced = [r for r in rev.rows if r.gain is None]
    if unpriced:
        lines.append("UNAVAILABLE (no price now, excluded from TOTAL): " + ", ".join(
            f"{r.month} {r.scenario} ({', '.join(r.unpriced)})" for r in unpriced))
    if len(priced) < ILLUSTRATIVE_MONTHS:
        lines.append(f"{len(priced)} priced month(s): fewer than {ILLUSTRATIVE_MONTHS} is an "
                     f"anecdote, not evidence that the tilt works.")
    if explain:
        status = Status.CALCULATED if priced else Status.UNAVAILABLE
        lines += [
            "\nProvenance (--explain) — reconstructed from source, not a log:",
            *_explain_metric(
                "Below-cost tilt review", status,
                f"€{_money(gain)} on €{_money(moved)} moved over {len(priced)} month(s)",
                f"picks table; latest close × nearest-prior EUR FX as of {rev.as_of}",
                "per month Σ (Pick€ − Plan€) × (price now / recorded price − 1)",
                REVIEW_CONTRACT,
            ),
        ]
    return lines


# ---------------------------------------------------------------------------
# CLI.
# ---------------------------------------------------------------------------


def _number(text: str) -> float:
    try:
        value = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text!r} is not a number") from None
    if not math.isfinite(value):
        raise argparse.ArgumentTypeError(f"must be finite (got {text})")
    return value


def _tilt_pct(text: str) -> float:
    value = _number(text)
    if not 0.0 <= value <= 100.0:
        raise argparse.ArgumentTypeError(f"must be in [0, 100] (got {text})")
    return value / 100.0


def _depth_pct(text: str) -> float:
    value = _number(text)
    if not 0.0 <= value < 100.0:
        raise argparse.ArgumentTypeError(f"must be in [0, 100) (got {text})")
    return value / 100.0


def _positive(text: str) -> float:
    value = _number(text)
    if value <= 0.0:
        raise argparse.ArgumentTypeError(f"must be > 0 (got {text})")
    return value


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


def _valid_date(text: str) -> str:
    try:
        date.fromisoformat(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"must be YYYY-MM-DD: {text}") from exc
    return text


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="e1f pick",
        description="Tilt this month's rebalance buys toward funds below their average "
        "cost (ADR-0062, experimental).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
Plan€ is the scenario's rebalance buy for one month ('rebalance' Monthly€). --tilt PCT
of the Plan€ of the other bought funds moves to the funds at least --min-depth below
their average cost, in proportion to Plan€ × depth below cost. Each can pull forward
at most --pull months of its Plan€ (never more than the plan's months − 1). The
month's total is unchanged, frozen and overweight funds (Plan€ 0) are never picked,
and the next 'rebalance' run buys less of what the tilt bought.

--record stores the month's Plan€, Pick€ and prices in the DB (one record per
scenario and month; --replace overwrites). --review reports each record's gain
against its plain plan since.

  e1f pick --scenario core-70 --tilt 30
  e1f pick --scenario core-70 --tilt 50 --pull 3 --min-depth 3 --record
  e1f pick --review
""",
    )
    parser.add_argument("--scenario", "-s", metavar="NAME",
                        help="Saved scenario whose rebalance plan is tilted")
    parser.add_argument("--tilt", type=_tilt_pct, metavar="PCT",
                        help="Percent of the donor funds' Plan€ offered to the funds below "
                        "cost, in [0, 100] (required with --scenario; no default)")
    parser.add_argument("--pull", type=_int_at_least(0), default=DEFAULT_PULL_MONTHS,
                        metavar="N", help="Months of its own Plan€ a fund below cost may "
                        f"pull forward (default {DEFAULT_PULL_MONTHS}, i.e. at most 2× Plan€)")
    parser.add_argument("--min-depth", type=_depth_pct, default=DEFAULT_MIN_DEPTH_PCT / 100.0,
                        metavar="PCT", help="How far below average cost a fund must be to "
                        f"receive, in percent (default {DEFAULT_MIN_DEPTH_PCT:g})")
    parser.add_argument("--buffer", type=_depth_pct, default=DEFAULT_BUFFER_PCT / 100.0,
                        metavar="PCT", help="Max unit price of a Plan€ order above the last "
                        f"close, in percent (default {DEFAULT_BUFFER_PCT:g})")
    parser.add_argument("--monthly", type=_positive, metavar="EUR",
                        help="Override the scenario's monthly budget")
    parser.add_argument("--months", type=_int_at_least(1), metavar="N",
                        help="Override the scenario's months")
    parser.add_argument("--record", action="store_true",
                        help="Store this month's pick in the DB for --review")
    parser.add_argument("--replace", action="store_true",
                        help="With --record: overwrite this month's stored pick")
    parser.add_argument("--review", action="store_true",
                        help="Report every recorded pick's gain against its plain plan")
    parser.add_argument("--scenarios-file", default=DEFAULT_SCENARIOS,
                        help="Scenarios YAML file path")
    parser.add_argument("--as-of", type=_valid_date,
                        default=datetime.now(UTC).date().isoformat(), metavar="YYYY-MM-DD",
                        help="Value the book as of this date (default: today)")
    parser.add_argument("--db", "-d", default=DEFAULT_DB, help="Database file path")
    parser.add_argument("--config", "-c", default=DEFAULT_CONFIG, help="ETF universe config")
    parser.add_argument("--currency-meta", default=DEFAULT_CURRENCY_META,
                        help="Pinned currency metadata YAML")
    parser.add_argument("--explain", action="store_true", help="Add the provenance block")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.review:
        if args.scenario or args.tilt is not None or args.record:
            parser.error("--review takes no --scenario, --tilt or --record")
        rev = review(args.db, args.currency_meta, as_of=args.as_of)
        print("\n".join(render_review(rev, explain=args.explain)))
        return 0
    if not args.scenario or args.tilt is None:
        parser.error("--scenario and --tilt are required (or use --review)")
    if args.replace and not args.record:
        parser.error("--replace needs --record")
    try:
        run = evaluate(
            args.db, args.config, args.currency_meta,
            scenario_name=args.scenario, scenarios_path=args.scenarios_file,
            as_of=args.as_of, tilt=args.tilt, pull=args.pull, min_depth=args.min_depth,
            buffer=args.buffer,
            months=args.months, monthly=args.monthly,
        )
        print("\n".join(render(run, explain=args.explain)))
        if args.record:
            written = record(args.db, run, replace=args.replace)
            print(f"\nRecorded {written} fund(s) for {run.scenario} {run.as_of[:7]}.")
    except PickError as exc:
        print(f"✗ {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
