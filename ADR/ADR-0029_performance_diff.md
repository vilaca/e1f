# ADR-0029 — `performance --diff N`: signed change table

**Scope:** add a `--diff N` flag to `e1f performance` that reports the signed
change in market value, cost, and unrealized P&L over the last `N` calendar
days, per ISIN and portfolio-wide — without duplicating any valuation logic.

## Context

`e1f performance` produces a full point-in-time snapshot. Comparing two dates
requires running the command twice and subtracting columns by hand, ISIN by
ISIN — tedious and error-prone, especially when a position entered or left
the window and would be silently absent from one run.

## Decisions

**Reading A — "then's holdings at then's prices."** Each endpoint is valued
using its own share count and its own near-prior close. A fill inside the
window appears in ΔMktVal because you added money; ΔP&L cancels cost, so it
isolates pure gain change even under reading A. The alternative (reading B:
today's units priced at both dates) was not chosen because the return metrics
already answer that question better.

**Money columns only (ΔMktVal€, ΔCost€, ΔP&L€).** The rate/ratio columns
(XIRR, TWR, Vol, MaxDD, CAGR, P&L-contribution) are cumulative-since-inception
figures whose subtraction is a category error; they are dropped from diff
output rather than shown as meaningless numbers.

**Union of held ISINs.** Both entries (new position bought in-window, start
value = 0) and exits (position fully sold, end value = 0) appear as rows. An
ISIN not held at either endpoint does not appear.

**Calendar-day windows.** `N` is calendar days, not trading sessions.
`--diff N` composes with `--as-of`: `end = as_of`, `start = as_of − N`.
Plain `--diff N` (no `--as-of`) defaults to today as the end.

**Held-but-unpriceable endpoint → unavailable, never zero.** When an endpoint
holds an ISIN but has no close on/before that date, the delta is shown as `—`
and excluded from the TOTAL, matching the snapshot command's vocabulary for
unvaluable rows. A missing endpoint (ISIN not held → shares = 0) contributes
zero; these two cases are distinguished by `_diff_rows`.

**No new valuation code.** Both endpoints call the existing `_build_row` /
`_value_on` / carry-forward path. Carry-forward is flagged `~` on the affected
rows, and a `~ ΔMktVal estimated` note appears below the table.

**Single module (`src/e1f/performance.py`).** `--diff` is a mode of the
existing command. No change to `common`; the layer contract (ADR-0003) is
preserved.

**`portfolio --diff` explicitly deferred.** `portfolio`'s market-value column
uses a single latest close with no date parameter and no FX conversion. Adding
date-aware valuation there would duplicate `performance`'s core inside a
command whose contract declares "no market data".

## Implementation

`_build_endpoint_rows(db_path, config_path, meta_path, timeline, as_of)` —
builds `{isin: PerformanceRow}` for all ISINs held at `as_of` (including
unvaluable rows, which carry `valuable=False`).

`_diff_rows(start_rows, end_rows)` — pure merge function (no DB). Iterates the
union of ISINs; absent key = not held (contributes 0); `valuable=False` row =
held but unpriceable (delta unavailable). Returns `list[DiffRow]`.

`DiffRow` — `(isin, name, delta_market_value, delta_cost, estimated)`, with
`delta_pnl` as a property and `valuable` as `delta_market_value is not None`.

`--diff N` is validated in `main()` (not via argparse `type=`), returning exit
code 1 with a clear message for non-positive or non-integer values.

## Invariance

Because both endpoints reuse the existing valuation, `--diff N` output must
equal `performance --as-of end` minus `performance --as-of (end−N)` column by
column, to the cent. An explicit test asserts this on the TOTAL row.
