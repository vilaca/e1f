# ADR-0027 — Portfolio seasonality consensus (experimental)

**Scope:** extend `e1f seasonality` (ADR-0026) with an explicit `--portfolio`
mode that asks whether a *common* calendar-month pattern appears across
funds, without mixing short-history descriptive samples into the
inferential cohort, and without treating a repeated "strongest month"
count as evidence until a cross-sectional permutation says so.

Single-fund `--isin` behaviour is unchanged in contract: twelve months,
permutation omnibus, no auto-traded weakest month. This ADR adds the
cross-sectional layer and tightens how insufficient history is labelled.

Slogan: **a month that wins in many funds is still a coincidence until
the labels are shuffled across the book.**

## Context

ADR-0026 answers four questions about *one* series. After running it over
a book of ETFs, two things become visible that a single-ISIN test cannot
say:

1. Short series (a couple of years, ~25 complete months) produce wild
   month means. Putting those rows in the same hierarchy as a 14-year
   series, and attaching p-values to both, mixes "how much history we
   have" with "whether inference is allowed."
2. A month (November, August, September, …) can be the strongest or
   weakest in-sample month in many funds at once. "11 of 21 funds"
   looks like a market-wide season. With 12 months × N funds, some
   month will often concentrate by chance. Twenty-one separate
   per-fund p-values do not test that concentration, and a p-value
   above 0.05 on each fund does not establish that the variation is
   random — only that that test did not reject.

The economic / OOS rule layer stays single-ISIN (ADR-0026 decisions
10–11). `--portfolio` discovers whether a common pattern is even
worth freezing; it does not trade it.

## Decision

### 1. Two explicit modes — `--isin` XOR `--portfolio`

Exactly one is required.

- `--isin ISIN` — one fund, ADR-0026 contract.
- `--portfolio` — every configured ISIN that has a stored price series.

`--isin` never means "the whole book." `--rule` is single-ISIN only:
passing it with `--portfolio` is a hard error. Freeze a month, then
test it with `--isin … --rule …` on a later window.

The `--portfolio` header states the universe (configured ETFs with
prices), the inference floor, and that short-history funds are
descriptive only.

### 2. Two cohorts, never one table

A fund is **inferential** iff every calendar month has
`N ≥ SEASONALITY_MIN_N_INFER` complete observations (ADR-0026
decision 5). Otherwise it is **descriptive**.

`--isin` on a descriptive fund still prints the twelve-month table
(the sample happened), but the header and the table title say
`DESCRIPTIVE — insufficient history`. The statistical block is a
single UNAVAILABLE banner — no Kruskal-Wallis line, no p-value
lines, no month-vs-rest table. Those numbers are not computed.

`--portfolio` never prints 21 copies of the twelve-month table.
It prints:

1. an inferential roster (ISIN, name, total complete months,
   complete years, in-sample strongest / weakest month)
2. a `DESCRIPTIVE — insufficient history` roster (same columns;
   no p-values)
3. exclusions (distributing under total-return, no FX, no
   complete months), disclosed not silent
4. the consensus table and cross-sectional test over the
   **inferential cohort only**

Total complete-month count (`len(returns)`) is shown as history
depth so "24 months" and "168 months" are not confused with the
per-month *N* column of the single-fund table.

### 3. Softer per-fund statistical language

A permutation p-value ≥ 0.05 is reported as:

> No statistically significant calendar-month effect was detected
> at the 5% level.

It is **not** reported as "variations are consistent with random
noise" or "no seasonality." Failure to reject is not proof of the
null. The immutable ADR-0026 footer is unchanged.

### 4. Consensus table — the missing cross-section

For each calendar month, over the inferential cohort:

- number of funds with a mean for that month
- median of the *fund-level* monthly means
- mean of those fund-level means
- % of funds whose mean for that month is positive
- count (and %) of funds for which this is the strongest mean month
- count (and %) of funds for which this is the weakest mean month

Ties still break toward the earlier calendar month (ADR-0026).
This is descriptive of the book. It is not a trading signal.

### 5. Cross-sectional permutation — "11 of 21" is the question

H₀: given each fund's observed monthly returns, calendar-month
labels are exchangeable *within* each fund independently.

Procedure: preserve each inferential fund's returns; shuffle that
fund's month labels (same multiset, same as ADR-0026 decision 7);
recompute each fund's strongest-mean and weakest-mean month;
record the 12 nomination counts. Repeat `--permutations` times
from `--seed`.

Headline statistics (add-one empirical p):

- **max-concentration** — how often a shuffle produces a highest
  "strongest" (resp. "weakest") count ≥ the observed highest count.
  This is the multiple-comparison-safe answer to "is this
  concentration unusual?"
- The observed top month's own count vs its null, labelled **raw**
  (one month, chosen after seeing the data).

Kruskal-Wallis H and the permutation p-value remain separate
numbers on `--isin`. `--portfolio` does not emit 21 per-fund
omnibus tests — that is the trap this decision exists to avoid.

### 6. Findings, then stop

`--portfolio` findings name the most frequent strongest / weakest
months as **descriptive**, report the cross-sectional p-value,
and state that economic value is untested. They never say
"therefore skip September" or "therefore overweight November."

A strategy comparison still requires an explicit `--isin` +
`--rule` (and a train/test split if the month was discovered).
That is ADR-0026; economic evaluation of a *frozen* book
discovery is ADR-0028. This ADR does not add a portfolio-level
simulator.

### 7. Correlated-universe caveat — 10/14 is not 10 independent clocks

The inferential book is a configured universe, not a set of
independent economic experiments. Several names are wrappers on
the same developed-equity factor (all-world / S&P 500 / Europe /
Nasdaq / sector). A common market November will elect many funds
at once. The within-fund shuffle still answers "are these
nominations unusually concentrated under label exchangeability?"
It does **not** answer "did we observe N independent seasonal
clocks?"

Every `--portfolio` run prints a caveat that says so, with *N*
equal to the inferential cohort size. The wording is a module
helper; tests pin that the caveat is present and names *N*.

### 8. Equal-weight book — one series, 1/*N* per inferential fund

Nomination counts ask how often a month *wins*. An investor
holds a book. For every `(year, month)` in the **balanced
panel** — the intersection where *every* inferential fund has a
complete month-end return — the equal-weight book return is the
arithmetic mean of those *N* fund returns (each fund weight
`1/N`). Unbalanced months (a fund missing) are dropped, not
reweighted: composition must not drift toward the long-history
names.

The command prints that twelve-month table (N years, mean,
median, % positive) and, if the panel meets the same
inferential floor as a single ISIN, the same Kruskal-Wallis +
label-shuffle omnibus on the book series. That is a second
question ("does the 1/*N* book itself show a calendar effect?"),
not a replacement for the nomination test and not a new month
search. A thin intersection is UNAVAILABLE, not a manufactured
p-value.

Descriptive funds still do not enter the panel.

## Rationale

- **Cohorts are the honesty boundary.** Short-history means are
  real descriptions of a tiny sample; they are not the same kind
  of number as a 14-year mean, and they must not vote in a
  consensus or a p-value.
- **The interesting question moved.** After a book of single-fund
  tests, "is September bad for IE00…?" is underpowered. "Do many
  funds nominate the same month more often than a within-fund
  shuffle allows?" is the question the 11-of-21 observation
  actually asks.
- **Modes are explicit.** `--isin` is one series; `--portfolio`
  is the book. Collapsing them is how a confirmation-bias tool
  gets built by accident.
- **Still no auto-trade.** Discovery (consensus) and evaluation
  (`--rule`, OOS, ADR-0028) stay on opposite sides of a freeze.
- **Votes are not independent clocks.** The caveat exists so
  "10 of 14" cannot be read as fourteen replications.
- **A book return is not a nomination count.** The equal-weight
  panel asks whether the 1/*N* series itself is seasonal.

## Consequences

- `src/e1f/experimental/seasonality.py` gains `--portfolio`, the
  two-cohort roster, the consensus table, the cross-sectional
  permutation, the correlated-universe caveat, and the balanced
  equal-weight book (plus its own omnibus when the panel is
  thick enough). No new module, no `DeployMode`, no stable-layer
  change. The nomination test is unchanged by the equal-weight
  addendum: a new view, not a new hunt.
- `--isin` is no longer argparse-required; exactly one of
  `--isin` / `--portfolio` is enforced after parse.
- Tests: cohort split; consensus counts; cross-sectional
  permutation reproducibility and a synthetic "every inferential
  fund has a January premium" rejection; a short-history fund
  appears only in the descriptive roster; `--isin` + `--portfolio`
  and `--portfolio` + `--rule` fail; single-fund insufficient
  history is labelled `DESCRIPTIVE — insufficient history` with
  no p-value lines; single-fund p ≥ 0.05 uses the softer
  sentence; the caveat names the inferential *N*; a synthetic
  shared-month book appears in the equal-weight table and its
  omnibus. Coverage floor 90%.
- README / CLAUDE.md mention `--portfolio` when this lands.

## Deferred (not in this ADR)

- Held-only vs configured-universe as a switch (`--held`).
  v1 is the configured universe with prices.
- A portfolio-level seasonal *contribution* simulator (shift
  every fund's August `C` to a later month). Single-ISIN
  `--evaluate` (ADR-0028) tests one series after this freeze.
- Conditional / regime seasonality (ADR-0026 deferred).
