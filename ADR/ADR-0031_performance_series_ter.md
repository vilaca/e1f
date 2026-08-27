# ADR-0031 — `performance --series` weighted TER + estimated annual cost columns

**Scope:** add two trailing columns to the `performance --series` table
(ADR-0030) — the market-value-weighted TER (`WTER`) and the estimated annual fee
in EUR (`Fee€/yr`) — so the daily timeline also shows the book's cost of
ownership as it evolves. `portfolio` already reports both, cost-basis weighted.

## Context

`portfolio` prints a weighted average TER and an estimated `~€/yr` in fees, both
weighted by cost basis (`total_paid`), because `portfolio`'s whole frame is
cost-basis. `--series` is a market-value timeline: every column is derived from
EUR market value. Carrying the fee view into the series makes it visible how
WTER and annual cost drift as the holdings mix and AUM change.

## Decisions

**Market-value weighted, not cost-basis.** A fund's TER is levied on AUM, so both
columns weight by each holding's EUR market value on the day:
`WTER = Σ(terᵢ × MktValᵢ) / Σ MktValᵢ` and `Fee€/yr = Σ(terᵢ/100 × MktValᵢ)`,
with `Fee€/yr = WTER% × Σ MktVal` by construction. This is internally consistent
with every other series column and answers "what am I paying per year at today's
value". It deliberately differs from `portfolio`'s cost-basis figure — mixing a
cost-basis metric into an otherwise market-value table would be the category
error `--diff` already avoids (ADR-0029).

**Missing TER dilutes (matches `portfolio`).** A holding with no TER metadata
contributes 0 to the fee but stays in the denominator, so `WTER` is the effective
rate across the whole book, not just the covered slice. When no held ISIN has a
TER (or nothing is valuable) both columns are `n/a` and the explanatory footnote
is suppressed.

**Trailing columns, shared columns untouched.** `WTER` and `Fee€/yr` are appended
after `CAGR`, so the nine columns the ADR-0030 invariance test pins (`MktVal …
CAGR`) are unchanged and still equal `performance --as-of <day>`'s TOTAL.

**No cross-command import.** `performance` does not import `portfolio`'s
`yearly_fee_est` (the layer contract, ADR-0003, bars command-to-command imports).
The fee arithmetic (`ter/100 × value`) is a trivial inline computation in
`_weighted_ter_cost`; graduating a shared helper to `common` was not worth it for
a one-line formula.

**`--series` only.** The snapshot and `--diff` tables are unchanged; the request
was scoped to the series.

## Implementation

`SeriesPoint(day, total, weighted_ter, annual_cost)` replaces the bare
`(day, PerformanceRow)` tuple `_series_rows` returned.

`_ter_by_isin(config, isins)` — `{isin: ter%|None}` from config metadata
(ADR-0007), built once per command.

`_weighted_ter_cost(rows, ter_by_isin)` — pure; market-value-weighted `WTER` and
`Fee€/yr` over a day's valuable per-ISIN rows, `(None, None)` when uncovered.

`_series_point(...)` — one `_snapshot` pass per day feeds both `_total_row` (the
shared columns) and `_weighted_ter_cost` (the TER columns), so no extra DB work.
