# e1f

ETF universe config and historical price fetching into SQLite.

## Running checks

```bash
./scripts/check.sh              # all gates: lint, layers, shell, actions, types, dead, package, mutation, test
./scripts/check.sh lint         # one gate (lint | layers | shell | actions | types | dead | package | mutation | test)
uv run pytest                   # tests only, no coverage floor
```

The script is the single definition of "green". CI runs the same gates via `scripts/check.sh` (`actions` in a separate job; see `.github/workflows/ci.yml`).

## Layout

```
src/e1f/
  cli.py       — entry point; routes top-level commands
  autocomplete.py — autocomplete command: Bash/Zsh shell completion
  config.py    — config subcommand: OpenFIGI resolution, YAML management; remove/trim refuse live holdings unless forced (ADR-0035)
  fetch.py     — fetch subcommand: ftgo/yfinance price + FX fetching, SQLite
  validate.py  — validate command: config/DB sync and price-data quality
  transactions.py — transactions subcommand: broker export ingest (Trade Republic CSV, XTB Excel)
  portfolio.py   — portfolio subcommand: holdings from transactions; FX-converted EUR market value + market-value-weighted fee/TER (ADR-0032)
  performance.py — performance subcommand: EUR valuation, XIRR/TWR/risk metrics; --diff N signed change over N calendar days (ADR-0029); --series N daily cumulative TOTAL over N days (ADR-0030); --isin restricts any view to one holding (ADR-0038); --metrics portfolio-level extended risk report (incl. days-since-high, best/worst month, trailing 1M/3M/6M), composes with --series; --contrib per-holding Cariño-linked return contribution summing to the TOTAL TWR (ADR-0033)
  benchmark.py   — benchmark command: portfolio vs benchmark ETFs (beta, R², tracking error, information ratio, relative strength) over the shared window; default set of seven broad indices, `*` marks held ones (ADR-0033 Phase B, ADR-0039)
  deposits.py    — deposits command: organic-vs-reported value + ROIC + per-deposit contribution impact; reuses performance's EUR valuation so totals reconcile (ADR-0033 Phase C); --group week|month|year aggregates the table into deposit vintages (one row per period × fund), drops the top summary block, and adds a bottom ── ALL ── grand-total row (ADR-0036)
  correlation.py — correlation command: return co-movement redundancy + clustering (scipy)
  rebalance.py — rebalance command: minimum-cash buy-only target rebalance + N-month DCA plan
  scenario.py  — scenario command: CRUD for named ISIN:pct baskets (one YAML, many scenarios)
  glossary.py  — glossary command: parse + query data/glossary.md (metric definitions + usefulness) (ADR-0034)
  common/      — shared primitives package (ADR-0025); commands import from `e1f.common`
    defaults.py    — DEFAULT_* paths, base currency, exchange/currency constants
    retry.py       — HTTP retry/backoff
    universe.py    — ETFDefinition, OpenFIGI, ConfigManager, fund-metadata enrichment
    holdings.py    — trades, position timeline, FX conversion, EUR valuation
    returns.py     — daily EUR return series: per-fund + whole-book (portfolio_return_series), value/contribution aggregation, wealth-index recurrence, per-holding Cariño return contribution (contribution_to_return) (ADR-0033)
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
  glossary.md            — metric glossary read by the `glossary` command (ADR-0034, checked in)
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
- `ADR-0013` — `overlap` command: cross-fund single-name exposure floor (`≥`) via reviewed `canonical_key`; valuation core + provenance vocabulary graduate into stable `common`; ADR-0024 later moves experimental-only `overlap_candidates` to `experimental/common.py`
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
- `ADR-0031` — `performance --series` gains two trailing columns: market-value-weighted TER (`WTER`) and estimated annual fee (`Fee€/yr`) at that day's MktVal; missing-TER dilutes; shared columns untouched; ADR-0032 later aligns `portfolio` on the same market-value basis, implemented through shared `common/fees.py` primitives
- `ADR-0032` — `portfolio` FX-converts its market value (`Value€` via shared `convert_to_eur`, FX as of the close's date) and moves the estimated annual fee + weighted avg TER onto market value (was cost basis); missing price/FX → `—` and excluded (with a warning); `Weight` stays cost-basis; supersedes ADR-0029's no-FX portfolio-value note (`portfolio --diff` still deferred)
- `ADR-0033` — `performance` analytics expansion, **clean-only scope** + deferral log (umbrella; Phase A landed). Phase A adds `--metrics`, a portfolio-level extended risk report — MaxDD duration (peak→recovery, ongoing-aware), days since the current running peak, total underwater time, recovery factor (`TWR/|MaxDD|`), best/worst single-period return + gain/loss ratio, best/worst calendar month, and trailing 1M/3M/6M time-weighted returns (window predating inception → n/a) — computed from the same time-weighted return series as `risk_metrics` (MaxDD agrees by construction); durations in calendar days; composes with `--series N` for a per-day table (cumulative since inception). Also `--contrib`: per-holding Cariño-linked return contribution summing exactly to the TOTAL TWR (`contribution_to_return` in `common/returns.py`). **Deferred on the record:** anything needing €STR (Sharpe, Treynor, Jensen alpha, Sortino), convention-choice metrics (Calmar, avg drawdown, up/down capture, win/profit), and rolling series. Phase B **landed** as a new `benchmark` command (Beta/R²/Tracking Error/Information Ratio/Relative Strength/Out% vs a default set of six broad benchmarks incl. MSCI World `IE00B4L5Y983`, `*` marks held ones); return-series primitives graduated to `common/returns.py`; no minimum-overlap floor by default (the book may be young — n is always shown), raise `--min-overlap` for a stricter bar. Phase C **landed** as a new `deposits` command (invested/reported/organic-gain/ROIC summary + per-deposit impact table valuing each buy to the as-of date; buy-and-hold, so it reconciles with the `performance` TOTAL). All three clean-scope phases shipped; the deferral log stands, sorted by what unblocks each: €STR-gated (Sharpe/Treynor/Jensen + the whole MAR family — Sortino, downside deviation — gate on €STR together, no interim MAR=0 pick), convention-blocked (Calmar/avg-drawdown/capture/win-profit — code-ready, need a chosen definition), young-book (trailing 1Y/2Y + rolling-252 — unbuilt but premature until the book ages; trailing 1M/3M/6M and per-holding return contribution have since landed), and data-blocked (sector/region/factor attribution — needs look-through classification e1f lacks; region UNAVAILABLE since ADR-0018)
- `ADR-0034` — `glossary` command: a checked-in `data/glossary.md` of every metric (definition + usefulness), read by `e1f glossary` (list all, or word-start-match a metric name like `TWR` / `P&L`, then fall back to group/body substring search); the Markdown file is the single source (parsed, not duplicated in code), scoped to stable-command metrics
- `ADR-0035` — destructive `config remove` / `config trim` preflight: refuse the whole operation when any candidate ISIN is a live holding; `--force` retains transactions but removes valuation data
- `ADR-0036` — `deposits --group week|month|year`: collapse the per-buy impact table into deposit vintages (one row per calendar period × fund; week is ISO-8601 `YYYY-Www`) rendered as per-period sections each closed by a `── total ──` subtotal; a pure repartition of the same buys, so ROIC (`Ret%`)/organic gain (`Gain€`) hold per row and the `performance`-TOTAL reconciliation is unchanged, and a bucket is unvaluable iff its fund is (never a partial bucket). Under `--group` the top summary block is dropped and a bottom `── ALL ──` grand-total row carries the whole-book Invested/Reported/Organic/ROIC; no date column in grouped mode (the period is a section heading); `--sort` is within-period, `--reverse` also flips period order; day and an isin axis (redundant with `portfolio`) rejected
- `ADR-0037` — canonical `--sort` tokens across table commands: the same quantity uses the same token (`value`, `cost`, `pnl`, `weight`, …); display headers stay as they are; old nicknames (`total`/`gain`/`amount`/`ret`) removed; `--sort` added to `benchmark`, `rebalance` (opt-in; binder-first default stays), `transactions list`, and `config list`
- `ADR-0038` — `performance --isin`: restrict the book to one holding before any view runs (snapshot / `--series` / `--metrics` / `--diff` / `--contrib`); `--series N --isin X` is `--series` on that one-fund book; unknown ISIN lists holdings and exits 1
- `ADR-0039` — `benchmark` default set gains SPDR MSCI ACWI IMI (`IE00B3YLTY66`), listed after SPDR MSCI ACWI; seven broad accumulating ETFs (supersedes ADR-0033's six)

## Skills

- `/doc-check` — audit user/agent docs, ADRs, and the metric glossary for drift, stale claims, dead links, and convention breaks

## Conventions

- One home per fact: the *why* lives in an ADR, code shapes live in code, README describes behaviour without duplicating argparse definitions.
- Financial timing/fill conventions are governed by their ADR. Any change must update or supersede that ADR and add or update a pinned-date regression test with hand-computed expected fills, terminal wealth, or equivalent numerics; property tests complement but do not replace date pins.
- Before calling a change done: `./scripts/check.sh` must be green.
- Coverage floor is 90%; ratchet up, never down without a recorded reason.
- New modules must satisfy the layer contract in `ADR-0003`; experimental modules live under `e1f.experimental` behind the one-way boundary in `ADR-0024` (no stable module may import them).
- Destructive commands must preflight the whole candidate set before mutation, provide an explicit override for deliberate data loss, preserve rollback, and test refusal, override, and failure paths.
- User-visible partial or unavailable results must have typed semantic outcome data and a disclosure test; do not rely only on full output snapshots or log-string matching.
- Every new stable metric/column must update `data/glossary.md` and its `Where` command/flag contract. Shared financial primitives used by multiple commands need a same-fixture reconciliation test when their outputs must agree.
- Application code (`src/`) keeps all imports at the top of the file (ruff `PLC0415`); tests may import locally.
