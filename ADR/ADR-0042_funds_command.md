# ADR-0042 — `e1f funds`: configured-universe candidate table

**Scope:** a new stable command that lists every configured ETF with cost
metadata, windowed stand-alone return/risk, and explicit day-count / missing-day
coverage. Does not change `config list` (YAML inventory), `performance`
(holdings + cash flows), or `benchmark` (book vs indices).

## Context

`config list` shows identity but hides TER / distribution / currency already
pinned in the YAML (ADR-0007). `performance` and `portfolio` are holdings-only
(`performance --isin` refuses an unheld ISIN, ADR-0038). `benchmark` scores the
*book* against seven indices. There is no table that treats the configured
universe as a candidate set — including funds not yet held — so "where to
invest next" has no home.

Comparing funds on since-inception TWR is dishonest when listings differ in
age. A Friday-to-Tuesday hole is still one return, and Vol treats it as one
day (the gap-bridged caveat already labelled on `benchmark`, ADR-0033). `n`
alone does not say how many trading days were skipped.

## Decisions

**A new command, not a `config list` flag.** Config stays YAML CRUD. `funds`
is the decision table.

**One row per configured ISIN.** Held funds are marked `*` (same convention as
`benchmark`). Unpriced or unconvertible rows stay in the table as `—` /
UNAVAILABLE, never dropped.

**`--from` / `--as-of` is the analysis window.** `--as-of` defaults to today.
`--from` is optional: omitted, each fund starts at its first EUR close. When
`--from` is set, closes are clipped to `[--from, --as-of]`. A fund that listed
later **stays listed**. Its `From` is the first EUR close actually used (after
`--from`); TWR / Vol / MaxDD and `n` use only that shorter series. Days before
listing are not missing data.

**`From` / `n` / `Gap` on every row.**

| Column | Definition |
|---|---|
| `From` | First EUR close actually used |
| `n` | Count of gap-bridged EUR **returns** that feed TWR / Vol / MaxDD |
| `Gap` | Interior holes: venue-consensus trading days this ISIN **spans** but lacks (the same vote as `validate`; primitive graduated to `e1f.common.quality`) |

A late `From` is a short series. `Gap` is a skipped fetch inside the series.
They are not the same. Pre-listing days and genuine venue holidays are not
`Gap`. A thin venue (fewer than three funds) cannot vote and reports `Gap = 0`
(under-reporting beats crying wolf). `--explain` lists gap dates.

The window is applied to **closes**, then returns are pairwise on what remains.
The first return does not bridge from a close before `--from`.

**TWR / Vol / MaxDD are the fund's own series**, not money-weighted and not
vs the book. Same definitions as `performance` (`Π(1+r)−1`, sample stdev ×√252,
wealth-index MaxDD). XIRR is out: it is cash-flow-weighted and meaningless for
a fund never bought. Sharpe stays €STR-gated (ADR-0033).

**Filters and sort.** `--unheld`, `--class`, `--dist`. `--sort` uses canonical
tokens (ADR-0037) plus command-local `from` / `gap` / `dist` / `ccy`. Default
order is `isin`.

**Deferred (same command, later increments):** full justETF investment-focus
on the table and `--group focus`; clone ρ / nearest held; β vs MSCI World;
current drawdown. Look-through overlap stays experimental.

## Invariance

On a fixture with EUR closes 100, 110, 99 (returns +10%, −10%):

- TWR = `1.10 × 0.90 − 1` = −1%
- MaxDD = `0.99 / 1.10 − 1` = −10%
- `n` = 2

On the same calendar, a fund whose first close is after `--from` prints that
later `From`, a shorter `n`, and `Gap = 0` for the pre-listing days. A
same-venue peer hole inside a covering span increments `Gap` by one and is
bridged into `n` (not filled).
