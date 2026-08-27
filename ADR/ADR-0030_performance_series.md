# ADR-0030 — `performance --series N`: daily cumulative totals table

**Scope:** add a `--series N` flag to `e1f performance` that lists the portfolio
TOTAL row for each trading day over the last `N` calendar days — one row per
day, the same cumulative-since-inception metrics the snapshot already computes,
without duplicating any valuation logic.

## Context

`e1f performance` values the book at a single point in time; `--diff N`
(ADR-0029) shows the net change between two endpoints. Neither shows the *path*:
how market value, P&L, XIRR, TWR and the risk metrics evolved day by day. Seeing
that path meant running `--as-of` repeatedly and copying TOTAL lines by hand.

## Decisions

**Cumulative since inception, not windowed.** Each row's metrics are valued
as-of that day over all history since the first trade — row for day `D` is
*identical* to `performance --as-of D`'s TOTAL line. `--series N` controls only
which days print (the last `N`), never the metric lookback. Windowed
(trailing-`N`-day) returns were considered and rejected as a different, more
exotic question the command does not claim to answer.

**Rows are trading days, defined by the price data.** A day gets a row only when
some held ISIN has a close in the DB within the window. Weekends and market
holidays (Dec 25 etc.) have no close, so they drop out — with **no hardcoded
holiday calendar**. A held ISIN with no close *on* a listed day is carried
forward (flagged `~`), exactly as the snapshot does; a day that predates the
first holding is dropped because its snapshot has nothing valuable (never a
phantom €0 row).

**Calendar-day window, composes with `--as-of`.** Like `--diff`: `end = as_of`,
`start = as_of − N`. Plain `--series N` defaults the end to today.

**Portfolio TOTAL only; `P&Lctr` dropped.** Rows are dates, so per-ISIN identity
(ISIN/Name) is gone. `P&Lctr` (a holding's share of total P&L) is always 100%
for the whole book, so it is dropped rather than printed as a constant. The
remaining columns match the snapshot TOTAL: `MktVal€, Cost€, P&L€, P&L%, XIRR,
TWR, Vol, MaxDD, CAGR`. Per-row `~` (stale close) and `*` (short history) flags
carry over, aggregated across the day's included holdings.

**`--series` and `--diff` are mutually exclusive**, rejected in `main()` with a
clear message + exit 1. In series mode `--sort` is inert (rows are always
date-ordered); `--reverse` flips to newest-first. `--show-status` / `--explain`
are snapshot/diff provenance concepts and are not wired into series output (every
priced day is trivially CALCULATED).

**No new valuation or metric math.** The snapshot loop body is extracted into
`_snapshot` (per-ISIN rows + series for a day) and `_snapshot_total` (the TOTAL,
or None when nothing is valuable). The snapshot command and `--series` both route
through it, so a series row equals `--as-of D`'s TOTAL by construction.

**Single module (`src/e1f/performance.py`).** `--series` is a mode of the
existing command; no change to `common` beyond reusing the already-exported
`load_price_series`. The layer contract (ADR-0003) is preserved.

## Implementation

`_snapshot(db, config, meta, timeline, as_of) → (rows, holdings)` — the single
definition of "the portfolio as of a day", shared with `_cmd_performance`. Events
are capped to `as_of` before `build_series`, since the return-metric flow helpers
read `series.events` unfiltered.

`_snapshot_total(...) → PerformanceRow | None` — the TOTAL row, None when no held
ISIN can be valued on the day.

`_trading_days(db, timeline, start, end)` — sorted days with a close in the window
across held ISINs (via `load_price_series`).

`_series_rows(db, config, meta, timeline, *, start, end) → list[(day, PerformanceRow)]`
— `_snapshot_total` per trading day, skipping days with nothing valuable.

`--series N` is validated by the shared `_validate_positive_int` (renamed from
`_validate_diff`), returning exit 1 for non-positive or non-integer values.

## Invariance

Because both paths reuse `_snapshot_total`, each series row's TOTAL must equal
`performance --as-of <that day>`'s TOTAL, column by column, to the cent (the
snapshot's `P&Lctr` column, always 100%, is the only one omitted). An explicit
test asserts this.
