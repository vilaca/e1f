# ADR-0003 — Module layer contract: cli → config/fetch/transactions → common

**Scope:** codebase architecture, import-linter enforcement

## Context

As e1f grows into a full ETF analysis and portfolio management tool, new modules
(analysis, portfolio, reporting, API, …) will be added. Without a recorded and
enforced layer contract, modules will accumulate cross-dependencies that make
the codebase hard to test, extend, or reason about in isolation.

## Decision

The module graph is stratified into three layers, enforced by import-linter
(`[tool.importlinter]` in `pyproject.toml`, `layers` gate in `scripts/check.sh`):

```
e1f.cli                              ← entry point; may import any layer below
e1f.config  |  e1f.fetch  |  e1f.transactions  ← commands; may import common, never each other
e1f.common                           ← shared primitives; imports nothing internal
```

New modules are placed at the lowest layer that satisfies their needs. A module
that only uses primitives goes in `common` or alongside it. A module that
orchestrates multiple commands or adds a new top-level subcommand goes at the
`cli` layer or between `cli` and the existing commands.

## Rationale

- **Testability** — lower layers have no internal dependencies; they can be
  tested without instantiating the full CLI stack.
- **Future isolation** — as provider modules are added (analysis, portfolio),
  the contract prevents them from importing each other, keeping the dependency
  graph a DAG.
- **Enforcement over convention** — the contract is machine-checked on every
  `./scripts/check.sh` run; it cannot silently erode.

## Consequences

- Adding a new module that violates the contract fails the `layers` gate.
- Cross-command helpers (code used by more than one command module) must live in
  `common`, not in any command module.
- This ADR must be updated when a new layer or module is added.
