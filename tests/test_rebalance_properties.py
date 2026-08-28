"""Property checks for the buy-only rebalance financial invariants."""

import math

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from e1f.common import compute_rebalance, post_rebalance_weights


@st.composite
def feasible_rebalance_cases(
    draw: st.DrawFn,
) -> tuple[dict[str, float], dict[str, float], frozenset[str]]:
    target_count = draw(st.integers(min_value=1, max_value=5))
    target_isins = [f"TARGET-{index}" for index in range(target_count)]
    raw_targets = draw(
        st.lists(
            st.floats(
                min_value=0.01,
                max_value=100.0,
                allow_nan=False,
                allow_infinity=False,
            ),
            min_size=target_count,
            max_size=target_count,
        )
    )
    target_total = draw(
        st.floats(
            min_value=0.01,
            max_value=0.95,
            allow_nan=False,
            allow_infinity=False,
        )
    )
    raw_total = sum(raw_targets)
    targets = {
        isin: raw * target_total / raw_total
        for isin, raw in zip(target_isins, raw_targets, strict=True)
    }

    values_list = draw(
        st.lists(
            st.floats(
                min_value=0.01,
                max_value=1_000_000.0,
                allow_nan=False,
                allow_infinity=False,
            ),
            min_size=target_count + 1,
            max_size=target_count + 1,
        )
    )
    all_isins = [*target_isins, "RESIDUAL"]
    values = dict(zip(all_isins, values_list, strict=True))
    return targets, values, frozenset(all_isins)


@given(feasible_rebalance_cases())
@settings(max_examples=200, deadline=None)
def test_rebalance_preserves_financial_invariants(case):
    targets, values, held = case
    plan = compute_rebalance(targets, values, held)

    assert plan.feasible
    assert math.isfinite(plan.c_min)
    assert plan.c_min >= -1e-9
    assert all(math.isfinite(buy) and buy >= -1e-9 for buy in plan.buys.values())
    assert sum(plan.buys.values()) == pytest.approx(plan.c_min, rel=1e-9, abs=1e-6)

    final_values = post_rebalance_weights(plan, values)
    final_total = sum(final_values.values())
    assert final_total == pytest.approx(plan.v_prime, rel=1e-9, abs=1e-6)
    assert sum(value / final_total for value in final_values.values()) == pytest.approx(1.0)
    for isin, target in targets.items():
        assert final_values[isin] / final_total == pytest.approx(target, rel=1e-9, abs=1e-9)
