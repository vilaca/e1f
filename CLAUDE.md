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
  correlation.py — correlation command: return co-movement redundancy + clustering (scipy)
  rebalance.py — rebalance command: minimum-cash buy-only target rebalance + N-month DCA plan
  scenario.py  — scenario command: CRUD for named ISIN:pct baskets (one YAML, many scenarios)
  common.py    — shared primitives: defaults, ETFDefinition, retry logic, FX conversion, position timeline, rebalance plan core
  experimental/ — isolated experimental tier (ADR-0024); no stable module imports it (cli routes it)
    common.py      — experimental-only primitives: look-through model + ingest, overlap-candidate signal, backtest simulator
    lookthrough.py — lookthrough command: refresh cached yfinance look-through snapshots (was part of fetch)
    concentration.py — concentration command: coverage-aware within-fund concentration
    overlap.py     — overlap command: cross-fund single-name exposure floor via canonical identity
    backtest.py    — backtest command: contribution-timing (dip-reserve vs constant-DCA + blind-deployment controls, plus within-month daily dip-slice strategies — one-slice-per-dip and a carry-forward variant) over one ETF's real history
data/
  etf_universe.yaml      — ETF config (ISINs, names, tickers)
  currency_metadata.yaml — pinned ftgo resolutions incl. FX pairs (ADR-0002, ADR-0010)
  scenarios.yaml         — named ISIN:pct baskets for rebalance/correlation (ADR-0017, gitignored)
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
- `ADR-0016` — `rebalance` command: minimum-cash buy-only target rebalance (dilute, never sell) + N-month DCA plan; targets are percents of the whole book with a pro-rata residual; target recap + feasibility verdict
- `ADR-0017` — `scenario` command: named ISIN:pct baskets in one gitignored YAML; CRUD-only, consumed by `rebalance --scenario` and `correlation --scenario` (the latter correlates the *post-rebalance* portfolio); rebalance plan core graduates into `common`
- `ADR-0018` — portfolio-level country/region concentration (country HHI) in `concentration`: reviewed per-fund country sidecar (`data/region_metadata.yaml`), exact over covered funds, fund-level coverage disclosed and never gated; graduates the `REGION_CONTRACT` from `region_unavailable_v1` to `country_hhi_v1`
- `ADR-0019` — `backtest` command: contribution-timing backtest over one real ETF series; a dip-**reserve** model keeps ∑ contributions equal across strategies (invariance by construction), 0% reserve default (`--cash-rate` knob), terminal wealth includes leftover cash, drawdown-vs-rolling-high signal on the EUR series with a warm-up burn; **evaluator only — never fits or ranks in-sample** (walk-forward + proxy-index history deferred to v2); XIRR graduates from `performance` into `common` alongside the pure sim core
- `ADR-0020` — blind-deployment control in `backtest`: drawdown-blind `DeployMode` schedules (`even`/`delayed`/`random`) that empty the reserve by the horizon, so a dip decomposes into **reserve cost / deployment benefit / timing benefit (dip − blind-even) / total**; matched cash-drag + blind-even per distinct β; deterministic `blind-even` headline + supplementary seeded `blind-random` distribution (`--blind-seeds`, default 500); invariance holds for every mode by construction
- `ADR-0021` — within-month **daily dip-slice** strategy in `backtest` (`deploy=daily-dip`, `--slices N`/`n=N`, default 20): each month's `C` is cut into N slices spent on **down days** (close < prior close), with a catch-up + last-day rule that fully deploys `C` inside the month; **no cross-month reserve** (a sibling of constant-DCA, probing intra-month timing), so invariance is unconditional (`reserve_cash == 0`, no `--cash-rate` effect) and it gets no β-matched controls; own daily-loop core `_simulate_daily_dip` in `common`; warm-up skipped when no signal dip is present
- `ADR-0022` — `backtest` **MaxDD** is sampled **daily** for every strategy (was monthly): crash troughs fall mid-month, so monthly sampling undercounted the loss (~22%→~34% for a fully-invested all-world book) and was incomparable across strategies; daily sampling also surfaces the reserve's drawdown cushion (idle cash doesn't fall in a crash); shares/reserve still move only at fills, wealth/XIRR unchanged; supersedes ADR-0019's monthly-sampled interim drawdown
- `ADR-0023` — **carry-forward** daily dip-slice strategy in `backtest` (`deploy=daily-dip-carry`, shares `--slices N`/`n=N`): a sibling of ADR-0021 that accrues one slice per trading day and, on each **down day**, spends *every* accrued-but-unspent slice (day's own + all earlier unspent) rather than one, with the same last-day flush; holds cash through up-runs and dumps the pool onto the next dip; no cross-month reserve, so invariance is unconditional (`reserve_cash == 0`, `--cash-rate` inert) and it gets no β-matched controls; own core `_simulate_daily_dip_carry` in `common`, sharing the result-assembly helper `_daily_dip_result` with ADR-0021's core
- `ADR-0024` — **experimental tier isolation**: `concentration`, `overlap`, `backtest` (the least-settled commands) move to `src/e1f/experimental/` with their own `experimental/common.py` for experimental-only primitives; a **one-way import boundary** (import-linter `forbidden` contract) bars every stable module and `common` from importing `e1f.experimental` — only `cli` (the router) may. Look-through *fetching* leaves stable `fetch` for a new experimental `lookthrough` command (run it after `fetch`). Shared primitives (`xirr`, valuation, `Status`/`MetricContract`) stay in `common`; experimental code consumes them freely

## Skills

- `/doc-check` — audit README, CLAUDE.md, and ADRs for drift, stale claims, and dead links

## Conventions

- One home per fact: the *why* lives in an ADR, code shapes live in code, README describes behaviour without duplicating argparse definitions.
- Before calling a change done: `./scripts/check.sh` must be green.
- Coverage floor is 90%; ratchet up, never down without a recorded reason.
- New modules must satisfy the layer contract in `ADR-0003`; experimental modules live under `e1f.experimental` behind the one-way boundary in `ADR-0024` (no stable module may import them).
- Application code (`src/`) keeps all imports at the top of the file (ruff `PLC0415`); tests may import locally.
