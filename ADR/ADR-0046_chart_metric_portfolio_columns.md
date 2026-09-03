# ADR-0046 — `performance --chart-metric`: portfolio-derived columns

**Scope:** add six new `--chart-metric` choices to `e1f performance --chart` —
`pweight`, `ter`, `fee_yr`, `units`, `avg`, `last_px` — the numeric columns
`portfolio` already reports, now chartable alongside the existing
`pnl/pnl_eur/xirr/twr/cagr/vol/maxdd/value/cost`. No new subcommand, no change
to `portfolio` itself, no EUR conversion added anywhere.

## Context

`--chart-metric` already covers return/risk/valuation metrics; the only things
missing were the columns `portfolio` shows that `performance` didn't: book
weight, TER, estimated annual fee, share count, average cost, and last close.
Promoting `--chart` to its own verb was considered and rejected — it would
duplicate `performance`'s snapshot/series/as-of scaffolding for no new
capability, and `--chart`/`--chart-metric` already compose with the snapshot
and `--series` views a new verb would have to rebuild.

## Decisions

**Six new tokens, reusing ADR-0037's vocabulary.** `ter`, `fee_yr`, and `units`
are exactly ADR-0037's canonical `--sort` tokens for those quantities; `avg`
and `last_px` are its command-specific tokens (already reserved, never
assigned). `pweight` is deliberately **not** `weight` — ADR-0037 already
defined `weight` as market-value share (`--contrib`'s `Weight` column,
`_sort_key`'s `"weight": row.market_value`); `pweight` is `portfolio`'s
cost-basis `Weight` (`holding_weight_pct`), a different basis under a
different name so the two are never confused on one chart.

**`avg` / `last_px` / `units` require `--isin`.** A per-share figure (price,
average cost, share count) is only comparable across time for one fund —
denomination and split history make it meaningless as a cross-fund axis in
the snapshot bar chart or `--all-holdings`. Requesting any of them without
`--isin` is a `ValueError` before any chart is built.

**`last_px` stays native; no EUR conversion.** `avg` is EUR only because
`PerformanceRow.cost` is already EUR by the ADR-0011 contract (trades are
booked at EUR transaction price); `last_px` is the fund's own pinned quote
currency (`series.currency`) and the axis/title say which. An earlier version
of this decision converted `last_px` to EUR via `convert_to_eur`; that was
reverted — the chart is meant to show the fund's own tape, not a EUR-book
view of it. `portfolio` itself is untouched: no `last_px`-to-EUR conversion
was added there either.

**`pweight`'s denominator is always the whole book.** Unlike every other
per-holding metric, `pweight` does not answer "what's this row's share of
what's on screen" — it answers "what's this row's share of everything held",
so `--isin X --chart-metric pweight` must show `X`'s share of the *whole*
book over time, not a constant 100%. The denominator is computed from the
book's cost basis (pure position math, no pricing needed — cost basis is
known even for an unpriceable holding), reloading the unrestricted timeline
when `--isin` narrowed the view; the unfiltered snapshot/`--all-holdings`
paths already hold the whole book, so they reuse it directly with no extra
query.

**`pweight`/`ter`/`fee_yr` are per-holding only — not the portfolio-total
series.** `--chart-metric pweight` on the plain (non-`--isin`,
non-`--all-holdings`) `--series` total would be a constant 100% (same
reasoning ADR-0030 dropped `P&Lctr` from that series for). `ter`/`fee_yr`
avoid re-deriving ADR-0031's market-value-weighted `WTER`/`Fee€/yr` as a
second, redundant "total" chart line — that blend already has a home, the
`--series` table itself. All three remain available on the snapshot,
`--all-holdings`, and `--isin` views, which are genuinely per-holding.
Requesting one of them on the total series is a `ValueError` before any
chart is built.

**No new `common` primitive.** This is a mode of the existing `performance`
command; the layer contract (ADR-0003) is unchanged. `fee_yr` reuses the
existing `annual_fee_estimate` primitive (`ter/100 × market_value`) rather
than reimplementing it.

## Implementation

`PerformanceRow` gains `shares: float`, `last_close: float | None` (native),
`currency: str | None`, and `ter: float | None`, populated in `_build_row`
from data it already has (`shares` from `_position_asof`, `last_close` from
`close_asof(series, as_of)`, `currency` from `series.currency`, `ter` from
config via the new `_etf_ter`). A post-hoc `cost_weight: float | None` mirrors
`pnl_contribution` — assigned by `_assign_cost_weights(rows, book_cost)` once
the denominator is known, never at row-construction time.

`_book_cost_on(timeline, day)` sums cost basis across every ISIN still held
in `timeline` on `day` — pure position math, no DB/pricing. `_whole_book_cost`
picks the cheap path (reuse `rows`' own cost sum) when unfiltered, or reloads
the unrestricted timeline via `_require_timeline(..., None)` when `--isin`
narrowed it.

`_total_row` passes `shares`/`last_close`/`currency`/`ter` through from the
single included holding when the aggregated set has exactly one row (the
`--isin`-restricted case) — the only place a per-share/native figure needs to
survive the TOTAL-row aggregation used by `--series --isin`'s chart line.

`_row_metric_value` gains the six branches; `fee_yr` calls
`annual_fee_estimate(row.ter, row.market_value)`. `last_px`'s label is
currency-dependent, so it is not in the static `_CHART_METRIC_LABEL` table;
`_metric_label(metric, currency)` special-cases it and falls back to the
static table otherwise. All three renderers (`_render_snapshot_chart`,
`_render_series_chart`) resolve the one held fund's currency from their row
data and call `_metric_label`; `_render_holdings_series_chart` is untouched
since `last_px` (and `avg`/`units`) can never reach it — `--all-holdings`
implies `isin is None`, which the `--isin`-only gate already refuses.

Gating lives in `main()` next to the existing `--chart` compatibility checks:
`_ISIN_ONLY_CHART_METRICS` (`avg`/`last_px`/`units`) refused when `isin is
None`; `_HOLDING_ONLY_CHART_METRICS` (`pweight`/`ter`/`fee_yr`) refused when
`series_n is not None and not all_holdings and isin is None` (the
portfolio-total series).

## Consequences

`data/glossary.md`'s Fees section documents `ter`/`fee_yr` as also reachable
from `--chart`; `pweight`, `units`, `avg`, `last_px` get new entries
cross-referencing the existing `Weight` (portfolio cost-basis) and `TER`/
`Fee€/yr` entries so the vocabulary has one home. No existing chart metric,
column, or command output changes.
