# ADR-0036 — `deposits --group week|month|year`: deposit-vintage aggregation

**Scope:** add a `--group week|month|year` flag to `e1f deposits` that collapses the
per-buy impact table into one row per calendar period **and** fund (deposit
vintages), leaving all reconciliation guarantees untouched. Under `--group` the top
summary block is replaced by an in-table `── ALL ──` grand-total row.

## Context

`e1f deposits` (ADR-0033 Phase C) prints one row per BUY. With a savings plan
that is many small, near-identical rows, and the useful question they bury —
*how are my 2024-vintage deposits doing versus 2025's?* — has to be answered by
eye. A coarser view that groups deposits by when they were made surfaces that
cohort/vintage lens directly.

## Decisions

**Group by period × ISIN, not period alone.** A bucket is one `(period, fund)`
pair, never all funds of a period merged. Period-only would blend distinct funds
into a single money-weighted `Ret%` that means little; keeping the fund axis
preserves per-fund meaning and, critically, makes valuability clean (below). The
per-buy default table stays the finest grain; `--group` is a strictly coarser
view of the *same* buys.

**Grouping is a partition, so nothing about the totals changes.** Amounts and
values sum within a bucket; `%P&L` is reassigned across the grouped rows by the
same `_assign_pnl_shares`. Because the grouped rows are exactly a repartition of
the buys the summary already sums, `Invested` / `Market value (reported)` /
`Organic gain` / `ROIC` and the reconciliation with the `performance` TOTAL are
identical whether or not `--group` is passed. The summary is always computed
from the ungrouped impacts.

**A bucket is unvaluable iff its fund is.** All buys of one ISIN share one
`unit_value` (computed once per ISIN in `deposit_impacts`), so every buy in a
`(period, ISIN)` bucket is valuable together or `None` together — a bucket never
mixes valued and unvaluable buys. An unvaluable bucket renders `—`, is excluded
from totals, and disclosed exactly like a single unvaluable row today; no
partial-bucket accounting or new disclosure path is introduced. This is the
decisive reason for period × ISIN over period-only.

**Each period is a section: heading, funds, subtotal.** The grouped view has no
date column — the period is printed as a section heading, then the shared column
header, the fund rows, and a `── total ──` row summing their `Amount€ / Value€ /
Gain€` with `Ret% = Gain/Amount` and `%P&L = Σ` of the members' shares. Like the
grand summary, the subtotal is over the *valuable* funds only (unvaluable funds
still show in the detail as `—` but are excluded), so the row is internally
consistent (`Gain = Value − Amount`) and the subtotals reconcile: `Value`
subtotals sum to the reported market value and `%P&L` subtotals sum to 100%. A
period whose funds are all unvaluable still lists those rows, but omits the
`── total ──` (a `0.00 / —` subtotal under paid amounts looks broken). Periods
are always label-ordered (`--reverse` flips period order); `--sort` applies to
the funds *within* each period, and `--reverse` also reverses that within-period
order. Sorting by `date` inside a period is a no-op (every row shares the period
label).

**Period labels.** `month` → `YYYY-MM`, `year` → `YYYY` (ISO-date prefixes
`date[:7]` / `date[:4]`). `week` → ISO-8601 `YYYY-Www` from
`datetime.date.isocalendar()` (Monday-start, week-numbering year; a late-December
day can fall in week 1 of the next ISO year). Week numbers are zero-padded so
labels sort lexicographically. Day granularity was considered and rejected as
noise for a buy-and-hold book.

**No isin grouping axis — that is the `portfolio` view.** A `--group isin` (one
aggregate row per fund across all dates) was prototyped and dropped: it collapses to
exactly what `portfolio` already reports, so it earned no place here. Per-fund ROIC
and organic gain are already the `Ret%` / `Gain€` columns on every `(period, fund)`
row.

**Under `--group` the top summary block is dropped for a bottom `── ALL ──` row.**
The labelled Invested/Reported/Organic/ROIC block prints only in the ungrouped view;
under `--group` the same four figures live in a final `── ALL ──` grand-total row
(the same columns as the subtotals), so everything sits in one table with no
duplicated labelled block. The `── ALL ──` row is `_total_row` over the grouped rows
— identical to the old summary because grouping is a partition — and its `%P&L` foots
to 100%.

**No new columns, no new metrics.** The grouped table reuses `Amount€ / Value€ /
Gain€ / Ret% / %P&L`; it drops the per-buy `Date` column (the period is the
section heading instead) and adds nothing. The glossary gains no new entry, only a
note on the `deposits` row grains. The buy-and-hold SELL refusal and the
future-trade cutoff are upstream in `deposit_impacts` and so apply unchanged.

**Single module (`src/e1f/deposits.py`).** `group_impacts(impacts, by)` is a pure
aggregation over the existing `DepositImpact` list; no change to `common`, no new
valuation. The layer contract (ADR-0003) is preserved.

## Implementation

`_period_key(day, by)` maps a `YYYY-MM-DD` buy date to the period label. `group_impacts`
buckets by `(period_key, isin)`, sums `amount`, sums `value` (or `None` when any
member is unvaluable), carries the fund name, then calls `_assign_pnl_shares` on
the grouped rows.

`_row_cells(impact)` renders the shared metric columns; `_format_row` prefixes the
per-buy `Date` column, `_grouped_header` / `_row_cells` omit it (the period is a
section heading). `_total_row(members, *, label)` builds a total over the valuable
members; `_subtotal_row` is `_total_row(…, label="── total ──")` and the grand total
is `_total_row(grouped, label="── ALL ──")`.

`_render_grouped` prints the period sections (heading + header + fund rows +
subtotal when the period has a valuable fund). `--group {week,month,year}`
(default `None`) threads through `_cmd_deposits` and `main`: when set it skips
the top summary block, renders the sections, and appends the `── ALL ──`
grand-total row.

## Reconciliation

Because grouping only repartitions the buys the summary already sums, a pinned
test asserts that `Σ` grouped `amount` equals `summary.invested` and `Σ` grouped
`value` equals `summary.reported` (and thus the `performance` TOTAL market value),
to the cent, over a multi-year single-fund book with hand-computed vintage totals.
A second pin asserts ISO-week labels across the calendar-year boundary
(2024-12-29 → `2024-W52`, 2024-12-30 → `2025-W01`).
