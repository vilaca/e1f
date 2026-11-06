#!/usr/bin/env python
"""e1f rebalance — minimum-cash buy-only target rebalance & DCA plan (ADR-0016).

Computes the cheapest way to reach user-supplied target weights for one or more
funds without ever selling anything (dilution only), and an optional N-month
dollar-cost-averaging schedule.

Usage:
    e1f rebalance --target IE00B4L5Y983:30 --target IE00BK5BQT80:40
    e1f rebalance --target IE00B4L5Y983:30 --target IE00BK5BQT80:40 --months 10
    e1f rebalance --target IE00B4L5Y983:30 --explain
    e1f rebalance --target IE00B4L5Y983:60 --target IE00BK5BQT80:40 --frozen IE00B3YCGJ38
    e1f rebalance --scenario growth     # a stored sleeve plan, flat or by category (ADR-0061)
"""

import argparse
import math
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from e1f.common import (
    DEFAULT_CONFIG,
    DEFAULT_CURRENCY_META,
    DEFAULT_DB,
    DEFAULT_SCENARIOS,
    Category,
    ConfigManager,
    MetricContract,
    RebalancePlan,
    Status,
    _explain_metric,
    _FLOAT_CLAMP,
    assemble_rebalance_valuations as _assemble,
    compute_rebalance as compute_rebalance,  # re-exported (ADR-0017)
    get_scenario,
    load_price_series,
    load_trades,
    plan_rebalance,
    position_asof,
    position_timeline,
)

_NAME_W = 24   # name column width; " (resid)" / " (frozen)" fit within it
_EUR_W = 12    # money columns: Current€, Buy€, Monthly€, Final€
_PCT_W = 7     # percent columns: Cur% / Sleeve%, Tgt%, Book%
_STATUS_W = 11
SORT_FIELDS = ("isin", "name", "value", "weight", "tgt", "buy", "final")


REBALANCE_CONTRACT = MetricContract(
    method_version="buy_only_rebalance_v1",
    requires=("a current EUR valuation per held and targeted fund",),
    does_not_require=("return forecasts", "a covariance estimate", "look-through holdings"),
    supports=("minimum-cash buy plan", "per-fund buy amounts", "N-month DCA schedule",
              "feasibility verdict"),
    limitations=(
        "buy-only: an overweight holding is diluted, never sold",
        "plan computed at the as-of snapshot; realized weights drift as prices move — re-run",
        "targets are user-supplied, not optimized",
        "untargeted holdings share the residual pro-rata by current EUR value",
        "C_min is fresh cash to inject on top of current fund values; idle broker "
        "cash (e.g. XTB cash operations) is not a holding and is not counted",
        "keyed by net-ISIN across brokers; which broker to buy at is not chosen",
    ),
)

SLEEVE_CONTRACT = MetricContract(
    method_version="buy_only_sleeve_v1",
    requires=REBALANCE_CONTRACT.requires,
    does_not_require=REBALANCE_CONTRACT.does_not_require,
    supports=("minimum-cash or budgeted buy plan", "per-fund buy amounts",
              "N-month DCA schedule", "feasibility verdict"),
    limitations=(
        "buy-only: an overweight holding is diluted, never sold",
        "plan computed at the as-of snapshot; realized weights drift as prices move — re-run",
        "targets are user-supplied, not optimized",
        "frozen funds are never bought; their share of the book only shrinks",
        "a budget below C_min leaves the most overweight funds above target",
        "keyed by net-ISIN across brokers; which broker to buy at is not chosen",
    ),
)


# ---------------------------------------------------------------------------
# Display rows (built by the DB layer, consumed by the renderer).  The plan math
# (RebalancePlan, compute_rebalance) and valuation assembly live in `common`
# (graduated in ADR-0017); this module owns rendering + the CLI.
# ---------------------------------------------------------------------------


@dataclass
class RebalanceRow:
    isin: str
    name: str           # truncated display name (24 chars or less)
    v: float            # current EUR value
    cur_pct: float      # v / V * 100
    t_pct: float        # target % (pinned) or implied residual % (untargeted)
    buy: float
    final_v: float      # v + buy (final % is omitted: it equals t_pct by construction)
    is_residual: bool
    is_binder: bool
    price_date: str | None
    estimated: bool     # price_date is not None and price_date < as_of
    status: Status
    is_frozen: bool = False  # sleeve plan: never bought; cur_pct/t_pct unused
    book_pct: float = 0.0    # sleeve plan: final_v / V' * 100


# ---------------------------------------------------------------------------
# Formatting helpers (local copies — command→command import would break ADR-0003).
# ---------------------------------------------------------------------------


def _fmt_money(v: float, *, stale: bool = False) -> str:
    return f"{v:,.2f}" + ("~" if stale else "")


def _fmt_pct(v: float) -> str:
    return f"{v:.1f}%"


def _display_name(name: str, *, is_residual: bool, is_frozen: bool = False) -> str:
    if is_frozen:
        return f"{name[:(_NAME_W - 9)]} (frozen)"
    if is_residual:
        return f"{name[:(_NAME_W - 8)]} (resid)"
    return name[:_NAME_W]


# ---------------------------------------------------------------------------
# Rendering — table + --explain block.
# ---------------------------------------------------------------------------


def _header_line(*, months: int, show_status: bool, sleeve: bool = False) -> str:
    monthly = f" {'Monthly€':>{_EUR_W}}" if months > 1 else ""
    status = f" {'Status':>{_STATUS_W}}" if show_status else ""
    cur = "Sleeve%" if sleeve else "Cur%"
    book = f" {'Book%':>{_PCT_W}}" if sleeve else ""
    return (
        f"{'ISIN':<14} {'Name':<{_NAME_W}} {'Current€':>{_EUR_W}} {cur:>{_PCT_W}}"
        f" {'Tgt%':>{_PCT_W}} {'Buy€':>{_EUR_W}}{monthly}"
        f" {'Final€':>{_EUR_W}}{book}{status}"
    )


def _per_month(amount: float, months: int, first_share: float | None) -> float:
    """The buy for the coming month: an equal 1/months, or ``first_share`` of the plan
    when this month's earlier buys shrank the first instalment."""
    return amount / months if first_share is None else amount * first_share


def _format_row(
    row: RebalanceRow, *, months: int, show_status: bool, sleeve: bool = False,
    marker: str = "◄ binds", indent: bool = False, first_share: float | None = None,
) -> str:
    name_cell = _display_name(row.name, is_residual=row.is_residual, is_frozen=row.is_frozen)
    money_cur = _fmt_money(row.v, stale=row.estimated)
    monthly = (
        f" {_fmt_money(_per_month(row.buy, months, first_share)):>{_EUR_W}}"
        if months > 1 else ""
    )
    binder = f"  {marker}" if row.is_binder else ""
    status = f" {row.status.value:>{_STATUS_W}}" if show_status else ""
    cur, tgt = (
        ("—", "—") if row.is_frozen else (_fmt_pct(row.cur_pct), _fmt_pct(row.t_pct))
    )
    book = f" {_fmt_pct(row.book_pct):>{_PCT_W}}" if sleeve else ""
    isin_cell = f"  {row.isin}" if indent else row.isin  # a fund under its category
    return (
        f"{isin_cell:<14} {name_cell:<{_NAME_W}}"
        f" {money_cur:>{_EUR_W}} {cur:>{_PCT_W}}"
        f" {tgt:>{_PCT_W}} {_fmt_money(row.buy):>{_EUR_W}}{monthly}"
        f" {_fmt_money(row.final_v):>{_EUR_W}}{book}{status}{binder}"
    )


def _rule_width(*, months: int, show_status: bool, sleeve: bool = False) -> int:
    # 14 (ISIN) + 1 + NAME_W + 1 + EUR_W + 1 + PCT_W (Cur) + 1 + PCT_W (Tgt)
    # + 1 + EUR_W (Buy) + [1+EUR_W (Monthly)] + 1 + EUR_W (Final) + [1+PCT_W (Book)]
    w = 14 + 1 + _NAME_W + 1 + _EUR_W + 1 + _PCT_W + 1 + _PCT_W + 1 + _EUR_W
    w += 1 + _EUR_W  # Final€
    if months > 1:
        w += 1 + _EUR_W
    if sleeve:
        w += 1 + _PCT_W
    if show_status:
        w += 1 + _STATUS_W
    return w


def _scaled_targets(targets: dict[str, float]) -> list[tuple[str, float, float]]:
    """Targets normalized to 100% among themselves.

    Returns ``[(isin, book_fraction, scaled_fraction)]`` sorted by weight
    descending (ties by ISIN). ``scaled_fraction = book_fraction / Σ book`` —
    the targeted funds' shares of the *targeted sleeve* alone (Σ scaled = 1),
    as opposed to their shares of the whole book (Σ book = 1 − residual).
    """
    t_sum = sum(targets.values())
    rows = [
        (isin, t, (t / t_sum if t_sum > 0.0 else 0.0)) for isin, t in targets.items()
    ]
    rows.sort(key=lambda r: (-r[1], r[0]))
    return rows


def render_target_summary(targets: dict[str, float], names: dict[str, str]) -> list[str]:
    """Recap of the user-supplied targets (ADR-0016 decision 9).

    A compact table of each targeted fund's book weight (``Tgt%``, of the whole
    valued book) alongside its ``Scaled%`` (of the targeted sleeve, normalized to
    100%). The TOTAL row's ``Tgt%`` is the target sum (< 100% ⇒ a residual);
    ``Scaled%`` always totals 100%.
    """
    t_sum = sum(targets.values())
    rule = "-" * (14 + 1 + _NAME_W + 1 + _PCT_W + 1 + _PCT_W)
    lines = [
        "Targets (Tgt% = of book · Scaled% = of targeted sleeve, normalized to 100%):",
        "",
        f"{'ISIN':<14} {'Name':<{_NAME_W}} {'Tgt%':>{_PCT_W}} {'Scaled%':>{_PCT_W}}",
        rule,
    ]
    for isin, book_frac, scaled_frac in _scaled_targets(targets):
        name = names.get(isin, "")[:_NAME_W]
        lines.append(
            f"{isin:<14} {name:<{_NAME_W}}"
            f" {_fmt_pct(book_frac * 100):>{_PCT_W}} {_fmt_pct(scaled_frac * 100):>{_PCT_W}}"
        )
    lines.append(rule)
    lines.append(
        f"{'TOTAL':<14} {'':<{_NAME_W}}"
        f" {_fmt_pct(t_sum * 100):>{_PCT_W}} {_fmt_pct(100.0):>{_PCT_W}}"
    )
    return lines


def render_table(
    rows: list[RebalanceRow],
    plan: RebalancePlan,
    *,
    as_of: str,
    months: int,
    show_status: bool,
    untargeted_unvaluable: list[str],
    stale_rows: list[RebalanceRow],
    target_summary: list[str],
) -> list[str]:
    """Render the full rebalance table for a feasible plan."""
    dca = f"   ·   DCA: {months} month{'s' if months > 1 else ''}" if months > 1 else ""
    lines: list[str] = [f"\nBuy-only rebalance to target as of {as_of} (EUR){dca}", ""]
    lines.extend(target_summary)
    lines.append("")
    lines.append(_header_line(months=months, show_status=show_status))
    rule = "-" * _rule_width(months=months, show_status=show_status)
    lines.append(rule)
    for row in rows:
        lines.append(_format_row(row, months=months, show_status=show_status))
    lines.append(rule)

    # TOTAL row
    monthly_total = (
        f" {_fmt_money(plan.c_min / months):>{_EUR_W}}" if months > 1 else ""
    )
    status_total = f" {'':>{_STATUS_W}}" if show_status else ""
    total_line = (
        f"{'TOTAL':<14} {'':>{_NAME_W}} {_fmt_money(plan.v):>{_EUR_W}}"
        f" {_fmt_pct(100.0):>{_PCT_W}} {_fmt_pct(100.0):>{_PCT_W}}"
        f" {_fmt_money(plan.c_min):>{_EUR_W}}{monthly_total}"
        f" {_fmt_money(plan.v_prime):>{_EUR_W}}{status_total}"
    )
    lines.append(total_line)
    lines.append("")

    # Summary line
    if plan.c_min <= 0.0:
        lines.append("Portfolio already at target — no purchases needed.")
    elif months > 1:
        lines.append(
            f"Inject €{_fmt_money(plan.c_min)} total"
            f" (€{_fmt_money(plan.c_min / months)} / month × {months})"
            f" to reach targets buy-only."
        )
    else:
        lines.append(f"Inject €{_fmt_money(plan.c_min)} to reach targets buy-only.")

    # Footer: untargeted unvaluable (excluded from table, disclosed here)
    footer = ", ".join(untargeted_unvaluable) if untargeted_unvaluable else "—"
    lines.append(f"Untargeted (unvaluable, excluded): {footer}")
    lines.extend(_stale_lines(stale_rows, as_of))
    return lines


def _stale_lines(stale_rows: list[RebalanceRow], as_of: str) -> list[str]:
    """Stale-close disclosure for the ``~`` rows."""
    if not stale_rows:
        return []
    if len(stale_rows) == 1:
        row = stale_rows[0]
        return [
            f"\n~ Current€ estimated: no close on {as_of} — "
            f"freshest data is {row.price_date} for {row.isin} (fetch to refresh)."
        ]
    items = ", ".join(
        f"{row.isin} ({row.price_date})" for row in sorted(stale_rows, key=lambda r: r.isin)
    )
    return [
        f"\n~ Current€ estimated from the latest price before {as_of} "
        f"(no close on the as-of day — fetch to refresh): {items}"
    ]


def _cash_phrase(amount: float, months: int) -> str:
    phrase = f"€{_fmt_money(amount)}"
    if months > 1:
        phrase += f" (€{_fmt_money(amount / months)} / month × {months})"
    return phrase


def _budget_phrase(budget: float, months: int, monthly: float | None, spent: float) -> str:
    """The budget with its schedule; says so when this month's buys already took a share."""
    if monthly is None or spent <= 0.0:
        return _cash_phrase(budget, months)
    if months < 2:
        return f"€{_fmt_money(budget)}"
    return (
        f"€{_fmt_money(budget)} (€{_fmt_money(monthly - spent)} this month,"
        f" then €{_fmt_money(monthly)} / month × {months - 1})"
    )


def _first_share(plan: RebalancePlan, monthly: float | None, spent: float) -> float | None:
    """This month's share of the plan when earlier buys shrank its instalment, else None."""
    if monthly is None or spent <= 0.0 or not plan.budget:
        return None
    return max(0.0, monthly - spent) / plan.budget


@dataclass(frozen=True)
class FundCost:
    """A fund's cost basis against its price at the as-of date, netted across brokers."""

    avg_paid: float | None   # cost basis / shares (EUR, fees included); None if not held
    last_px: float | None    # latest close on or before as-of, native currency
    chg_pct: float | None    # EUR value / cost basis − 1, in percent; None if unvaluable


_NO_COST = FundCost(avg_paid=None, last_px=None, chg_pct=None)


def fund_costs(
    db_path: str, isins: set[str], values: dict[str, float | None], as_of: str
) -> dict[str, FundCost]:
    """Avg paid, Last px and Chg% per ISIN, the same figures `portfolio` shows per broker."""
    timeline = position_timeline(load_trades(db_path))
    costs: dict[str, FundCost] = {}
    for isin in isins:
        shares, cost = position_asof(timeline.get(isin, []), as_of)
        _dates, closes = load_price_series(db_path, isin, as_of)
        value = values.get(isin)
        held = shares > 1e-9 and cost > 0.0
        costs[isin] = FundCost(
            avg_paid=cost / shares if held else None,
            last_px=closes[-1] if closes else None,
            chg_pct=100.0 * (value / cost - 1.0) if held and value is not None else None,
        )
    return costs


def render_month(
    rows: list[RebalanceRow],
    plan: RebalancePlan,
    *,
    as_of: str,
    months: int,
    monthly: float,
    spent: float,
    bought: dict[str, float],
    names: dict[str, str],
    start_plan: RebalancePlan,
    costs: dict[str, FundCost],
    categories: tuple[Category, ...] | None = None,
) -> list[str]:
    """The as-of month of a budgeted sleeve plan, in ``rows`` order (under each category
    when it has them): each fund's goal — this month's buy in ``start_plan``, the plan as
    stated at the previous month-end, before this month's buys — what it already got and
    what is left to buy: goal − bought, floored at 0 (the live plan's Monthly€ when there
    is no goal)."""
    goals = (
        {isin: buy / months for isin, buy in start_plan.buys.items()}
        if start_plan.feasible else None
    )
    if goals is None:
        first_share = _first_share(plan, monthly, spent)
        to_buy = {r.isin: _per_month(r.buy, months, first_share) for r in rows}
    else:
        to_buy = {isin: max(0.0, goal - bought.get(isin, 0.0)) for isin, goal in goals.items()}

    def any_amount(isin: str) -> bool:
        amounts = (to_buy.get(isin, 0.0), bought.get(isin, 0.0), (goals or {}).get(isin, 0.0))
        return max(amounts) >= 0.005

    shown = [r.isin for r in rows if any_amount(r.isin)]
    shown += sorted(isin for isin in {*bought, *(goals or {})}
                    if isin not in to_buy and any_amount(isin))
    frozen = {r.isin for r in rows if r.is_frozen}
    total_bought = sum(bought.values())
    left = sum(to_buy.values())
    over = total_bought - monthly
    bought_text = f"€{_fmt_money(total_bought)} already bought"
    if over >= 0.005:
        bought_text += f" (€{_fmt_money(over)} over the instalment)"
    width = 14 + 1 + _NAME_W + 3 * (1 + _EUR_W) + (1 + 10) + (1 + 8) + (1 + _PCT_W)
    rule = "-" * width
    lines = [
        f"\nThis month ({as_of[:7]}): €{_fmt_money(monthly)} instalment, {bought_text},"
        f" €{_fmt_money(left)} left to buy.",
        "",
        f"{'ISIN':<14} {'Name':<{_NAME_W}} {'Goal€':>{_EUR_W}} {'Bought€':>{_EUR_W}}"
        f" {'To buy€':>{_EUR_W}} {'Avg paid':>10} {'Last px':>8} {'Chg%':>{_PCT_W}}",
        rule,
    ]

    def goal(*isins: str) -> float | None:
        return None if goals is None else sum(goals.get(isin, 0.0) for isin in isins)

    def line(label: str, target: float | None, got: float, buy: float,
             *, off_plan: bool = False, cost: FundCost | None = None) -> str:
        target_cell = (
            "---" if off_plan else "—" if target is None else _fmt_money(target)
        )
        price = ""
        if cost is not None:
            avg = "—" if cost.avg_paid is None else f"{cost.avg_paid:.4f}"
            px = "—" if cost.last_px is None else f"{cost.last_px:.2f}"
            chg = "n/a" if cost.chg_pct is None else f"{cost.chg_pct:+.1f}%"
            price = f" {avg:>10} {px:>8} {chg:>{_PCT_W}}"
        return (
            f"{label[:14 + 1 + _NAME_W]:<{14 + 1 + _NAME_W}} {target_cell:>{_EUR_W}}"
            f" {_fmt_money(got):>{_EUR_W}} {_fmt_money(buy):>{_EUR_W}}{price}"
        )

    def fund(isin: str, indent: str = "") -> str:
        name = _display_name(names.get(isin, ""), is_residual=False, is_frozen=isin in frozen)
        target, got = goal(isin), bought.get(isin, 0.0)
        off_plan = target is not None and target < 0.005 and got >= 0.005  # bought, no goal
        return line(f"{indent + isin:<14} {name}", target, got, to_buy.get(isin, 0.0),
                    off_plan=off_plan, cost=costs.get(isin, _NO_COST))

    if categories is None:
        lines.extend(fund(isin) for isin in shown)
    else:
        listed: set[str] = set()
        for category in categories:
            members = [isin for isin in shown if isin in category.funds]
            listed.update(category.funds)
            if not members:
                continue
            lines.append(line(category.name, goal(*members),
                              sum(bought.get(i, 0.0) for i in members),
                              sum(to_buy.get(i, 0.0) for i in members)))
            lines.extend(fund(isin, "  ") for isin in members)
        lines.extend(fund(isin) for isin in shown if isin not in listed)
    lines.append(rule)
    lines.append(line("TOTAL", goal(*shown), total_bought, left))
    if goals is None:
        lines.append(
            f"Goal€ unavailable — no plan as of {_month_start_eve(as_of)}: "
            f"{_reason_message(start_plan, {})}  To buy€ is the plan's Monthly€."
        )
    return lines


def _month_start_eve(as_of: str) -> str:
    """The day before ``as_of``'s month began: the snapshot of this month's stated plan."""
    return (date.fromisoformat(as_of).replace(day=1) - timedelta(days=1)).isoformat()


def render_sleeve_targets(
    targets: dict[str, float],
    frozen: tuple[str, ...],
    names: dict[str, str],
    categories: tuple[Category, ...] | None = None,
) -> list[str]:
    """Recap of a sleeve plan's inputs, shown when the plan is UNAVAILABLE (ADR-0060),
    with each fund under its category when the scenario has them (ADR-0061)."""
    rule = "-" * (14 + 1 + _NAME_W + 1 + _PCT_W)
    lines = [
        "Targets (Tgt% = of the non-frozen sleeve):",
        "",
        f"{'ISIN':<14} {'Name':<{_NAME_W}} {'Tgt%':>{_PCT_W}}",
        rule,
    ]

    def fund_lines(isins: list[str], indent: str) -> list[str]:
        ordered = sorted(isins, key=lambda isin: (-targets.get(isin, 0.0), isin))
        return [
            f"{indent + isin:<14} {names.get(isin, ''):<{_NAME_W}}"
            f" {_fmt_pct(targets.get(isin, 0.0) * 100):>{_PCT_W}}"
            for isin in ordered
        ]

    if categories is None:
        lines.extend(fund_lines(list(targets), ""))
    else:
        for category in categories:
            lines.append(
                f"{category.name[:14 + 1 + _NAME_W]:<{14 + 1 + _NAME_W}}"
                f" {_fmt_pct(category.pct):>{_PCT_W}}"
            )
            lines.extend(fund_lines(list(category.funds), "  "))
    lines.append(rule)
    lines.append(f"{'TOTAL':<14} {'':<{_NAME_W}} {_fmt_pct(sum(targets.values()) * 100):>{_PCT_W}}")
    lines.append(f"Frozen (never bought): {', '.join(sorted(frozen)) or '—'}")
    return lines


def render_sleeve_table(
    rows: list[RebalanceRow],
    plan: RebalancePlan,
    *,
    as_of: str,
    months: int,
    show_status: bool,
    frozen_unvaluable: list[str],
    stale_rows: list[RebalanceRow],
    categories: tuple[Category, ...] | None = None,
    monthly: float | None = None,
    spent: float = 0.0,
) -> list[str]:
    """Render a feasible sleeve plan (ADR-0060): active rows, a SLEEVE subtotal,
    frozen rows and the book TOTAL.  With categories (ADR-0061) each category's
    subtotal row heads its funds, in scenario order; ``rows`` order is kept within it."""
    dca = f"   ·   DCA: {months} month{'s' if months > 1 else ''}" if months > 1 else ""
    lines: list[str] = [
        f"\nBuy-only rebalance of the non-frozen sleeve as of {as_of} (EUR){dca}",
        "Sleeve% and Tgt% are of the sleeve, Book% of the whole book after buying;"
        " frozen funds are never bought.",
    ]
    if categories is not None:
        lines.append("Each category row totals the funds under it; its Tgt% is the category's.")
    lines += ["", _header_line(months=months, show_status=show_status, sleeve=True)]
    rule = "-" * _rule_width(months=months, show_status=show_status, sleeve=True)
    marker = "◄ no buy" if plan.shortfall > 0.0 else "◄ binds"

    v_final = plan.v_final
    status = f" {'':>{_STATUS_W}}" if show_status else ""
    first_share = _first_share(plan, monthly, spent)

    def total(label: str, current: float, cur: str, tgt: str, buy: float, final: float) -> str:
        per_month = (
            f" {_fmt_money(_per_month(buy, months, first_share)):>{_EUR_W}}" if months > 1 else ""
        )
        book = _fmt_pct(100.0 * final / v_final if v_final > 0.0 else 0.0)
        return (
            f"{label[:14 + 1 + _NAME_W]:<{14 + 1 + _NAME_W}} {_fmt_money(current):>{_EUR_W}}"
            f" {cur:>{_PCT_W}} {tgt:>{_PCT_W}} {_fmt_money(buy):>{_EUR_W}}"
            f"{per_month} {_fmt_money(final):>{_EUR_W}} {book:>{_PCT_W}}{status}"
        )

    def fund_line(row: RebalanceRow, *, indent: bool = False) -> str:
        return _format_row(row, months=months, show_status=show_status, sleeve=True,
                           marker=marker, indent=indent, first_share=first_share)

    lines.append(rule)
    active = [r for r in rows if not r.is_frozen]
    if categories is None:
        lines.extend(fund_line(row) for row in active)
    else:
        for category in categories:
            members = [r for r in active if r.isin in category.funds]
            current = sum(r.v for r in members)
            held_back = [r for r in rows if r.is_frozen and r.isin in category.funds]
            cur = _fmt_pct(100.0 * current / plan.sleeve_v if plan.sleeve_v > 0.0 else 0.0)
            lines.append(total(category.name, current, cur, _fmt_pct(category.pct),
                               sum(r.buy for r in members), sum(r.final_v for r in members)))
            lines.extend(fund_line(row, indent=True) for row in members)
            lines.extend(fund_line(row, indent=True) for row in held_back)
    lines.append(rule)

    frozen_rows = [r for r in rows if r.is_frozen]
    if frozen_rows:
        lines.append(total("SLEEVE", plan.sleeve_v, _fmt_pct(100.0), _fmt_pct(100.0),
                           plan.injection, plan.level))
        lines.append(rule)
        listed = {isin for c in categories or () for isin in c.funds}
        apart = [r for r in frozen_rows if r.isin not in listed]
        for row in apart:
            lines.append(_format_row(row, months=months, show_status=show_status, sleeve=True,
                                     first_share=first_share))
        if apart:
            lines.append(rule)
    lines.append(total("TOTAL", plan.v, "", "", plan.injection, v_final))
    lines.append("")

    budget_text = (
        "" if plan.budget is None
        else _budget_phrase(plan.budget, months, monthly, spent)
    )
    if plan.budget is None:
        if plan.c_min <= 0.0:
            lines.append("Sleeve already at target — no purchases needed.")
        else:
            lines.append(
                f"Inject {_cash_phrase(plan.c_min, months)} to reach targets buy-only."
            )
    elif plan.shortfall > 0.0:
        lines.append(
            f"Budget {budget_text} is €{_fmt_money(plan.shortfall)}"
            f" short of the €{_fmt_money(plan.c_min)} that reaches the targets: buys close"
            f" the largest gaps first and ◄ funds stay above target."
        )
    else:
        lines.append(
            f"Budget {budget_text} reaches the targets buy-only"
            f" (minimum €{_fmt_money(plan.c_min)})."
        )

    frozen_v = plan.v - plan.sleeve_v
    if frozen_rows and plan.v > 0.0:
        lines.append(
            f"Frozen: €{_fmt_money(frozen_v)} · {_fmt_pct(100.0 * frozen_v / plan.v)} of the"
            f" book now → {_fmt_pct(100.0 * frozen_v / v_final)} after."
        )
    footer = ", ".join(frozen_unvaluable) if frozen_unvaluable else "—"
    lines.append(f"Frozen (unvaluable, excluded): {footer}")
    lines.extend(_stale_lines(stale_rows, as_of))
    return lines


def render_explain(
    plan: RebalancePlan,
    targets: dict[str, float],
    *,
    as_of: str,
    months: int,
) -> list[str]:
    """One --explain block (ADR-0016 decision 8 — one block, not per-row).

    Reconstructed from source, not a persisted log; works for both feasible and
    infeasible plans.
    """
    lines = ["\nProvenance (--explain) — reconstructed from source, not a log:"]
    if plan.sleeve:
        lines.extend(_explain_sleeve(plan, targets, months=months))
        return lines

    if not plan.feasible:
        lines.extend(_explain_metric(
            "Buy-only rebalance plan",
            Status.UNAVAILABLE,
            f"UNAVAILABLE ({plan.reason})"
            + (
                f" — affected: {', '.join(plan.unvaluable_targets)}"
                if plan.unvaluable_targets else ""
            ),
            f"feasibility check: {plan.reason}",
            "V'_min = max(v_i/t_i for i∈P, [v_rest/R if R>0 and v_rest>0]); "
            "requires V > 0 and R/residual consistent with the held set",
            REBALANCE_CONTRACT,
        ))
        return lines

    R = 1.0 - sum(targets.values())

    # Binder description: which bound(s) produced V'_min
    binder_parts: list[str] = []
    for isin in plan.binders:
        t_i = targets[isin]
        v_i = plan.v_prime * t_i  # by construction: c_i=0, so v_i = t_i * V'_min
        binder_parts.append(f"{isin} (v/t = {v_i:.2f}/{t_i:.4f} = {plan.v_prime:.2f})")
    if plan.residual_bound_binds and R >= _FLOAT_CLAMP:
        binder_parts.append(f"residual bucket (v_rest/R = {plan.v_prime:.2f})")
    binders_str = ", ".join(binder_parts) if binder_parts else "n/a"

    c_desc = f"C_min = €{_fmt_money(plan.c_min)}"
    if months > 1:
        c_desc += f" (€{_fmt_money(plan.c_min / months)}/month × {months})"
    result = (
        f"V = €{_fmt_money(plan.v)} · V'_min = €{_fmt_money(plan.v_prime)} · "
        f"{c_desc} · binding: {binders_str}"
    )
    residual_note = (
        f"residual R = {R * 100:.1f}%; untargeted buys split pro-rata by current EUR value"
        if R >= _FLOAT_CLAMP else "fully specified (Σ targets = 100%)"
    )
    inputs = (
        f"pinned: {list(targets)} · {residual_note} · "
        f"V'_min = max(v_i/t_i for i∈P"
        + (", v_rest/R" if R >= _FLOAT_CLAMP else "") + ")"
    )
    lines.extend(_explain_metric(
        "Buy-only rebalance plan",
        Status.CALCULATED,
        result,
        inputs,
        "V'_min = max(pin bounds, residual bound); c_i = t_i·V'_min − v_i; "
        "c_rest split pro-rata to untargeted by v_j (ADR-0016 decisions 3/4)",
        REBALANCE_CONTRACT,
    ))
    return lines


_SLEEVE_METHOD = (
    "L solves Σ max(0, s_i·L − v_i) = B, with B = C_min = max(v_i/s_i) − S when no "
    "budget is set; c_i = max(0, s_i·L − v_i); frozen funds get no buy (ADR-0060)"
)


def _explain_sleeve(
    plan: RebalancePlan, targets: dict[str, float], *, months: int
) -> list[str]:
    """The --explain block for a sleeve plan (ADR-0060)."""
    if not plan.feasible:
        affected = plan.unvaluable_targets or plan.unclassified
        return _explain_metric(
            "Buy-only sleeve plan",
            Status.UNAVAILABLE,
            f"UNAVAILABLE ({plan.reason})"
            + (f" — affected: {', '.join(affected)}" if affected else ""),
            f"feasibility check: {plan.reason}",
            "every held fund is targeted or frozen; the sleeve has a value or a budget",
            SLEEVE_CONTRACT,
        )
    binders = ", ".join(
        f"{isin} (v/s = {plan.level * targets[isin]:.2f}/{targets[isin]:.4f})"
        if plan.shortfall <= 0.0 else isin
        for isin in plan.binders
    ) or "none (the budget is above C_min)"
    budget = (
        "none (B = C_min)" if plan.budget is None
        else _cash_phrase(plan.budget, months)
    )
    result = (
        f"V = €{_fmt_money(plan.v)} · S = €{_fmt_money(plan.sleeve_v)} · "
        f"L = €{_fmt_money(plan.level)} · C_min = €{_fmt_money(plan.c_min)} · "
        f"budget: {budget} · "
        + ("no buy" if plan.shortfall > 0.0 else "binding")
        + f": {binders}"
    )
    inputs = (
        f"sleeve targets: {list(targets)} · frozen held: {sorted(plan.frozen)}"
    )
    return _explain_metric(
        "Buy-only sleeve plan", Status.CALCULATED, result, inputs, _SLEEVE_METHOD,
        SLEEVE_CONTRACT,
    )


# ---------------------------------------------------------------------------
# Display rows built from the plan + valuations (assembly lives in `common`).
# ---------------------------------------------------------------------------


def _etf_name(config_path: str, isin: str) -> str:
    data = ConfigManager(config_path).get(isin)
    return str((data or {}).get("name", ""))[:_NAME_W]


def _build_rows(
    plan: RebalancePlan,
    targets: dict[str, float],
    values: dict[str, float | None],
    price_dates: dict[str, str | None],
    config_path: str,
    as_of: str,
) -> list[RebalanceRow]:
    """Build display rows: valued-held union targeted ISINs, sorted per ADR-0016 decision 9."""
    R = 1.0 - sum(targets.values())
    v_rest = sum(
        v for isin, v in values.items()
        if v is not None and isin in plan.buys and isin not in targets and v > 0.0
    )

    rows: list[RebalanceRow] = []
    for isin, buy in plan.buys.items():
        is_pinned = isin in targets
        if is_pinned:
            t_fraction = targets[isin]
        else:
            v_j = values.get(isin) or 0.0
            t_fraction = (R * v_j / v_rest) if v_rest > 0.0 else 0.0

        v_cur = values.get(isin) or 0.0
        cur_pct = 100.0 * v_cur / plan.v if plan.v > 0.0 else 0.0
        t_pct = t_fraction * 100.0
        final_v = v_cur + buy

        pd = price_dates.get(isin)
        estimated = pd is not None and pd < as_of

        rows.append(RebalanceRow(
            isin=isin,
            name=_etf_name(config_path, isin),
            v=v_cur,
            cur_pct=cur_pct,
            t_pct=t_pct,
            buy=buy,
            final_v=final_v,
            is_residual=not is_pinned,
            is_binder=(isin in plan.binders),
            price_date=pd,
            estimated=estimated,
            status=Status.CALCULATED,
        ))

    # Sort: pinned by Tgt% desc, then residual by Current€ desc; ties by ISIN
    def _sort_key(r: RebalanceRow) -> tuple[int, float, str]:
        group = 0 if not r.is_residual else 1
        order = -r.t_pct if not r.is_residual else -r.v
        return (group, order, r.isin)

    rows.sort(key=_sort_key)
    return rows


def _build_sleeve_rows(
    plan: RebalancePlan,
    targets: dict[str, float],
    values: dict[str, float | None],
    price_dates: dict[str, str | None],
    config_path: str,
    as_of: str,
) -> list[RebalanceRow]:
    """Display rows of a sleeve plan: targeted funds by Tgt%, then frozen by Current€."""
    v_final = plan.v_final
    rows: list[RebalanceRow] = []
    for isin, buy in plan.buys.items():
        is_frozen = isin in plan.frozen
        v_cur = values.get(isin) or 0.0
        pd = price_dates.get(isin)
        rows.append(RebalanceRow(
            isin=isin,
            name=_etf_name(config_path, isin),
            v=v_cur,
            cur_pct=(100.0 * v_cur / plan.sleeve_v
                     if plan.sleeve_v > 0.0 and not is_frozen else 0.0),
            t_pct=0.0 if is_frozen else 100.0 * targets[isin],
            buy=buy,
            final_v=v_cur + buy,
            is_residual=False,
            is_binder=isin in plan.binders,
            price_date=pd,
            estimated=pd is not None and pd < as_of,
            status=Status.CALCULATED,
            is_frozen=is_frozen,
            book_pct=100.0 * (v_cur + buy) / v_final if v_final > 0.0 else 0.0,
        ))
    rows.sort(key=lambda r: (r.is_frozen, -r.v if r.is_frozen else -r.t_pct, r.isin))
    return rows


def _column_sort_key(row: RebalanceRow, sort_by: str) -> str | float:
    if sort_by == "isin":
        return row.isin
    if sort_by == "name":
        return row.name.lower()
    return {
        "value": row.v,
        "weight": row.cur_pct,
        "tgt": row.t_pct,
        "buy": row.buy,
        "final": row.final_v,
    }[sort_by]


def sort_rows(
    rows: list[RebalanceRow], *, sort_by: str, reverse: bool = False
) -> list[RebalanceRow]:
    """Reorder plan rows by a column (ADR-0037); default order stays in `_build_rows`."""
    return sorted(rows, key=lambda r: _column_sort_key(r, sort_by), reverse=reverse)


# ---------------------------------------------------------------------------
# Command.
# ---------------------------------------------------------------------------


def _reason_message(plan: RebalancePlan, targets: dict[str, float]) -> str:
    if plan.reason == "target_unvaluable":
        isins = ", ".join(plan.unvaluable_targets)
        return (
            f"target_unvaluable — held targeted fund(s) have no close/FX on or before "
            f"the as-of date: {isins}.  Run 'e1f fetch' to get prices, then re-run."
        )
    if plan.reason == "empty_portfolio":
        return (
            "empty_portfolio — no fund has a positive valued anchor (empty book, or all "
            "held funds unvaluable).  A rebalance requires a current mix to walk from; "
            "run 'e1f fetch' to price your holdings."
        )
    if plan.reason == "residual_full":
        t_sum = sum(targets.values())
        return (
            f"residual_full — targets sum to {t_sum * 100:.1f}% (= 100%) but valued "
            f"untargeted holdings exist.  Either target those funds or lower the sum below 100%."
        )
    if plan.reason == "residual_unallocable":
        R = 1.0 - sum(targets.values())
        return (
            f"residual_unallocable — {R * 100:.1f}% residual weight has no valued untargeted "
            f"fund to absorb it.  Add a target covering the gap (so Σ targets = 100%), "
            f"or hold a valued untargeted fund."
        )
    if plan.reason == "unclassified":
        return (
            f"unclassified — held fund(s) are neither targeted nor frozen: "
            f"{', '.join(plan.unclassified)}.  Give each a target or add it to 'frozen'."
        )
    if plan.reason == "empty_sleeve":
        return (
            "empty_sleeve — no targeted fund is held and no budget is set.  Set "
            "'monthly' in the scenario (or --monthly) to open the sleeve."
        )
    return plan.reason or "unknown"


_BUY_SIDES = frozenset({"BUY", "SAVINGS_PLAN"})


def bought_in_month(db_path: str, as_of: str) -> dict[str, float]:
    """EUR bought per ISIN (shares × price + fee) in ``as_of``'s calendar month,
    up to ``as_of``."""
    month = as_of[:7]
    bought: dict[str, float] = {}
    for _broker, dt, isin, side, shares, price, fee in load_trades(db_path):
        day = str(dt)[:10]
        if side in _BUY_SIDES and day[:7] == month and day <= as_of and (shares or 0.0) > 0.0:
            cost = (shares or 0.0) * (price or 0.0) + (fee or 0.0)
            bought[isin] = bought.get(isin, 0.0) + cost
    return bought


def spent_in_month(db_path: str, as_of: str) -> float:
    """EUR bought (shares × price + fee) in ``as_of``'s calendar month, up to ``as_of``."""
    return sum(bought_in_month(db_path, as_of).values())


def _cmd_rebalance(
    db_path: str,
    config_path: str,
    currency_meta_path: str,
    *,
    targets_raw: list[tuple[str, float]],
    months: int,
    as_of: str,
    show_status: bool,
    explain: bool,
    sort_by: str | None = None,
    reverse: bool = False,
    frozen: tuple[str, ...] | None = None,
    budget: float | None = None,
    categories: tuple[Category, ...] | None = None,
    this_month: bool = False,
) -> int:
    show_status = show_status or explain

    targets: dict[str, float] = {isin: pct / 100.0 for isin, pct in targets_raw}
    values, held, untargeted_unvaluable, price_dates = _assemble(
        db_path, currency_meta_path, targets, as_of
    )
    spent = 0.0
    bought: dict[str, float] = {}
    monthly = None if budget is None else budget / months
    if budget is not None and monthly is not None:
        # This month's buys are already in `values`; they also used up part of this
        # month's instalment, so only the remainder is left to plan.
        bought = bought_in_month(db_path, as_of)
        spent = min(sum(bought.values()), monthly)
        budget -= spent
    plan = plan_rebalance(
        targets, values, held, None if frozen is None else frozenset(frozen), budget
    )

    names = {isin: _etf_name(config_path, isin) for isin in targets}
    target_summary = (
        render_target_summary(targets, names) if frozen is None
        else render_sleeve_targets(targets, frozen, names, categories)
    )

    if not plan.feasible:
        print(f"\nBuy-only rebalance as of {as_of} (EUR)\n")
        for line in target_summary:
            print(line)
        print(f"\nUNAVAILABLE — {_reason_message(plan, targets)}")
        if explain:
            for line in render_explain(plan, targets, as_of=as_of, months=months):
                print(line)
        return 0

    build = _build_rows if frozen is None else _build_sleeve_rows
    rows = build(plan, targets, values, price_dates, config_path, as_of)
    if sort_by is not None:
        rows = sort_rows(rows, sort_by=sort_by, reverse=reverse)
    elif reverse:
        rows.reverse()
    stale_rows = [r for r in rows if r.estimated]

    if frozen is None:
        lines = render_table(
            rows, plan,
            as_of=as_of,
            months=months,
            show_status=show_status,
            untargeted_unvaluable=untargeted_unvaluable,
            stale_rows=stale_rows,
            target_summary=target_summary,
        )
    else:
        lines = render_sleeve_table(
            rows, plan,
            as_of=as_of,
            months=months,
            show_status=show_status,
            frozen_unvaluable=untargeted_unvaluable,
            stale_rows=stale_rows,
            categories=categories,
            monthly=monthly,
            spent=spent,
        )
    for line in lines:
        print(line)
    if spent > 0.0:
        print(
            f"Already bought in {as_of[:7]}: €{_fmt_money(spent)}"
            " — deducted from this month's instalment."
        )
    if this_month and monthly is not None and frozen is not None:
        # The goal is the plan as stated before this month's buys: the full instalment
        # on the holdings at the previous month-end, whatever was actually bought since.
        eve = _month_start_eve(as_of)
        values0, held0, _unvaluable0, _dates0 = _assemble(db_path, currency_meta_path,
                                                           targets, eve)
        start_plan = plan_rebalance(targets, values0, held0, frozenset(frozen),
                                    monthly * months)
        month_names = {r.isin: r.name for r in rows}
        month_names.update(
            (isin, _etf_name(config_path, isin))
            for isin in {*bought, *start_plan.buys} if isin not in month_names
        )
        costs = fund_costs(db_path, set(month_names), values, as_of)
        for line in render_month(rows, plan, as_of=as_of, months=months, monthly=monthly,
                                 spent=spent, bought=bought, names=month_names,
                                 start_plan=start_plan, costs=costs, categories=categories):
            print(line)

    if explain:
        for line in render_explain(plan, targets, as_of=as_of, months=months):
            print(line)

    return 0


# ---------------------------------------------------------------------------
# CLI.
# ---------------------------------------------------------------------------


def _parse_target(text: str) -> tuple[str, float]:
    """Validate ``ISIN:PCT`` — loose ISIN check, PCT in (0, 100]."""
    if ":" not in text:
        raise argparse.ArgumentTypeError(
            f"expected ISIN:PCT (e.g. IE00B4L5Y983:30), got {text!r} — missing colon"
        )
    isin, pct_str = text.split(":", 1)
    if not isin:
        raise argparse.ArgumentTypeError(f"ISIN part is empty in {text!r}")
    try:
        pct = float(pct_str)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"PCT part {pct_str!r} is not a number in {text!r}"
        ) from None
    if not (0.0 < pct <= 100.0):
        raise argparse.ArgumentTypeError(
            f"PCT must be in (0, 100] — got {pct} in {text!r}. "
            f"Targets are percentages of the whole valued book (e.g. 30 = 30%%)."
        )
    return isin, pct


def _positive_amount(text: str) -> float:
    try:
        value = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text!r} is not a number") from None
    if not math.isfinite(value) or value <= 0.0:
        raise argparse.ArgumentTypeError(f"must be a finite amount > 0 (got {text})")
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


def _validate_as_of(as_of: str) -> None:
    try:
        date.fromisoformat(as_of)
    except ValueError as exc:
        raise ValueError(f"--as-of must be YYYY-MM-DD: {as_of}") from exc


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="e1f rebalance",
        description="Compute the minimum-cash buy-only rebalance to user-supplied target "
        "weights, and an optional N-month DCA schedule (ADR-0016).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Targets are percentages of the WHOLE VALUED BOOK, not of a named sleeve.
  --target A:30 --target B:40   means A=30% and B=40% of the TOTAL portfolio.
  If any other valued fund is held, the remaining 30% is the residual, split
  among untargeted funds pro-rata by their current EUR value.
  The output opens with a target recap: the target sum (Tgt%) and each target
  scaled to 100% among the targeted funds (Scaled%).
  If Σ targets = 100%% while valued untargeted funds exist, the plan is
  UNAVAILABLE (residual_full) — target those funds or lower the sum below 100%%.

Sleeve plans (ADR-0060): --frozen ISIN (or a scenario's 'frozen' list) names
  funds that are never bought. Targets are then percents of the non-frozen
  sleeve and must sum to 100%%; every other held fund must be targeted
  (else UNAVAILABLE: unclassified). --monthly EUR sets a budget of EUR × months:
  above C_min it is spread to keep the targets; below C_min it closes the
  largest gaps first. Without a budget the plan injects C_min.
  Buys already made in the as-of month are deducted from this month's instalment;
  --this-month lists them per fund with this month's goal (the plan at the
  previous month-end) and what is left to buy: goal − bought, floored at 0.
  A sleeve scenario may group its funds into categories (ADR-0061): each
  category row totals its funds, which --sort reorders only within it.

Buy-only: an overweight fund is diluted by buying more of everything else — it
  is never sold. A badly overweight fund can force a large cash injection.

All amounts are in EUR. The plan is computed once at the as-of snapshot; prices
  move between months, so realized weights will drift — re-run to refresh.

Provenance (ADR-0014, off by default): --show-status adds a Status column;
  --explain adds one provenance block (implies --show-status), reconstructed
  from source — which bound produced V'_min, all binding fund(s), residual split.

Scenarios (ADR-0017): save a basket once with 'e1f scenario save NAME ...' and
  recall it with --scenario NAME (loads its targets and stored months). A
  --months / --as-of typed here overrides the scenario's stored value.

Examples:
  e1f rebalance --target IE00B4L5Y983:30 --target IE00BK5BQT80:40
  e1f rebalance --target IE00B4L5Y983:30 --target IE00BK5BQT80:40 --months 10
  e1f rebalance --target IE00B4L5Y983:30 --explain
  e1f rebalance --target IE00B4L5Y983:60 --target IE00BK5BQT80:40 --as-of 2025-12-31
  e1f rebalance --scenario core
  e1f rebalance --scenario core --months 6 --explain
  e1f rebalance --target IE00B4L5Y983:30 --sort buy --reverse
  e1f rebalance --target IE00B4L5Y983:60 --target IE00BK5BQT80:40 --frozen IE00B3YCGJ38
  e1f rebalance --scenario core --monthly 2500
  e1f rebalance --scenario core --this-month
        """,
    )
    parser.add_argument(
        "--target",
        metavar="ISIN:PCT",
        action="append",
        dest="targets",
        type=_parse_target,
        help="Target weight — repeatable. PCT is a percent of the whole valued book in "
        "(0, 100] (e.g. 30 = 30%%). Percents are of the WHOLE book.",
    )
    parser.add_argument(
        "--frozen",
        metavar="ISIN",
        action="append",
        help="A held fund that is never bought — repeatable; makes this a sleeve plan "
        "whose targets are percents of the non-frozen funds and sum to 100 (ADR-0060).",
    )
    parser.add_argument(
        "--monthly",
        type=_positive_amount,
        default=None,
        metavar="EUR",
        help="Sleeve plans: invest EUR per month (× --months) instead of C_min. "
        "Overrides a scenario's stored monthly.",
    )
    parser.add_argument(
        "--this-month",
        action="store_true",
        help="Budgeted sleeve plans: add a per-fund list of what was already bought in "
        "the as-of month and what is left to buy in it.",
    )
    parser.add_argument(
        "--months",
        type=_int_at_least(1),
        default=None,
        metavar="N",
        help="Spread the plan into N equal monthly buys (≥ 1; default 1 = lump sum). "
        "Overrides a scenario's stored months.",
    )
    parser.add_argument(
        "--scenario",
        "-s",
        metavar="NAME",
        help="Load targets (and months) from a saved scenario instead of --target "
        "(see 'e1f scenario'). Mutually exclusive with --target.",
    )
    parser.add_argument(
        "--scenarios-file",
        default=DEFAULT_SCENARIOS,
        help="Scenarios YAML file path (used with --scenario).",
    )
    parser.add_argument(
        "--as-of",
        default=datetime.now(UTC).date().isoformat(),
        metavar="YYYY-MM-DD",
        help="Value the portfolio as of this date (default: today).",
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
        "--show-status",
        action="store_true",
        help="Add a per-row provenance Status column (ADR-0014).",
    )
    parser.add_argument(
        "--explain",
        action="store_true",
        help="Add one provenance block (implies --show-status); reconstructs V'_min, "
        "binding fund(s), and the residual split from source.",
    )
    parser.add_argument(
        "--sort",
        choices=SORT_FIELDS,
        default=None,
        help="Sort plan rows by column (default: pinned by Tgt%%, then residual by Current€)",
    )
    parser.add_argument(
        "--reverse", "-r", action="store_true", help="Descending sort order"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    # Resolve the target source: --scenario or --target / --frozen (never both).
    if args.scenario and (args.targets or args.frozen):
        parser.error("--scenario and --target / --frozen are mutually exclusive")

    targets_raw: list[tuple[str, float]]
    scenario_months: int | None = None
    frozen: tuple[str, ...] | None
    categories: tuple[Category, ...] | None = None
    monthly: float | None = args.monthly
    if args.scenario:
        try:
            scenario = get_scenario(args.scenario, args.scenarios_file)
        except Exception as e:  # noqa: BLE001 — surface as a clean parser error
            parser.error(str(e))
        targets_raw = sorted(scenario.targets.items())
        scenario_months = scenario.months
        frozen = scenario.frozen
        categories = scenario.categories
        if monthly is None:
            monthly = scenario.monthly
    else:
        targets_raw = args.targets or []
        frozen = tuple(args.frozen) if args.frozen else None

    # Post-parse cross-arg checks (cannot live in per-arg type=)
    if not targets_raw:
        parser.error("at least one --target ISIN:PCT (or --scenario NAME) is required")
    isins = [isin for isin, _ in targets_raw]
    if len(isins) != len(set(isins)):
        parser.error("duplicate ISIN in targets")
    total_pct = sum(pct for _, pct in targets_raw)
    if frozen is None:
        if total_pct > 100.0 + 1e-9:
            parser.error(f"target percentages sum to {total_pct:.2f}% — must not exceed 100%")
        if monthly is not None:
            parser.error("--monthly needs a sleeve plan (--frozen, or a scenario with 'frozen')")
    else:
        if abs(total_pct - 100.0) > 1e-6:
            parser.error(
                f"target percentages sum to {total_pct:.2f}% — with frozen funds they are "
                f"of the non-frozen sleeve and must sum to 100%"
            )
        if len(set(frozen)) != len(frozen):
            parser.error("duplicate ISIN in frozen funds")
        if set(frozen) & set(isins):
            parser.error("a fund cannot be both targeted and frozen")
    if args.this_month and monthly is None:
        parser.error("--this-month needs a budget (--monthly, or a scenario's 'monthly')")

    # --months (CLI) overrides a scenario's stored months; default lump-sum is 1.
    months = args.months if args.months is not None else (scenario_months or 1)
    budget = None if monthly is None else monthly * months

    try:
        _validate_as_of(args.as_of)
        return _cmd_rebalance(
            args.db,
            args.config,
            args.currency_meta,
            targets_raw=targets_raw,
            months=months,
            as_of=args.as_of,
            show_status=args.show_status,
            explain=args.explain,
            sort_by=args.sort,
            reverse=args.reverse,
            frozen=frozen,
            budget=budget,
            categories=categories,
            this_month=args.this_month,
        )
    except Exception as e:  # noqa: BLE001 — CLI top-level; all errors become exit code 1
        print(f"✗ Error: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
