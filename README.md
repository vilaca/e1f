# e1f

Build an ETF universe from ISINs and fetch historical prices into SQLite.
Prices come from ftgo (FT Markets) with an optional yfinance fallback (`--fallback`).

## Setup

Requires Python 3.14+.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

This installs the `e1f` command. Enable completion for the current Bash or Zsh
session with:

```bash
source <(e1f autocomplete)
```

The shell is inferred from `$SHELL`; pass `bash` or `zsh` explicitly to override it.

## Which command answers my question?

| Your question | Run | Read in the output |
|---|---|---|
| What do I hold, and at what weight? | `e1f portfolio` (add `--show-cost-basis` for EUR value) | **Weight** (share of the book); with the flag, **Value€** (EUR market value) |
| How did *my* investing go — my cash, my timing? | `e1f performance` | **XIRR** — your annualized return, size- and timing-weighted (a late deposit that's flat drags it) |
| How did the *funds* do, timing stripped out? | `e1f performance` | **TWR** (cumulative) and **CAGR** (that per year) — the holdings' return, comparable to a factsheet |
| Am I up or down, in euros? | `e1f performance` | **P&L€** (euros vs what you paid), **P&L%** (that over cost) |
| Which holdings actually drove the return? | `e1f performance --contrib` | **Ctr%** — each holding's share of the book's TWR (sums to TOTAL), beside its **TWR** and value **Weight**; for the euro share **P&Lctr**, see the plain `e1f performance` table |
| How rough was the ride — worst drop, time underwater? | `e1f performance --metrics` | **MaxDD** (deepest fall), **SinceHi** (days off the current peak), **RecFac** (TWR ÷ that fall — >1 means it paid off) |
| What changed over the last N days? | `e1f performance --diff N` | **ΔMktVal€ / ΔP&L€** — a **ΔCost€** spike is cash *in*, not a gain |
| How did the TOTAL move day by day? | `e1f performance --series N` | one cumulative-since-inception TOTAL snapshot per trading day (levels, not deltas) |
| How did one fund move day by day? | `e1f performance --series N --isin X` | the same series, restricted to that holding (ADR-0038) |
| How did each individual deposit do? | `e1f deposits` | **ROIC** (gain ÷ invested), **Organic gain** (market growth, excludes new cash), per-lot **Ret%** |
| Did I beat the market? | `e1f benchmark` | **Out%** (raw gap) / **RelStr** (compounded: 1.05 = €1 became 5% more), **IR** (gap per unit of drift). Check **n** (thin history is noise) and **R²** (a poor mirror means you beat the wrong benchmark) first |
| Are my funds redundant — do they move alike? | `e1f correlation` | **ρ** (near 1 = a second helping of the same bet), the clusters, and **n** |
| What should I buy to hit target weights? | `e1f rebalance --target ISIN:PCT …` | **Buy€** per fund and the minimum cash `C_min` (a `--target` or `--scenario` is required) |
| What does a metric mean? | `e1f glossary <term>` | the entry itself |

Most bold names are glossary terms — `e1f glossary Out%` (or `RelStr`, `RecFac`, …)
prints the full mechanics, when to trust it, and what *not* to conclude (the
rebalance plan's `Buy€` / `C_min` are the exception — see `e1f rebalance --help`).
For *which metric* answers a question rather than which command, see the
metric-family table in `data/glossary.md`.

## Workflow

The tool exposes stable commands around a shared config/DB. The least-settled
commands form an isolated **experimental** tier (ADR-0024) — still rough, and
walled off so no stable command depends on them:

- **`e1f autocomplete`** — print Bash or Zsh completion setup.
- **`e1f config`** — build the ETF universe YAML from ISINs (via OpenFIGI).
- **`e1f fetch`** — populate the SQLite DB with prices and FX rates.
- **`e1f validate`** — check config/DB sync, history depth, and data quality (including interior single-day price gaps — a day a fund lacks that its same-exchange peers have — repairable with `e1f fetch <isin> --force`).
- **`e1f transactions`** — ingest ETF trades from broker exports (Trade Republic CSV, XTB Excel) and list stored trades.
- **`e1f portfolio`** — open ETF holdings per broker from `transactions`; `--show-cost-basis` adds FX-converted EUR market value, and the estimated annual fee and weighted-average TER are weighted by market value (ADR-0032).
- **`e1f performance`** — market value, unrealized P&L, and return metrics (XIRR, TWR, volatility, drawdown, CAGR) in EUR, per holding and portfolio-wide; see `e1f glossary` for metric definitions and `e1f performance --help` for report modes.
- **`e1f benchmark`** — compare the portfolio's time-weighted EUR returns against benchmark ETFs; see the glossary for the reported metrics and ADR-0033 for the benchmark set and overlap policy.
- **`e1f deposits`** — compare invested capital with reported value and inspect each deposit's impact. Buy-and-hold totals reconcile with the `performance` TOTAL (ADR-0033).
- **`e1f correlation`** — return co-movement redundancy: highly-correlated fund pairs carrying real combined weight, plus a hierarchical clustering of held funds.
- **`e1f rebalance`** — minimum-cash, buy-only plan to reach user-supplied target weights (never selling), plus an optional N-month DCA schedule.
- **`e1f scenario`** — save/list/show/delete named ISIN:pct baskets in one YAML file; recall them with `rebalance --scenario` and `correlation --scenario`.
- **`e1f glossary`** — look up what a metric means and what it's useful for; reads the checked-in `data/glossary.md` (ADR-0034).

Experimental tier (ADR-0024):

- **`e1f lookthrough`** — refresh cached yfinance look-through snapshots for held funds; run it after `fetch` to feed `concentration` / `overlap`.
- **`e1f concentration`** — coverage-aware within-fund concentration (security, sector, asset-class) with rank-constrained bounds on the unobserved tail.
- **`e1f overlap`** — cross-fund single-name exposure floor (`≥ €`, `≥ %`), summing a security across funds only via a reviewed canonical identity.
- **`e1f backtest`** — evaluate contribution-timing strategies against controlled DCA and blind-deployment baselines (ADR-0019 through ADR-0023).
- **`e1f seasonality`** — analyze calendar-month effects and pre-specified or frozen-OOS rules without auto-trading the weakest in-sample month (ADR-0026 through ADR-0028).

```bash
# 1. Add ETFs by ISIN (OpenFIGI resolution; config shape in src/e1f/common/universe.py)
e1f config add IE00BM67HK77
e1f config add IE00BM67HK77 IE00BDBRDM35 IE00BKM4GZ66
e1f config list
e1f config update IE00BM67HK77

# Remove ETFs from config, DB, and currency metadata.
# Both remove and trim refuse to strand live positions unless --force;
# transaction history is always retained.
e1f config remove IE00BM67HK77
e1f config trim        # keep only ISINs present in config, DB, and metadata

# Check config/DB sync, history depth, and data quality
e1f validate

# 2. Fetch prices into the SQLite DB
e1f fetch                 # all ETFs in the config
e1f fetch IE00BM67HK77    # a single ISIN
e1f fetch --force         # ignore the cache and re-download
e1f fetch IE00BM67HK77 --replace  # atomically replace one ISIN's stored series (repair)

# 3. Ingest broker transactions into the SQLite DB
e1f transactions trade-republic ~/Downloads/transactions.csv
e1f transactions tr ~/Downloads/transactions.csv
e1f transactions trade-republic transactions.csv --db data/e1f.db
e1f transactions xtb ~/Downloads/EUR_38472916_2006-01-01_2026-08-15.xlsx
e1f transactions list
e1f portfolio
e1f portfolio --explain              # + provenance block (ADR-0014)

# 4. Value the portfolio and measure returns (EUR)
e1f performance
e1f performance --as-of 2025-12-31   # historical snapshot
e1f performance --diff 7             # signed change over the last 7 calendar days (ADR-0029)
e1f performance --as-of 2025-12-31 --diff 7  # change in the week ending Dec 31
e1f performance --series 90          # portfolio TOTAL for each trading day, last 90 days (ADR-0030)
e1f performance --series 60 --isin IE00B3VSSL01  # that holding's path only (ADR-0038)
e1f performance --metrics            # portfolio-level extended risk report (ADR-0033)
e1f performance --metrics --series 14 # that report tabulated per trading day, last 14 days
e1f performance --contrib            # per-holding return contribution (sums to TOTAL TWR, ADR-0033)
e1f performance --sort value --reverse
e1f performance --show-status        # + per-holding provenance Status column (ADR-0014)
e1f performance --explain            # + per-holding provenance blocks (implies --show-status)

# Benchmark the book against broad indices (EUR, time-weighted; ADR-0033)
e1f benchmark                        # vs the seven defaults (MSCI World, MSCI Europe, WEBN, S&P 500, MSCI ACWI, ACWI IMI, FTSE All-World)
e1f benchmark --against IE00B5BMR087,IE00B4K48X80  # a custom benchmark set by ISIN
e1f benchmark --min-overlap 60       # demand ~a quarter-year of shared history
e1f benchmark --explain              # + provenance block

# Decompose contributions vs market gain, and rank each deposit's impact (ADR-0033)
e1f deposits                         # invested/reported/organic/ROIC + per-deposit table
e1f deposits --sort pnl --reverse    # biggest contributors to P&L first
e1f deposits --as-of 2025-12-31      # value each deposit as of a past date
e1f deposits --group year            # deposit vintages: one row per fund per year
e1f deposits --group week            # same, per ISO week (YYYY-Www)

# 5. Inspect within-fund concentration (experimental; look-through cached by `e1f lookthrough`)
e1f lookthrough                      # refresh yfinance look-through snapshots first
e1f concentration                    # every held fund + overlap candidates
e1f concentration VWCE               # one fund by ISIN, ticker, or name
e1f concentration VWCE --explain     # per-metric provenance chain

# 6. Establish cross-fund single-name overlap (needs reviewed canonical identities)
e1f overlap candidates               # resolution worklist (co-occurrence seed + full roster)
e1f overlap resolve "Apple Inc." apple-ord   # assert a reviewed identity
e1f overlap                          # the ≥ floor report over resolved names in ≥2 funds
e1f overlap --explain                # per-security Vf×w reconstruction

# 7. Measure return co-movement redundancy (a separate axis from overlap)
e1f correlation                      # redundant pairs (ρ, combined weight) + clusters
e1f correlation --explain            # reconstruct each flagged pair from source
e1f correlation --explain IE00B4L5Y983 IE00BK5BQT80   # reconstruct one named pair
e1f correlation --rho-flag 0.95 --weight-flag 0.10   # tune the flag thresholds

# 8. Plan a buy-only rebalance to target weights (percents of the whole book)
e1f rebalance --target IE00B4L5Y983:30 --target IE00BK5BQT80:40   # min-cash plan
e1f rebalance --target IE00B4L5Y983:30 --target IE00BK5BQT80:40 --months 10  # DCA
e1f rebalance --target IE00B4L5Y983:30 --explain     # + provenance block (ADR-0014)
e1f rebalance --target IE00B4L5Y983:60 --as-of 2025-12-31   # historical snapshot

# 9. Save a basket once, reuse it across commands (ADR-0017)
e1f scenario save core --target IE00B4L5Y983:60 --target IE00BK5BQT80:40 --months 10
e1f scenario list                    # names, target counts, sums
e1f scenario show core               # targets with fund names
e1f rebalance --scenario core        # recall the saved basket (--months here overrides)
e1f correlation --scenario core      # correlate the POST-rebalance portfolio it implies

# Look up what a metric means (ADR-0034)
e1f glossary                         # list every metric, grouped
e1f glossary TWR                     # one term; e1f glossary "P&L" fans out to the P&L family

# 10. Backtest contribution timing over one ETF's real history (ADR-0019)
e1f backtest --isin IE00B3YLTY66                       # dip vs DCA + matched cash-drag/blind-even controls
e1f backtest --isin IE00B3YLTY66 --aggressiveness 8 --curvature 2   # tune the dip response
e1f backtest --isin IE00B3YLTY66 --window 120          # rolling-window outcome distribution
e1f backtest --isin IE00B3YLTY66 --strategy "deploy=daily-dip,n=20"   # within-month daily dip slices (ADR-0021)
e1f backtest --isin IE00B3YLTY66 --strategy "deploy=daily-dip-carry,n=20"  # carry unspent slices onto the next dip (ADR-0023)
e1f backtest --isin IE00B3YLTY66 --blind-seeds 0       # skip the blind-random robustness block
e1f backtest --isin IE00B3YLTY66 --explain             # + assumptions/provenance block

# 11. Calendar seasonality of one ETF (experimental; not a dip-strategy knob)
e1f seasonality --isin IE00B3YLTY66
e1f seasonality --isin IE00B3YLTY66 --explain
e1f seasonality --portfolio            # consensus + cross-sectional permutation (ADR-0027)
e1f seasonality --isin IE00B3YLTY66 --evaluate   # frozen Aug/Nov vs DCA (ADR-0028)
```

Defaults (from `src/e1f/common/defaults.py`): config `data/etf_universe.yaml`, database
`data/e1f.db`, fetch start date `2000-01-01` (earlier than any ETF inception, so
the first fetch returns each ETF's full history). Paths resolve against the
project root, so commands work from any directory. Flag overrides are per command
— `e1f config --help`, `e1f fetch --help`, `e1f transactions --help`,
`e1f portfolio --help`, `e1f performance --help`, `e1f correlation --help`,
`e1f rebalance --help`, `e1f scenario --help`, `e1f validate --help`, and for the
experimental tier `e1f lookthrough --help`, `e1f concentration --help`,
`e1f overlap --help`, `e1f backtest --help`, `e1f seasonality --help`.

## Price sources

ftgo resolves securities **by ISIN** and pins the first match in
`data/currency_metadata.yaml`, preferring the listing quoted in the fund's own
share-class currency, so the fetched security can't drift as FT Markets search
ordering changes. When `--fallback` is set and ftgo has no data for an ISIN, fetch tries
yfinance using the tickers from the config (trying `.L` and `.DE` suffixes for
London/Xetra listings).

Fetch is incremental by default (`--force` re-downloads and overwrites matching
dates). `e1f fetch <ISIN> --replace` repairs one series by deleting its stored rows and
re-inserting the fetched range; unless `--allow-shrink` is given it refuses to
drop any stored date (shorter range, narrower window, or interior hole), so a
truncated response can't silently wipe history
(`ADR/ADR-0008_price_series_replace_repair.md`).

Prices are stored in each fund's native quote currency. A bulk `e1f fetch` also
refreshes a daily FX series (`fx_rates` table) for the currencies the **held**
portfolio needs — derived from each held ISIN's pinned quote currency, sourced
from ftgo (yfinance `EURUSD=X` under `--fallback`), and forward-filled at read
time — so a mixed-currency portfolio can be valued in EUR. A held currency with
no EUR FX rule (e.g. GBX pence) fails loud rather than mis-converting. See
`ADR/ADR-0010_currency_fx_foundation.md`.

`e1f validate` distinguishes **errors** from **warnings**: it exits `1`
only on errors (duplicate keys, null or non-positive closes, weekend rows, invalid
dates, malformed pinned quote-currency metadata, or a config/DB desync) and `0`
when clean or when only warnings remain (over-limit business-day gaps, large price
moves, short/sparse/cash-like history).
See `e1f validate --help` for the full taxonomy and
`ADR/ADR-0009_validate_exit_code_contract.md` for the why.

The SQLite DB holds a `prices` table and an `fx_rates` table (schemas in
`src/e1f/fetch.py`) and a `transactions` table (schema in
`src/e1f/transactions.py`); all can be read directly with any SQLite client or
`pandas.read_sql`.

Broker ingest stores **ETF buy/sell rows only** in `transactions` (Trade Republic
CSV via `e1f transactions trade-republic` or `tr`; XTB Excel Cash Operations via
`e1f transactions xtb` — see `ADR/ADR-0004_broker_transaction_import.md` and
`ADR/ADR-0006_xtb_cash_operations_excel_import.md`). Cash, dividends, and non-ETF
trades are skipped. Ingest does not modify `etf_universe.yaml`. After ingest it
lists ETF ISINs from the file that are not yet in the config, with a ready-to-run
`e1f config add …` line (XTB also reports unmapped tickers). Re-ingesting the
same file is idempotent (duplicate `(broker, transaction_id)` rows are skipped).
Use `e1f transactions list` to view stored trades.

Holdings and cost basis: `ADR/ADR-0005_portfolio_holdings_from_transactions.md`
(output in `src/e1f/portfolio.py`). `--show-cost-basis` adds an FX-converted EUR
market value and market-value-weighted fee / TER (`ADR/ADR-0032_portfolio_fx_market_value.md`);
holdings with no price or FX are excluded from those totals with a warning.
`--show-status` / `--explain` add opt-in provenance disclosure (ADR-0014; see below).

Performance and returns: `ADR/ADR-0011_performance_command.md` (output in
`src/e1f/performance.py`). `e1f performance` values holdings in EUR
and reports per-holding and portfolio-wide valuation, return, risk, and
attribution metrics. `--as-of DATE` values a past snapshot; unavailable or
carried-forward inputs are disclosed rather than silently fabricated.
`--show-status` / `--explain` add opt-in provenance disclosure (ADR-0014).
Metric definitions, formulas, interpretation, and complementary reads live only
in `data/glossary.md` (`e1f glossary <term>`).

Concentration (experimental tier, ADR-0024): `ADR/ADR-0012_concentration_command.md`
(output in `src/e1f/experimental/concentration.py`). `e1f concentration` reports
each held fund's *within-fund* concentration — by security, sector, and asset
class — against an explicit coverage denominator, reading the look-through
snapshots `e1f lookthrough` caches from yfinance `funds_data` (so it runs offline). Because that
source names only the top-10 holdings, the security dimension is reported as an
**observed** figure plus **rank-constrained bounds** on the unobserved tail
(math in `src/e1f/experimental/concentration.py`), never a false-precise point
value; sector and asset-class weightings are complete and reported as point
values. Each metric carries a four-state status — CALCULATED / BOUNDED /
UNAVAILABLE / UNRESOLVED — and `--explain` reconstructs each figure's provenance
(Result / Inputs / Method / limited-by) from the immutable snapshot rather than
a logged audit trail. Region is UNAVAILABLE (no reliable free source; never
inferred from swap collateral). This is deliberately **not** portfolio
diversification analysis: cross-fund single-name overlap is not asserted here —
matching names surface only as an *unresolved candidate* signal; summing a
reviewed identity across funds is `e1f overlap` (`ADR/ADR-0013_overlap_command.md`).
Look-through is stored in `holdings_snapshot`, `holding`, and `security_alias`
tables (immutable, append-only; schema in `src/e1f/experimental/common.py`).

Correlation: `ADR/ADR-0015_correlation_command.md` (output in
`src/e1f/correlation.py`). `e1f correlation` measures return **co-movement** — the
statistical axis of redundancy that look-through data cannot see. Where `overlap`
asks what the funds *hold* in common, `correlation` asks how they *move* in common;
the two stay separate commands so statistical co-movement is never mistaken for
established shared holdings. Each fund pair is correlated over its own shared window
(an exact-date inner join of the two funds' EUR daily returns), so a young fund
neither drops out nor truncates every other pair; a pair below the minimum
overlap, or with a degenerate sample, is reported UNAVAILABLE with an explicit
reason and its window and `n`, never as a point estimate. Thresholds
(`--rho-flag`, `--weight-flag`, `--min-overlap`, `--cluster-rho`) live in
`src/e1f/correlation.py`. It reports (a) redundant pairs — high correlation
*and* real combined EUR weight (both tunable), sorted by `ρ × weight` — and
(b) a hierarchical clustering (average linkage) run only over funds with a
valid distance to *every* peer, so no fabricated distance ever reaches the
linkage and one sparse fund can shrink the clustered set. `--explain` reconstructs each flagged pair
from source data (a bounded preview of the aligned return vectors plus a digest),
never a persisted result; naming two held ISINs (`--explain ISIN_A ISIN_B`)
reconstructs just that pair on demand, at any status and regardless of the flag
thresholds. Each ISIN in the report is annotated with its fund name from the
universe config (`--config`). This command adds a runtime dependency on scipy (used only
for the clustering step; Pearson ρ is computed in pure NumPy).

Rebalance: `ADR/ADR-0016_rebalance_command.md` (output in `src/e1f/rebalance.py`).
`e1f rebalance` is the family's one **prescriptive** command — but arithmetic, not an
optimizer: the user names target weights and it computes the unique **buy-only**
plan (never selling; an overweight holding is diluted, never trimmed) that reaches
them at the minimum fresh cash `C_min`. Targets are percents of the **whole valued
book**, so they need not sum to 100% — the remainder is a residual shared pro-rata
by current EUR value among untargeted holdings. The report opens with a target recap
(the target sum, and each target scaled to 100% among the targeted funds), then the
plan table — one row per fund in (valued held ∪ targeted), each fund's `Buy€`, the
binding fund(s) that force `C_min`, and a `TOTAL`. `--months N` slices the plan into
N equal monthly buys (a today's-prices snapshot — re-run to refresh). Infeasible
targets are reported UNAVAILABLE with the reason and fix (never an approximate plan
that hides a sale), exit code `0`. Provenance is opt-in per ADR-0014
(`--show-status` / `--explain`).

Scenarios: `ADR/ADR-0017_scenarios.md` (`src/e1f/scenario.py`; shared I/O in
`src/e1f/common/scenarios.py` and the rebalance plan core in
`src/e1f/common/rebalance.py`). `e1f scenario` saves named ISIN:pct
baskets in one gitignored `data/scenarios.yaml` — CRUD only (`save` / `list` /
`show` / `delete`); it never runs an analysis. The two consumers recall a basket
with `--scenario NAME`: `rebalance` loads its targets and stored `months` (a
`--months` / `--as-of` typed on the CLI overrides), while `correlation` correlates
the **post-rebalance** portfolio the basket implies — targeted funds at their
targets, untargeted funds diluted, weighted by final EUR value — answering "how
correlated is the book I'd hold *after* this rebalance?". A basket fund need not be
held yet (it needs only price history); an infeasible implied rebalance is reported
UNAVAILABLE. This is why the buy-only plan core graduated into `common`, so
`correlation` can run the plan without importing `rebalance` (ADR-0003).

Backtest (experimental tier, ADR-0024): `ADR/ADR-0019_backtest_command.md`
(`src/e1f/experimental/backtest.py`; sim core in
`src/e1f/experimental/common.py`; XIRR in `src/e1f/common/metrics.py`).
`e1f backtest --isin X` runs contribution-timing strategies over one ETF's real
EUR daily-close history and asks whether shifting a fixed monthly budget toward
dips beats a constant DCA. A dip-reserve keeps ∑ contributions equal across
strategies (invariance by construction). Headline metrics are terminal wealth
and XIRR versus lump-sum / constant-DCA / cash-drag. It is an evaluator, not an
optimizer — strategies are tabulated, never fitted on the same history. Flags
and defaults: `e1f backtest --help`.

Blind-deployment controls (`ADR/ADR-0020_blind_deployment_control.md`) isolate
*why* a dip wins or loses (reserve cost / deployment benefit / timing benefit /
total) and can add a `blind-random` distribution.

A **daily dip-slice** strategy (`ADR/ADR-0021_daily_dip_slice_strategy.md`) and a
**carry-forward** variant (`ADR/ADR-0023_daily_dip_carry_strategy.md`) probe
within-month timing with no cross-month reserve. **MaxDD** is sampled daily
(`ADR/ADR-0022_daily_drawdown_sampling.md`). `--isin` is required: a missing or
unknown ISIN prints the candidate series with their spans, and a series with no
EUR/FX rate is refused rather than mis-valued.

Seasonality (experimental tier, ADR-0024): `ADR/ADR-0026_calendar_seasonality.md`
(`src/e1f/experimental/seasonality.py`). `e1f seasonality --isin X` describes all
twelve calendar months of one ETF's EUR month-end returns, tests whether those
differences exceed a label-shuffling null, and only evaluates a seasonal rule when
one is pre-specified (or frozen on a training window and scored on a later one).
`e1f seasonality --portfolio` asks whether a common month pattern appears across
the configured universe (inferential cohort only) and prints a balanced
equal-weight book. `e1f seasonality --isin X --evaluate` scores the frozen
August/November contribution rules against DCA. It does not modify the dip
strategies. Flags: `e1f seasonality --help`.

Provenance disclosure: `ADR/ADR-0014_provenance_generalization.md`. `concentration`
and `overlap` always speak the shared provenance vocabulary — a four-state `Status`
(CALCULATED / BOUNDED / UNAVAILABLE / UNRESOLVED) and a per-metric contract. ADR-0014
extends the *same* vocabulary to `performance` and `portfolio`, where it is **opt-in**
so the default table (with its `~` / `*` / `n/a` markers) stays byte-for-byte
unchanged: `--show-status` adds a lightweight `Status` column and `--explain` adds the
verbose per-metric block (Result / Inputs / Method / limited-by) and implies the
column. No new numbers are computed — this is disclosure only.

All sources (OpenFIGI, ftgo, yfinance) are fetched with retry-on-failure:
rate limits (HTTP 429) and server errors are retried with backoff, honoring
the server's `Retry-After` header when present and falling back to
exponential backoff otherwise.

## Development

```bash
uv sync --extra dev
./scripts/check.sh          # lint, layers, shell, actions, types, dead, package, mutation, test + coverage
uv run pytest               # tests only
```

The full check requires `shellcheck` and `actionlint`; a missing gate tool is a
failure, not a skipped success. On macOS: `brew install shellcheck actionlint`.
