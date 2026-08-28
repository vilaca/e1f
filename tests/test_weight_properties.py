"""Property checks for the shared experimental NAV-weight boundary."""

import math

from hypothesis import given
from hypothesis import strategies as st

from e1f.experimental import concentration, overlap
from e1f.experimental.common import (
    NAV_WEIGHT_UPPER_TOLERANCE,
    NavWeightIssue,
    nav_weight_issue,
)


@given(st.floats(allow_nan=True, allow_infinity=True, width=64))
def test_nav_weight_policy_and_command_wrappers_agree(weight: float) -> None:
    issue = nav_weight_issue(weight)
    if not math.isfinite(weight):
        assert issue is NavWeightIssue.NON_FINITE
    elif weight < 0.0:
        assert issue is NavWeightIssue.NEGATIVE
    elif weight > 1.0 + NAV_WEIGHT_UPPER_TOLERANCE:
        assert issue is NavWeightIssue.ABOVE_NAV
    else:
        assert issue is None

    assert (overlap._weight_issue(weight) is None) is (issue is None)
    assert (concentration.security_issue([weight]) is None) is (issue is None)


def test_nav_weight_policy_pins_exact_boundaries() -> None:
    assert nav_weight_issue(-1e-12) is NavWeightIssue.NEGATIVE
    assert nav_weight_issue(0.0) is None
    assert nav_weight_issue(1.0 + NAV_WEIGHT_UPPER_TOLERANCE) is None
    assert nav_weight_issue(1.0 + NAV_WEIGHT_UPPER_TOLERANCE + 1e-12) is (
        NavWeightIssue.ABOVE_NAV
    )
