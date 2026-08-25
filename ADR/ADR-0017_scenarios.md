# ADR-0017 — `scenario` command (named ISIN:pct baskets, shared by rebalance & correlation)

**Scope:** a new `scenario` subcommand that *only* creates and maintains named
target baskets in one YAML sidecar (`data/scenarios.yaml`), plus a `--scenario
NAME` flag on the two consumers — `rebalance` (ADR-0016) and `correlation`
(ADR-0015) — that recall a saved basket. The shared on-disk shape and its I/O
live in `common` (`Scenario`, `load_scenarios`, `get_scenario`, `save_scenario`,
`delete_scenario`), so the manager and both consumers speak one format without
importing each other — ADR-0003's `cli → command modules → common` contract
unchanged. Because `correlation --scenario` runs on the *post-rebalance*
portfolio, the buy-only plan core (`RebalancePlan`, `compute_rebalance`, the
valuation assembly) also graduates from `rebalance` into `common`.

Slogan: **name a basket once; run it through many lenses.**

## Context

`rebalance` takes its targets as repeated `--target ISIN:PCT` flags. A realistic
allocation is 5–8 funds, so every run is a long, error-prone command line, and
comparing scenarios ("core" vs "aggressive") means retyping the whole set. The
same basket is also a natural input to `correlation` — "how do the funds in the
allocation I am *planning* move together?" — which today can only correlate the
funds already held.

The need is therefore a reusable, named basket that is (a) persisted once and
(b) consumed by more than one command. The consuming commands must not learn to
manage files, and the file format must not be duplicated across them.

## Decision

### 1. A basket is `{ISIN → percent}` plus an optional `months`

A scenario is exactly the payload `rebalance` already validates: ISIN → percent
of the whole book, percents in `(0, 100]`, Σ ≤ 100. It additionally stores an
optional `months` (the DCA horizon `rebalance` accepts). `correlation` reads the
targets and ignores `months`. No other command-specific knobs are stored — a
scenario is a *basket*, not a saved command line.

### 2. One file, many scenarios

All scenarios live in a single `data/scenarios.yaml`, keyed by name:

```yaml
scenarios:
  core:
    months: 10
    targets:
      IE0003XJA0J9: 40.0
      IE00BDBRDM35: 15.0
```

The file is **gitignored** — target allocations are personal, like `e1f.db`.
The path is overridable everywhere (`--file` on `scenario`, `--scenarios-file`
on the consumers).

### 3. The `scenario` command only manages the file

`e1f scenario save NAME --target … [--months N]`, `list`, `show NAME`, `delete
NAME`. It never runs an analysis — recall is the consumers' job. `save` upserts
one entry (preserving the rest) and re-validates dupes and the Σ ≤ 100 bound, so
a stored basket is always a legal `rebalance` input.

### 4. Recall is a flag on each consumer, not dispatch from `scenario`

`rebalance --scenario NAME` and `correlation --scenario NAME` load the basket via
the `common` loader. `scenario` does not import `rebalance`/`correlation`, and
they do not import it — the shared shape lives one layer down in `common`. This
keeps the dependency graph the DAG ADR-0003 mandates; a `scenario run` that
dispatched into another command module would have broken it.

- **rebalance:** `--scenario` is mutually exclusive with `--target`. The stored
  `months` is used unless `--months` is typed on the CLI (CLI wins); `--as-of`
  and the provenance flags apply on top as usual.
- **correlation:** `--scenario` correlates the **post-rebalance portfolio the
  scenario implies**, not the raw basket. It runs the same buy-only plan
  `rebalance` computes (targeted funds reach their targets, untargeted funds are
  diluted), then takes the universe and weights from each fund's **final EUR
  value** (`current + buy`, normalized). This answers "how correlated is the
  portfolio I'd *hold after* this rebalance?" — which is why the residual
  untargeted funds must be included and re-weighted, exactly as rebalance does.
  A fund not yet held is included as long as it has a usable return series (the
  held-value valuation gate is skipped in scenario mode); one without a series is
  still disclosed as `no_history`. If the implied rebalance is infeasible,
  correlation reports UNAVAILABLE and points at `rebalance` for the diagnosis.

  This is what forced the **rebalance compute core** (`RebalancePlan`,
  `compute_rebalance`, the valuation assembly) to graduate from `rebalance` into
  `common` — correlation must run the plan without importing a sibling command
  (ADR-0003). This follows ADR-0013's precedent of graduating the shared
  valuation core; `rebalance` re-exports the names and keeps its own rendering.

## Rationale

- **One home per fact** — the basket format and its I/O live once, in `common`.
  Both consumers and the manager depend on that one shape.
- **Composability over a saved command line** — storing the *basket* (not flags)
  is what lets the same scenario feed a rebalance plan and a correlation report;
  a serialized `rebalance` invocation could not.
- **Layer contract intact** — CRUD in a new command module, shared shape in
  `common`, recall via a flag: no command imports another (ADR-0003).
- **Provenance unchanged** — a scenario only *supplies inputs*. rebalance's and
  correlation's own provenance vocabularies (ADR-0014/0015/0016) describe the
  result exactly as before; nothing about "the targets came from a file" changes
  what the numbers establish.

## Consequences

- New module `e1f.scenario` joins the command layer in the import-linter
  contract (`pyproject.toml`) and the CLI dispatch table.
- `data/scenarios.yaml` is gitignored; a fresh clone has no scenarios until the
  user saves one.
- `correlation.analyze` gains optional `isins` / `weights` overrides; the default
  (held-portfolio, EUR-value-weighted) path is unchanged when neither is passed.
- A scenario's Σ may be < 100 (rebalance's residual bucket) — correlation
  normalizes over the basket regardless, so a shown weight is a share of the
  basket, not of the whole book (consistent with ADR-0015 decision 8).
