# ADR-0023 — carry-forward daily dip-slice strategy (within-month timing)

**Scope:** add a **carry-forward** sibling of the ADR-0021 daily dip-slice
strategy to the `backtest` command. ADR-0021 places **one** slice on each down
day; this variant lets unspent slices **accumulate** and spends the **whole
accrued pool** on the next down day. It reuses ADR-0021's CLI knob (`--slices N`
/ `n=N`), its full-within-the-month deployment, and its no-cross-month-reserve
property; only the per-day spend rule changes. No new module — it joins the
existing ADR-0003 `cli → command modules → common` contract.

Slogan: **same monthly budget, still sliced across the month — but hold each
slice back until a down day, then buy every one you've saved up.**

## Context

ADR-0021 answered "given I commit `C` this month regardless, is it better to
drip it in on the month's down days than to buy the whole `C` on the 1st?" Its
rule spends **exactly one slice (`C/N`) per down day**, with a catch-up rule and
a last-day dump guaranteeing full deployment. That paces the euros evenly across
the dips of a month but never concentrates them: three quiet up-days followed by
one sharp down-day still buy just one slice on the dip, the same as a lone
down-day would.

A distinct within-month question is left open:

> If I am going to buy on the down days anyway, should a down day that follows a
> run of up-days buy **more** — the slices I *would* have placed on those up-days,
> saved up — rather than a single slice?

This is still **within-month timing only**, and still carries **no cross-month
cash**: the last trading day flushes whatever is unspent, so each month deploys
its full `C`. It is therefore another true sibling of constant-DCA (identical
`∑ = N·C`), differing only in *which days of the month* the euros land on — a
clean, low-variance timing probe, exactly as ADR-0021 framed daily-dip. The
governing invariant is unchanged: **no analytical result may imply information
its provenance does not establish**, and the anti-overfit stance of ADR-0019 §6
holds — this strategy is *evaluated*, never fitted or ranked in-sample.

## Decision

### 1. The rule — accrue a slice a day, flush the pool on a down day

Each month commits `C` (`--contribution`), cut into `N` equal slices of `C/N`
(`--slices N`, default 20, shared with ADR-0021). One slice **accrues per trading
day**. Walking the month's trading days in order, on each day (except the last):

```
is_dip = close_today < close_prev_trading_day        # a down day
pool   = (slices accrued so far) − (slices already spent)   # accrued, unspent
buy the whole pool  iff  is_dip and pool > 0
```

- **`is_dip`** is measured against the **immediately preceding trading day's
  close** in the full EUR series — the same plainest reading of "the ETF dipped
  today" as ADR-0021. A day with no prior close (series start) is not a dip.
- **The pool includes today's own slice.** On a down day the strategy spends the
  day's slice *plus* every earlier day's slice that a prior buy has not yet
  consumed — "the slices from that day and the previous days that weren't spent."
  A down day therefore empties the pool to zero; the next accrual starts fresh.
- **Up days buy nothing** — the slice accrues and the pool grows.
- **The final trading day of each month flushes whatever budget remains**, so `C`
  is fully spent every month regardless of how the dips fell (a month with no
  down day at all deploys entirely on its last day).

Concretely, if days 1–3 are up and day 4 dips, day 4 buys 4 slices (days 1–4);
if day 5 also dips it buys 1 (only day 5 has accrued since); if days 6–9 are up
and day 10 dips, day 10 buys 5 (days 6–10). No catch-up rule is needed — the
accrual-plus-last-day-flush already guarantees full deployment.

The mechanics use a cumulative `released = min(C, (j+1)·C/N)` (slices accrued
through day `j`, capped once all `N` are out) and `pool = released − spent`, which
is always within `[0, budget]`; a down day spends `min(pool, budget)`.

Three things follow, all deliberate: (a) the strategy leans **harder** into a dip
that follows an up-run than ADR-0021's daily-dip does, so a single deep V-shaped
dip can absorb most of the month's `C`; (b) as with ADR-0021 there is **no
base-fraction split** — the whole `C` is sliced, so `β`, `a`, `b`, `D0` are all
inapplicable; (c) there is **no cross-month reserve**, so
`reserve_contributed = reserve_deployed = reserve_cash = 0`.

### 2. Invariance — unconditional, as in ADR-0021

Every month deploys exactly `C` (the last-day flush absorbs any accrued remainder
and any float residual), so `equity_cost = N·C = total_invested` and
`reserve_cash = 0` **for every price series and every `N`**, with no `--cash-rate`
caveat. The property test is extended to cover the mode alongside daily-dip.

### 3. Cash assumptions — no intra-month interest modelled

As in ADR-0021 §3, each month's uncommitted cash is held only for **days** before
deployment, so `--cash-rate` is **not applied intra-month** and the unconditional
`reserve_cash == 0` invariance is preserved. A daily-accrual refinement remains a
shared Deferred item on both cores.

### 4. It is a `DeployMode`, with its own daily loop sharing the result core

`simulate_strategy` gains `DeployMode.DAILY_DIP_CARRY`; `StrategyParams.slices`
is reused unchanged. The mode dispatches to a dedicated pure core
`_simulate_daily_dip_carry` in `common` that partitions the series into months
exactly as `_simulate_daily_dip` does and runs the day loop above. The two cores
share a new `_daily_dip_result` helper that assembles the identical
reserve-free `BacktestResult` (`reserve_cash` and reserve flows zero), so the
duplication between them is only the per-day spend logic that genuinely differs.
Keeping it a `DeployMode` means the command still calls `simulate_strategy`
uniformly and the row renders like any other.

### 5. CLI surface — reuse the daily-dip knobs

- **`--slices N`** and the `slices=N`/`n=N` `--strategy` overrides are shared with
  daily-dip; `deploy=daily-dip-carry` selects this variant. The auto label is
  `daily-dip-carry(N=…)`. Both variants can appear in one table, and several `N`
  of each can be swept together.
- **The default (no-`--strategy`) run is unchanged** — carry is opt-in, so no
  existing output or test moves.
- **Matched β controls (ADR-0020) are not generated** (no reserve), and the mode
  is excluded from `_distinct_betas` / the reserve decomposition / blind-random
  blocks, sharing daily-dip's `_DAILY_DIP_MODES` exclusion set.
- **`--window N` includes carry** as a comparison-vs-DCA row, alongside daily-dip
  and any signal dips.
- **Warm-up is skipped when no strategy consults the drawdown signal** — a
  carry-only run needs no `--lookback` history, exactly as for daily-dip.

### 6. Provenance

`--explain` records the carry contract (one slice accrued per trading day, a down
day spends every accrued-but-unspent slice, last-day flush, no intra-month cash
rate) as its own clause, distinct from daily-dip's. `method_version` stays
`contribution_timing_backtest_v1` — the EUR valuation is unchanged and this is an
added *strategy*, not a new valuation method. The anti-overfit note stands.

## Rationale

- **It isolates a sharper within-month timing question** than ADR-0021 — dip
  *concentration*, not just dip *bias* — and the two are directly comparable in
  one table because they share `∑ = N·C` and the same last-day flush.
- **Invariance is as strong as ADR-0021's** — full monthly deployment makes
  `reserve_cash` identically zero, no cash-rate asterisk.
- **Thin command over a pure core**, tested without IO; the shared
  `_daily_dip_result` keeps one home for the reserve-free result shape.
- **It never fits.** Sweeping `N` (or comparing against daily-dip) reports a
  distribution of pre-specified rules; no rule is selected or ranked as a winner.

## Consequences

- **`common.py` gains** `DeployMode.DAILY_DIP_CARRY`, the pure
  `_simulate_daily_dip_carry` core, and a shared `_daily_dip_result` helper that
  `_simulate_daily_dip` now also uses; `simulate_strategy` dispatches to it. No
  new module; ADR-0003 untouched. `StrategyParams` is unchanged (`slices` reused).
- **`backtest.py` gains** the `daily-dip-carry(N=…)` label, `deploy=daily-dip-carry`
  parsing, a `_DAILY_DIP_MODES` set shared by the β-control exclusion and the
  `--window` dip selection, and a distinct `--explain` provenance clause.
- **Tests:** a golden month verifying a dip flushes the accrued pool and the last
  day flushes the remainder; a contrast test showing carry buys strictly more than
  daily-dip when a deep dip follows an up-run; invariance extended to the mode
  (`reserve_cash == 0` for random `N` and series); parse/label, no-β-controls, a
  `--db` end-to-end run with the provenance clause, and a `--window` run including
  it. Coverage floor stays 90%.
- **README** gains a carry description and example; **CLAUDE.md** Layout /
  Key-decisions gain this ADR.
- No new storage — `backtest` still only *reads* prices, FX, and config.

## Deferred (not in this ADR)

- **Intra-month cash accrual**, **dip magnitude weighting**, and **sub-monthly
  `∑` framing / blind controls in `--window`** remain deferred exactly as in
  ADR-0021 — they apply identically to this core.
- **A "spend the pool only past a threshold size" rule** (e.g. hold until the
  accrued pool exceeds `k` slices) is a natural knobbed generalization of both
  daily-dip variants; not built, no evidence it is worth the extra parameter.
