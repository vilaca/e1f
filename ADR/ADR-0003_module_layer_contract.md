# ADR-0003 — Module layer contract: cli → command modules → common

**Scope:** codebase architecture, import-linter enforcement

## Context

As e1f grows, additional command modules (analysis, reporting, API, …) may be
added alongside the existing `config`, `fetch`, `validate`, `transactions`, and
`portfolio` commands. Without a recorded and enforced layer contract, modules will accumulate
cross-dependencies that make the codebase hard to test, extend, or reason about
in isolation.

## Decision

The module graph is stratified into three layers, enforced by import-linter
(`[tool.importlinter]` in `pyproject.toml`, `layers` gate in `scripts/check.sh`):

```
e1f.cli
  ↓
stable command modules:
  autocomplete, benchmark, config, correlation, deposits, fetch, funds,
  glossary, performance, portfolio, rebalance, scenario, transactions, validate
  ↓
e1f.common
```

The CLI entry point may import every lower layer. Stable commands may import
`e1f.common`, but never one another. `e1f.common` submodules may import one
another, but never command modules.

New modules are placed at the lowest layer that satisfies their needs. A module
that only uses primitives goes in `common` or alongside it. A module that
orchestrates multiple commands or adds a new top-level subcommand goes at the
`cli` layer or between `cli` and the existing commands.

## Rationale

- **Testability** — lower layers have no internal dependencies; they can be
  tested without instantiating the full CLI stack.
- **Future isolation** — as further provider modules are added (analysis,
  reporting, …), the contract prevents them from importing each other, keeping
  the dependency graph a DAG.
- **Enforcement over convention** — the contract is machine-checked on every
  `./scripts/check.sh` run; it cannot silently erode.

## Consequences

- Adding a new module that violates the contract fails the `layers` gate.
- Cross-command helpers (code used by more than one command module) must live in
  `e1f.common`, not in any command module. The layer is a package (ADR-0025);
  command modules keep importing from `e1f.common`.
- This ADR must be updated when a new layer or module is added.
- Experimental command modules live under `e1f.experimental` behind a separate
  one-way boundary (ADR-0024); they are not on this command layer. The live
  import-linter list is `[tool.importlinter]` in `pyproject.toml`.
