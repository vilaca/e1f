# ADR-0022 — daily-sampled interim drawdown in `backtest`

**Scope:** change the `backtest` command's **max-drawdown** column to be computed
on a **daily** value curve for every strategy, replacing the monthly-sampled
curve ADR-0019 shipped. Terminal wealth, XIRR, and all reserve accounting are
unchanged; only the interim-drawdown risk statistic changes. One edit to the pure
core in `common` (how each strategy's `value_curve` is built); no CLI, storage, or
layer change.

Slogan: **measure the drawdown an investor actually lived through — the crash
bottoms fall mid-month, so sample the value curve every day, not once a month.**

## Context

ADR-0019 reported `BacktestResult.max_drawdown` as the peak-to-trough of a value
curve sampled **once per month** (at each contribution fill). ADR-0021's daily-dip
work exposed a flaw in that: two strategies holding near-identical books reported
materially different drawdowns (22.0% vs 18.5%) purely because they sampled the
value curve on **different days of the month** (the monthly engine at the fill day
≈ the 1st; the daily-dip core, initially, at month-end). The crash troughs the
metric is meant to catch — COVID-2020 bottomed **2020-03-23**, mid-month — fall
*between* monthly samples, so monthly sampling both **undercounts** the drawdown
and makes it **incomparable across strategies** (whoever samples nearest the
bottom looks riskier, for no economic reason).

Measured on the real all-world series (SPDR ACWI IMI acc, IE00B3YLTY66):

| Strategy | monthly-sampled (old) | daily-sampled (new) |
|---|---|---|
| lump-sum | 23.3% | 34.5% |
| constant-DCA | 22.0% | 34.1% |
| daily-dip(N=20) | 22.0% | 34.0% |
| dip(β=0.75,a=5,b=2) | (noisy) | 30.8% |
| blind-even(β=0.75) | (noisy) | 30.2% |
| cash-drag(β=0.75) | (noisy) | 28.1% |

Two things the old metric hid, both now visible: (1) the **true** worst paper loss
is ~34% for a fully-invested book, not ~22%; (2) holding a **reserve genuinely
cushions drawdown** — cash-drag/blind-even/dip sit ~3–6 points below the
fully-invested strategies because idle cash does not fall in a crash. That cushion
is the flip side of the reserve's *return* cost (ADR-0020) and was previously
unmeasurable.

The governing invariant is untouched: this changes a *reported statistic's
sampling*, not any valuation or accounting result, so no provenance claim moves.

## Decision

### 1. Build every strategy's value curve daily

`simulate_strategy` and `_simulate_daily_dip` now append one value point **per
trading day** over the test span `[fills[0], end_idx]`, and `max_drawdown` is the
peak-to-trough of that daily curve. Shares and reserve still change **only at the
monthly fills** (the contribution/deploy logic is unchanged); the daily walk
merely **revalues** the current holding (`shares · close_day + reserve`) at every
close, so the metric catches intra-month troughs. For the reserve model the
reserve is grown daily (Actual/365) instead of fill-to-fill — mathematically
identical at the horizon (same total exponent), so terminal `reserve_cash` and the
invariance law are byte-for-byte unaffected; at `--cash-rate 0` daily growth is a
no-op.

### 2. Nothing else changes

- **Terminal wealth, XIRR, `reserve_cash`, `reserve_deployed`, share counts** are
  identical to before — only `max_drawdown` moves.
- **`method_version` stays `contribution_timing_backtest_v1`.** The EUR valuation
  and reserve accounting are unchanged; a risk statistic's sampling granularity is
  not a valuation method. `--explain` needs no new clause beyond the field's daily
  semantics.
- **The metric is now comparable across strategies** — every one samples the same
  days — which is the property that makes the reserve's drawdown cushion legible.

### 3. This supersedes the monthly-sampled interim drawdown of ADR-0019

ADR-0019 decision 4 / the `BacktestResult.max_drawdown` field described a
monthly-sampled drawdown. That sampling is retired here; the field's documented
semantics become **daily**. ADR-0019's other decisions (reserve model, XIRR,
fills, no-fit) stand.

## Rationale

- **Honesty.** ~22% was an undercount for *every* strategy; ~34% is the loss an
  investor would actually have seen. A risk column that undercounts risk is worse
  than no column.
- **Comparability.** Sampling all strategies on the same days removes the
  same-day-of-month artifact that made the column meaningless across rows.
- **It surfaces a real effect.** The reserve's drawdown cushion (~3–6 points) was
  invisible under monthly noise; daily sampling makes the return-cost / risk-benefit
  trade the tool is meant to show.
- **Cheap and contained.** Shares/reserve still move only monthly; the daily loop
  is a revaluation, ~3.7k points per strategy — negligible — and touches only the
  pure core.

## Consequences

- **`common.py`:** `simulate_strategy` (lump-sum and reserve branches) and
  `_simulate_daily_dip` build `value_curve` daily over `[fills[0], end_idx]`; the
  duplicate terminal append is dropped (the daily curve already ends at the horizon
  value); the reserve grows daily. `BacktestResult.max_drawdown`'s comment becomes
  "daily".
- **No test rebless needed for asserted values** — no test asserts a specific
  backtest `max_drawdown`; the pure `_max_drawdown` unit test and `performance`'s
  own (already-daily) drawdown are untouched. A test is added asserting the daily
  curve catches an intra-month trough a monthly sample would miss.
- **README / CLAUDE.md:** the `backtest` drawdown description notes daily sampling
  and this ADR; the ADR log gains this file (no numbering gap).
- No CLI, storage, or layer (ADR-0003) change.

## Deferred (not in this ADR)

- **Reporting the drawdown *date* / recovery time.** The daily curve makes
  time-to-trough and time-to-recovery computable; a richer risk block is a later,
  additive change.
- **A drawdown row in `--window` mode.** Per-window drawdown distributions, as with
  the other window statistics; mechanical once wanted.
