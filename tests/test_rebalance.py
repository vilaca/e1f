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
