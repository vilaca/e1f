---
name: doc-check
description: >
  Audit e1f's README, CLAUDE.md, ADRs, and the metric glossary for drift, redundancy,
  and convention breaks against the actual implementation. Enforces "one home per fact":
  finds prose that restates a code shape instead of linking it, claims whose referenced
  code has changed or gone, stale Python version / flag / command references,
  duplicated facts, dead internal links, ADR convention violations, and glossary
  entries that drift from the metrics the stable commands actually emit.
  Use when the user says "check the docs", "are the docs stale", "doc-check",
  "audit ADRs", "docs consistency", or before a release. Reports findings; only
  edits when asked.
allowed-tools: Bash, Read, Grep, Glob, Edit
---

# e1f doc-check

e1f keeps **one home per fact**: the *why* lives in an ADR, a *code shape* lives
in code (docs link to it, never copy it), and README / CLAUDE.md prose describes
behaviour without duplicating argparse definitions or module internals. This skill
finds drift, redundancy, and convention breaks.

Report findings grouped by section, each as `file:line — what's wrong — the fix`.
Do **not** edit unless the user asks; if they do, fix and re-run `scripts/check.sh`.

## 0. Scope

Targets: `README.md`, `CLAUDE.md`, `ADR/*.md`, `.claude/skills/*/SKILL.md`, and
`data/glossary.md` (the checked-in metric glossary read by `e1f glossary`; ADR-0034).

**Not in scope:** `scripts/*` comments (e.g. coverage footnotes in `check.sh`) —
those are maintainer notes, not user-facing docs. Flag them only if the user asks.

Read **`README.md`**, **`CLAUDE.md`**, **all `ADR/*.md`**, and **`data/glossary.md`**
first to calibrate the current state before checking anything.

## 1. Prose that restates a code shape (should link instead)

The rule: CLI subcommands, flags, argparse defaults, SQLite schema columns, config
YAML keys, and module/class names live in **code**; docs reference the source
location, never copy the shape into prose. Find violations:

- **CLI commands and flags** — compare `README.md` command examples against the
  actual `argparse` definitions in `src/e1f/config.py` (config subcommands),
  `src/e1f/fetch.py` (fetch flags), `src/e1f/transactions.py`
  (`list`, `trade-republic`, `tr`, `xtb`, `--config`, `--db`), `src/e1f/portfolio.py`
  (`--db`, `--config`), `src/e1f/performance.py`
  (`--db`, `--config`, `--currency-meta`, `--as-of`, `--sort`, `--reverse`), and
  `src/e1f/autocomplete.py` (shell completion). Top-level
  commands are wired in `src/e1f/cli.py`
  (`COMMANDS`; frozen in `tests/test_contracts.py`). A README that re-enumerates
  subcommand names or default values is a candidate for drift.
- **SQLite schema** — the `prices` and `fx_rates` table columns live in
  `DataExtractor._init_database`; the `transactions` table columns live in
  `init_transactions_database()` in `src/e1f/transactions.py`; all are frozen in
  `tests/test_contracts.py`. Any prose that lists column names should link to
  the source, not restate them.
- **Config YAML structure** — the expected YAML shape (`etfs:` key, per-ETF fields)
  lives in `ConfigManager` and `ETFDefinition.from_config`. Doc prose listing
  these keys is suspect.
- **Module / class names** — verify any module, class, or function name mentioned
  in docs still exists at the stated path.
- **`CLAUDE.md` layout blurbs** — one-line module descriptions must describe
  current behaviour, not the first shipped version (link to ADRs for history).

For each violation, confirm the code shape via `grep`/`Read`, then flag the prose
with the source file to link to instead.

## 2. Stale claims (code the prose describes has changed or gone)

Extract every concrete code reference from the docs — command names, flag names,
file paths in backticks, default values, Python version strings, ISIN examples —
and verify each still matches the code:

- **Python version** — `README.md` "Requires Python X.Y+" must match
  `pyproject.toml` `requires-python`. (Also caught deterministically by
  `tests/test_contracts.py`; flag here for completeness.)
- **Default paths** — README mentions `data/etf_universe.yaml`, `data/e1f.db`,
  `data/currency_metadata.yaml`; verify these match `common/defaults.py` `DEFAULT_*`
  constants.
- **Flag names** — grep each flag in the argparse definitions. At minimum:
  `config`: `--config`, `--db`, `--currency-meta`; `fetch`: `--config`, `--db`,
  `--start`, `--force`, `--fallback`, `--currency-meta`; `transactions`:
  `--db`, `--config` (on `trade-republic` and `xtb`); `portfolio`: `--db`, `--config`;
  `performance`: `--db`, `--config`, `--currency-meta`, `--as-of`, `--sort`, `--reverse`.
- **Command list** — top-level (`cli.py` `COMMANDS`; frozen in
  `tests/test_contracts.py`): stable `autocomplete`, `config`, `fetch`,
  `validate`, `transactions`, `portfolio`, `performance`, `benchmark`,
  `deposits`, `correlation`, `rebalance`, `scenario`, `glossary`; experimental
  `lookthrough`, `concentration`, `overlap`, `backtest`, `seasonality`. Nested:
  `config`: `add`, `list`, `update`, `remove`,
  `trim` (in `config.py`); `transactions`: `list`, `trade-republic`, `tr`,
  `xtb` (in `transactions.py`); `scenario`: `save`, `list`, `show`, `delete`
  (in `scenario.py`); `overlap`: default report, `candidates`, `resolve`
  (in `src/e1f/experimental/overlap.py`). `autocomplete`, `validate`,
  `portfolio`, `performance`, `benchmark`, `deposits`, `correlation`,
  `rebalance`, `glossary`, `lookthrough`, `concentration`, `backtest`, and
  `seasonality` have no nested subcommands.
- **Backtick file paths** — confirm each exists.
- **`CLAUDE.md` check gates (mandatory)** — derive the canonical gate set from
  `scripts/check.sh`: the `gates=(…)` default when invoked with no arguments
  (currently `lint`, `layers`, `shell`, `actions`, `types`, `dead`, `package`,
  `mutation`, `test`) and
  the gate names in the usage comment. Then verify:
  1. Every gate appears in `CLAUDE.md` Running checks examples/comments.
  2. `CLAUDE.md` does not list gates that `check.sh` no longer defines.
  3. The CI claim matches `.github/workflows/ci.yml`: every default gate runs
     in CI (splitting `actions` into a separate job is OK; omitting a gate is not).
- **`CLAUDE.md` module blurbs** — Layout one-liners must match current modules
  (e.g. `transactions.py` = Trade Republic CSV **and** XTB Excel after ADR-0006).

Flag anything named in prose that no longer resolves or no longer matches.

## 3. ADR conventions

Read all existing `ADR/*.md` files to learn the template, then check each one:

- Opens with `# ADR-NNNN — Title` (H1, em-dash, no "Status:" field).
- Has a `**Scope:**` line immediately after the title.
- Sections: `## Context`, `## Decision`, `## Rationale`, `## Consequences`.
- Filenames are `ADR-NNNN_Title.md`, sequentially numbered with no gaps; flag
  gaps, duplicate numbers, or filenames that don't match the H1 title.
- One decision per ADR.
- Timing ("when will this happen") is not restated in ADRs — flag any milestone
  dates copied in.

### Financial timing/fill conventions

Milestone timing above is distinct from ADR-governed financial timing. For the
registered conventions below, compare all three sources: the ADR's Decision text,
the named implementation, and the pinned regression test. Flag a missing pin or
any disagreement about which date/fill performs an action. Do not require README
or CLAUDE.md to repeat fill mechanics; high-level prose that links the ADR is
correct under "one home per fact."

- `monthly_fill_indices`: ADR-0019 / ADR-0026;
  `src/e1f/experimental/common.py::monthly_fill_indices`;
  `tests/test_backtest.py::test_monthly_fill_indices_one_per_month_on_or_after`.
- `sit-out-month`: ADR-0026 / ADR-0028;
  `src/e1f/experimental/seasonality.py::simulate_seasonal`;
  `tests/test_seasonality.py::test_sit_out_sells_at_selected_fill_and_reenters_at_next_fill`.
- `avoid-month` redeployment: ADR-0026 / ADR-0028;
  `src/e1f/experimental/seasonality.py::simulate_seasonal`;
  `tests/test_seasonality.py::test_shift_september_matches_avoid_august`.

The pinned test must use literal dates and hand-computed expected fill indices,
terminal wealth, or equivalent numeric outcomes. A property/invariance test may
supplement that pin but cannot replace it when the convention selects a date.

## 4. Redundancy / second source of truth

Flag the same fact asserted in two places:
- A decision recorded outside the ADR log (e.g. in README prose).
- A rationale stated in README that belongs in an ADR and should be linked.
- Two ADRs that describe the same decision.

## 5. Dead internal links

Extract every relative markdown link and confirm the target exists:
- Links to `ADR/*.md` files.
- Links to source files (`src/e1f/*.py`).
- Any `](scripts/…)` or `](.github/…)` references.

## 6. Agent instructions (skills)

Each `.claude/skills/*/SKILL.md` references files, scripts, and conventions.
Verify every named file path, command, flag, and **scope target** still resolves
and matches the actual codebase. A stale pointer in a skill breaks the next audit.

## 7. Metric glossary (`data/glossary.md`)

`data/glossary.md` is the single source for `e1f glossary` — parsed, never
duplicated in code (ADR-0034). `## ` headings are groups, `### ` headings are
terms; everything else (title, group intros, the `## Metric families` /
`## What isn't measured` sections) is orientation prose the parser ignores.
`src/e1f/glossary.py` parses it; `tests/test_glossary.py` pins the core terms and
the structural contract. Check:

- **`**Where:**` references resolve** — each term's `**Where:**` line names the
  command(s) and flag(s) that emit the metric. Verify each command is in `cli.py`
  `COMMANDS` and each flag (`--metrics`, `--series`, `--contrib`, `--diff`,
  `--as-of`, …) exists in that command's argparse. A term pointing at a flag that
  was renamed or removed is stale.
- **Scope discipline (ADR-0034)** — the glossary is scoped to *stable-command*
  metrics: `performance`, `portfolio`, `deposits`, `benchmark`, `correlation`.
  Flag any `**Where:**` line naming an experimental command (`concentration`,
  `overlap`, `backtest`, `seasonality`, `lookthrough`) or a rebalance plan column
  — those are out of scope and belong in an ADR/README note, not here.
- **Coverage drift** — a metric column newly emitted by a stable command's output
  with no `### ` term, or a `### ` term for a metric the code no longer prints, is
  drift. Spot-check the table/`--metrics`/`--series`/`--contrib` headers in
  `performance.py`, `portfolio.py`, `deposits.py`, `benchmark.py`, `correlation.py`
  against the term list.
- **Structural contract** — every term has `**Useful for:**` and `**Read with:**`
  (pinned by `tests/test_glossary.py`; flag here only for completeness). Terms in
  `**Read with:**` should name real metrics elsewhere in the file.
- **No second source** — README / CLAUDE.md must not re-state a metric's
  definition; they describe *that* the glossary exists and link to it, per
  "one home per fact".

## 8. Report

Group by the sections above. Lead with stale claims and dead links (correctness),
then restated code shapes (drift risk), then redundancy and convention. End with a
one-line count: `N findings: X stale, Y drift, Z dead links, W convention`.
If asked to fix, prefer "replace restated shape with a link to the source" over
rewording, and re-run `scripts/check.sh` if any source files changed.

## 9. Regression corpus (run when the prompt or model changes)

`.claude/skills/doc-check/corpus/cases.yaml` holds cases that pin expected
verdicts. Run before committing a new version of this skill or switching models.

For each case:
1. Read the `finding:` description — it describes the doc state under test.
2. Apply the judgment: would this scenario, in the context of the current repo,
   be flagged by doc-check?
3. Verify the verdict matches (`VIOLATED` or `HOLDS`).

A `HOLDS` mismatch (judge flags something that should pass) is a false-positive
failure — the prompt is too eager. A `VIOLATED` mismatch (judge misses a real
bug) is a false-negative failure — the prompt is too lenient. Both fail the
corpus. Record divergences as calibration issues and update this skill or the
corpus accordingly.
