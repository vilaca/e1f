# ADR-0053 — `funds` price-position columns, and why they are not valuation

**Scope:** add three descriptive columns to `funds` — distance from the 50-day
mean, distance from the 200-day mean, and distance from the trailing 52-week high
— computed on the gap-bridged EUR series the command already builds. Plus the
glossary entry that states what they do **not** mean.

## Context

`funds` reports what a fund *returned* over a window (TWR, Vol, MaxDD) and how
well it is *covered* (From, n, Gap). It reports nothing about where the current
price sits inside its own recent range, so the natural screen — "funds with a
positive long-run return that are currently below their average" — has no tool
behind it and gets hand-rolled against `prices`.

The screen is worth supporting. Run over the current universe it isolates six
funds, four of which are not obvious from any existing column, and it surfaced the
one unheld candidate (`IE00B4ND3602`) whose low correlation to the book survived
scrutiny.

It is also the most dangerous thing this repo could add without a caveat attached,
because its plain-language reading is backwards. "Below its average" sounds like
*cheap*. It is not a valuation statement at all — there is no earnings yield, no
NAV discount, no spread in it. It is a **momentum** statement, and the sign of the
historical relationship runs the other way: in the time-series momentum
literature, price below a long moving average has been associated with *lower*
subsequent returns, not higher. A user reading the column as "on sale" would be
buying negative momentum and calling it value.

The repo's own evidence points the same direction. ADR-0020 through ADR-0023
established that dip-reserve and daily-dip contribution strategies lose to
constant DCA on this data, and ADR-0026 through ADR-0028 established that no
calendar rule here clears its own significance floor. Shipping a "what's cheap"
column into that context, unqualified, would invite exactly the strategy the
backtests already rejected.

## Decisions

### 1. Three columns, all price-position, none forecasting

`vs50d`, `vs200d`, `vs52wHi` — percentage distance of the latest EUR close from
each reference. Descriptive statistics of the stored series, nothing more.

### 2. Computed on the same gap-bridged EUR series as TWR/Vol/MaxDD

One series definition per command. A moving average built on a different
gap-handling rule than the risk columns beside it would be silently inconsistent.

### 3. Insufficient history renders `n/a`, never a shortened window

A 200-day mean needs 200 observations. Quietly computing it from 40 would produce
a number that looks like the others and means something else. `IE0001UQQ933` (100
observations) shows `n/a` for `vs200d` and a value for `vs50d`.

### 4. The columns are `--sort` keys, and the glossary carries the warning

`vs50d`, `vs200d`, `vs52w` join the sort tokens (ADR-0037). The glossary entry for
each states plainly: *this is a momentum/position descriptor, not a valuation
measure; a price below its moving average has historically preceded lower, not
higher, returns; see ADR-0020–0023 for this repo's own findings on dip-timing.*
This is the `Where` contract that the project convention requires of a new stable
metric, and here the "what it is useful for" half is load-bearing.

### 5. No derived screen, flag or ranking

No `--below-average` filter, no composite "value" score, no highlighting. Columns
and sort keys only. A built-in screen would be the tool asserting the strategy the
glossary entry warns against, and would encode a threshold nothing here justifies.

### 6. Status is CALCULATED

Unlike ADR-0049/0050, these are descriptive facts about stored prices with no
estimation, no in-sample fitting and no forward claim — nothing to bound. A fund
flagged by ADR-0048 is unaffected: mark quality attenuates *covariance*, not the
level of a close, and a moving average of noisy marks is still that fund's mean
close.

## Implementation

`funds.py`: `_price_position(series) -> (vs50d, vs200d, vs52w_high)` on the
existing gap-bridged EUR series; three columns; three sort tokens registered
through the ADR-0037 canonical mapping.

`data/glossary.md`: **vs50d**, **vs200d**, **vs52wHi**, each carrying the
not-valuation warning and its `Where` contract.

## Invariance

A pinned-date test asserts all three values for a fixture fund against
hand-computed means and a hand-identified 52-week high, and asserts `n/a` for
`vs200d` on a fund with 100 observations while `vs50d` still renders. Adding the
columns changes no existing `funds` output value; the ADR-0042 column contract is
extended, not altered.
