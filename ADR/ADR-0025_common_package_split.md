# ADR-0025 — Split `e1f.common` from one module into a package

**Scope:** the shared-primitives layer (ADR-0003). No command behaviour, CLI
surface, or experimental-tier boundary (ADR-0024) changes.

Slogan: **one concern per module; commands still import `e1f.common`.**

## Context

`src/e1f/common.py` started as defaults + `ETFDefinition` + OpenFIGI + the YAML
universe manager. Every later graduation of a cross-command primitive landed in
the same file: retry, fund-metadata enrichment, trades/positions, FX, EUR
valuation, provenance (`Status` / `MetricContract`), scenarios, the buy-only
rebalance core, XIRR. By ADR-0024 it was ~1,350 lines covering six unrelated
jobs, and it defined `_SHARE_EPSILON` twice.

ADR-0003 still holds: command modules must not import each other, so anything
used by more than one command lives in `e1f.common`. That constraint does not
require the layer to be a single file.

## Decision

Replace `src/e1f/common.py` with a package `src/e1f/common/`:

| Module | Holds |
|---|---|
| `defaults.py` | `DEFAULT_*` paths, `BASE_CURRENCY`, exchange/currency constants |
| `retry.py` | `call_with_retry` |
| `universe.py` | name parsers, OpenFIGI, `ConfigManager`, fund-metadata enrichment |
| `holdings.py` | trades, position timeline, FX, point-in-time EUR valuation |
| `metrics.py` | `Status` / `MetricContract` / `--explain` helpers, XIRR |
| `scenarios.py` | named ISIN:pct basket I/O (ADR-0017) |
| `rebalance.py` | buy-only plan math + valuation assembly (ADR-0016/0017) |

`__init__.py` re-exports the names command modules already import, so
`from e1f.common import …` stays the documented path. Command modules do not
need to know the internal layout.

Intra-package imports are allowed (`holdings` → `defaults`, `rebalance` →
`holdings`, …) and must stay a DAG. The layer still imports nothing from
command modules. The import-linter contract continues to name the layer
`e1f.common` (the package and its descendants).

`e1f.experimental.common` is unchanged (ADR-0024).

## Rationale

The file's own section banners were already the seams: each block graduated
from a different ADR and is consumed by a different pair of commands. Splitting
along those banners makes a primitive's home greppable without weakening
ADR-0003. A facade `__init__.py` keeps the public import path stable so this is
a layout change, not an API change.

## Consequences

- Default-path resolution in `defaults.py` uses `Path(__file__).parents[3]`
  (one extra parent versus the old `common.py`).
- Tests that monkeypatch helpers must target the submodule that looks the name
  up (e.g. `e1f.common.retry.time.sleep`, `e1f.common.universe._ftgo_load`).
- ADR-0003's layer name is unchanged; its "imports nothing internal" clause
  now means "nothing from command modules", not "the layer is one file".
- Historical ADRs that say "`common.py` gains …" stay as written; new work
  lands in the matching package module.

## Deferred (not in this ADR)

- Splitting `e1f.experimental.common` the same way.
- Teaching command modules to import from submodules instead of the facade.
