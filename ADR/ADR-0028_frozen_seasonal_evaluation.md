# ADR-0028 — Frozen August/November seasonal evaluation (experimental)

**Scope:** add an `--evaluate` mode to `e1f seasonality` that scores
the already-discovered August-weak / November-strong pattern against
constant-DCA. Discovery does not run again. The months are module
constants frozen from the ADR-0027 configured-universe result.
Contribution-timing is the primary experiment; selling the whole
book is secondary. No `DeployMode`, no import of `backtest`.

Slogan: **a concentrated calendar rank is not a contribution edge
until the frozen rule is scored against DCA.**

## Context

ADR-0027's cross-sectional permutation rejected a calendar-free
shuffle of strongest/weakest nominations: November was #1 in most
inferential funds, August last in half the book. That is evidence
of *concentration across the configured universe*. It is not
evidence that an investor can exploit it.

Two reasons it is not yet a strategy:

1. **Economic value is a different question.** Skipping a weak
   month's contribution, or sitting the whole book out, changes
   terminal wealth only after later-month returns and idle-cash
   drag. A −0.1% August mean can lose to DCA.
2. **The freeze already used the book's history.** August and
   November were named by looking at the full inferential sample.
   Scoring the same years is in-sample. A later window can show
   whether the *rule* still made money; it cannot pretend the
   test never saw the discovery.

ADR-0026 already has `--rule avoid-month --month 8`. That is one
named rule on one window. This ADR is the *battery* that the
book discovery now justifies: August-skip vs DCA as the headline,
November-skip as a direction placebo, two later redeploys as
pre-specified shifts, sit-out as a secondary claim — and an
honest label on every window.

The discovery phase is finished. `--evaluate` must not search
for another month, change the freeze, or promote September
because it also looked weak.

## Decision

### 1. `--evaluate` is a third mode, not a new hunt

Exactly one of `--isin`, `--portfolio`, `--evaluate` is wrong:
`--evaluate` requires `--isin` and is incompatible with
`--portfolio`, `--rule`, and `--month`. The twelve-month
descriptive table and the per-fund omnibus do **not** print.
Those are discovery. This mode is economics.

The header names the freeze (weak = August, strong = November),
states that the months are not re-selected on this series or
window, and repeats that the ADR-0027 freeze used the configured
book's full history. A later window is a **stability check**,
not an independent discovery.

`--portfolio --from/--to` remains the way to *re-run* book
discovery on an era. `--evaluate` will not do that.

### 2. Frozen months are constants

```
FROZEN_WEAK_MONTH = 8
FROZEN_STRONG_MONTH = 11
```

They live in `seasonality.py`. Tests pin the values. Changing
them is a new ADR, not a flag. September is not a candidate.

### 3. Three tests, ranked by claim strength

All use the existing fill convention (`monthly_fill_indices`:
first close on-or-after the 1st) and `--contribution` /
`--cash-rate`. Month-end returns and fill timing remain
different samples (ADR-0026).

**Test A — contribution skip (primary)**

- **A — constant-DCA.** Invest `C` at every monthly fill.
- **August-skip.** Hold the August `C` in cash; invest it at
  the next monthly fill (September) together with that month's
  `C`. This *is* August→September.
- **November-skip.** The same policy on November (redeploy at
  December). Opposite-direction placebo, not a candidate policy.
- **Cash-drag.** Hold the skipped August `C` until the horizon
  (never redeploy). Isolates "skip" from "skip and put it back."

Headline comparison: August-skip − DCA after cash income.

**Test B — contribution shift (pre-specified redeploys)**

August `C` is held and deployed at a *later named month in the
same calendar year*:

- August → October
- August → November

August → September is Test A, not a third search. Both shifts
are always run. The command does not pick the richer one as
the winner.

**Test C — full-portfolio sit-out (secondary)**

Sit out August: sell at the August fill, hold cash through the
month, buy at the September fill (existing `sit-out-month`
semantics). Compared only to DCA. This is the strongest claim
and is labelled secondary. Taxes and spreads are still omitted.

Contribution rules keep the ADR-0026 invariance at cash-rate 0:
`N·C == equity_cost + leftover cash`. Sit-out does not claim it.

### 4. Years better / worse — isolated contribution years

For each calendar year that has twelve contribution fills in
the evaluation window, replay that year alone (start at zero
shares, that year's fills only). Count years where the named
rule's terminal exceeds DCA, falls short, or ties. This is
not a path-dependent attribution of the full-horizon wealth
curve; it asks how often a standalone year would have preferred
the rule.

### 5. Windows — in-sample, chronological, reverse-era

`--from` / `--to` without a split: one battery on that window,
labelled **in-sample**.

`--test-from` / `--test-to` without training: one battery on
the test window, labelled **holdout of the frozen rule**.

All four of `--training-from` / `--training-to` /
`--test-from` / `--test-to`: two batteries. Training is
**discovery-era (not a test)**. Test is **chronological
holdout** when it starts at or after training ends, or
**reverse-era (not prospective)** when it ends at or before
training starts. Overlapping complete months are a hard error.

The test window does **not** need the inferential floor in
every calendar month. It needs complete months and at least
one August fill. A seven-year holdout is allowed to be thin.

Findings report the holdout (or the single window) as the
economic headline. Training numbers are shown and not mixed
into that sentence.

XIRR is the return column. The command does not invent a
CAGR that disagrees with a contribution schedule.

### 6. Output does not become advice

A four-line findings block states the primary Δ, the placebo
Δ, the two shift Δs, and the sit-out Δ. The trading line
stays non-prescriptive even when August-skip wins. The
immutable ADR-0026 footer still closes the report.

`--explain` records the frozen constants, the window labels,
cash-rate, invariance, fill vs month-end, and that the month
was not selected from this ISIN or this window.

## Rationale

- **Freeze, then score.** Another descriptive cut (drop a
  sector, add September) would spend the discovery twice.
  The interesting work is now "did the already-named rule
  make money?"
- **Contribution first.** That matches the dip-reserve
  research and avoids the tax/opportunity-cost claim of
  selling the book every July. Sit-out is present so the
  stronger claim can be refused with a number, not omitted.
- **Placebo and pre-specified shifts.** November-skip asks
  whether *any* skip looks good. October and November
  redeploys are named before seeing terminals so the
  August→November story cannot pick its own landing month.
- **Honest holdouts.** Because the freeze used the full
  book, a 2019–2026 score is a stability check. Calling it
  confirmatory discovery would launder look-ahead. Re-running
  `--portfolio` on 2008–2018 is a different command.

## Consequences

- `src/e1f/experimental/seasonality.py` gains `--evaluate`,
  `DeployKind.SHIFT` (same-year redeploy), the frozen-month
  constants, a year scorecard, and an evaluation report.
  `--isin` / `--portfolio` discovery paths are unchanged.
- Tests: freeze pins; August-skip ≡ next-fill avoid-August;
  Aug→October holds through the September fill; contribution
  invariance at cash-rate 0; a synthetic August crash makes
  August-skip beat DCA and November-skip not be the headline
  win; isolated years count better/worse; `--evaluate` rejects
  `--portfolio` / `--rule` / `--month`; overlap split is
  refused; reverse-era is labelled not prospective; the
  twelve-month discovery table is absent. Coverage floor 90%.
- README / CLAUDE.md mention `--evaluate` when this lands.

## Deferred (not in this ADR)

- Re-discovering the book on a training era inside
  `--evaluate` (use `--portfolio --from/--to` instead).
- Walk-forward re-selection, leave-one-era-out automation,
  or a portfolio-level (every-ISIN) contribution simulator.
- Fees, taxes, spreads, lot rounding (same as ADR-0019).
- Conditional / regime seasonality (ADR-0026 deferred).
