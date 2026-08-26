# ADR-0021 — daily dip-slice contribution strategy (within-month timing)

**Scope:** add a **within-month, daily-cadence** contribution strategy to the
`backtest` command. Every strategy so far (ADR-0019/0020) works at **monthly**
cadence and holds cash in a **cross-month reserve**; this one spends each month's
budget *inside* that month, one small slice at a time, biased toward down days.
The pure simulator graduates into `common` alongside the existing core; the
command owns the CLI knob, the strategy construction, and rendering. No new
module — it joins the ADR-0003 `cli → command modules → common` contract.

Slogan: **same monthly budget, sliced across the month — buy the down days, and
never carry cash into next month.**

## Context

ADR-0019's reserve model answers "should I hold cash back across months and
deploy it on deep drawdowns?" The answer on real 2011–2026 history was *no* (the
reserve's drag dominates its timing benefit, ADR-0020). But that is a statement
about a **cross-month** reserve rule keyed to a rolling-high drawdown. A distinct,
much smaller-stakes question is left unanswered:

> Given I am committing `C` this month regardless, is it better to drip it in on
> the month's down days than to buy the whole `C` on the 1st?

This is **within-month timing only**. It carries **no cross-month cash** — each
month deploys its full `C` before the month ends — so it is a true sibling of
constant-DCA (identical `∑ = N·C`), differing solely in *which days of the month*
the euros land on. That makes it a clean, low-variance timing probe that the
reserve machinery cannot express (the reserve deploys at the monthly fill only;
a within-month V-shaped dip is invisible to it — ADR-0019 §7, stated limitation).

The governing invariant still binds: **no analytical result may imply information
its provenance does not establish**, and the anti-overfit stance of ADR-0019 §6
is unchanged — this strategy is *evaluated*, never fitted or ranked in-sample.

## Decision

### 1. The rule — slice the month, buy the down days, finish inside the month

Each month commits `C` (`--contribution`). The month's budget is cut into `N`
equal slices of `C/N` (`--slices N`, default 20). Walking the month's **trading
days** in order, on each day (except the last):

```
is_dip   = close_today < close_prev_trading_day      # a down day
catch_up = trading_days_left ≤ slices_left           # running out of days to place slices
buy one slice (C/N)  iff  (is_dip or catch_up) and budget remains
```

- **`is_dip`** is measured against the **immediately preceding trading day's
  close** in the full EUR series (not a rolling high) — the plainest reading of
  "the ETF dipped today". The day before the first month can precede the test
  start; that is fine, it is only price data. A day with no prior close (series
  start) is treated as not-a-dip.
- **`catch_up`** is the deadline pressure. As days elapse, `trading_days_left`
  falls by one each day while `slices_left` falls only on a buy; the gap
  `days_left − slices_left` therefore decreases by 1 (no buy) or 0 (buy) per day.
  Starting positive (when `N ≤ M`, the month's trading-day count), it first hits
  exactly zero, at which point catch-up fires every remaining day and holds it at
  zero — so **exactly `N` slices deploy by month-end with no leftover**, by the
  same gap argument that makes the reserve model's invariance structural.
- **The final trading day of each month deploys whatever budget remains.** In the
  normal `N ≤ M` case that is ≤ one slice (usually zero); it exists to guarantee
  full deployment when `N > M` (more slices than the month has trading days — a
  degenerate config), where the month simply cannot spread `N` slices and dumps
  the remainder on the last day. Either way `C` is fully spent every month.

Four things follow, all deliberate: (a) if `N` dips arrive early, the budget is
exhausted early and the rest of the month is idle — that is the strategy leaning
into a falling month; (b) a month with no dips at all still fully deploys, via
catch-up + last-day, degenerating gracefully toward "buy late in the month"; (c)
there is **no immediate base-fraction split (`β`)** — the whole `C` is sliced, so
`β`, `a`, `b`, `D0` are all inapplicable; (d) there is **no cross-month reserve**,
so `reserve_contributed = reserve_deployed = reserve_cash = 0`.

### 2. Invariance — free, and stricter than the reserve model's

Because every month deploys exactly `C` (slices sum to `C`; the last-day dump
absorbs any float residual), `equity_cost = N·C = total_invested` and
`reserve_cash = 0` **for every price series and every `N`**, with no `--cash-rate`
caveat. The centerpiece invariance property test is extended to cover the mode;
it is the strongest form of the ADR-0019 conservation law (no leftover cash to
account for at all).

### 3. Cash assumptions — no intra-month interest modelled

The strategy holds each month's uncommitted cash only for **days** before
deploying it, so `--cash-rate` is **not applied intra-month** (its effect over a
few days is sub-basis-point and would muddy the clean invariance above). This is
stated in `--explain` and the ADR rather than buried: unlike the cross-month
reserve, this strategy carries no meaningful idle balance for a cash rate to act
on. A daily-accrual refinement is noted in Deferred, not built.

### 4. It is a `DeployMode`, but with its own daily loop

`simulate_strategy` gains `DeployMode.DAILY_DIP`; `StrategyParams` gains
`slices: int`. Rather than bolt a daily cadence onto the monthly reserve loop,
the mode dispatches to a dedicated pure core `_simulate_daily_dip` in `common`
that partitions the series into months (`[fills[k], fills[k+1]-1]`, last month
`[fills[-1], end_idx]`), runs the day loop above per month, and returns the same
`BacktestResult`. Keeping it a `DeployMode` means the command still calls
`simulate_strategy` uniformly and the row renders like any other; giving it its
own loop keeps the monthly reserve arithmetic untouched.

### 5. CLI surface — a knob to sweep `N`, and `--strategy` to place rows

- **`--slices N`** (default 20, `N ≥ 1`) sets the default slice count.
- **`--strategy "deploy=daily-dip"`** places a daily-dip row; `slices=N`/`n=N`
  overrides the count for that row, so several `N` are swept in one table:
  `--strategy "deploy=daily-dip,n=10" --strategy "deploy=daily-dip,n=40"`. The
  auto label is `daily-dip(N=…)`.
- **The default (no-`--strategy`) run is unchanged** — it still shows the
  ADR-0019 signal dip; daily-dip is opt-in, so no existing output or test moves.
- **Matched β controls (ADR-0020) are not generated for daily-dip rows** (it has
  no reserve, so cash-drag / blind-even are meaningless for it); it is excluded
  from `_distinct_betas` and from the reserve decomposition / blind-random blocks.
- **`--window N` includes daily-dip** as a comparison-vs-DCA row (win-rate,
  median/worst/best excess), alongside any signal dips.
- **Warm-up is skipped when no strategy consults the drawdown signal**: a
  daily-dip needs no `--lookback` history, so a daily-dip-only run starts at the
  series start (more data), while any run containing a signal dip keeps the
  ADR-0019 warm-up burn. All strategies in one run still share one start index,
  so the comparison stays controlled.

### 6. Provenance

`--explain` records the daily-dip contract (slices per month, dip = down-day vs
prior close, catch-up + last-day full deployment, no intra-month cash rate).
`method_version` stays `contribution_timing_backtest_v1` — the EUR valuation is
unchanged and this is an added *strategy*, not a new valuation method. The
anti-overfit note stands: the strategy is pre-specified and only tabulated.

## Rationale

- **It isolates within-month timing.** No cross-month cash means the daily-dip vs
  constant-DCA gap is purely "which days of the month" — the exact question the
  reserve model cannot pose (it acts monthly).
- **Invariance is stronger here.** Full monthly deployment makes `reserve_cash`
  identically zero, so the conservation law holds unconditionally, no cash-rate
  asterisk.
- **The catch-up + last-day rule guarantees full deployment** by a simple gap
  argument for `N ≤ M`, and degrades gracefully (last-day dump) for `N > M`.
- **Thin command over a pure core**, tested without IO, same as ADR-0019 §9.
- **It never fits.** Sweeping `N` reports a distribution of pre-specified rules;
  no `N` is selected or ranked as a winner.

## Consequences

- **`common.py` gains** `DeployMode.DAILY_DIP`, a `slices` field on
  `StrategyParams` (default 0), and the pure `_simulate_daily_dip` core;
  `simulate_strategy` dispatches to it. No new module; ADR-0003 untouched.
- **`backtest.py` gains** `--slices`, `deploy=daily-dip` + `slices`/`n` parsing,
  the `daily-dip(N=…)` label, daily-dip-aware strategy construction (excluded
  from β-matched controls, included in `--window`), and warm-up-skip when no
  signal strategy is present.
- **Tests:** invariance extended to `DAILY_DIP` (`reserve_cash == 0` exactly for
  random `N` and series); a golden month verifying which days buy; a monotone-up
  month (catch-up + last-day still fully deploys); `N > M` dumps the remainder on
  the last day; `--strategy "deploy=daily-dip,n=…"` parses and labels; a `--db`
  end-to-end run showing the row and a `--window` run including it. Coverage floor
  stays 90%.
- **README** gains a daily-dip description; **CLAUDE.md** Layout/Key-decisions gain
  this ADR when the code lands.
- No new storage — `backtest` still only *reads* prices, FX, and config.

## Deferred (not in this ADR)

- **Intra-month cash accrual.** Growing each month's undeployed slices at a daily
  risk-free rate before they are spent — negligible over days at today's rates,
  and it would forfeit the unconditional `reserve_cash == 0` invariance. Cheap to
  add on the same core if a realistic short-rate series ever lands.
- **Dip threshold / magnitude weighting.** Buying a bigger slice on a *bigger*
  down day (e.g. scale by the day's return), rather than one flat slice per down
  day. A separate signal with its own knobs; slots onto the same daily loop.
- **Sub-monthly `∑` framing in `--window`** and blind controls for daily-dip
  remain out of scope, as for ADR-0020's window mode.
