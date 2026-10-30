"""Rebalance: buy-only plan math, DCA schedule, and the table command (ADR-0016)."""

import sqlite3
from contextlib import closing

import pytest
import yaml

from e1f import rebalance as reb
from e1f.rebalance import RebalancePlan, compute_rebalance

# ---------------------------------------------------------------------------
# Synthetic ISINs (13-char, matching the ADR's loose-check requirement)
# ---------------------------------------------------------------------------
A = "IE00FUND000A0"
B = "IE00FUND000B0"
C = "IE00FUND000C0"
D = "IE00FUND000D0"

ALL_HELD = frozenset({A, B, C})


# ---------------------------------------------------------------------------
# Pure-math core — no DB, no fixtures, tests compute_rebalance directly.
# ---------------------------------------------------------------------------


def _plan(targets, values, held=None) -> RebalancePlan:
    """Call compute_rebalance with conveniently-typed inputs.

    ``targets`` maps ISIN → fraction (e.g. 0.30).
    ``values`` maps ISIN → float|None.
    ``held`` defaults to {isin for isin, v in values.items() if v is not None and v >= 0}.
    If you need to distinguish held-but-unvaluable (None) from genuinely unheld (0.0),
    pass ``held`` explicitly.
    """
    if held is None:
        held = frozenset(
            isin for isin, v in values.items() if v is not None
        )
    return compute_rebalance(targets, values, frozenset(held))


@pytest.mark.parametrize(
    "targets",
    [{}, {A: False}, {A: True}, {A: 0.0}, {A: float("nan")}, {A: 0.60, B: 0.50}],
)
def test_compute_rebalance_rejects_invalid_targets(targets):
    with pytest.raises(ValueError):
        compute_rebalance(targets, {A: 6000.0, B: 4000.0}, frozenset({A, B}))


# ---------------------------------------------------------------------------
# Feasibility: whole-plan UNAVAILABLE reasons (ADR-0016 decision 5)
# ---------------------------------------------------------------------------


def test_empty_portfolio_no_held_funds():
    # No transactions / all unvaluable — V = 0.
    plan = _plan({A: 0.30}, {})
    assert not plan.feasible
    assert plan.reason == "empty_portfolio"
    assert plan.v == 0.0


def test_empty_portfolio_all_unvaluable():
    # A is held but unvaluable → target_unvaluable fires first; B is untargeted unvaluable.
    # To hit empty_portfolio, A must be unheld (v=0) while B is held-unvaluable (untargeted).
    # A is targeted but not held → v=0 (unheld); B is held-unvaluable.
    plan = compute_rebalance(
        targets={A: 0.50},
        values={A: 0.0, B: None},
        held=frozenset({B}),  # B held but unvaluable; A unheld
    )
    # V = 0 (only B is held, but B=None excluded from V; A is unheld so v=0)
    assert not plan.feasible
    assert plan.reason == "empty_portfolio"


def test_target_unvaluable_held_fund():
    # A is held AND targeted but has no price.
    plan = compute_rebalance(
        targets={A: 0.30},
        values={A: None, B: 5000.0},
        held=frozenset({A, B}),
    )
    assert not plan.feasible
    assert plan.reason == "target_unvaluable"
    assert A in plan.unvaluable_targets


def test_target_unvaluable_reports_all_offenders():
    # Both A and B are held targeted with no price.
    plan = compute_rebalance(
        targets={A: 0.30, B: 0.40},
        values={A: None, B: None, C: 3000.0},
        held=frozenset({A, B, C}),
    )
    assert not plan.feasible
    assert plan.reason == "target_unvaluable"
    assert sorted(plan.unvaluable_targets) == sorted([A, B])


def test_target_unvaluable_takes_precedence_over_empty_portfolio():
    # A is held-unvaluable AND the only fund. target_unvaluable fires before empty_portfolio.
    plan = compute_rebalance(
        targets={A: 0.30},
        values={A: None},
        held=frozenset({A}),
    )
    assert plan.reason == "target_unvaluable"


def test_unheld_targeted_fund_is_not_target_unvaluable():
    # A is targeted but NOT held — open-a-position, CALCULATED (v=0).
    plan = compute_rebalance(
        targets={A: 0.30},
        values={A: 0.0, B: 7000.0},
        held=frozenset({B}),  # A unheld, B held
    )
    assert plan.feasible  # not target_unvaluable


def test_residual_full_sigma_100_with_untargeted():
    # Σt = 100%, but C is still held with positive value.
    plan = compute_rebalance(
        targets={A: 0.60, B: 0.40},
        values={A: 6000.0, B: 4000.0, C: 1000.0},
        held=frozenset({A, B, C}),
    )
    assert not plan.feasible
    assert plan.reason == "residual_full"


def test_residual_unallocable_no_untargeted_fund():
    # R = 30% but no untargeted valued fund exists.
    plan = compute_rebalance(
        targets={A: 0.70},
        values={A: 7000.0},
        held=frozenset({A}),
    )
    assert not plan.feasible
    assert plan.reason == "residual_unallocable"


def test_fully_specified_no_residual_feasible():
    # Σt = 100% and no untargeted holdings → feasible (R=0, v_rest=0).
    plan = compute_rebalance(
        targets={A: 0.60, B: 0.40},
        values={A: 6000.0, B: 4000.0},
        held=frozenset({A, B}),
    )
    assert plan.feasible
    assert plan.reason is None


# ---------------------------------------------------------------------------
# ADR-0016 decision 3 worked example: A=6000, B=3000, C=1000 (C untargeted)
# A→30%, B→40%  →  V'_min=20000, c_A=0, c_B=5000, c_C=5000, C_min=10000
# ---------------------------------------------------------------------------


def test_overweight_binding_worked_example():
    plan = compute_rebalance(
        targets={A: 0.30, B: 0.40},
        values={A: 6000.0, B: 3000.0, C: 1000.0},
        held=frozenset({A, B, C}),
    )
    assert plan.feasible
    assert plan.v == pytest.approx(10000.0)
    assert plan.v_prime == pytest.approx(20000.0)
    assert plan.c_min == pytest.approx(10000.0)
    assert plan.buys[A] == pytest.approx(0.0)
    assert plan.buys[B] == pytest.approx(5000.0)
    assert plan.buys[C] == pytest.approx(5000.0)
    assert plan.binders == [A]
    assert not plan.residual_bound_binds


def test_overweight_tie_both_binders():
    # A=6000 and B=6000, both targeted at 30% → both bind at V'_min=20000.
    plan = compute_rebalance(
        targets={A: 0.30, B: 0.30},
        values={A: 6000.0, B: 6000.0, C: 1000.0},
        held=frozenset({A, B, C}),
    )
    assert plan.feasible
    assert plan.v_prime == pytest.approx(20000.0)
    assert plan.buys[A] == pytest.approx(0.0)
    assert plan.buys[B] == pytest.approx(0.0)
    assert sorted(plan.binders) == sorted([A, B])


def test_already_on_target_is_calculated_not_unavailable():
    # A=3000 (30%), B=4000 (40%), C=3000 (30% residual) — exactly on target.
    plan = compute_rebalance(
        targets={A: 0.30, B: 0.40},
        values={A: 3000.0, B: 4000.0, C: 3000.0},
        held=frozenset({A, B, C}),
    )
    assert plan.feasible
    assert plan.v == pytest.approx(10000.0)
    assert plan.v_prime == pytest.approx(10000.0)
    assert plan.c_min == pytest.approx(0.0)
    assert plan.buys[A] == pytest.approx(0.0)
    assert plan.buys[B] == pytest.approx(0.0)
    assert plan.buys[C] == pytest.approx(0.0)


def test_unheld_target_open_a_position():
    # A is targeted but not held (v=0) → open-a-position: buy = t * V'_min.
    # B is held with value 7000; R = 0.70 (residual).
    # V'_min is driven by the residual bound: 7000/0.70 = 10000.
    plan = compute_rebalance(
        targets={A: 0.30},
        values={A: 0.0, B: 7000.0},
        held=frozenset({B}),  # A unheld
    )
    assert plan.feasible
    assert plan.v == pytest.approx(7000.0)
    assert plan.v_prime == pytest.approx(10000.0)
    assert plan.buys[A] == pytest.approx(3000.0)  # 0.30 * 10000 - 0
    # Residual bound (7000/0.70=10000) binds; pin bound for A is 0/0.30=0.
    assert plan.binders == []
    assert plan.residual_bound_binds


def test_residual_receives_a_buy():
    # A=3000 (held, →30%), C=4000 (held, untargeted residual).
    # Pin bound: 3000/0.30 = 10000 (binds); residual bound: 4000/0.70 = 5714.29 (does not bind).
    # c_rest = 0.70*10000 - 4000 = 3000; c_C = 3000.
    plan = compute_rebalance(
        targets={A: 0.30},
        values={A: 3000.0, C: 4000.0},
        held=frozenset({A, C}),
    )
    assert plan.feasible
    assert plan.v_prime == pytest.approx(10000.0)
    assert plan.buys[A] == pytest.approx(0.0)
    assert plan.buys[C] == pytest.approx(3000.0)
    assert plan.binders == [A]


def test_float_clamp_binder_buy_never_negative():
    # Use 1/3 — not representable exactly in float — so c_A = t*V'_min - v may be -ε.
    t = 1.0 / 3.0
    plan = compute_rebalance(
        targets={A: t, B: t},
        values={A: 1000.0, B: 500.0, C: 300.0},
        held=frozenset({A, B, C}),
    )
    assert plan.feasible
    # A's bound: 1000/(1/3) = 3000. c_A = (1/3)*3000 - 1000 ≈ -1e-13 → clamped to 0.
    assert plan.buys[A] >= 0.0
    assert plan.buys[A] == pytest.approx(0.0, abs=1e-9)
    assert A in plan.binders


def test_implied_residual_tgt_pct_sums_to_100():
    # Σ target fractions + implied residuals must sum to 1.0.
    # A→30%, B→40%; R=30%; residual split: C gets 30% (its implied target).
    # Implied t_C = R * v_C / v_rest = 0.30 * 1000 / 1000 = 0.30 = 30%.
    plan = compute_rebalance(
        targets={A: 0.30, B: 0.40},
        values={A: 6000.0, B: 3000.0, C: 1000.0},
        held=frozenset({A, B, C}),
    )
    assert plan.feasible
    R = 1.0 - 0.30 - 0.40
    v_rest = 1000.0
    implied_t_C = R * 1000.0 / v_rest
    total = 0.30 + 0.40 + implied_t_C
    assert total == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# Output shape tests — verify --months column presence/absence.
# ---------------------------------------------------------------------------


def test_months_1_omits_monthly_column(tmp_path, capsys):
    db, config, meta = _seed(
        tmp_path,
        transactions=[_buy("t1", "2024-01-01", A, 100.0, 10.0)],
        prices=[(A, "2024-01-01", 10.0)],
        currencies={A: "EUR"},
        names={A: "Fund A"},
    )
    code = reb.main(_args(db, config, meta,
                         "--target", f"{A}:100",
                         "--months", "1",
                         "--as-of", "2024-01-01"))
    out = capsys.readouterr().out
    assert code == 0
    assert "Monthly" not in out


def test_months_gt_1_includes_monthly_column(tmp_path, capsys):
    db, config, meta = _seed(
        tmp_path,
        transactions=[_buy("t1", "2024-01-01", A, 100.0, 10.0)],
        prices=[(A, "2024-01-01", 10.0)],
        currencies={A: "EUR"},
        names={A: "Fund A"},
    )
    code = reb.main(_args(db, config, meta,
                         "--target", f"{A}:100",
                         "--months", "6",
                         "--as-of", "2024-01-01"))
    out = capsys.readouterr().out
    assert code == 0
    assert "Monthly" in out


# ---------------------------------------------------------------------------
# --explain on infeasible plan emits UNAVAILABLE block.
# ---------------------------------------------------------------------------


def test_explain_on_infeasible_plan_emits_unavailable_block(tmp_path, capsys):
    db, config, meta = _seed(
        tmp_path,
        transactions=[_buy("t1", "2024-01-01", A, 100.0, 10.0)],
        # No prices → target_unvaluable
        currencies={A: "EUR"},
        names={A: "Fund A"},
    )
    code = reb.main(_args(db, config, meta,
                         "--target", f"{A}:30",
                         "--explain",
                         "--as-of", "2024-01-01"))
    out = capsys.readouterr().out
    assert code == 0
    assert "UNAVAILABLE" in out
    assert "Provenance" in out


# ---------------------------------------------------------------------------
# Carried-forward stale close is CALCULATED (not UNAVAILABLE).
# ---------------------------------------------------------------------------


def test_stale_close_is_calculated_not_unavailable(tmp_path, capsys):
    db, config, meta = _seed(
        tmp_path,
        transactions=[_buy("t1", "2024-01-01", A, 100.0, 10.0)],
        prices=[(A, "2024-01-01", 10.0)],  # no close on as-of 2024-12-31
        currencies={A: "EUR"},
        names={A: "Fund A"},
    )
    code = reb.main(_args(db, config, meta,
                         "--target", f"{A}:100",
                         "--as-of", "2024-12-31"))
    out = capsys.readouterr().out
    assert code == 0
    # Plan is feasible: carried-forward close → CALCULATED, ~ flagged
    assert "UNAVAILABLE" not in out
    assert "~" in out or "estimated" in out  # stale close disclosure


# ---------------------------------------------------------------------------
# As-of seed: a fund bought before and SOLD AFTER the as-of date still appears.
# Guards against using portfolio_isins() (which is as-of-blind).
# ---------------------------------------------------------------------------


def test_asof_seed_fund_sold_after_asof_appears_in_snapshot(tmp_path, capsys):
    # A is bought 2024-01-01, sold 2024-06-01. As-of = 2024-03-01.
    db, config, meta = _seed(
        tmp_path,
        transactions=[
            _buy("t1", "2024-01-01", A, 100.0, 10.0),
            _sell("t2", "2024-06-01", A, 100.0, 12.0),
        ],
        prices=[
            (A, "2024-01-01", 10.0),
            (A, "2024-03-01", 11.0),
            (A, "2024-06-01", 12.0),
        ],
        currencies={A: "EUR"},
        names={A: "Fund A"},
    )
    # At as-of 2024-03-01, A is still held (sold on 2024-06-01, after as-of).
    code = reb.main(_args(db, config, meta,
                         "--target", f"{A}:100",
                         "--as-of", "2024-03-01"))
    out = capsys.readouterr().out
    # A should appear in the table (held as-of 2024-03-01)
    assert code == 0
    assert A in out
    assert "UNAVAILABLE" not in out


# ---------------------------------------------------------------------------
# Argparse rejections (all should return non-zero or error via parser.error).
# ---------------------------------------------------------------------------


def test_argparse_no_target_required():
    with pytest.raises(SystemExit):
        reb.main(["--db", "/tmp/x.db"])


def test_argparse_sum_exceeds_100():
    with pytest.raises(SystemExit):
        reb.main(["--db", "/tmp/x.db", "--target", f"{A}:60", "--target", f"{B}:50"])


def test_argparse_duplicate_isin():
    with pytest.raises(SystemExit):
        reb.main(["--db", "/tmp/x.db", "--target", f"{A}:30", "--target", f"{A}:40"])


def test_argparse_pct_zero_rejected():
    with pytest.raises(SystemExit):
        reb.main(["--db", "/tmp/x.db", "--target", f"{A}:0"])


def test_argparse_malformed_missing_colon():
    with pytest.raises(SystemExit):
        reb.main(["--db", "/tmp/x.db", "--target", f"{A}30"])


def test_argparse_malformed_non_numeric_pct():
    with pytest.raises(SystemExit):
        reb.main(["--db", "/tmp/x.db", "--target", f"{A}:abc"])


def test_argparse_13char_isin_is_valid(tmp_path):
    # 13-char synthetic ISINs like IE00EUR000001 must pass the loose ISIN check.
    long_isin = "IE00EUR000001"
    assert len(long_isin) == 13
    # _parse_target should NOT raise for 13-char ISINs
    result = reb._parse_target(f"{long_isin}:30")
    assert result == (long_isin, 30.0)


# ---------------------------------------------------------------------------
# Integration test: main() end-to-end via --db, feasible + infeasible, both exit 0.
# ---------------------------------------------------------------------------


def test_main_feasible_basic(tmp_path, capsys):
    db, config, meta = _seed(
        tmp_path,
        transactions=[
            _buy("t1", "2024-01-01", A, 100.0, 60.0),  # A = 6000
            _buy("t2", "2024-01-01", B, 100.0, 30.0),  # B = 3000
            _buy("t3", "2024-01-01", C, 100.0, 10.0),  # C = 1000 (untargeted)
        ],
        prices=[
            (A, "2024-01-01", 60.0),
            (B, "2024-01-01", 30.0),
            (C, "2024-01-01", 10.0),
        ],
        currencies={A: "EUR", B: "EUR", C: "EUR"},
        names={A: "Fund A", B: "Fund B", C: "Fund C"},
    )
    code = reb.main(_args(db, config, meta,
                         "--target", f"{A}:30",
                         "--target", f"{B}:40",
                         "--as-of", "2024-01-01"))
    out = capsys.readouterr().out
    assert code == 0
    assert "10,000.00" in out  # C_min
    assert "20,000.00" in out  # V'_min
    assert "0.00" in out       # Fund A buys €0 (binder)
    assert "◄ binds" in out
    assert "TOTAL" in out


def test_main_infeasible_target_unvaluable_exit_0(tmp_path, capsys):
    # A is held but no prices → target_unvaluable → exit 0 (honest result).
    db, config, meta = _seed(
        tmp_path,
        transactions=[_buy("t1", "2024-01-01", A, 100.0, 10.0)],
        # No prices table rows for A
        currencies={A: "EUR"},
        names={A: "Fund A"},
    )
    code = reb.main(_args(db, config, meta,
                         "--target", f"{A}:30",
                         "--as-of", "2024-01-01"))
    out = capsys.readouterr().out
    assert code == 0
    assert "UNAVAILABLE" in out
    assert "target_unvaluable" in out


def test_main_dca_monthly_column_sums(tmp_path, capsys):
    # Buy-only plan with 5 months DCA; Monthly€ = Buy€ / 5.
    db, config, meta = _seed(
        tmp_path,
        transactions=[
            _buy("t1", "2024-01-01", A, 100.0, 30.0),  # A = 3000
            _buy("t2", "2024-01-01", C, 100.0, 40.0),  # C = 4000 (untargeted)
        ],
        prices=[(A, "2024-01-01", 30.0), (C, "2024-01-01", 40.0)],
        currencies={A: "EUR", C: "EUR"},
        names={A: "Fund A", C: "Fund C"},
    )
    code = reb.main(_args(db, config, meta,
                         "--target", f"{A}:30",
                         "--months", "5",
                         "--as-of", "2024-01-01"))
    out = capsys.readouterr().out
    assert code == 0
    assert "Monthly" in out
    assert "DCA: 5 months" in out


def test_main_explain_feasible(tmp_path, capsys):
    db, config, meta = _seed(
        tmp_path,
        transactions=[_buy("t1", "2024-01-01", A, 100.0, 30.0)],
        prices=[(A, "2024-01-01", 30.0)],
        currencies={A: "EUR"},
        names={A: "Fund A"},
    )
    code = reb.main(_args(db, config, meta,
                         "--target", f"{A}:100",
                         "--explain",
                         "--as-of", "2024-01-01"))
    out = capsys.readouterr().out
    assert code == 0
    assert "Provenance" in out
    assert "buy_only_rebalance_v1" in out


def test_main_show_status_column(tmp_path, capsys):
    db, config, meta = _seed(
        tmp_path,
        transactions=[_buy("t1", "2024-01-01", A, 100.0, 30.0)],
        prices=[(A, "2024-01-01", 30.0)],
        currencies={A: "EUR"},
        names={A: "Fund A"},
    )
    code = reb.main(_args(db, config, meta,
                         "--target", f"{A}:100",
                         "--show-status",
                         "--as-of", "2024-01-01"))
    out = capsys.readouterr().out
    assert code == 0
    assert "Status" in out
    assert "CALCULATED" in out


# ---------------------------------------------------------------------------
# Float-clamp: a binder whose t·V'_min − v rounds to a tiny NEGATIVE is
# clamped to exactly 0.0 (compute_rebalance buy loop).
# ---------------------------------------------------------------------------


def test_float_clamp_negative_binder_buy_clamped_to_zero():
    # t = 3/29 is not float-representable; with v_A = 1000 the binder bound is
    # 1000/(3/29) = 9666.67 and c_A = t·9666.67 − 1000 ≈ -1.1e-13 → clamp to 0.0.
    t = 3.0 / 29.0
    plan = compute_rebalance(
        targets={A: t},
        values={A: 1000.0, C: 1000.0},  # C untargeted keeps residual bound below A's
        held=frozenset({A, C}),
    )
    assert plan.feasible
    assert plan.binders == [A]
    assert plan.buys[A] == 0.0  # exactly 0.0, not a tiny negative


def test_float_clamp_negative_residual_buy_clamped_to_zero():
    # R = 1 − 1/3 is not float-representable; the residual bound 700/R binds and
    # c_rest = R·(700/R) − 700 ≈ -1.1e-13, which rendered as "-0.00" → clamp to 0.0.
    plan = compute_rebalance(
        targets={A: 1.0 / 3.0},
        values={A: 1.0, C: 700.0},
        held=frozenset({A, C}),
    )
    assert plan.feasible
    assert plan.residual_bound_binds
    assert plan.buys[C] == 0.0  # exactly 0.0, not a tiny negative


# ---------------------------------------------------------------------------
# --explain reconstructs the residual-bound binder and the DCA per-month split.
# ---------------------------------------------------------------------------


def test_explain_reports_residual_bound_binder(tmp_path, capsys):
    # A targeted but unheld, B held (residual) → the residual bound drives V'_min.
    db, config, meta = _seed(
        tmp_path,
        transactions=[_buy("t1", "2024-01-01", B, 100.0, 70.0)],  # B = 7000, untargeted
        prices=[(B, "2024-01-01", 70.0)],
        currencies={B: "EUR"},
        names={B: "Fund B"},
    )
    code = reb.main(_args(db, config, meta,
                         "--target", f"{A}:30",
                         "--explain",
                         "--as-of", "2024-01-01"))
    out = capsys.readouterr().out
    assert code == 0
    assert "residual bucket" in out


def test_explain_feasible_with_months_shows_per_month(tmp_path, capsys):
    db, config, meta = _seed(
        tmp_path,
        transactions=[
            _buy("t1", "2024-01-01", A, 100.0, 30.0),  # A = 3000
            _buy("t2", "2024-01-01", C, 100.0, 40.0),  # C = 4000 (residual)
        ],
        prices=[(A, "2024-01-01", 30.0), (C, "2024-01-01", 40.0)],
        currencies={A: "EUR", C: "EUR"},
        names={A: "Fund A", C: "Fund C"},
    )
    code = reb.main(_args(db, config, meta,
                         "--target", f"{A}:30",
                         "--months", "4",
                         "--explain",
                         "--as-of", "2024-01-01"))
    out = capsys.readouterr().out
    assert code == 0
    assert "Provenance" in out
    assert "/month" in out


# ---------------------------------------------------------------------------
# Multiple stale (~) rows render the itemised block (not the single-line form).
# ---------------------------------------------------------------------------


def test_multiple_stale_closes_itemised(tmp_path, capsys):
    # Two held funds, both with a close only before the as-of date → both estimated.
    db, config, meta = _seed(
        tmp_path,
        transactions=[
            _buy("t1", "2024-01-01", A, 100.0, 50.0),
            _buy("t2", "2024-01-01", B, 100.0, 50.0),
        ],
        prices=[(A, "2024-01-01", 50.0), (B, "2024-01-01", 50.0)],
        currencies={A: "EUR", B: "EUR"},
        names={A: "Fund A", B: "Fund B"},
    )
    code = reb.main(_args(db, config, meta,
                         "--target", f"{A}:50",
                         "--target", f"{B}:50",
                         "--as-of", "2024-12-31"))
    out = capsys.readouterr().out
    assert code == 0
    assert "estimated from the latest price" in out  # itemised (multi-row) form
    assert A in out and B in out


# ---------------------------------------------------------------------------
# Infeasible reasons surfaced end-to-end via main() (all exit 0, honest result).
# ---------------------------------------------------------------------------


def test_main_empty_portfolio_message(tmp_path, capsys):
    # No transactions → the targeted fund is unheld and V = 0.
    db, config, meta = _seed(tmp_path, names={A: "Fund A"})
    code = reb.main(_args(db, config, meta,
                         "--target", f"{A}:30",
                         "--as-of", "2024-01-01"))
    out = capsys.readouterr().out
    assert code == 0
    assert "empty_portfolio" in out


def test_main_residual_full_message(tmp_path, capsys):
    # Σ targets = 100% while a valued untargeted fund (C) is held.
    db, config, meta = _seed(
        tmp_path,
        transactions=[
            _buy("t1", "2024-01-01", A, 100.0, 60.0),  # A = 6000
            _buy("t2", "2024-01-01", B, 100.0, 40.0),  # B = 4000
            _buy("t3", "2024-01-01", C, 100.0, 10.0),  # C = 1000 (untargeted)
        ],
        prices=[
            (A, "2024-01-01", 60.0),
            (B, "2024-01-01", 40.0),
            (C, "2024-01-01", 10.0),
        ],
        currencies={A: "EUR", B: "EUR", C: "EUR"},
        names={A: "Fund A", B: "Fund B", C: "Fund C"},
    )
    code = reb.main(_args(db, config, meta,
                         "--target", f"{A}:60",
                         "--target", f"{B}:40",
                         "--as-of", "2024-01-01"))
    out = capsys.readouterr().out
    assert code == 0
    assert "residual_full" in out


def test_main_residual_unallocable_message(tmp_path, capsys):
    # R = 30% but no untargeted valued fund exists to absorb it.
    db, config, meta = _seed(
        tmp_path,
        transactions=[_buy("t1", "2024-01-01", A, 100.0, 70.0)],  # A = 7000, only fund
        prices=[(A, "2024-01-01", 70.0)],
        currencies={A: "EUR"},
        names={A: "Fund A"},
    )
    code = reb.main(_args(db, config, meta,
                         "--target", f"{A}:70",
                         "--as-of", "2024-01-01"))
    out = capsys.readouterr().out
    assert code == 0
    assert "residual_unallocable" in out


# ---------------------------------------------------------------------------
# More argparse rejections + the main() top-level error handler.
# ---------------------------------------------------------------------------


def test_parse_target_empty_isin_rejected():
    with pytest.raises(reb.argparse.ArgumentTypeError):
        reb._parse_target(":30")


def test_argparse_months_non_integer():
    with pytest.raises(SystemExit):
        reb.main(["--db", "/tmp/x.db", "--target", f"{A}:30", "--months", "abc"])


def test_argparse_months_below_minimum():
    with pytest.raises(SystemExit):
        reb.main(["--db", "/tmp/x.db", "--target", f"{A}:30", "--months", "0"])


def test_main_invalid_as_of_returns_1(capsys):
    # Bad --as-of raises ValueError, caught by main()'s top-level handler → exit 1.
    code = reb.main(["--db", "/tmp/x.db", "--target", f"{A}:30", "--as-of", "not-a-date"])
    out = capsys.readouterr().out
    assert code == 1
    assert "✗ Error" in out


def test_reason_message_unknown_reason_falls_through():
    # Defensive fallback for an unrecognised reason string.
    plan = RebalancePlan(
        feasible=False, reason="something_new", unvaluable_targets=[],
        v=0.0, v_prime=0.0, c_min=0.0, buys={}, binders=[],
        residual_bound_binds=False,
    )
    assert reb._reason_message(plan, {A: 0.30}) == "something_new"


# ---------------------------------------------------------------------------
# _assemble edge cases: post-as-of-only fund is skipped; untargeted unvaluable
# held fund is disclosed in the footer (not counted, not fatal).
# ---------------------------------------------------------------------------


def test_fund_bought_after_asof_is_skipped(tmp_path, capsys):
    # B's only transaction is AFTER the as-of date → it has no as-of position at all.
    db, config, meta = _seed(
        tmp_path,
        transactions=[
            _buy("t1", "2024-01-01", A, 100.0, 30.0),  # A held as-of
            _buy("t2", "2024-06-01", B, 100.0, 50.0),  # B bought after as-of
        ],
        prices=[(A, "2024-01-01", 30.0), (B, "2024-06-01", 50.0)],
        currencies={A: "EUR", B: "EUR"},
        names={A: "Fund A", B: "Fund B"},
    )
    code = reb.main(_args(db, config, meta,
                         "--target", f"{A}:100",
                         "--as-of", "2024-03-01"))
    out = capsys.readouterr().out
    assert code == 0
    assert "UNAVAILABLE" not in out
    assert B not in out  # B has no as-of position; excluded entirely


def test_untargeted_unvaluable_held_fund_disclosed_in_footer(tmp_path, capsys):
    # B is held as-of but has no close/FX and is untargeted → excluded from the
    # table, disclosed in the footer, and does not make the plan UNAVAILABLE.
    db, config, meta = _seed(
        tmp_path,
        transactions=[
            _buy("t1", "2024-01-01", A, 100.0, 30.0),  # A held + valued + targeted
            _buy("t2", "2024-01-01", B, 100.0, 50.0),  # B held, untargeted
        ],
        prices=[(A, "2024-01-01", 30.0)],  # no price for B → unvaluable
        currencies={A: "EUR", B: "EUR"},
        names={A: "Fund A", B: "Fund B"},
    )
    code = reb.main(_args(db, config, meta,
                         "--target", f"{A}:100",
                         "--as-of", "2024-01-01"))
    out = capsys.readouterr().out
    assert code == 0
    assert "UNAVAILABLE" not in out
    assert f"Untargeted (unvaluable, excluded): {B}" in out


# ---------------------------------------------------------------------------
# Target summary recap (ADR-0017): target sum + percents scaled to 100%.
# ---------------------------------------------------------------------------


def test_scaled_targets_normalizes_to_one():
    scaled = reb._scaled_targets({A: 0.30, B: 0.40})
    # Sorted by weight desc: B (0.40) then A (0.30).
    assert [isin for isin, _, _ in scaled] == [B, A]
    assert scaled[0] == pytest.approx((B, 0.40, 0.40 / 0.70), abs=1e-9)  # type: ignore[arg-type]
    assert scaled[1][2] == pytest.approx(0.30 / 0.70)
    assert sum(s for _, _, s in scaled) == pytest.approx(1.0)


def test_scaled_targets_single_target_is_full_sleeve():
    scaled = reb._scaled_targets({A: 0.30})
    assert scaled == [(A, 0.30, 1.0)]


def test_render_target_summary_shows_sum_and_scaled():
    lines = reb.render_target_summary({A: 0.30, B: 0.40}, {A: "Fund A", B: "Fund B"})
    text = "\n".join(lines)
    assert "Scaled%" in text
    assert "57.1%" in text and "42.9%" in text  # scaled shares of the sleeve
    # TOTAL row: Tgt% column = target sum (70%), Scaled% = 100%.
    total = next(ln for ln in lines if ln.startswith("TOTAL"))
    assert "70.0%" in total and "100.0%" in total


def test_main_feasible_shows_target_summary(tmp_path, capsys):
    db, config, meta = _seed(
        tmp_path,
        transactions=[
            _buy("t1", "2024-01-01", A, 100.0, 60.0),
            _buy("t2", "2024-01-01", B, 100.0, 30.0),
            _buy("t3", "2024-01-01", C, 100.0, 10.0),
        ],
        prices=[(A, "2024-01-01", 60.0), (B, "2024-01-01", 30.0), (C, "2024-01-01", 10.0)],
        currencies={A: "EUR", B: "EUR", C: "EUR"},
        names={A: "Fund A", B: "Fund B", C: "Fund C"},
    )
    code = reb.main(_args(db, config, meta,
                         "--target", f"{A}:30",
                         "--target", f"{B}:40",
                         "--as-of", "2024-01-01"))
    out = capsys.readouterr().out
    assert code == 0
    assert "Targets (" in out
    assert "Scaled%" in out
    assert "70.0%" in out   # target sum
    assert "57.1%" in out   # B scaled share


def test_main_infeasible_shows_target_summary(tmp_path, capsys):
    # Even an UNAVAILABLE plan recaps what the user asked for.
    db, config, meta = _seed(
        tmp_path,
        transactions=[_buy("t1", "2024-01-01", A, 100.0, 70.0)],  # only A → residual_unallocable
        prices=[(A, "2024-01-01", 70.0)],
        currencies={A: "EUR"},
        names={A: "Fund A"},
    )
    code = reb.main(_args(db, config, meta,
                         "--target", f"{A}:70",
                         "--as-of", "2024-01-01"))
    out = capsys.readouterr().out
    assert code == 0
    assert "Targets (" in out
    assert "residual_unallocable" in out


# ---------------------------------------------------------------------------
# DB / seed helpers (same pattern as test_performance.py)
# ---------------------------------------------------------------------------


def _seed(tmp_path, *, transactions=(), prices=(), fx=(), currencies=None, names=None):
    db = tmp_path / "e1f.db"
    with closing(sqlite3.connect(str(db))) as conn:
        conn.execute(
            "CREATE TABLE transactions (broker TEXT, transaction_id TEXT, datetime TEXT, "
            "symbol TEXT, side TEXT, shares REAL, price REAL, fee REAL, tax REAL, "
            "PRIMARY KEY (broker, transaction_id))"
        )
        conn.execute(
            "CREATE TABLE prices (isin TEXT, date TEXT, close REAL, PRIMARY KEY (isin, date))"
        )
        conn.execute(
            "CREATE TABLE fx_rates (base TEXT, quote TEXT, date TEXT, rate REAL, "
            "PRIMARY KEY (base, quote, date))"
        )
        conn.executemany(
            "INSERT INTO transactions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", transactions
        )
        conn.executemany("INSERT INTO prices VALUES (?, ?, ?)", prices)
        conn.executemany("INSERT INTO fx_rates VALUES (?, ?, ?, ?)", fx)
        conn.commit()
    config = tmp_path / "config.yaml"
    config.write_text(yaml.dump({
        "etfs": {isin: {"name": n} for isin, n in (names or {}).items()}
    }))
    meta = tmp_path / "meta.yaml"
    meta.write_text(yaml.dump({
        isin: {"currency": c, "symbol": f"{isin}:X:{c}", "xid": "1"}
        for isin, c in (currencies or {}).items()
    }))
    return str(db), str(config), str(meta)


def _buy(txid, day, isin, shares, price_eur, fee=0.0, broker="tr"):
    return (broker, txid, day, isin, "BUY", shares, price_eur, fee, 0.0)


def _sell(txid, day, isin, shares, price_eur, fee=0.0, broker="tr"):
    return (broker, txid, day, isin, "SELL", shares, price_eur, fee, 0.0)


def _args(db, config, meta, *extra):
    return ["--db", db, "--config", config, "--currency-meta", meta, *extra]


# ---------------------------------------------------------------------------
# --scenario: load targets (and months) from a saved scenario (ADR-0017)
# ---------------------------------------------------------------------------


def _feasible_seed(tmp_path):
    return _seed(
        tmp_path,
        transactions=[
            _buy("t1", "2024-01-01", A, 100.0, 60.0),  # A = 6000
            _buy("t2", "2024-01-01", B, 100.0, 40.0),  # B = 4000
        ],
        prices=[(A, "2024-01-01", 60.0), (B, "2024-01-01", 40.0)],
        currencies={A: "EUR", B: "EUR"},
        names={A: "Fund A", B: "Fund B"},
    )


def _write_scenario(tmp_path, **body):
    from e1f.common import Scenario, save_scenario
    path = str(tmp_path / "s.yaml")
    save_scenario(Scenario(**body), path)
    return path


def test_scenario_loads_targets_and_months(tmp_path, capsys):
    db, config, meta = _feasible_seed(tmp_path)
    sf = _write_scenario(tmp_path, name="core", targets={A: 60.0, B: 40.0}, months=5)
    code = reb.main(_args(db, config, meta,
                         "--scenario", "core", "--scenarios-file", sf,
                         "--as-of", "2024-01-01"))
    out = capsys.readouterr().out
    assert code == 0
    assert "DCA: 5 months" in out


def test_scenario_cli_months_overrides_stored(tmp_path, capsys):
    db, config, meta = _feasible_seed(tmp_path)
    sf = _write_scenario(tmp_path, name="core", targets={A: 60.0, B: 40.0}, months=5)
    code = reb.main(_args(db, config, meta,
                         "--scenario", "core", "--scenarios-file", sf,
                         "--months", "3", "--as-of", "2024-01-01"))
    out = capsys.readouterr().out
    assert code == 0
    assert "DCA: 3 months" in out and "DCA: 5" not in out


def test_scenario_without_months_defaults_to_lump_sum(tmp_path, capsys):
    db, config, meta = _feasible_seed(tmp_path)
    sf = _write_scenario(tmp_path, name="core", targets={A: 60.0, B: 40.0})
    code = reb.main(_args(db, config, meta,
                         "--scenario", "core", "--scenarios-file", sf,
                         "--as-of", "2024-01-01"))
    out = capsys.readouterr().out
    assert code == 0
    assert "DCA" not in out  # months=1 → no DCA banner


def test_scenario_and_target_mutually_exclusive(tmp_path):
    db, config, meta = _feasible_seed(tmp_path)
    sf = _write_scenario(tmp_path, name="core", targets={A: 60.0})
    with pytest.raises(SystemExit):
        reb.main(_args(db, config, meta,
                       "--scenario", "core", "--scenarios-file", sf,
                       "--target", f"{A}:60"))


def test_scenario_missing_name_errors(tmp_path):
    db, config, meta = _feasible_seed(tmp_path)
    sf = _write_scenario(tmp_path, name="core", targets={A: 60.0})
    with pytest.raises(SystemExit):
        reb.main(_args(db, config, meta, "--scenario", "ghost", "--scenarios-file", sf))


def test_sort_rows_by_value():
    from e1f.common import Status
    from e1f.rebalance import RebalanceRow

    def row(isin: str, v: float) -> RebalanceRow:
        return RebalanceRow(
            isin=isin, name=isin, v=v, cur_pct=v / 10.0, t_pct=0.0, buy=0.0,
            final_v=v, is_residual=False, is_binder=False, price_date=None,
            estimated=False, status=Status.CALCULATED,
        )

    ordered = reb.sort_rows([row("A", 10.0), row("B", 50.0)], sort_by="value", reverse=True)
    assert [r.isin for r in ordered] == ["B", "A"]
    by_name = reb.sort_rows([row("B", 1.0), row("A", 1.0)], sort_by="name")
    assert [r.isin for r in by_name] == ["A", "B"]
    by_isin = reb.sort_rows([row("B", 1.0), row("A", 1.0)], sort_by="isin")
    assert [r.isin for r in by_isin] == ["A", "B"]


def test_main_sort_value_reverse_orders_plan_rows(tmp_path, capsys):
    db, config, meta = _seed(
        tmp_path,
        transactions=[
            _buy("t1", "2024-01-01", A, 100.0, 60.0),
            _buy("t2", "2024-01-01", B, 100.0, 30.0),
            _buy("t3", "2024-01-01", C, 100.0, 10.0),
        ],
        prices=[
            (A, "2024-01-01", 60.0),
            (B, "2024-01-01", 30.0),
            (C, "2024-01-01", 10.0),
        ],
        currencies={A: "EUR", B: "EUR", C: "EUR"},
        names={A: "Fund A", B: "Fund B", C: "Fund C"},
    )
    code = reb.main(_args(
        db, config, meta,
        "--target", f"{A}:30", "--target", f"{B}:40",
        "--as-of", "2024-01-01", "--sort", "value", "--reverse",
    ))
    out = capsys.readouterr().out
    assert code == 0
    lines = out.splitlines()
    header = next(i for i, ln in enumerate(lines) if "Current€" in ln)
    plan = [ln.split()[0] for ln in lines[header + 2:] if ln.startswith("IE00")]
    assert plan == [A, B, C]


# ---------------------------------------------------------------------------
# Sleeve plans: frozen funds + budget (ADR-0060). Hand-computed pins.
# ---------------------------------------------------------------------------

F = "IE00FUND000F0"
G = "IE00FUND000G0"


def _sleeve(targets, values, frozen, budget=None, held=None):
    from e1f.common import compute_sleeve_rebalance
    held = frozenset(values) if held is None else frozenset(held)
    return compute_sleeve_rebalance(targets, values, held, frozenset(frozen), budget)


def test_sleeve_minimum_plan_ignores_the_frozen_fund():
    # S = 6000 + 1000 = 7000. Bounds: A 6000/0.6 = 10000, B 1000/0.4 = 2500, so
    # L = 10000, C_min = 3000; A binds, B buys 0.4·10000 − 1000 = 3000. The frozen
    # €3000 forces nothing (the residual rule would have made it ≤ its share).
    plan = _sleeve({A: 0.6, B: 0.4}, {A: 6000.0, B: 1000.0, F: 3000.0}, {F})
    assert plan.feasible and plan.sleeve
    assert plan.sleeve_v == pytest.approx(7000.0)
    assert plan.level == pytest.approx(10000.0)
    assert plan.c_min == pytest.approx(3000.0)
    assert plan.buys == pytest.approx({A: 0.0, B: 3000.0, F: 0.0})
    assert plan.binders == [A]
    assert plan.v == pytest.approx(10000.0) and plan.v_final == pytest.approx(13000.0)
    assert plan.shortfall == 0.0


def test_sleeve_budget_above_minimum_keeps_the_targets():
    # B = 5000 ≥ C_min 3000 → L = 7000 + 5000 = 12000: A 7200 − 6000, B 4800 − 1000.
    plan = _sleeve({A: 0.6, B: 0.4}, {A: 6000.0, B: 1000.0, F: 3000.0}, {F}, budget=5000.0)
    assert plan.level == pytest.approx(12000.0)
    assert plan.buys == pytest.approx({A: 1200.0, B: 3800.0, F: 0.0})
    assert plan.injection == pytest.approx(5000.0)
    assert plan.binders == [] and plan.shortfall == 0.0


def test_sleeve_budget_below_minimum_closes_the_largest_gap_first():
    # B = 1000 < C_min 3000. Only B is below the level: L = (1000 + 1000)/0.4 = 5000
    # ≤ A's bound 10000, so B buys 0.4·5000 − 1000 = 1000 and A nothing.
    plan = _sleeve({A: 0.6, B: 0.4}, {A: 6000.0, B: 1000.0, F: 3000.0}, {F}, budget=1000.0)
    assert plan.level == pytest.approx(5000.0)
    assert plan.buys == pytest.approx({A: 0.0, B: 1000.0, F: 0.0})
    assert plan.binders == [A]
    assert plan.shortfall == pytest.approx(2000.0)


def test_sleeve_budget_below_minimum_crosses_a_bound():
    # Bounds C 0 (unheld), B 1000/0.3 = 3333.33, A 5000/0.5 = 10000; B = 2000.
    # C alone: L = 2000/0.2 = 10000 > 3333.33, so B joins: L = (2000 + 1000)/0.5 = 6000.
    # Buys C 0.2·6000 = 1200, B 0.3·6000 − 1000 = 800, A 0.
    plan = _sleeve({A: 0.5, B: 0.3, C: 0.2}, {A: 5000.0, B: 1000.0, C: 0.0}, set(),
                   budget=2000.0, held={A, B})
    assert plan.level == pytest.approx(6000.0)
    assert plan.buys == pytest.approx({A: 0.0, B: 800.0, C: 1200.0})
    assert sum(plan.buys.values()) == pytest.approx(2000.0)


def test_sleeve_with_budget_reproduces_a_binding_residual_plan():
    # Residual plan: A 57%, B 38% of the book, F untargeted (R = 5%). Values A 6000,
    # B 1000, F 1000: the residual bound 1000/0.05 = 20000 binds (pins 10526.32,
    # 2631.58), so C_min = 12000, A buys 0.57·20000 − 6000 = 5400, B 6600, F 0.
    # The same targets as a sleeve (60/40, F frozen) with that €12000 budget give
    # L = 7000 + 12000 = 19000 and the same buys.
    values = {A: 6000.0, B: 1000.0, F: 1000.0}
    residual = compute_rebalance({A: 0.57, B: 0.38}, values, frozenset(values))
    sleeve = _sleeve({A: 0.6, B: 0.4}, values, {F}, budget=residual.c_min)
    assert residual.c_min == pytest.approx(12000.0)
    assert residual.buys == pytest.approx({A: 5400.0, B: 6600.0, F: 0.0})
    assert sleeve.buys == pytest.approx(residual.buys)
    assert sleeve.v_final == pytest.approx(residual.v_prime)


def test_sleeve_unclassified_held_fund_is_unavailable():
    plan = _sleeve({A: 1.0}, {A: 6000.0, F: 3000.0, G: 50.0}, {F})
    assert not plan.feasible
    assert plan.reason == "unclassified" and plan.unclassified == [G]


def test_sleeve_target_unvaluable_comes_before_unclassified():
    plan = _sleeve({A: 1.0}, {A: None, G: 50.0}, set())
    assert plan.reason == "target_unvaluable" and plan.unvaluable_targets == [A]


def test_sleeve_empty_without_budget_is_unavailable():
    plan = _sleeve({A: 1.0}, {A: 0.0, F: 3000.0}, {F}, held={F})
    assert not plan.feasible and plan.reason == "empty_sleeve"


def test_sleeve_empty_with_budget_opens_the_sleeve():
    plan = _sleeve({A: 0.75, B: 0.25}, {A: 0.0, B: 0.0, F: 3000.0}, {F},
                   budget=1000.0, held={F})
    assert plan.buys == pytest.approx({A: 750.0, B: 250.0, F: 0.0})


def test_sleeve_frozen_fund_unheld_or_unvaluable_gets_no_row():
    plan = _sleeve({A: 1.0}, {A: 6000.0, F: None}, {F, G})
    assert plan.buys == pytest.approx({A: 0.0})
    assert plan.v == pytest.approx(6000.0) and plan.frozen == frozenset()


@pytest.mark.parametrize(
    "targets, frozen, budget",
    [({A: 0.6, B: 0.3}, set(), None), ({A: 1.0}, {A}, None), ({A: 1.0}, set(), -1.0)],
)
def test_sleeve_rejects_invalid_inputs(targets, frozen, budget):
    with pytest.raises(ValueError):
        _sleeve(targets, {A: 6000.0, B: 1000.0}, frozen, budget)


def test_plan_rebalance_budget_needs_a_frozen_set():
    from e1f.common import plan_rebalance
    with pytest.raises(ValueError, match="frozen"):
        plan_rebalance({A: 0.5}, {A: 1.0}, frozenset({A}), None, 100.0)


def _sleeve_seed(tmp_path):
    # A €6000, B €1000, F €3000 (frozen) — the pinned example above.
    return _seed(
        tmp_path,
        transactions=[
            _buy("t1", "2024-01-01", A, 100.0, 60.0),
            _buy("t2", "2024-01-01", B, 100.0, 10.0),
            _buy("t3", "2024-01-01", F, 100.0, 30.0),
        ],
        prices=[(A, "2024-01-01", 60.0), (B, "2024-01-01", 10.0), (F, "2024-01-01", 30.0)],
        currencies={A: "EUR", B: "EUR", F: "EUR"},
        names={A: "Fund A", B: "Fund B", F: "Fund F"},
    )


def test_main_sleeve_table_shows_subtotal_frozen_rows_and_book_share(tmp_path, capsys):
    db, config, meta = _sleeve_seed(tmp_path)
    code = reb.main(_args(db, config, meta, "--target", f"{A}:60", "--target", f"{B}:40",
                          "--frozen", F, "--as-of", "2024-01-01"))
    out = capsys.readouterr().out
    assert code == 0
    assert "Sleeve%" in out and "Book%" in out
    sleeve_row = next(line for line in out.splitlines() if line.startswith("SLEEVE"))
    assert "7,000.00" in sleeve_row and "3,000.00" in sleeve_row and "76.9%" in sleeve_row
    frozen_row = next(line for line in out.splitlines() if line.startswith(F))
    assert "Fund F (frozen)" in frozen_row and "23.1%" in frozen_row
    assert "Inject €3,000.00 to reach targets buy-only." in out
    assert "Frozen: €3,000.00 · 30.0% of the book now → 23.1% after." in out


def test_main_sleeve_budget_short_of_minimum_is_disclosed(tmp_path, capsys):
    db, config, meta = _sleeve_seed(tmp_path)
    code = reb.main(_args(db, config, meta, "--target", f"{A}:60", "--target", f"{B}:40",
                          "--frozen", F, "--monthly", "500", "--months", "2",
                          "--as-of", "2024-01-01"))
    out = capsys.readouterr().out
    assert code == 0
    assert ("Budget €1,000.00 (€500.00 / month × 2) is €2,000.00 short of the €3,000.00"
            in out)
    a_row = next(line for line in out.splitlines() if line.startswith(A))
    assert a_row.endswith("◄ no buy")


def test_main_sleeve_scenario_with_monthly(tmp_path, capsys):
    db, config, meta = _sleeve_seed(tmp_path)
    sf = _write_scenario(tmp_path, name="s", targets={A: 60.0, B: 40.0}, months=2,
                         frozen=(F,), monthly=2500.0)
    code = reb.main(_args(db, config, meta, "--scenario", "s", "--scenarios-file", sf,
                          "--as-of", "2024-01-01", "--explain"))
    out = capsys.readouterr().out
    assert code == 0
    assert "Budget €5,000.00 (€2,500.00 / month × 2) reaches the targets" in out
    assert "L = €12,000.00" in out and "buy_only_sleeve_v1" in out


def test_main_sleeve_unclassified_message(tmp_path, capsys):
    db, config, meta = _sleeve_seed(tmp_path)
    code = reb.main(_args(db, config, meta, "--target", f"{A}:100", "--frozen", F,
                          "--as-of", "2024-01-01", "--explain"))
    out = capsys.readouterr().out
    assert code == 0
    assert f"UNAVAILABLE — unclassified — held fund(s) are neither targeted nor frozen: {B}" in out
    assert "Targets (Tgt% = of the non-frozen sleeve)" in out


def test_main_empty_sleeve_message(tmp_path, capsys):
    db, config, meta = _sleeve_seed(tmp_path)
    code = reb.main(_args(db, config, meta, "--target", f"{D}:100",
                          "--frozen", A, "--frozen", B, "--frozen", F,
                          "--as-of", "2024-01-01"))
    assert code == 0
    assert "UNAVAILABLE — empty_sleeve" in capsys.readouterr().out


@pytest.mark.parametrize(
    "extra",
    [
        ["--target", f"{A}:60", "--monthly", "100"],                       # no sleeve
        ["--target", f"{A}:60", "--frozen", F],                            # Σ ≠ 100
        ["--target", f"{A}:100", "--frozen", A],                           # both
        ["--target", f"{A}:100", "--frozen", F, "--frozen", F],            # dupe
        ["--target", f"{A}:100", "--frozen", F, "--monthly", "0"],         # amount
    ],
)
def test_argparse_sleeve_rejections(tmp_path, extra):
    db, config, meta = _sleeve_seed(tmp_path)
    with pytest.raises(SystemExit):
        reb.main(_args(db, config, meta, *extra))


def test_scenario_and_frozen_mutually_exclusive(tmp_path):
    db, config, meta = _sleeve_seed(tmp_path)
    sf = _write_scenario(tmp_path, name="s", targets={A: 100.0}, frozen=(F,))
    with pytest.raises(SystemExit):
        reb.main(_args(db, config, meta, "--scenario", "s", "--scenarios-file", sf,
                       "--frozen", B))


# ---------------------------------------------------------------------------
# Category scenarios (ADR-0061): grouped display over the same sleeve plan.
# ---------------------------------------------------------------------------


def _category_scenario(tmp_path, **kwargs):
    from e1f.common import Category, Scenario, save_scenario
    # core 60% = A; rest 40% = B 75 / D 25 → flat sleeve targets A 60, B 30, D 10.
    path = str(tmp_path / "s.yaml")
    save_scenario(Scenario.from_categories(
        "g",
        (Category("core", 60.0, {A: 100.0}), Category("rest", 40.0, {B: 75.0, D: 25.0})),
        frozen=(F,),
        **kwargs,
    ), path)
    return path


def _plan_block(out):
    lines = out.splitlines()
    header = next(i for i, ln in enumerate(lines) if "Current€" in ln)
    end = next(i for i in range(header + 2, len(lines)) if lines[i].startswith("---"))
    return lines[header + 2:end]


def test_main_category_table_groups_funds_under_subtotals(tmp_path, capsys):
    # S = 7000 (A 6000, B 1000, D unheld), F €3000 frozen. Bounds: A 6000/0.6 = 10000,
    # B 1000/0.3 = 3333.33, D 0 → L = 10000, C_min = 3000; A binds, B buys
    # 3000 − 1000 = 2000, D 1000. V' = 13000. core: 6000 (85.7% of S) → 6000 (46.2%);
    # rest: 1000 (14.3%) → 4000 (30.8%).
    db, config, meta = _sleeve_seed(tmp_path)
    sf = _category_scenario(tmp_path)
    code = reb.main(_args(db, config, meta, "--scenario", "g", "--scenarios-file", sf,
                          "--as-of", "2024-01-01"))
    out = capsys.readouterr().out
    assert code == 0
    assert "Each category row totals the funds under it" in out
    block = _plan_block(out)
    assert [ln.split()[0] for ln in block] == ["core", A, "rest", B, D]
    assert block[0].split() == ["core", "6,000.00", "85.7%", "60.0%", "0.00", "6,000.00", "46.2%"]
    assert block[2].split() == ["rest", "1,000.00", "14.3%", "40.0%", "3,000.00", "4,000.00",
                                "30.8%"]
    assert block[1].startswith(f"  {A}") and block[1].endswith("◄ binds")
    b = block[3].split()
    assert b[0] == B and b[-4:] == ["30.0%", "2,000.00", "3,000.00", "23.1%"]
    d = block[4].split()
    assert d[0] == D and d[-4:] == ["10.0%", "1,000.00", "1,000.00", "7.7%"]
    sleeve_row = next(ln for ln in out.splitlines() if ln.startswith("SLEEVE"))
    assert sleeve_row.split()[1:] == ["7,000.00", "100.0%", "100.0%", "3,000.00", "10,000.00",
                                      "76.9%"]


def test_main_category_monthly_column_and_sort_within_category(tmp_path, capsys):
    db, config, meta = _sleeve_seed(tmp_path)
    sf = _category_scenario(tmp_path, months=2)
    code = reb.main(_args(db, config, meta, "--scenario", "g", "--scenarios-file", sf,
                          "--as-of", "2024-01-01", "--sort", "buy"))
    out = capsys.readouterr().out
    assert code == 0
    block = _plan_block(out)
    # --sort reorders funds inside their category; categories keep scenario order.
    assert [ln.split()[0] for ln in block] == ["core", A, "rest", D, B]
    assert block[2].split()[4:6] == ["3,000.00", "1,500.00"]  # Buy€, Monthly€ = Buy€ / 2


def test_main_category_unavailable_recap_is_grouped(tmp_path, capsys):
    from e1f.common import Category, Scenario, save_scenario
    db, config, meta = _sleeve_seed(tmp_path)
    sf = str(tmp_path / "s.yaml")
    save_scenario(Scenario.from_categories(
        "g", (Category("core", 100.0, {A: 100.0}),), frozen=(F,)), sf)
    code = reb.main(_args(db, config, meta, "--scenario", "g", "--scenarios-file", sf,
                          "--as-of", "2024-01-01"))
    out = capsys.readouterr().out
    assert code == 0
    assert f"UNAVAILABLE — unclassified — held fund(s) are neither targeted nor frozen: {B}" in out
    recap = out.splitlines()
    core = next(i for i, ln in enumerate(recap) if ln.startswith("core"))
    assert recap[core].split() == ["core", "100.0%"]
    assert recap[core + 1].startswith(f"  {A}") and recap[core + 1].endswith("100.0%")


def test_categories_reproduce_the_flat_residual_buys():
    # The 2026-10-30 growth-aggressive basket, flat at Σ 95% of the book (ADR-0060 §2),
    # rewritten as categories with exact fractions: core 60/95, regions 16/95
    # (11/16 · 5/16), sectors 19/95 = 20 (7/19 · 6/19 · 3/19 · 3/19). With the residual
    # plan's C_min as the budget, the category sleeve buys exactly what the flat plan did.
    from e1f.common import Category, Scenario, plan_rebalance
    acwi, exusa, europe = "IE0003XJA0J9", "IE0006WW1TQ4", "IE00B4K48X80"
    hc, semis, util, stoxx_hc = "IE00BM67HK77", "IE00BMC38736", "IE00BM67HQ30", "LU0292103222"
    flat = {acwi: 0.60, exusa: 0.11, hc: 0.07, semis: 0.06, europe: 0.05, stoxx_hc: 0.03,
            util: 0.03}
    categories = (
        Category("core", 6000 / 95, {acwi: 100.0}),
        Category("regions", 1600 / 95, {exusa: 1100 / 16, europe: 500 / 16}),
        Category("sectors", 20.0, {hc: 700 / 19, semis: 600 / 19, util: 300 / 19,
                                   stoxx_hc: 300 / 19}),
    )
    values = {acwi: 10000.0, exusa: 1000.0, hc: 800.0, semis: 500.0, europe: 900.0,
              stoxx_hc: 444.0, util: 100.0, F: 1500.0, G: 615.0}
    held = frozenset(values)
    residual = plan_rebalance(flat, values, held, None, None)
    # The frozen €2115 binds the residual plan: V' = 2115 / 0.05 = 42300.
    assert residual.v_prime == pytest.approx(42300.0)
    scenario = Scenario.from_categories("ga", categories, frozen=(F, G))
    sleeve_targets = {isin: pct / 100.0 for isin, pct in scenario.targets.items()}
    sleeve = plan_rebalance(sleeve_targets, values, held, frozenset({F, G}), residual.c_min)
    assert sleeve.buys == pytest.approx(residual.buys, rel=1e-12, abs=1e-9)
    assert sleeve.v_final == pytest.approx(residual.v_prime)
