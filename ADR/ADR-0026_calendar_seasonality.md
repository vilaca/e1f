# ADR-0026 — Calendar seasonality analysis (experimental)

**Scope:** a new read-only experimental command `e1f seasonality` that evaluates
whether one ETF's monthly EUR returns show a calendar-month pattern that is (a)
descriptively visible, (b) larger than ordinary month-to-month noise, (c)
large enough to matter after cash drag, and (d) durable out of sample. It
analyses all twelve months symmetrically. It does **not** assume September is
special, does **not** modify the dip-buying strategies (ADR-0019–0023), and
does **not** turn an in-sample ranking into a trading recommendation.

One module `src/e1f/experimental/seasonality.py` on the experimental command
layer (ADR-0024). The stable core is unchanged.

Slogan: **a month can look bad without being a season, and a season can be real
without being a strategy.**

## Context

A long-run S&P 500 folklore — September as the weakest average month, often
quoted around −0.6% to −0.9% depending on the sample — is easy to project onto
a core accumulating all-world ETF (the series already used for contribution
timing, `IE00B3YLTY66`). That projection does not establish any of the things
an investor actually needs:

- that *this* fund is more likely than not to fall in September
- that the month-to-month differences exceed ordinary sampling noise
- that selling before September and buying back after, or skipping the
  September contribution, improves terminal wealth after cash drag
- that whichever month looks weakest in *this* 14–15-year sample is a season
  rather than the month that happened to finish last of twelve

With roughly one observation per calendar month per year, a UCITS series
starting ~2011 yields ~14–15 points per month. That is enough to *describe*
the sample and too thin to *discover* a 12-way seasonal rule and then trade
it. The failure mode is the same one ADR-0019 refused for dip thresholds:
look at the data, pick the impressive cell, and treat the same data as
evidence.

Two further traps are specific to this question:

1. **Price vs total return.** For an accumulating share class the stored close
   already embeds reinvested income; for a distributing class it does not. A
   wealth question answered on raw price can invent or hide a seasonal pattern
   around ex-dividend months. e1f stores one close series (`prices.close`); it
   does not store a separate total-return index or a dividend history.
2. **Conflating a calendar story with the dip experiment.** Adding a
   "skip September" knob to `backtest` would turn a new observation into
   another in-sample parameter. The September (or March, or any month)
   hypothesis needs its own frozen experiment and its own controls.

The governing invariant is unchanged (ADR-0012):

> **No analytical result may imply information that its provenance does not
> establish.**

For seasonality, provenance is the **sample of complete calendar months**, the
**return definition**, and whether a month was **chosen before seeing the
test data**. A lowest-mean month is a descriptive ranking of that sample. It
is not a recommendation.

## Decision

### 1. A separate experimental command — not a `backtest` mode

`seasonality` is a new experimental sibling (`lookthrough`, `concentration`,
`overlap`, `backtest`). It joins `EXPERIMENTAL_*` in `cli.py` and the
import-linter experimental-layers contract. It may import `e1f.common` and
`e1f.experimental.common`; it must not import `e1f.experimental.backtest` and
must not add a `DeployMode`.

The default invocation is descriptive + statistical analysis of one required
`--isin`. There is no default ISIN (same reason as ADR-0019: the series *is*
the experiment). A missing or unknown ISIN prints the candidate list — every
ISIN with a stored price series, with span, distribution, and name — and
exits non-zero.

The first intended subject is the same accumulating all-world series the
timing work already uses (`IE00B3YLTY66`), chosen explicitly by the user.

### 2. Four questions, reported as four layers — never collapsed

Every run answers the first two. The third and fourth run only when the
caller opts into them (decision 10–11). The output labels each block so a
descriptive ranking cannot be read as a trading finding.

| Layer | Question | Default? |
|---|---|---|
| **Descriptive** | Which calendar months produced higher/lower returns in this sample? | yes |
| **Statistical** | Are the month-to-month differences larger than a calendar-free shuffle of the same returns would produce? | yes |
| **Economic** | Does a *pre-specified* seasonal policy beat ordinary DCA after cash drag? | only with `--rule` |
| **Out-of-sample** | Does a month *discovered* in a training window still help on a later window that did not choose it? | only with `--rule historical-weakest` and an explicit split |

The standard output ends with a four-line interpretation that restates these
layers in prose (decision 13). The system must never translate "month M had
the lowest historical average" into "therefore avoid M."

### 3. All twelve months, symmetrically — September is not a flag

The command always emits a twelve-row table, January through December. No
month is hard-coded, privileged, highlighted by default, or usable as a
filter on that table.

`--month` exists only as an argument to a **pre-specified** rule
(`--rule avoid-month` / `--rule sit-out-month`). Passing `--month` without
`--rule` is a hard error: the flag is not a way to ask "is September bad?"

Individual-month commentary (worst/best mean, worst/best median, highest/
lowest positive frequency) is labelled **in-sample descriptive ranking**.
The words "winner" and "loser" are banned. The weakest month is not
auto-promoted into `--rule`.

### 4. Return definition — EUR, month-end, total-return by default

For each complete calendar month *M*:

```
r_M = P_end(M) / P_end(M−1) − 1
```

where `P_end(M)` is the last available EUR close in month *M* (not an
arbitrary mid-month trading day, not a forward-filled calendar-day price).
Gaps inside the month are ignored; a month with **no** close is missing for
that year and is not interpolated.

**Complete month (included in the primary analysis)** iff all of:

1. The window's `--to` date is on or after the first day of the *next*
   calendar month (the month has fully elapsed in the requested window).
2. At least one close exists in *M*.
3. At least one close exists in the previous calendar month (so `P_end(M−1)`
   is observed, not invented).

The series' first month and the window's trailing partial month are
therefore excluded automatically. `--explain` records how many months were
dropped as partial and why. Partial months may be listed as a diagnostic
footnote; they never enter means, tests, rankings, or strategy fills.

Returns are computed on the **EUR-converted** close (ADR-0010 / ADR-0015):
that is the investor's experience. A EUR share class passes through; a USD
class converts at the nearest-prior EUR/USD rate. A month whose month-end
close cannot be converted is missing, not zero.

**`--price-mode`** (name and choices live in the argparse definition):

- **`total-return` (default).** Allowed only when the fund's config
  `distribution` is `Accumulating`. For that share class the stored close
  *is* the total-return series (NAV already reflects reinvested income).
  If `distribution` is `Distributing` or missing, the command **fails
  explicitly** and points at `--price-mode price` for a diagnostic run. It
  must not silently substitute price return.
- **`price`.** Always allowed. `--explain` labels every number as price
  return. For an accumulating class this equals total return; for a
  distributing class it understates wealth and is not an actionable
  seasonal result.

There is no dividend ledger and no separate total-return column in
`prices`. v1 does not reconstruct one. A future TR-index splice is
deferred with proxy history (ADR-0019 v2).

`--from` / `--to` clip the window; the effective span (first complete month
… last complete month) is what `--explain` and the table header report.

### 5. Descriptive table — every month, same columns, no preselection

For January–December, over the complete-month sample:

- observation count *N*
- arithmetic mean return
- median return
- sample standard deviation
- minimum return and its year
- maximum return and its year
- % of observations > 0 and % < 0 (zeros counted in *N* but in neither %)
- mean excess vs the other eleven months (`mean_m − mean_{≠m}`)
- median excess vs the other eleven months

A month with *N* = 0 is shown as `n/a`, not as a zero return.

Below the table, the in-sample rankings of decision 3. Then the raw
strongest-minus-weakest mean spread, as a descriptive magnitude — not an
economic claim (decision 10).

Small *N* is disclosed, not overcome. With a ~2011 inception the expected
*N* is the mid-teens. `--explain` names the complete-year count and the
per-month *N*. If any month has *N* below the inferential floor
(`SEASONALITY_MIN_N_INFER`, a named constant in the module; intended value
8), the descriptive table still prints and the statistical layer is
**UNAVAILABLE** with that reason — never a p-value on a handful of points.

### 6. Omnibus test — permutation-primary, month-blind

H₀: calendar month has no systematic relationship with monthly returns
(the twelve month-groups are exchangeable).

The primary statistic is Kruskal–Wallis *H* on the twelve groups. *H* does
not require first picking the weakest month, and it does not assume
normality. The **p-value is not** the χ² approximation: it is the
empirical tail from decision 7. The χ² p-value may appear in `--explain`
as a diagnostic; it is not the reported result.

Report the observed statistic and the permutation p-value on **separate
lines**. The former is what the sample produced; the latter is how unusual
that value is under the label-shuffling null. The permutation machinery
must not collapse the two into one number.

```
Seasonality test
────────────────────────────────
Null: calendar month is exchangeable with the other months
Kruskal–Wallis H: …
Permutation p-value: …   (P permutations, seed S)
```

A parametric one-way ANOVA is not the primary test. Month-level pairwise
tests are not a substitute for this omnibus: they are a supplementary
layer behind multiple-testing correction (decision 8).

### 7. Permutation / placebo — the same shuffle answers two questions

Preserve the observed list of complete-month returns. Re-assign the
**existing multiset** of calendar-month labels (so each month keeps its
observed *N*) uniformly at random. Recompute *H*. Repeat `--permutations`
times (default 10_000) from `--seed` (default 0).

```
p = (1 + #{H* ≥ H_obs}) / (1 + P)
```

The `+1` is the usual conservative correction. Identical `(seed, P,
returns)` must reproduce identical p-values. Changing the seed must change
only stochastic results.

The **same** permutations also build the extreme-month placebo:

- each shuffle: twelve monthly means → record `min(means)` and `max(means)`
- compare the observed weakest-month mean to the null distribution of
  `min(means)`; likewise the observed strongest-month mean to `max(means)`
- report those two empirical p-values next to the omnibus

That is the multiple-comparisons question in one number: *with twelve
months, how surprising is it that one of them looks this bad (or this
good)?* A historically weak September that is a typical "worst of twelve"
under the shuffle is reported as such.

`--explain` records *P*, the seed, the statistic, and the add-one formula.

### 8. Month-level tests are secondary and corrected

If the command reports a per-month "this month vs the other eleven" test
(the natural statistic is that month's mean excess, using the same
permutations as decision 7 so the loop is not re-run twelve times), it
must show **both**:

- raw empirical p-value
- Benjamini–Hochberg adjusted p-value (12 tests, FDR)

A low raw p-value for one month is not a seasonal effect. Holm is an
acceptable alternative; the method name is recorded in `--explain`. v1
implements BH.

No month is declared "significant" in the headline interpretation unless
the **omnibus** (decision 6–7) also rejects. The extreme-month placebo is
the sentence that belongs next to "September was the weakest."

### 9. No in-sample search

The command must not search over month, entry date, exit date, holding
period, cash allocation, or threshold, and it must not rank rules by
terminal wealth on the same sample that selected them.

"Best month" / "worst month" are **descriptive labels of the table**, not
inputs to the economic layer. The only way a discovered month reaches a
strategy comparison is decision 11 (frozen on a training window, scored on
a later window).

### 10. Economic layer — pre-specified rules only, contribution-first

Statistical significance is not an economic result. The table's
strongest-minus-weakest spread is shown on every run as magnitude; it is
not a strategy P&L.

A strategy comparison runs only when `--rule` is set. Two families, both
**named by the caller** — never inferred from the descriptive ranking:

| Rule | What it does |
|---|---|
| `avoid-month` | Stay invested. Skip the selected month's contribution; hold that `C` in cash; deploy it at the next monthly fill together with that next month's `C`. |
| `sit-out-month` | Sell the entire position at the selected month's fill (the first close on or after the 1st); hold cash through the month (earning `--cash-rate`); buy back at the next monthly fill. ADR-0028 freezes and reaffirms this convention. |

`--month` (1–12) is required for both. `--cash-rate` is the same Actual/365
idle-cash convention as ADR-0019 (default 0). `--contribution` follows the
same fixed-`C` convention as `backtest` so Control A is comparable to
constant-DCA.

**Controls, always shown together when `--rule` is set:**

- **A — constant-DCA.** Invest `C` at every monthly fill, including the
  selected month. The baseline.
- **B — the named rule.** `avoid-month` or `sit-out-month` as above.
- **C — cash-drag of the same idle cash.** For `avoid-month`, hold the
  skipped `C` in cash until horizon (never redeploy). This isolates "skip
  the month" from "skip and put it back." For `sit-out-month`, Control C is
  omitted — the sit-out *is* the cash period; the comparison is A vs B.

Output per control: terminal wealth (equity + leftover cash), XIRR, max
drawdown (daily-sampled, ADR-0022), cash held, cash-rate income, and
difference vs A.

**Invariance (contribution rules):** over *N* fills, every control that
does not sit out still commits `N·C`. `avoid-month` merely delays one `C`
per year; leftover cash at the horizon is counted. If this identity fails,
the accounting is broken. `sit-out-month` is a *position* experiment, not a
contribution-timing experiment; it does not claim the `N·C` identity.

Fill convention is reused from `monthly_fill_indices` in
`e1f.experimental.common` (first close on-or-after the 1st). That is a
different sampling than the month-end return table, and `--explain` must
say so: the table asks whether *month-end-to-month-end* returns differ by
calendar month; the strategy block asks whether *changing contribution (or
exposure) timing* on that calendar changed wealth. They can disagree.

The seasonal simulator lives in `seasonality.py`. It does not extend
`simulate_strategy` / `DeployMode`. Fees, taxes, and lot rounding are
omitted for the same reason as ADR-0019 (stated assumption).

`--cash-rate` without `--rule` only annotates the descriptive spread with
an opportunity-cost sketch ("holding cash for one calendar month at this
rate forgoes roughly … of a fully-invested month"). It must not print a
strategy table or a "therefore skip" line.

### 11. Out-of-sample — discovery and evaluation must not share observations

`--rule historical-weakest` (contribution avoid) and
`--rule historical-weakest-sit-out` (position sit-out) are the only
discovery paths. They require an explicit split:

- `--training-from` / `--training-to`
- `--test-from` / `--test-to`

Training and test must be non-overlapping on complete months. The weakest
calendar month is the lowest **mean** complete-month return **in the
training window only**. That month is frozen. The test window applies
`avoid-month` / `sit-out-month` for that frozen month and never recomputes
it. Thresholds, deployment fractions, cash holding period, and any other
parameter are likewise forbidden from depending on test data.

If either window fails the inferential floor (training:
`SEASONALITY_MIN_N_INFER` in every month; test: a lower named floor
`SEASONALITY_MIN_N_OOS`, intended value 3 — enough to run, not enough to
impress), the command **refuses** the OOS block with an explicit
insufficient-history message. It still prints the descriptive/statistical
layers for the `--from`/`--to` window if those are independently valid.

`--explain` records training span, test span, the month selected from
training, the frozen rule, and a one-line "no test leakage" statement.

A walk-forward that re-picks the weakest month every year is a different
experiment (Deferred). v1 is one freeze, one test window.

### 12. Provenance — `--explain` names the layer of every number

Following ADR-0014, `--explain` is opt-in. The command is
assumption-laden, so the block has to earn its keep:

- ISIN, distribution, price-mode, EUR conversion
- requested vs effective span; complete months vs partials dropped
- observations per month; inferential floor and whether it was met
- permutation count, seed, omnibus statistic, add-one p-value formula
- multiple-testing method (BH, 12 tests)
- which blocks are descriptive / statistical / in-sample / out-of-sample /
  backtested
- when `--rule` is set: fill convention vs month-end returns, cash-rate,
  invariance claim
- when OOS: training/test spans, frozen month, "selected on training only"

A normal run is `CALCULATED` when the inferential floor is met;
`UNAVAILABLE` for the statistical layer when it is not. The metric
contract lives in the command module:

```python
MetricContract(
    method_version="calendar_seasonality_v1",
    requires=(
        "a daily EUR close series with complete calendar months",
        "Accumulating distribution when price-mode is total-return",
    ),
    does_not_require=(
        "a September (or any month) prior",
        "look-through holdings",
        "a dividend ledger or a separate total-return index",
        "a dip / drawdown signal",
    ),
    supports=(
        "twelve-month descriptive statistics",
        "permutation omnibus + extreme-month placebo",
        "pre-specified seasonal rules vs constant-DCA",
        "frozen-month out-of-sample evaluation",
    ),
    limitations=(
        "N per month is years of history — mid-teens is typical and thin",
        "total-return is the accumulating NAV, not a reconstructed TR index",
        "evaluator only — no in-sample search, no auto-promoted rule",
        "month-end returns and contribution-fill timing are different samples",
        "no fees/taxes/spread; fractional shares",
        "conditional (month × regime) seasonality is out of scope",
    ),
)
```

### 13. Interpretation footer — required, wording frozen

Every successful run ends with an **immutable** four-line interpretation.
The wording is a module constant; tests pin the exact sentences. A short
findings block may precede it (what this sample showed) but must not
replace or rephrase the footer.

```
Descriptive only: monthly rankings describe this sample; they do not establish a tradable effect.
Inference: statistical inference is unavailable below the minimum per-month sample floor.
Selection: no month is automatically selected for trading.
Actionability: a seasonal rule requires an explicitly pre-specified rule and, where applicable, a non-overlapping test period.
```

These four sentences are the structural expression of decision 2. They
protect the output even when the findings block names a weakest month.
Tests pin that a weakest-month ranking never appears as trading advice.

### 14. Reproducibility and refused combinations

Stochastic results are a function of `(seed, permutations, input
returns)` only. Two runs with those identical produce identical output.

Hard errors (non-zero, no table):

- missing / unknown `--isin`
- `--price-mode total-return` on a distributing or unknown share class
- `--month` without `--rule`, or `--rule` that needs `--month` without it
- `historical-weakest*` without a complete non-overlapping train/test split
- overlapping train/test complete months
- `--permutations < 1`, out-of-range `--month`, negative `--cash-rate`
- a window with no complete months

Insufficient *N* for inference is not a hard error: the descriptive table
prints and the statistical / OOS blocks become UNAVAILABLE with a reason.

## Rationale

- **All twelve months, or we are confirming a story.** A `--month 9`
  default would spend the degrees of freedom on a hypothesis the sample
  is too small to privilege. The placebo "worst of twelve" exists
  specifically because one month will look worst by chance (decisions 3,
  7).
- **Permutation-primary.** Mid-teen *N*, twelve groups, and a
  pre-existing folklore make an asymptotic KW p-value the wrong headline.
  Shuffling the observed labels asks the question we mean (decisions 6–7).
- **Discovery ≠ evaluation.** Auto-trading the in-sample weakest month is
  the same overfit ADR-0019 refused. The only discovery path freezes the
  month on training data (decisions 9, 11).
- **Wealth is the economic question, and it is optional.** A −1% average
  September is compatible with losing to DCA once August, October, and
  idle-cash drag are counted. That comparison is a separate, opt-in
  experiment with its own controls (decision 10).
- **Separate from dip-buying.** A new observation must not become another
  `DeployMode`. Isolation is physical: a new experimental module, no
  import of `backtest`, reuse of fill indices and `xirr` only
  (decision 1).
- **Fail closed on total return.** Silently analysing a distributor's
  price as wealth is a worse bug than refusing to run (decision 4).
- **Thin sample is a first-class result.** Refusing inference below a
  named floor is more honest than a precise p-value on eight Januarys
  (decision 5).

## Consequences

- New module `src/e1f/experimental/seasonality.py`; wired into `cli.py`
  `EXPERIMENTAL_*`, the experimental `--help` heading, autocomplete,
  `tests/test_contracts.py::test_cli_commands_surface`, and the
  import-linter experimental-layers command list (one name). No change to
  the stable layer contract (ADR-0003) or to `e1f.common`.
- `e1f.experimental.common` is **not** extended with a new `DeployMode`.
  `seasonality` may call `monthly_fill_indices` (and EUR loaders / `xirr`
  from `e1f.common`). The seasonal loop and all statistics stay in
  `seasonality.py`.
- No new storage; the command only reads prices, FX, and config.
- Tests (pure functions over synthetic `(year, month, return)` lists and
  synthetic daily series; no dependence on the user's DB):
  - month-end return: last close in *M* over last close in *M−1*; a
    mid-month first/last month is excluded; a month with no close is
    missing, not interpolated
  - `--to` inside a month drops that month; the previous complete month
    remains
  - mean / median / excess-vs-other / positive-negative frequency,
    including zeros counted in *N* only
  - KW *H* on a hand-computed fixture
  - permutation reproducibility: seed 0 × 2 runs match; a different seed
    changes only stochastic fields
  - permutation preserves per-month *N*; empirical p is in `[1/(1+P), 1]`
  - extreme-month placebo: a no-effect series does not treat its weakest
    month as unusual at any conventional level we pin
  - BH: a known raw-p vector matches a hand-computed adjustment
  - `total-return` + Distributing (or missing distribution) exits non-zero
    without a table; `price` is allowed
  - `--month` without `--rule` exits non-zero
  - `--rule avoid-month --month 9`: Control A ≡ constant-DCA on the same
    fills; invariance `N·C == equity_cost + leftover_cash` at `--cash-rate 0`
  - `sit-out-month`: literal January/February/March fills pin sale at the
    selected month's fill and reinvestment at the next fill
  - `--rule historical-weakest` with a synthetic January premium in
    training only: selected month is January; test evaluation does not
    reread training; test-period January returns do not change the pick
  - train/test overlap and too-short test window: OOS block refused, no
    leaked pick
  - synthetic **known effect**: every January has a deterministic positive
    excess → January ranks strongest, omnibus + permutation reject,
    extreme-max placebo is extreme
  - synthetic **no effect**: i.i.d. (or a fixed permutation of one month's
    returns across labels) → omnibus does not manufacture a season at the
    pinned seed; Trading line stays non-prescriptive
  - CLI/contract: `e1f seasonality --isin …` on a tiny fixture DB prints
    the twelve-row table and the four-line footer; `--explain` names seed,
    *P*, price-mode, and partial-month handling
  Coverage floor 90%.

## Deferred (not in this ADR)

- **Conditional seasonality** — month × prior drawdown, month × prior
  3/6/12-month return, month × regime. Potentially more relevant to
  dip-buying than unconditional calendar effects. A later ADR; the v1
  types should not paint us into a corner (a month-return row can later
  grow a regime key) but v1 must not mix the questions.
- **Walk-forward re-selection** — re-picking the weakest month each year
  on a trailing window. Different leakage profile; not a v1 default.
- **`--benchmark ISIN`** — a second series printed under the same
  contract, never mixed into the primary sample. Useful (SPY vs the
  UCITS core) but not required to answer the four questions.
- **Reconstructed total return for distributors** — needs a dividend
  series we do not store. Until then, distributors are
  `--price-mode price` only.
- **Proxy-index history** — the same v2 splice ADR-0019 deferred. A
  14-year ETF cannot settle a century-scale September folklore; this
  command will not pretend otherwise.
- **Any in-sample optimiser** — best month, best entry/exit day, best
  cash fraction. Explicitly out of scope so it cannot land as a flag
  on this command.
- **Graduation to the stable tier** — not until the research question
  has a settled, non-misleading default. Until then it stays
  experimental (ADR-0024).
