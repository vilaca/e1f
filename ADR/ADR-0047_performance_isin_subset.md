# ADR-0047 — `performance --isin` repeatable: compare a subset of holdings

**Scope:** let `--isin` repeat so `e1f performance` restricts every view to a
*subset* of holdings, not just one. No new valuation or metric math. Headline use
is comparing two funds: `performance --isin A --isin B` (table) and
`performance --series N --chart --all-holdings --isin A --isin B` (per-holding
line chart). Supersedes ADR-0038's "one holding" wording; the single-`--isin`
contract is the one-element case and is unchanged.

## Context

ADR-0038 made `--isin X` restrict the book to one holding. The natural next ask
is "compare these two funds" — the default table already lists every holding as
its own row, but there was no way to narrow to an arbitrary pair, and the
per-holding series chart (`--all-holdings`, ADR-0046 era) only decomposed the
*whole* book. The missing piece was a repeatable subject filter, not new math.

## Decisions

**The book becomes those funds.** `--isin` uses argparse `action="append"`.
`_normalize_isins` strips/uppers each value and de-duplicates preserving input
order; `None`/empty → the whole book. `_restrict_timeline` filters
`position_timeline` to the named set (raising with the held list if *any* value
is not a holding) before any view runs. Snapshot, `--series`, `--metrics`,
`--diff`, and `--contrib` then operate on the restricted book with the same
`_snapshot` / `_total_row` path they already use. The subset TOTAL, P&L shares,
and Cariño contributions are therefore *of the subset* — a coherent sub-book,
exactly as a one-fund `--isin` already behaved.

**Banners name the subset.** One ISIN keeps ADR-0038's `Name (ISIN) …`; a subset
reads `N holdings (A, B) …` so a narrowed table is never mistaken for the book.

**Charts compare the subset.**
- The **snapshot** `--chart` already draws one bar per holding, so a subset is a
  bar-per-fund comparison with no extra logic.
- The **series** `--chart` treats the restricted book as one combined total line
  (whole book, or the subset) unless `--all-holdings` decomposes it into one line
  per holding. `--all-holdings` now honours a subset (previously whole-book only).
  So the two-fund comparison chart is
  `--series N --chart --all-holdings --isin A --isin B`.
- `--chart-overlay` composes with both: on the combined-total line it overlays
  the metrics (as before); on the per-holding chart it collapses the per-metric
  subplots onto one shared panel — one line per holding×metric, colour by
  holding, dash by metric (mixed units, the user's explicit choice).

**Per-share / per-book metrics still need the right subject (ADR-0046).**
`units`/`avg`/`last_px` require a *single* `--isin`: a per-share or native-currency
figure isn't comparable across funds (and `last_px`'s axis unit is one fund's
currency). `pweight`/`ter`/`fee_yr` stay blocked on a *combined-total* series —
now defined as the whole book **or** a 2+ ISIN subset without `--all-holdings`,
since a multi-holding TOTAL carries no single TER/weight. They compose with a
single `--isin` (that total IS the fund) and with `--all-holdings` (per-holding).
`pweight`'s denominator is always the WHOLE book: a restricted per-holding series
reloads the unrestricted timeline for the per-day cost basis.

**Single module (`src/e1f/performance.py`).** The layer contract (ADR-0003) is
unchanged. `common` is not extended.

## Implementation

`_normalize_isins(raw)` — `list[str] | None`; strip/upper/dedupe, reject empties.

`_restrict_timeline(timeline, isins, config_path)` — `{i: events}` over the named
set, or `ValueError` listing the held ISINs when any is missing.

`_require_timeline`, `_subject_phrase`, `_whole_book_cost`, and every
`_cmd_performance*` thread `isins: list[str] | None` instead of `isin: str | None`.

`_per_isin_series` gained `full_timeline`: when the plotted timeline is an
`--isin` subset, `pweight` weighs each fund against that full book's per-day cost.

`_render_holdings_series_chart` gained `overlay`; `_render_holdings_overlay`
draws the single-panel holding×metric overlay.

## Invariance

Each `--series N` subset row (combined total) equals `performance --as-of D` on
the same subset book's TOTAL — the ADR-0038 invariant, now over the subset. The
subset TOTAL is the sub-book of exactly the named funds (a pinned MktVal test
asserts the third fund is excluded), and the `--all-holdings` `pweight` line
weighs each fund against the whole book, not the subset (a pinned test asserts
33.33% for a one-of-three-equal-cost fund).
