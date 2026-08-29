# ADR-0038 — `performance --isin`: restrict the book to one holding

**Scope:** add an optional `--isin` flag to `e1f performance` that restricts every
view (snapshot, `--series`, `--metrics`, `--diff`, `--contrib`) to one holding.
No new valuation or metric math. Headline use is `performance --series N --isin X`.

## Context

`--series N` (ADR-0030) lists the portfolio TOTAL for each trading day. Watching
one fund's path meant scanning the snapshot table or subtracting it out of the
book by eye. The snapshot already computes per-ISIN rows; the missing piece was
a subject filter, not a new series.

## Decisions

**The book becomes that fund.** `--isin X` filters `position_timeline` to `{X}`
before any view runs. Snapshot, `--series`, `--metrics`, `--diff`, and `--contrib`
then operate on a one-holding book with the same `_snapshot` / `_snapshot_total`
path they already use. No new valuation, no second series builder.

**`--series N --isin X` is the same contract as `--series N` on that book.** Each
row for day `D` equals `performance --as-of D --isin X`'s TOTAL. Trading days
are that ISIN's closes only (weekends/holidays still drop out with no hardcoded
calendar). Days before the first holding, or with nothing valuable, are skipped
exactly as ADR-0030 does for the whole book. This supersedes ADR-0030's
"portfolio TOTAL only" when `--isin` is set.

**Unknown ISIN is an error.** `--isin` must name a holding (an ISIN present in
the position timeline). A missing or never-traded ISIN exits 1 and lists the
held ISINs. An empty book still prints the existing "No ETF holdings" message
(the filter never runs). Lookup is case-insensitive after strip/upper.

**Banners name the subject.** Unfiltered titles stay `Portfolio …`. With `--isin`
they become `Name (ISIN) …` so a one-fund table is not mistaken for the book.

**No new columns.** Series rows stay date-keyed; the ISIN lives in the banner.
WTER / Fee€/yr on a one-fund book are that holding's TER and TER × MktVal
(missing TER is still `n/a`). `--sort` stays inert under `--series` (ADR-0030).

**Single module (`src/e1f/performance.py`).** The layer contract (ADR-0003) is
unchanged. `common` is not extended.

## Implementation

`_normalize_isin(raw)` — `None` stays `None`; otherwise strip/upper; empty after
strip is rejected.

`_restrict_timeline(timeline, isin, config_path)` — `{isin: events}` or
`ValueError` with the held list.

`_require_timeline(db_path, config_path, isin)` — load, print the empty-book
message and return `None`, or restrict.

`--isin` is optional argparse. `main()` normalizes it and threads it into every
`_cmd_performance*`.

## Invariance

Because the filter is applied before `_snapshot` / `_series_rows`, each
`--series N --isin X` row must equal `performance --as-of <that day> --isin X`'s
TOTAL, column by column, to the cent (same shared-column comparison as
ADR-0030). An explicit test asserts this on a two-fund book, so the unfiltered
TOTAL cannot accidentally satisfy the check. A second test asserts that a
second fund's close dates do not appear in `X`'s series.
