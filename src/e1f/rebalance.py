#!/usr/bin/env python
"""e1f rebalance — minimum-cash buy-only target rebalance & DCA plan (ADR-0016).

Computes the cheapest way to reach user-supplied target weights for one or more
funds without ever selling anything (dilution only), and an optional N-month
dollar-cost-averaging schedule.

Usage:
    e1f rebalance --target IE00B4L5Y983:30 --target IE00BK5BQT80:40
    e1f rebalance --target IE00B4L5Y983:30 --target IE00BK5BQT80:40 --months 10
    e1f rebalance --target IE00B4L5Y983:30 --explain
"""

import argparse
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime

from e1f.common import (
    DEFAULT_CONFIG,
    DEFAULT_CURRENCY_META,
    DEFAULT_DB,
    DEFAULT_SCENARIOS,
    ConfigManager,
    MetricContract,
    RebalancePlan,
    Status,
    _explain_metric,
    _FLOAT_CLAMP,
    assemble_rebalance_valuations as _assemble,
    compute_rebalance,
    get_scenario,
)

_NAME_W = 24   # name column width; " (resid)" (8 chars) fits within it
_EUR_W = 12    # money columns: Current€, Buy€, Monthly€, Final€
_PCT_W = 7     # percent columns: Cur%, Tgt%
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


# ---------------------------------------------------------------------------
# Formatting helpers (local copies — command→command import would break ADR-0003).
# ---------------------------------------------------------------------------


def _fmt_money(v: float, *, stale: bool = False) -> str:
    return f"{v:,.2f}" + ("~" if stale else "")


def _fmt_pct(v: float) -> str:
    return f"{v:.1f}%"


def _display_name(name: str, *, is_residual: bool) -> str:
    if is_residual:
        return f"{name[:(_NAME_W - 8)]} (resid)"
    return name[:_NAME_W]


# ---------------------------------------------------------------------------
# Rendering — table + --explain block.
# ---------------------------------------------------------------------------


def _header_line(*, months: int, show_status: bool) -> str:
    monthly = f" {'Monthly€':>{_EUR_W}}" if months > 1 else ""
    status = f" {'Status':>{_STATUS_W}}" if show_status else ""
    return (
        f"{'ISIN':<14} {'Name':<{_NAME_W}} {'Current€':>{_EUR_W}} {'Cur%':>{_PCT_W}}"
        f" {'Tgt%':>{_PCT_W}} {'Buy€':>{_EUR_W}}{monthly}"
        f" {'Final€':>{_EUR_W}}{status}"
    )


def _format_row(row: RebalanceRow, *, months: int, show_status: bool) -> str:
    name_cell = _display_name(row.name, is_residual=row.is_residual)
    money_cur = _fmt_money(row.v, stale=row.estimated)
    monthly = f" {_fmt_money(row.buy / months):>{_EUR_W}}" if months > 1 else ""
    binder = "  ◄ binds" if row.is_binder else ""
    status = f" {row.status.value:>{_STATUS_W}}" if show_status else ""
    return (
        f"{row.isin:<14} {name_cell:<{_NAME_W}}"
        f" {money_cur:>{_EUR_W}} {_fmt_pct(row.cur_pct):>{_PCT_W}}"
        f" {_fmt_pct(row.t_pct):>{_PCT_W}} {_fmt_money(row.buy):>{_EUR_W}}{monthly}"
        f" {_fmt_money(row.final_v):>{_EUR_W}}{status}{binder}"
    )


def _rule_width(*, months: int, show_status: bool) -> int:
    # 14 (ISIN) + 1 + NAME_W + 1 + EUR_W + 1 + PCT_W (Cur) + 1 + PCT_W (Tgt)
    # + 1 + EUR_W (Buy) + [1+EUR_W (Monthly)] + 1 + EUR_W (Final)
    w = 14 + 1 + _NAME_W + 1 + _EUR_W + 1 + _PCT_W + 1 + _PCT_W + 1 + _EUR_W
    w += 1 + _EUR_W  # Final€
    if months > 1:
        w += 1 + _EUR_W
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

    # Stale-close disclosure (~ rows)
    if stale_rows:
        if len(stale_rows) == 1:
            row = stale_rows[0]
            lines.append(
                f"\n~ Current€ estimated: no close on {as_of} — "
                f"freshest data is {row.price_date} for {row.isin} (fetch to refresh)."
            )
        else:
            items = ", ".join(
                f"{row.isin} ({row.price_date})"
                for row in sorted(stale_rows, key=lambda r: r.isin)
            )
            lines.append(
                f"\n~ Current€ estimated from the latest price before {as_of} "
                f"(no close on the as-of day — fetch to refresh): {items}"
            )

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
    return plan.reason or "unknown"


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
) -> int:
    show_status = show_status or explain

    targets: dict[str, float] = {isin: pct / 100.0 for isin, pct in targets_raw}
    values, held, untargeted_unvaluable, price_dates = _assemble(
        db_path, currency_meta_path, targets, as_of
    )
    plan = compute_rebalance(targets, values, held)

    names = {isin: _etf_name(config_path, isin) for isin in targets}
    target_summary = render_target_summary(targets, names)

    if not plan.feasible:
        print(f"\nBuy-only rebalance as of {as_of} (EUR)\n")
        for line in target_summary:
            print(line)
        print(f"\nUNAVAILABLE — {_reason_message(plan, targets)}")
        if explain:
            for line in render_explain(plan, targets, as_of=as_of, months=months):
                print(line)
        return 0

    rows = _build_rows(plan, targets, values, price_dates, config_path, as_of)
    if sort_by is not None:
        rows = sort_rows(rows, sort_by=sort_by, reverse=reverse)
    elif reverse:
        rows.reverse()
    stale_rows = [r for r in rows if r.estimated]

    for line in render_table(
        rows, plan,
        as_of=as_of,
        months=months,
        show_status=show_status,
        untargeted_unvaluable=untargeted_unvaluable,
        stale_rows=stale_rows,
        target_summary=target_summary,
    ):
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

    # Resolve the target source: --scenario or --target (never both).
    if args.scenario and args.targets:
        parser.error("--scenario and --target are mutually exclusive")

    targets_raw: list[tuple[str, float]]
    scenario_months: int | None = None
    if args.scenario:
        try:
            scenario = get_scenario(args.scenario, args.scenarios_file)
        except Exception as e:  # noqa: BLE001 — surface as a clean parser error
            parser.error(str(e))
        targets_raw = sorted(scenario.targets.items())
        scenario_months = scenario.months
    else:
        targets_raw = args.targets or []

    # Post-parse cross-arg checks (cannot live in per-arg type=)
    if not targets_raw:
        parser.error("at least one --target ISIN:PCT (or --scenario NAME) is required")
    isins = [isin for isin, _ in targets_raw]
    if len(isins) != len(set(isins)):
        parser.error("duplicate ISIN in targets")
    total_pct = sum(pct for _, pct in targets_raw)
    if total_pct > 100.0 + 1e-9:
        parser.error(f"target percentages sum to {total_pct:.2f}% — must not exceed 100%")

    # --months (CLI) overrides a scenario's stored months; default lump-sum is 1.
    months = args.months if args.months is not None else (scenario_months or 1)

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
        )
    except Exception as e:  # noqa: BLE001 — CLI top-level; all errors become exit code 1
        print(f"✗ Error: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
