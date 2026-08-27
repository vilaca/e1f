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
  portfolio.py   — portfolio subcommand: holdings from transactions; FX-converted EUR market value + market-value-weighted fee/TER (ADR-0032)
  performance.py — performance subcommand: EUR valuation, XIRR/TWR/risk metrics; --diff N signed change over N calendar days (ADR-0029); --series N daily cumulative TOTAL over N days (ADR-0030); --metrics portfolio-level extended risk report (ADR-0033)
  correlation.py — correlation command: return co-movement redundancy + clustering (scipy)
  rebalance.py — rebalance command: minimum-cash buy-only target rebalance + N-month DCA plan
  scenario.py  — scenario command: CRUD for named ISIN:pct baskets (one YAML, many scenarios)
  common/      — shared primitives package (ADR-0025); commands import from `e1f.common`
    defaults.py    — DEFAULT_* paths, base currency, exchange/currency constants
    retry.py       — HTTP retry/backoff
    universe.py    — ETFDefinition, OpenFIGI, ConfigManager, fund-metadata enrichment
    holdings.py    — trades, position timeline, FX conversion, EUR valuation
    metrics.py     — Status / MetricContract / --explain helpers, XIRR
    scenarios.py   — named ISIN:pct basket I/O
    rebalance.py   — buy-only rebalance plan core
  experimental/ — isolated experimental tier (ADR-0024); no stable module imports it (cli routes it)
    common.py      — experimental-only primitives: look-through model + ingest, overlap-candidate signal, backtest simulator
    lookthrough.py — lookthrough command: refresh cached yfinance look-through snapshots (was part of fetch)
    concentration.py — concentration command: coverage-aware within-fund concentration
    overlap.py     — overlap command: cross-fund single-name exposure floor via canonical identity
    backtest.py    — backtest command: contribution-timing (dip-reserve vs constant-DCA + blind-deployment controls, plus within-month daily dip-slice strategies — one-slice-per-dip and a carry-forward variant) over one ETF's real history
    seasonality.py — seasonality command: twelve-month calendar analysis + permutation omnibus; `--portfolio` consensus + equal-weight book (ADR-0027); `--evaluate` frozen August/November vs DCA (ADR-0028); optional pre-specified / frozen-OOS rules (ADR-0026)
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
- `ADR-0018` — **design, not landed**: portfolio-level country HHI via a reviewed sidecar; code still reports region UNAVAILABLE (`region_unavailable_v1` in `src/e1f/experimental/concentration.py`)
- `ADR-0019` — `backtest` command: contribution-timing backtest over one real ETF series; a dip-**reserve** model keeps ∑ contributions equal across strategies (invariance by construction), 0% reserve default (`--cash-rate` knob), terminal wealth includes leftover cash, drawdown-vs-rolling-high signal on the EUR series with a warm-up burn; **evaluator only — never fits or ranks in-sample** (walk-forward + proxy-index history deferred to v2); XIRR graduates from `performance` into `common`; the sim core later moved to `experimental/common` (ADR-0024)
- `ADR-0020` — blind-deployment control in `backtest`: drawdown-blind `DeployMode` schedules (`even`/`delayed`/`random`) that empty the reserve by the horizon, so a dip decomposes into **reserve cost / deployment benefit / timing benefit (dip − blind-even) / total**; matched cash-drag + blind-even per distinct β; deterministic `blind-even` headline + supplementary seeded `blind-random` distribution; invariance holds for every mode by construction
- `ADR-0021` — within-month **daily dip-slice** strategy in `backtest` (`deploy=daily-dip`): each month's `C` is cut into N slices spent on **down days** (close < prior close), with a catch-up + last-day rule that fully deploys `C` inside the month; **no cross-month reserve** (a sibling of constant-DCA, probing intra-month timing), so invariance is unconditional (`reserve_cash == 0`, no `--cash-rate` effect) and it gets no β-matched controls; own daily-loop core `_simulate_daily_dip` in `experimental/common`; warm-up skipped when no signal dip is present
- `ADR-0022` — `backtest` **MaxDD** is sampled **daily** for every strategy (was monthly): crash troughs fall mid-month, so monthly sampling undercounted the loss (~22%→~34% for a fully-invested all-world book) and was incomparable across strategies; daily sampling also surfaces the reserve's drawdown cushion (idle cash doesn't fall in a crash); shares/reserve still move only at fills, wealth/XIRR unchanged; supersedes ADR-0019's monthly-sampled interim drawdown
- `ADR-0023` — **carry-forward** daily dip-slice strategy in `backtest` (`deploy=daily-dip-carry`): a sibling of ADR-0021 that accrues one slice per trading day and, on each **down day**, spends *every* accrued-but-unspent slice (day's own + all earlier unspent) rather than one, with the same last-day flush; holds cash through up-runs and dumps the pool onto the next dip; no cross-month reserve, so invariance is unconditional (`reserve_cash == 0`, `--cash-rate` inert) and it gets no β-matched controls; own core `_simulate_daily_dip_carry` in `experimental/common`, sharing the result-assembly helper `_daily_dip_result` with ADR-0021's core
- `ADR-0024` — **experimental tier isolation**: `concentration`, `overlap`, `backtest` (the least-settled commands) move to `src/e1f/experimental/` with their own `experimental/common.py` for experimental-only primitives; a **one-way import boundary** (import-linter `forbidden` contract) bars every stable module and `common` from importing `e1f.experimental` — only `cli` (the router) may. Look-through *fetching* leaves stable `fetch` for a new experimental `lookthrough` command (run it after `fetch`). Shared primitives (`xirr`, valuation, `Status`/`MetricContract`) stay in `common`; experimental code consumes them freely
- `ADR-0025` — **`e1f.common` is a package**: the old single `common.py` splits into `defaults` / `retry` / `universe` / `holdings` / `metrics` / `scenarios` / `rebalance`; command modules still `from e1f.common import …`
- `ADR-0026` — experimental `seasonality` command: all-twelve-month calendar analysis (descriptive + permutation omnibus + extreme-month placebo); no September privilege; total-return default (accumulating NAV only); pre-specified / frozen-OOS rules only, never an auto-traded weakest month; separate from dip `backtest`
- `ADR-0027` — `seasonality --portfolio`: inferential vs DESCRIPTIVE cohorts; consensus table of fund-level monthly means; cross-sectional permutation of strongest/weakest-month concentration; correlated-universe caveat; balanced equal-weight book; `--rule` stays single-ISIN only
- `ADR-0028` — `seasonality --evaluate`: frozen August/November contribution skip/shift vs DCA (sit-out secondary); in-sample or labelled holdout; months are constants, not re-selected
- `ADR-0029` — `performance --diff N`: signed change table over N calendar days (reading-A, money columns only, union of held ISINs, held-but-unpriceable → unavailable); composes with `--as-of`; `portfolio --diff` deferred
- `ADR-0030` — `performance --series N`: one portfolio-TOTAL row per trading day over the last N calendar days; cumulative-since-inception metrics (each row == `--as-of` that day, by reusing `_snapshot`/`_snapshot_total`); trading days come from the price data (weekends/holidays drop out, no hardcoded calendar); `P&Lctr` dropped; composes with `--as-of`, `--reverse` newest-first, mutually exclusive with `--diff`
- `ADR-0031` — `performance --series` gains two trailing columns: market-value-weighted TER (`WTER`) and estimated annual fee (`Fee€/yr`) at that day's MktVal; value-weighted (not `portfolio`'s cost-basis), missing-TER dilutes; shared columns untouched; no cross-command import (fee formula inline)
- `ADR-0032` — `portfolio` FX-converts its market value (`Value€` via shared `convert_to_eur`, FX as of the close's date) and moves the estimated annual fee + weighted avg TER onto market value (was cost basis); missing price/FX → `—` and excluded (with a warning); `Weight` stays cost-basis; supersedes ADR-0029's no-FX portfolio-value note (`portfolio --diff` still deferred)
- `ADR-0033` — `performance` analytics expansion, **clean-only scope** + deferral log (umbrella; Phase A landed). Phase A adds `--metrics`, a portfolio-level extended risk report — MaxDD duration (peak→recovery, ongoing-aware), total underwater time, recovery factor (`TWR/|MaxDD|`), best/worst single-period return + gain/loss ratio — computed from the same time-weighted return series as `risk_metrics` (MaxDD agrees by construction); durations in calendar days; composes with `--series N` for a per-day table (cumulative since inception). **Deferred on the record:** anything needing €STR (Sharpe, Treynor, Jensen alpha, Sortino), convention-choice metrics (Calmar, avg drawdown, up/down capture, win/profit), and rolling series. Phase B (a new `benchmark` command: Beta/R²/Tracking Error/Information Ratio/Relative Strength vs MSCI World `IE00B4L5Y983` + already-priced ACWI/S&P 500/Europe/All-World/WEBN, `eur_return_series` graduating to `common`) and Phase C (deposit/organic analysis: ROIC, organic-vs-reported, per-deposit impact) planned, not landed

## Skills

- `/doc-check` — audit README, CLAUDE.md, and ADRs for drift, stale claims, and dead links

## Conventions

- One home per fact: the *why* lives in an ADR, code shapes live in code, README describes behaviour without duplicating argparse definitions.
- Before calling a change done: `./scripts/check.sh` must be green.
- Coverage floor is 90%; ratchet up, never down without a recorded reason.
- New modules must satisfy the layer contract in `ADR-0003`; experimental modules live under `e1f.experimental` behind the one-way boundary in `ADR-0024` (no stable module may import them).
- Application code (`src/`) keeps all imports at the top of the file (ruff `PLC0415`); tests may import locally.
