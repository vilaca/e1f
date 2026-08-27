# ADR-0024 — Experimental command tier: physical isolation behind a one-way import boundary

**Scope:** move the three commands that "still don't work well" —
`concentration`, `overlap`, and `backtest` — out of the top-level package into a
new `e1f.experimental` subpackage with **its own `common.py`**, and enforce a
**one-way import boundary**: no stable module (nor shared `e1f.common`) may
import anything under `e1f.experimental`. The CLI router is the sole crossing, so
the commands stay reachable. The look-through *fetching* that `fetch` did on
behalf of these commands moves too, into a new experimental `lookthrough`
command, so stable `fetch` no longer depends on experimental machinery.

Slogan: **the experimental tier can depend on the stable core; the stable core
can never depend on the experimental tier — and the linter proves it.**

## Context

`concentration` (ADR-0012/0018), `overlap` (ADR-0013), and `backtest`
(ADR-0019–0023) are the least settled commands: coverage-limited look-through,
an unresolved cross-fund identity model, and a contribution-timing evaluator
whose results are still exploratory. Their code, their shared primitives, and —
critically — the look-through refresh embedded in `fetch` were interleaved with
the stable commands in one flat package under the ADR-0003 `cli → command
modules → common` contract. That contract kept `cli → commands → common`
direction honest but drew no line between *stable* and *experimental*: a stable
command could quietly grow a dependency on experimental code, and `fetch` already
had one (it refreshed look-through snapshots as a side effect of every bulk
price fetch).

We want the experimental work physically separated and its blast radius fenced,
without hiding it from users or freezing its development.

## Decision

### 1. A dedicated `e1f.experimental` subpackage

`backtest.py`, `concentration.py`, `overlap.py` move to
`src/e1f/experimental/`, joined by a new `lookthrough.py` (§3) and an
`experimental/common.py` (§2).

### 2. Its own `common.py`, holding only experimental-only primitives

Primitives used **solely** by the experimental commands graduate out of
`e1f.common` into `e1f.experimental.common`: the look-through snapshot model +
ingest (`HoldingRow`, `LookthroughSnapshot`, `init_lookthrough_schema`,
`insert_lookthrough_snapshot`, `latest_lookthrough_snapshot`, the `DIMENSION_*`
constants, `normalize_security_name`, the `security_alias` helpers,
`overlap_candidates`, `_snapshot_provenance`) and the whole contribution-timing
simulator (`simulate_strategy` and its `DeployMode` / `StrategyParams` /
`BacktestResult` vocabulary, the daily-dip cores, `monthly_fill_indices`,
`blind_schedule`, `deployment_fraction`, `_max_drawdown`, `running_high`).

Primitives shared with a **stable** command stay in `e1f.common`, even when an
experimental command also uses them: `xirr` (used by `performance`),
`load_price_series` / `fund_eur_value` / `pinned_quote_currency` (used by
`correlation` and `portfolio`), and the `Status` / `MetricContract` /
`_explain_metric` provenance vocabulary (used by `performance` / `portfolio`,
ADR-0014). `e1f.experimental.common` imports these freely — the boundary is
one-way (§4).

### 3. Look-through fetching leaves stable `fetch`

`fetch` no longer refreshes look-through: because stable `fetch` may not import
experimental code (§4), it cannot call the ingest that only experimental
commands consume. The refresh logic (yfinance `funds_data` → `HoldingRow`s →
immutable snapshot) becomes a standalone experimental command, `e1f
lookthrough`, run on demand — typically right after a bulk `e1f fetch`. This
trades the old one-command convenience for a clean boundary; `concentration` /
`overlap` read whatever snapshots `lookthrough` last cached, and their
"look-through unavailable" message now points at `e1f lookthrough`.

### 4. The boundary is enforced by import-linter

Three contracts in `pyproject.toml` replace ADR-0003's single one:

1. **Stable layers** — `cli → {stable command modules} → common`, unchanged in
   spirit but with the three experimental modules removed from the command
   layer.
2. **Forbidden** — every stable command module *and* `e1f.common` may not import
   `e1f.experimental` (package and descendants). `e1f.cli` is deliberately
   **absent** from the sources: the router is the one sanctioned crossing, so it
   can register the experimental commands and nothing else can reach them.
3. **Experimental layers** — `{experimental command modules} →
   experimental.common`; the experimental commands do not import each other, and
   `experimental.common` never imports the commands. It may still consume shared
   `e1f.common` (unconstrained by this contract).

### 5. CLI surface — grouped, not hidden

All experimental commands (`lookthrough`, `concentration`, `overlap`,
`backtest`, `seasonality`) remain first-class `e1f <command>`s. `cli.py` splits its registries
into `STABLE_*` and `EXPERIMENTAL_*` maps (merged for dispatch) so the split has
one home, and the top-level `--help` lists them under an **"Experimental
(ADR-0024 — isolated tier; may change or give wrong results)"** heading.

## Rationale

Physical isolation + a linter-proven one-way boundary is stronger than a naming
convention or a runtime flag: the stable core provably cannot regress on
experimental churn, and a future graduation of one of these commands is a
deliberate act (move the module up, delete a line from the forbidden contract),
not an accident. Keeping shared primitives in `e1f.common` — rather than
duplicating them down into the experimental tier — preserves "one home per fact"
(the experimental tier reuses `xirr`, valuation, and the provenance vocabulary
rather than forking them). Extracting look-through into its own command is the
honest consequence of the boundary: the one place stable code depended on
experimental behaviour is now an explicit, user-invoked step.

## Consequences

- `e1f fetch` no longer refreshes look-through; run `e1f lookthrough` after a
  fetch to keep `concentration` / `overlap` data current. Stable `fetch` also no
  longer creates the look-through schema — `insert_lookthrough_snapshot` creates
  it on first write (it always did), so `lookthrough` is self-sufficient.
- Graduating an experimental command to stable = move its module to
  `src/e1f/`, move any now-shared primitives from `experimental/common.py` into
  the matching `src/e1f/common/` module (ADR-0025), and drop it from the
  forbidden contract's sources.
- The three import-linter contracts are the enforcement; `./scripts/check.sh
  layers` fails if any stable module (or `common`) grows an
  `e1f.experimental` import.
- Supersedes ADR-0003's single flat layer contract for the split between tiers;
  ADR-0003's `cli → commands → common` direction still governs within each tier.

## Deferred (not in this ADR)

- Re-attaching look-through refresh to `fetch` via an inversion (a post-fetch
  hook the router wires up) to restore one-command UX without a stable→
  experimental import — rejected here as reintroducing runtime coupling that
  softens the isolation; revisit only if the extra command proves burdensome.
- Any change to what the experimental commands *compute* — this ADR only
  relocates code and draws the boundary.
