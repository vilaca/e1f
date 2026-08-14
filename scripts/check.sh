#!/usr/bin/env bash
# The single definition of "green" for e1f. CI runs this exact script (see
# .github/workflows/ci.yml), so local and CI cannot drift. Tool versions are
# pinned by uv.lock and run via `uv run`, so there is no second place to keep
# in sync.
#
# Usage:
#   scripts/check.sh                # run every gate
#   scripts/check.sh lint           # run only named gate(s): lint | layers | types | test
#
# Deliberately NOT `set -e`: every gate runs even after one fails, so a single
# invocation reports all problems and names each failing gate.
set -uo pipefail

cd "$(dirname "$0")/.."

# Coverage floor. Ratchet up as coverage grows; never down without a recorded
# reason. Current coverage is ~94%.
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
gate_types()  { uv run mypy src; }
gate_test() {
    uv run pytest -q \
        --cov=e1f --cov-report=term-missing \
        --cov-fail-under="$COVERAGE_FLOOR"
}

# Default to all gates; otherwise run only those named on the command line.
gates=("$@")
if [ ${#gates[@]} -eq 0 ]; then
    gates=(lint layers types test)
fi

for gate in "${gates[@]}"; do
    case "$gate" in
        lint)   run_gate lint   gate_lint ;;
        layers) run_gate layers gate_layers ;;
        types)  run_gate types  gate_types ;;
        test)   run_gate test   gate_test ;;
        *) echo "unknown gate: $gate (choose from: lint layers types test)" >&2; exit 2 ;;
    esac
done

if [ ${#failed[@]} -ne 0 ]; then
    echo "FAILED gates: ${failed[*]}"
    exit 1
fi
echo "All gates passed."
