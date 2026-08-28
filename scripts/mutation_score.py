"""Enforce the targeted mutmut score used by the mutation quality gate."""

from __future__ import annotations

import subprocess
import sys

TARGETS = (
    "e1f.common.fees.x_weighted_ter_cost__mutmut_",
    "e1f.common.rebalance.x_compute_rebalance__mutmut_",
    "e1f.common.scenarios.x__validate_scenario__mutmut_",
    "e1f.experimental.seasonality.x__shift_schedule_refusal__mutmut_",
    "e1f.glossary.x_find_terms__mutmut_",
    "e1f.transactions.x__parse_float__mutmut_",
)
MINIMUM_SCORE = 70.5
TERMINAL_STATES = {"killed", "survived", "timeout", "suspicious"}


def main() -> int:
    output = subprocess.run(
        ["mutmut", "results", "--all", "true"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    states: list[str] = []
    for line in output.splitlines():
        name, separator, state = line.strip().partition(": ")
        if separator and name.startswith(TARGETS):
            states.append(state)

    unfinished = sorted(set(states) - TERMINAL_STATES)
    if not states or unfinished:
        detail = "no targeted mutants found" if not states else f"unfinished states: {unfinished}"
        print(f"mutation score unavailable: {detail}", file=sys.stderr)
        return 1

    killed = states.count("killed")
    score = killed / len(states) * 100.0
    print(f"Targeted mutation score: {score:.1f}% ({killed}/{len(states)} killed)")
    if score < MINIMUM_SCORE:
        print(f"minimum required mutation score is {MINIMUM_SCORE:.1f}%", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
