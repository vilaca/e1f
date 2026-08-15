---
name: doc-check
description: >
  Audit e1f's README and ADRs for drift, redundancy, and convention breaks
  against the actual implementation. Enforces "one home per fact": finds prose
  that restates a code shape instead of linking it, claims whose referenced code
  has changed or gone, stale Python version / flag / command references,
  duplicated facts, dead internal links, and ADR convention violations.
  Use when the user says "check the docs", "are the docs stale", "doc-check",
  "audit ADRs", "docs consistency", or before a release. Reports findings; only
  edits when asked.
allowed-tools: Bash, Read, Grep, Glob, Edit
---

# e1f doc-check

e1f keeps **one home per fact**: the *why* lives in an ADR, a *code shape* lives
in code (docs link to it, never copy it), and README prose describes behaviour
without duplicating argparse definitions or module internals. This skill finds
drift, redundancy, and convention breaks.

Report findings grouped by section, each as `file:line — what's wrong — the fix`.
Do **not** edit unless the user asks; if they do, fix and re-run `scripts/check.sh`.

## 0. Scope

Targets: `README.md`, `ADR/*.md`, and `.claude/skills/*/SKILL.md`.

Read `README.md` and all existing ADRs first to calibrate the current state
before checking anything.

## 1. Prose that restates a code shape (should link instead)

The rule: CLI subcommands, flags, argparse defaults, SQLite schema columns, config
YAML keys, and module/class names live in **code**; docs reference the source
location, never copy the shape into prose. Find violations:

- **CLI commands and flags** — compare `README.md` command examples against the
  actual `argparse` definitions in `src/e1f/config.py` (config subcommands),
  `src/e1f/fetch.py` (fetch flags), and `src/e1f/transactions.py`
  (`list`, `trade-republic`, `--config`, `--db`). A README that re-enumerates
  subcommand names or default values is a candidate for drift.
- **SQLite schema** — the `prices` table columns live in
  `DataExtractor._init_database`; the `transactions` table columns live in
  `TradeRepublicImporter._init_database`; both are frozen in
  `tests/test_contracts.py`. Any prose that lists column names should link to
  the source, not restate them.
- **Config YAML structure** — the expected YAML shape (`etfs:` key, per-ETF fields)
  lives in `ConfigManager` and `ETFDefinition.from_config`. Doc prose listing
  these keys is suspect.
- **Module / class names** — verify any module, class, or function name mentioned
  in docs still exists at the stated path.

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
  `data/currency_metadata.yaml`; verify these match `common.py` `DEFAULT_*`
  constants.
- **Flag names** — grep each flag in the argparse definitions. At minimum:
  `config`: `--config`, `--db`, `--currency-meta`; `fetch`: `--config`, `--db`,
  `--start`, `--force`, `--fallback`, `--currency-meta`; `transactions`:
  `--db`, `--config` (on `trade-republic` only).
- **Subcommand list** — `config`: `add`, `list`, `update`, `remove`, `trim`,
  `validate` (in `config.py`); `transactions`: `list`, `trade-republic`
  (in `transactions.py`).
- **Backtick file paths** — confirm each exists.

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
Verify every named file path, command, and flag still resolves and matches the
actual codebase. A stale pointer in a skill breaks the next audit.

## 7. Report

Group by the sections above. Lead with stale claims and dead links (correctness),
then restated code shapes (drift risk), then redundancy and convention. End with a
one-line count: `N findings: X stale, Y drift, Z dead links, W convention`.
If asked to fix, prefer "replace restated shape with a link to the source" over
rewording, and re-run `scripts/check.sh` if any source files changed.

## 8. Regression corpus (run when the prompt or model changes)

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
