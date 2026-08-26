# ADR-0019 — `backtest` command (contribution-timing backtest)

**Scope:** a new read-only `backtest` subcommand that runs one or more
**contribution-timing strategies** over the real EUR daily-close history of a
**single** ETF and reports, per strategy, the terminal wealth, money-weighted
return (XIRR), and interim drawdown — so the standing question *"does shifting
predetermined monthly contributions toward market dips beat a constant DCA,
after paying the opportunity cost of holding cash?"* can be answered on evidence
rather than intuition. One self-contained module `src/e1f/backtest.py`, joining
the command siblings under ADR-0003's `cli → command modules → common` contract.
The period-loop / reserve arithmetic and the XIRR solver live in `common`
(decisions 2, 9); the command owns EUR-series assembly, the CLI, and rendering.

Slogan: **same total money, different timing — hold the budget fixed and only
change *when* it is deployed, then measure what timing was worth.**

## Context

Every command so far values or reshapes the *current* book. `backtest` is the
first that runs a **simulation over history**: it takes a price series and a
contribution rule and rolls a portfolio forward month by month. That is a
different shape of tool, and it carries a different failure mode — a backtest can
lie by construction (survivorship, look-ahead, overfitting, an unfair cash
assumption) far more easily than a point-in-time valuation can.

The governing invariant of the family still applies —

> **No analytical result may imply information that its provenance does not
> establish.**

— and for a backtest its provenance is exactly three things: **which real price
series** was used, **over which dates**, and **under which assumptions** (reserve
return, fees, fills). The command makes all three loud, on every run, because the
whole value of the exercise evaporates if any of them is silent.

Two temptations are refused outright, because they are the classic ways a
backtest deceives:

1. **Synthetic / spliced history.** The plan's ambition is to span 2000–02,
   2008, 2020, 2022. The DB's longest genuine all-world series begins **2011-05**
   (SPDR ACWI IMI acc) / **2012** (Vanguard FTSE All-World), so it cannot reach
   the dot-com crash. Rather than manufacture history from a proxy index, v1
   **uses only real stored prices** and **reports the actual span and which
   crashes fall inside it**. A long proxy-index splice (MSCI/FTSE total-return
   back to ~1988) is real research with its own methodology decisions
   (total-return vs price, dividend handling, splice point) and is **deferred to
   v2** (its own ADR).

2. **In-sample overfitting.** With a grid of drawdown thresholds it is trivial to
   "discover" a dip rule perfectly fitted to 2008/2020/2022 and then report that
   same history as its evidence. v1 therefore **never fits or ranks a strategy**:
   it evaluates *pre-specified* rules only. Any future parameter search **must be
   walk-forward / out-of-sample**, and is deferred to v2 — where the proxy
   history gives enough independent folds for it to mean anything (with ~2011+
   real data it would yield ~2–3 folds, which would manufacture false confidence,
   the very trap this refusal exists to avoid).

## Decision

### 1. It is a backtest, not an advisor — and it runs on ONE real series

`backtest` answers a research question about the past. It does **not** tell you
what to invest this month (a forward `--as-of today` advisor is a trivial later
derivative and is out of scope). It runs over **exactly one** ETF's price
history — the "one all-world ETF" framing that turns an allocation problem into a
pure contribution-*timing* problem. The series is chosen by the user, never
guessed:

- **`--isin` is required. There is no default.** The universe has no
  "all-world" tag (`asset_class` is only `Equity`), so auto-selecting "the
  all-world fund" would silently backtest whatever regional fund happened to have
  the longest history — a footgun for a tool whose entire thesis is the
  all-world case. A missing/unknown `--isin` prints the **candidate list** —
  every ISIN that has a stored price series, with its span, distribution, and
  name — and exits non-zero. The choice of series *is* the most consequential
  input; the user makes it consciously.

### 2. Model B — the reserve, and invariance by construction

The scientific constraint (∑ contributions identical across strategies, else the
test measures "more money = more money", not timing) is enforced **structurally**
by a dip-**reserve** accounting model, not by a year-end true-up.

Each month the investor commits a fixed `C` (`--contribution`, default €1000). A
base fraction `β` (`--base-fraction`, default 0.75) buys shares immediately; the
remainder `(1−β)·C` accrues to a **reserve**. A deployment rule (decision 3)
pulls `deploy ∈ [0, reserve]` from the reserve each month to buy *extra* shares.
Because deployment is bounded by the reserve balance, the reserve can never go
negative and the investor can **never spend money not yet committed**. Total
committed over `N` months is `N·C` for *every* strategy regardless of deployment
timing — invariance holds by construction. Any reserve **left unspent at the
horizon is counted as cash** in terminal wealth (decision 4).

The **invariant**, asserted directly as the centerpiece test:

> With `--cash-rate 0`, for any `(β, a, b, D0)` and any price series,
> `total_invested (= N·C) == equity_cost + leftover_reserve_cash` exactly. If it
> ever fails, the accounting is broken.

Model A ("DCA acceleration": invest more now, reduce later to hold an annual
budget) is **not** built separately — it is expressible within Model B (a large
`a` deploys the reserve fast), and its invariance would only hold per-annum via a
true-up. The §7-style caps `[0.5C, 3C]` need no separate machinery: `β ≥ 0.5`
guarantees the floor and a finite reserve guarantees the ceiling.

### 3. The deployment rule — fraction of the current reserve, nonlinear in drawdown

At each monthly fill the drawdown `D` (decision 5) maps to a **fraction of the
current reserve** to deploy:

```
D_eff  = max(0, D − D0)                       # D0 = --deadzone (default 0)
f      = clamp(a · D_eff^b, 0, 1)             # a = --aggressiveness, b = --curvature (default 2)
deploy = f · reserve_balance
```

Four knobs `(β, a, b, D0)`. `b = 2` (nonlinear) ignores ordinary 5–10% noise and
leans into deep crashes; `clamp(·, 1)` is the natural ceiling (you cannot deploy
more than the reserve you hold); a deep enough crash empties the reserve (the
plan's ">30% → deploy the remainder"). This is the "fraction-of-reserve"
formulation, coherent with the reserve model — **not** a `(M(D)−1)·C` multiple,
which is the Model-A framing set aside in decision 2.

### 4. Cash assumptions — 0% reserve by default, leftover cash counted

The opportunity cost of holding the reserve is the *point* of the experiment, so
it is modelled, not hidden:

- **Idle reserve earns 0% by default**, `--cash-rate ANNUAL` (Actual/365, accrued
  between events) to sensitivity-test. 0% is the honest conservative case and
  needs no new data; it does pre-load the dial toward "dip underperforms", which
  the ADR states as an assumption rather than burying. A realistic risk-free
  *series* is deferred (like the proxy — its own decision).
- **Terminal wealth always = equity value + leftover reserve cash**, reported as
  two components so cash drag is visible. Excluding leftover cash would penalise
  the dip strategies for money that simply never got deployed — an unfair
  comparison of equal-total contributions.
- **XIRR** is computed on the monthly **contribution outflows** (`−C` each,
  whatever the split) plus the terminal inflow (equity + cash). Money-weighted is
  the right lens because contribution timing *is* the variable under test; it
  penalises late/never deployment with no rate assumption at all. **TWR is
  omitted** precisely because it strips out contribution timing — it would be
  near-identical across strategies and misleading here.

### 5. The signal — drawdown from a trailing high, on the EUR series

```
D_t = max(0, 1 − P_t / high_t)      P = EUR close;  high = max over a trailing window
```

- `--drawdown-ref {rolling-high, all-time-high}`, default `rolling-high` with
  `--lookback DAYS` (default 252 ≈ 12 months). A rolling high self-heals after a
  long bear (a decade-old peak stops reading as "cheap" — the fatal flaw of ATH);
  `all-time-high` is simply the special case `lookback = ∞` (expanding max).
- The high and drawdown are computed on the **EUR-converted** series, because an
  EUR investor experiences and deploys in EUR. EUR funds pass through; USD funds
  convert at the nearest-prior EUR/USD rate (all funds are EUR/USD; FX covers the
  whole span, ADR-0010). A day with no usable FX/price is dropped from the series.
- **Warm-up burn (decision 7):** the first contribution waits until a full
  `lookback` window of prior closes exists, so the signal is never computed on a
  biased partial window. ATH needs no warm-up.

### 6. The compared set — fixed benchmarks + configurable dip strategies, never ranked

Every run shows three zero-config benchmarks so it is self-contextualising, plus
the dip strategy under test:

- **lump-sum** — the whole horizon budget `N·C` invested at `t0` (the
  "time-in-the-market" ceiling); a benchmark, not a claim the cash existed at
  `t0`.
- **constant-DCA** — `β = 1` (reserve always empty); the baseline everything is
  judged against.
- **cash-drag control** — `β < 1, a = 0` (reserve accrues, never deploys);
  isolates the pure cost of setting money aside and never using it.
- **dip strategy(ies)** — built from `(β, a, b, D0)`; `--strategy` is repeatable
  to sweep several configs in one table.

The command **only tabulates** — it never selects, ranks, or recommends a
"winner", and prints an anti-overfit note saying so. This is the structural
expression of the decision to refuse in-sample fitting (Context §2). The
`--window` sweep (decision 8) reports the outcome *distribution* of a **fixed**
rule across start dates — a robustness check on a pre-specified strategy, not a
fit.

### 7. Fill mechanics — monthly, fractional, frictionless

- **Monthly** contribution anchored to the 1st, filled at the **first close
  on-or-after** (reusing the nearest-prior/-after as-of convention). Deployment
  is evaluated at the same monthly cadence (a within-month V-shaped dip that
  fully recovers is not seen — a stated limitation).
- **Fractional shares** (`amount / price`); no lot rounding — the timing effect
  is what is under test, not lot-size noise.
- **No fees, taxes, or spread** — broadly proportional to amount invested, they
  would muddy a pure timing comparison; v1 is deliberately theoretical (stated
  assumption).
- **Close series used as-is.** For an **accumulating** fund the close is already
  total-return, so an accumulating series is the recommended default and a
  **distributing** series triggers a warning (its price understates total return).
- **Effective start** = `max(--from, series_start + warm-up)`; **horizon end** =
  `--to` (default today), via `load_price_series`' as-of cap. A run/window with
  fewer than `BACKTEST_MIN_CONTRIBUTIONS` (12) usable months is refused with a
  clear message, never a garbage result.

### 8. Windowing — single run by default, `--window N` sweeps the distribution

- **Default (no `--window`)**: one run over the full usable span, with per-strategy
  detail — the readable, `--explain`-friendly mode.
- **`--window N`** (months): step the first contribution monthly across every
  feasible start, run all strategies in each window, and summarise — per dip
  strategy vs constant-DCA: **win-rate, median / worst / best** excess terminal
  wealth and excess XIRR. The window count and covered span are reported, with an
  explicit warning when N is too few to be meaningful (overlapping windows are
  not independent; ~19 years of any single series is a handful of samples).
- **`--from` / `--to`** pin an explicit span; the coverage line names which known
  crashes fall inside it and which are excluded — no crash dates are special-cased
  in the math, only reported.

### 9. `common` gains the pure core (a downward graduation) — the command stays thin

Two things move into / live in `common`, so the arithmetic is unit-testable in
isolation and no command imports another (ADR-0003):

- **XIRR graduates down** from `performance`. `_npv`, `_npv_derivative`,
  `_newton`, `_bisect`, `xirr` relocate to `common`; `performance` imports `xirr`
  from `common` (re-exporting it — its public surface is unchanged), and the
  backtest core uses it for per-strategy IRR. This is a clean relocation of a
  well-tested, dependency-free solver, mirroring ADR-0013's graduation of the
  valuation core. Its pure unit tests move to `test_common.py` (its new home).
- **The backtest core is new in `common`**: `StrategyParams`, `SignalSpec`,
  `BacktestResult`, `running_high`, `deployment_fraction`, `monthly_fill_indices`,
  and `simulate_strategy` (the month-by-month reserve/deploy/buy loop, returning
  one `BacktestResult`). Pure functions over parallel `(dates, closes)` lists — no
  DB, no IO — so the invariance invariant and every degenerate case are tested
  without fixtures-on-disk.

`backtest.py` owns: `--isin` validation + candidate listing, EUR-series
assembly (`load_price_series` + nearest-prior FX), the warm-up / start / end
index arithmetic, strategy-preset construction, the `--window` orchestration,
rendering, and `--explain`. It **copies** the money/percent formatters locally
(as the siblings do — importing them would be a command→command edge) and imports
from `common` only the shared core above plus `ConfigManager`, `Status`,
`MetricContract`, `_explain_metric`. It **reads** prices/FX/config only; it writes
nothing and adds no table.

Layer placement is unchanged — `backtest` joins the command siblings:

```toml
layers = [
    "e1f.cli",
    "… | e1f.rebalance | e1f.scenario | e1f.backtest",   # backtest joins the siblings
    "e1f.common",
]
```

### 10. Provenance — `--explain` is the primary mechanism

Following ADR-0014, provenance is off by default and opt-in. This command is
**unusually assumption-laden**, so `--explain` earns its keep more here than
anywhere: it emits one block stating series + span + effective start, currency,
reserve rate (0% or `--cash-rate`), no-fees, accumulating-vs-distributing, the
signal definition, the window count, and the small-N / no-fit caveats. A normal
run is `CALCULATED`. `--show-status` adds a Status column but is thin here (there
is no per-row data-quality tiering); it is wired only if a natural per-strategy
status emerges (e.g. a window truncated by data), otherwise `--explain` carries
the provenance. The metric contract:

```python
MetricContract(
    method_version="contribution_timing_backtest_v1",
    requires=("a daily EUR close series spanning the lookback + a minimum contribution count",),
    does_not_require=("return forecasts", "look-through holdings", "a covariance estimate",
                      "synthetic or proxy history"),
    supports=("terminal wealth & XIRR per strategy", "excess vs constant-DCA",
              "rolling-window outcome distribution"),
    limitations=(
        "one real ETF price series — no proxy/synthetic history (v1; proxy deferred to v2)",
        "reserve earns 0% by default (--cash-rate to sensitivity-test)",
        "no fees/taxes/spread; fractional shares; monthly fills",
        "evaluator only — strategies are pre-specified, never fitted or ranked in-sample "
        "(walk-forward deferred to v2)",
        "distributing funds understate total return — prefer an accumulating series",
        "overlapping windows are not independent; small N is disclosed, not overcome",
    ),
)
```

## Rationale

- **One real series, coverage disclosed.** Refusing synthetic history keeps every
  number traceable to prices e1f actually stores; reporting the span and the
  in/out crash list makes the ambition-vs-data gap honest instead of hidden
  (Context §1, decision 8).
- **Invariance by construction.** The reserve model makes "same total money"
  structural, so the comparison measures timing, not size — the one thing that
  makes it science (decision 2).
- **The cash cost is modelled, not buried.** 0% reserve + leftover-cash-in-wealth
  + money-weighted XIRR mean a dip strategy is charged for idle cash exactly as
  reality would (decision 4).
- **It never fits.** Evaluating pre-specified rules and refusing to rank a winner
  makes the in-sample overfitting trap structurally impossible; walk-forward waits
  for the v2 history that could actually support it (Context §2, decision 6).
- **Thin command over a pure core.** The month-loop and XIRR live in `common` and
  are tested without IO; the command is assembly + rendering (decision 9).

## Consequences

- One new flat module `src/e1f/backtest.py`; wired into `cli.py`
  `COMMANDS` / `PARSER_FACTORIES`, the CLI epilog,
  `tests/test_contracts.py::test_cli_commands_surface`, and the import-linter
  `layers` command siblings (one line, no new layer).
- **`common.py` gains** the XIRR solver (graduated from `performance`) and the
  pure backtest core; **`performance.py` imports `xirr` from `common`** and drops
  its local copy (public surface unchanged — `from e1f.performance import xirr`
  still resolves). The pure XIRR/Newton/bisection unit tests move from
  `test_performance.py` to `test_common.py`.
- No new storage — `backtest` only *reads* prices, FX, and config.
- Tests (pure core in isolation, synthetic series as fixtures):
  - **invariance property test (centerpiece)** — for random `(β,a,b,D0)` and
    random series, `total_invested == equity_cost + reserve_cash` and `== N·C`;
  - reserve non-negativity and `deploy ≤ balance` every month;
  - degenerate equivalences — `β=1` ≡ constant-DCA (reserve always 0); `a=0` ≡
    cash-drag (all reserve leftover); a monotone-rising series ⇒ dip never
    deploys and ties DCA; lump-sum invests all at `t0`;
  - a hand-computed golden series (terminal wealth to the cent);
  - `running_high` rolling vs ATH; `deployment_fraction` dead-zone / clamp / curvature;
  - `monthly_fill_indices` — one fill per month, first close on-or-after, dedup;
  - warm-up burns exactly `lookback` days; a too-short span is refused;
  - `--cash-rate > 0` grows the reserve and lifts leftover cash;
  - CLI: missing/unknown `--isin` prints candidates and exits non-zero; a
    distributing series warns; `--window N` emits the distribution summary;
    `--explain` emits the single provenance block; a `--db` end-to-end run.
  Coverage floor 90%.
- README gains a `backtest` behaviour description and CLAUDE.md's Layout /
  Key-decisions gain the module and this ADR **when the code lands**.

## Deferred (not in this ADR)

- **Proxy-index history (v2).** Splicing a long total-return all-world index
  (MSCI/FTSE, ~1988+) behind the real ETF so 2000–02 and true 20-year windows are
  covered. Needs its own decisions (total-return vs price, dividend handling,
  splice methodology) and its own ADR.
- **Walk-forward / out-of-sample parameter search (v2).** Any mode that *chooses*
  `(β,a,b,D0)` from data must fit in-sample and score out-of-sample, rolling
  folds; it is meaningful only once the proxy history supplies enough independent
  folds. v1 is a non-ranking evaluator by deliberate refusal.
- **Trend overlay (v1.1).** The plan's second signal — distance below a moving
  average (`P/MA_{12m} − 1`) multiplying the deployment fraction — is a separate
  signal with its own MA-lookback and combining-function decisions. Slots in later
  as `--trend-lookback` without reworking the engine.
- **Forward advisor** — `--as-of today` printing this month's suggested
  contribution from the current drawdown. A trivial derivative once the rule
  exists; out of scope for a backtest command.
- **A realistic risk-free reserve *series*** (ECB €STR / T-bill) instead of the
  flat `--cash-rate`; and **weekly/quarterly cadences**. Cheap extensions of the
  same core.
