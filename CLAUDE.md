# e1f

ETF universe config and historical price fetching into SQLite.

## Running checks

```bash
./scripts/check.sh              # all gates: lint, layers, shell, actions, types, dead, test + coverage
./scripts/check.sh lint         # single gate (lint | layers | shell | actions | types | dead | test)
uv run pytest                   # tests only, no coverage floor
```

The script is the single definition of "green". CI runs the same gates via `scripts/check.sh` (`actions` in a separate job; see `.github/workflows/ci.yml`).

## Layout

```
src/e1f/
  cli.py       — entry point; routes top-level commands
  autocomplete.py — autocomplete command: Bash/Zsh shell completion
  config.py    — config subcommand: OpenFIGI resolution, YAML management
  fetch.py     — fetch subcommand: ftgo/yfinance price + FX fetching, SQLite
  validate.py  — validate command: config/DB sync and price-data quality
  transactions.py — transactions subcommand: broker export ingest (Trade Republic CSV, XTB Excel)
  portfolio.py   — portfolio subcommand: holdings from transactions
  performance.py — performance subcommand: EUR valuation, XIRR/TWR/risk metrics
  concentration.py — concentration command: coverage-aware within-fund concentration
  overlap.py   — overlap command: cross-fund single-name exposure floor via canonical identity
  correlation.py — correlation command: return co-movement redundancy + clustering (scipy)
  common.py    — shared primitives: defaults, ETFDefinition, retry logic, FX conversion, position timeline
data/
  etf_universe.yaml      — ETF config (ISINs, names, tickers)
  currency_metadata.yaml — pinned ftgo resolutions incl. FX pairs (ADR-0002, ADR-0010)
  e1f.db                 — SQLite DB: prices, fx_rates, transactions (gitignored)
ADR/           — decision log; one ADR per decision, no gaps in numbering
tests/         — pytest suite; 90% coverage floor enforced
.claude/skills/— project skills (see below)
```

## Key decisions

All recorded in `ADR/`. The most load-bearing:

- `ADR-0001` — ftgo is the default source; yfinance requires `--fallback`
- `ADR-0002` — ftgo resolution is pinned in `data/currency_metadata.yaml`
- `ADR-0003` — module layer contract: `cli → command modules → common`
- `ADR-0004` — broker transaction ingest (Trade Republic CSV)
- `ADR-0005` — portfolio holdings from transactions
- `ADR-0006` — XTB Cash Operations Excel import
- `ADR-0007` — fund metadata (TER, distribution, currency) at config time
- `ADR-0008` — destructive `--replace` series repair with a shrink guard
- `ADR-0009` — `validate` error/warning exit-code contract
- `ADR-0010` — currency + FX foundation: `fx_rates` table, FX auto-fetched into `fetch`, EUR conversion helper
- `ADR-0011` — `performance` command: XIRR-first return metrics, EUR valuation, net-across-brokers holdings
- `ADR-0012` — `concentration` command: coverage-aware within-fund concentration, rank-constrained HHI bounds, `overlap` (v1b) deferred
- `ADR-0013` — `overlap` command: cross-fund single-name exposure floor (`≥`) via reviewed `canonical_key`; valuation core + provenance vocabulary + `overlap_candidates` graduate into `common`
- `ADR-0014` — provenance generalization: `performance` / `portfolio` speak the shared `Status` / `MetricContract` / `--explain` vocabulary, opt-in via `--show-status` / `--explain` (default output unchanged)
- `ADR-0015` — `correlation` command: return co-movement redundancy + hierarchical clustering, pairwise-overlap alignment, EUR returns; scipy dependency

## Skills

- `/doc-check` — audit README, CLAUDE.md, and ADRs for drift, stale claims, and dead links

## Conventions

- One home per fact: the *why* lives in an ADR, code shapes live in code, README describes behaviour without duplicating argparse definitions.
- Before calling a change done: `./scripts/check.sh` must be green.
- Coverage floor is 90%; ratchet up, never down without a recorded reason.
- New modules must satisfy the layer contract in `ADR-0003`.
