#!/usr/bin/env bash
# The single definition of "green" for e1f. CI runs this exact script (see
# .github/workflows/ci.yml), so local and CI cannot drift. Tool versions are
# pinned by uv.lock and run via `uv run`, so there is no second place to keep
# in sync.
#
# Usage:
#   scripts/check.sh                # run every gate
#   scripts/check.sh lint           # named gate(s): lint | layers | shell | actions | types | dead | package | mutation | test
#
# Deliberately NOT `set -e`: every gate runs even after one fails, so a single
# invocation reports all problems and names each failing gate.
set -uo pipefail

cd "$(dirname "$0")/.." || exit 1

# Coverage floor. Ratchet up as coverage grows; never down without a recorded
# reason. Current coverage is ~93%.
COVERAGE_FLOOR=90

failed=()

run_gate() {
    local name="$1"
    shift
    echo "=== $name ==="
    if "$@"; then
        echo "--- $name ok"
    else
        echo "--- $name FAILED"
        failed+=("$name")
    fi
    echo
}

gate_lint()   { uv run ruff check; }
gate_layers() { uv run lint-imports; }
gate_shell()  {
    if ! command -v shellcheck &>/dev/null; then
        echo "shellcheck not found — skipping (install with: brew install shellcheck)"
        return 0
    fi
    git ls-files '*.sh' | xargs shellcheck --severity=warning
}
gate_actions() {
    if ! command -v actionlint &>/dev/null; then
        echo "actionlint not found — skipping (install with: brew install actionlint)"
        return 0
    fi
    actionlint
}
gate_types()  { uv run mypy src; }
gate_dead()   { uv run vulture src/e1f --min-confidence 80; }
gate_package() { uv run python scripts/package_smoke.py; }
gate_mutation() {
    uv run mutmut run --max-children 4 \
        'e1f.common.rebalance.x_compute_rebalance*' \
        'e1f.common.scenarios.x__validate_scenario*' \
        'e1f.transactions.x__parse_float*'
    uv run python scripts/mutation_score.py
}
gate_test() {
    uv run pytest -q \
        --cov=e1f --cov-report=term-missing \
        --cov-fail-under="$COVERAGE_FLOOR"
}

# Default to all gates; otherwise run only those named on the command line.
gates=("$@")
if [ ${#gates[@]} -eq 0 ]; then
    gates=(lint layers shell actions types dead package mutation test)
fi

for gate in "${gates[@]}"; do
    case "$gate" in
        lint)    run_gate lint    gate_lint ;;
        layers)  run_gate layers  gate_layers ;;
        shell)   run_gate shell   gate_shell ;;
        actions) run_gate actions gate_actions ;;
        types)   run_gate types   gate_types ;;
        dead)    run_gate dead    gate_dead ;;
        package) run_gate package gate_package ;;
        mutation) run_gate mutation gate_mutation ;;
        test)    run_gate test    gate_test ;;
        *) echo "unknown gate: $gate (choose from: lint layers shell actions types dead package mutation test)" >&2; exit 2 ;;
    esac
done

if [ ${#failed[@]} -ne 0 ]; then
    echo "FAILED gates: ${failed[*]}"
    exit 1
fi
echo "All gates passed."
