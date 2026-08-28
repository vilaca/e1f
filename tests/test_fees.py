"""Direct contracts for the shared TER and annual-fee arithmetic."""

import pytest

from e1f.common.fees import weighted_ter_cost


def test_weighted_ter_cost_is_market_value_weighted() -> None:
    assert weighted_ter_cost([(0.5, 3000.0), (0.1, 1000.0)]) == pytest.approx(
        (0.4, 16.0)
    )


def test_weighted_ter_cost_missing_ter_dilutes_but_unvaluable_rows_do_not() -> None:
    assert weighted_ter_cost(
        [
            (0.5, 3000.0),
            (None, 1000.0),
            (0.9, None),
            (0.9, 0.0),
            (0.9, -1.0),
        ]
    ) == pytest.approx((0.375, 15.0))


def test_weighted_ter_cost_requires_at_least_one_valuable_covered_holding() -> None:
    assert weighted_ter_cost([(None, 1000.0), (0.5, None)]) == (None, None)
    assert weighted_ter_cost([]) == (None, None)
