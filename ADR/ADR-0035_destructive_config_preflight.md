# ADR-0035 — destructive config commands protect live holdings

**Scope:** `config remove` and `config trim` operations that would remove an
ISIN from config, price data, or pinned currency metadata while stored
transactions still represent a net-positive position.

## Context

Valuation requires all three stores to agree: ETF config, price data, and
currency metadata. Removing an ISIN from any of them can make a live position
unvaluable even though its transaction history remains. `config remove`
already refused this by default, but `config trim` could perform the same
destructive transition when the stores were out of sync.

## Decisions

1. Both commands compute the complete candidate-removal set before mutating
   any store and intersect it with the net-positive positions returned by the
   shared holdings logic.
2. If any candidate is live, the whole command is refused. There is no partial
   batch success.
3. `--force` / `-f` is the explicit override for both commands. It removes
   valuation data while retaining transaction history and prints that
   consequence.
4. Existing cross-store rollback remains the failure policy after preflight
   succeeds.

## Consequences

Routine cleanup cannot silently strand live holdings. Operators can still
repair deliberately inconsistent stores, but must acknowledge the valuation
loss with `--force`. Tests pin refusal, whole-batch atomicity, the force path,
closed positions, and retained transaction history.
