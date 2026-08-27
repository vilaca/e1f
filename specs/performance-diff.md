# Spec: `performance --diff N`

Status: ready-for-agent

## Problem Statement

When I run `e1f performance`, I see a full point-in-time snapshot of my book — market value, cost, P&L, and return metrics as of one date. What I can't see is **what changed over a recent window**. To answer "how did my book move over the last week?" I have to run `performance` twice (once at today, once with `--as-of` a week ago) and subtract the columns in my head, ISIN by ISIN. That's tedious and error-prone, and it's easy to miss a position that entered or left the window.

## Solution

Add a `--diff N` flag to `performance` that reports the **change** in my holdings over the last `N` calendar days, in EUR, per ISIN. Instead of a snapshot it prints a signed change table: for each ISIN, how much its market value, cost basis, and unrealized P&L moved between two dates, plus a portfolio TOTAL. The command values my book at both endpoints using the same EUR/FX valuation the normal snapshot uses, and subtracts — so I get an honest "what moved" view without doing the arithmetic myself.

Only the **money** columns are diffed (MktVal€, Cost€, P&L€). The rate/ratio columns (XIRR, TWR, Vol, MaxDD, CAGR, P&L-contribution) are cumulative-since-inception figures whose subtraction is a category error, so they are dropped from diff output rather than shown as misleading numbers.

## User Stories

1. As an ETF investor, I want to run `e1f performance --diff 7`, so that I can see how my book moved over the last 7 calendar days without running the command twice and subtracting by hand.
2. As an investor, I want `--diff N` to count `N` in **calendar** days (1 = yesterday, 7 = a week ago), so that the window matches how I think about time rather than trading sessions.
3. As an investor, I want each row to show the **change** in market value, cost basis, and P&L for one ISIN, so that I can see per-holding movement at a glance.
4. As an investor, I want a **TOTAL** row summing each delta column, so that I can see the whole-book change in one line.
5. As an investor, I want deltas rendered with explicit signs (`+1,240.50` / `−310.00`), so that gains and losses are unmistakable.
6. As an investor, I want a **dated header** naming both endpoints (e.g. "Performance change 2025-12-24 → 2025-12-31 (EUR)"), so that I know exactly which window I'm looking at.
7. As an investor, I want `--diff` to **compose with `--as-of`**, so that `--as-of 2025-12-31 --diff 7` inspects the week ending Dec 31 rather than only the most recent window.
8. As an investor, I want plain `--diff 7` (no `--as-of`) to mean "now vs 7 days ago", so that the common case needs no extra flags (since `--as-of` defaults to today).
9. As an investor, I want each endpoint valued with **that date's actual holdings at that date's close** (a true historical snapshot), so that the change reflects what I really held and what it was really worth — not a hypothetical.
10. As an investor who bought a new ISIN inside the window, I want that position to appear with a **positive** delta equal to its current value (start = 0), so that new money shows up.
11. As an investor who fully sold an ISIN inside the window, I want that position to still appear with a **negative** delta down to zero (end = 0), so that a liquidation is visible rather than silently dropped.
12. As an investor, I want the rows to be the **union** of ISINs held at either endpoint, so that both entries and exits during the window are represented.
13. As an investor, I want an ISIN I did **not** hold at the earlier date to contribute a start value of **zero**, so that its delta is simply its current value.
14. As an investor, I want the rate/ratio columns (XIRR, TWR, Vol, MaxDD, CAGR, P&L-contribution) **omitted** from diff output, so that I'm not shown gibberish from subtracting cumulative-since-inception figures.
15. As an investor whose window endpoint lands on a weekend or holiday, I want the book valued using the **nearest-prior close** (carry-forward), so that a non-trading endpoint doesn't break the diff.
16. As an investor, I want a row flagged as **estimated** (`~`) when either endpoint was priced by carry-forward from a stale close, so that I know the delta rests on carried data.
17. As an investor whose two endpoints happen to resolve to the **same** prior close, I want the deltas to legitimately show **0**, so that "no new market data" reads as "no change" rather than an error.
18. As an investor who **held** a position at an endpoint but has **no close on/before** that date to price it, I want that row's delta shown as **unavailable** (`—`) and **excluded from TOTAL** with a note — never collapsed to zero — so that an unpriceable position isn't fabricated into a fake delta.
19. As an investor, I want `--diff` to keep working with `--sort`, sorting by the delta values, so that I can rank movers.
20. As an investor, I want `--show-status` / `--explain` to keep working in diff mode, reusing per-row Status, so that provenance stays available.
21. As an investor, I want `--diff` with a non-positive or non-integer `N` to be rejected with a clear message, so that I can't ask for a meaningless window.
22. As an investor, I want the diff to reuse the exact EUR/FX valuation the normal snapshot uses, so that a diff and two manual snapshots would agree to the cent.
23. As a maintainer, I want the decision recorded in an ADR, so that the reading-A / money-columns-only / union choices are documented alongside the existing performance ADRs.

## Implementation Decisions

- **Module touched:** `src/e1f/performance.py` only. No new command module; `--diff` is a mode of the existing `performance` command. No change to `common`.
- **Flag:** `--diff N`, integer `N ≥ 1`, calendar days. Composes with `--as-of` (which defaults to today). No mutual-exclusion constraint — the two flags cooperate: `end = as_of`, `start = as_of − N` (calendar subtraction).
- **Two-endpoint valuation:** compute the normal per-ISIN rows at `start` and at `end` using the **existing** row-building path (`_build_row` / `_value_on` / carry-forward), once per endpoint. No new pricing code — both endpoints call the current valuation at their respective dates.
- **New pure merge function** (the one new seam), e.g. `_diff_rows(start_rows, end_rows)`:
  - Input: the two endpoints' computed rows keyed by ISIN.
  - Output: signed delta rows over the **union** of ISINs.
  - Per money column (MktVal€, Cost€, P&L€): `delta = end_value − start_value`, treating a missing endpoint (ISIN absent = `shares == 0`) as **0**.
  - Distinguishes `shares == 0` (clean 0) from `shares > 0 but unpriceable` (endpoint value is `None`/unavailable → delta unavailable, row excluded from TOTAL).
  - Propagates an `estimated` flag when either endpoint's value is carried-forward.
  - Rate/ratio fields are not carried into the delta row (dropped).
- **Output shape (diff mode):** header line naming both dates and `(EUR)`; columns `ISIN · Name · ΔMktVal€ · ΔCost€ · ΔP&L€`; signed formatting; a TOTAL row summing each delta column over priceable rows; existing-style `~ estimated` and `—`/excluded-from-TOTAL notes.
- **Reused vocabulary:** `~ estimated` (carry-forward), UNAVAILABLE / excluded-from-TOTAL, and the `Status` / `--show-status` / `--explain` provenance mechanism (ADR-0014) all carry over unchanged.
- **Sorting:** `--sort` applies to delta values in diff mode; a sort field that no longer exists in diff output (e.g. `xirr`) falls back to sorting by value.
- **ADR:** add **ADR-0029** recording: reading-A (then's holdings at then's prices), money-columns-only (rate columns dropped as a category error), union-of-endpoints rows, calendar-day windows, compose-with-`--as-of`, held-but-unpriceable → unavailable (not zero). `performance`-only; `portfolio --diff` explicitly deferred.
- **Docs:** update README (behavior, not argparse duplication) and the CLAUDE.md `performance` one-liner to mention the diff mode; cross-reference ADR-0029.

## Testing Decisions

- **What makes a good test here:** assert on **external behavior** — the printed change table and the merge function's returned delta rows — not on internal control flow. Tests build a small `tmp_path` SQLite DB with a couple of holdings and known closes/FX, then either call `perf.main(argv)` and assert on `capsys` output, or call the pure merge function with hand-built endpoint rows and assert on the returned deltas.
- **Command seam (`perf.main(argv)` + `capsys`)** — the primary behavior seam, mirroring existing `test_main_*` tests. Cover:
  - Two held-through positions → correct signed ΔMktVal/ΔCost/ΔP&L and TOTAL; header names both dates.
  - New position bought in-window → appears with +full current value (start 0).
  - Position fully sold in-window → appears with negative delta to zero (union, not dropped).
  - Weekend/holiday endpoint → carry-forward, row flagged `~ estimated`.
  - Same prior close on both endpoints → all-zero deltas, no error.
  - Held-but-unpriceable endpoint → `—`, excluded from TOTAL, with note.
  - Rate columns absent from diff output.
  - `--as-of X --diff N` → window is `[X−N, X]` (composition).
  - Invalid `N` (0, negative, non-integer) → rejected with clear message / non-zero exit.
- **Unit seam (`_diff_rows`)** — mirrors `test_build_row_*` / `test_sort_rows_*`: pure, no DB. Cover the union merge, `shares==0 → 0` vs unpriceable → unavailable distinction, estimated-flag propagation, and TOTAL exclusion of unavailable rows.
- **Prior art:** `tests/test_performance.py` — `test_main_two_holdings_totals`, `test_main_unvaluable_row_excluded_from_total_with_warning`, `test_build_row_estimated_when_price_carried_forward`, `test_build_row_none_when_not_held_at_as_of`, `test_sort_rows_value_reverse_with_none_last`.
- Coverage floor (90%) must hold; `./scripts/check.sh` green before done.

## Out of Scope

- **`portfolio --diff`.** Portfolio's market-value column uses a single latest close (`_last_known_price`, no date parameter) and no FX conversion, and its weight is a cost-basis share — it has no date-aware EUR valuation to diff. Adding one would duplicate `performance`'s core inside a command whose contract declares "no market data". Deferred; revisit only if `portfolio` gains real valuation.
- **Diffing rate/ratio metrics** (XIRR, TWR, Vol, MaxDD, CAGR, P&L-contribution). A genuine "N-day return" is a different metric (a windowed TWR) and is not part of this spec.
- **Holdings-fixed / price-only diff** (reading B: today's units priced at both dates to isolate pure market movement). Not chosen; the return metrics already answer that question better.
- **Trading-day windows.** `N` is calendar days only.
- **Multi-window / historical diff series** (e.g. a sparkline of daily changes). Single window only.

## Further Notes

- **Invariance check:** because both endpoints reuse the existing valuation, a `--diff N` result must equal `performance --as-of end` minus `performance --as-of (end−N)` column by column, to the cent. This is the strongest correctness anchor and worth an explicit test.
- **Reading A rationale:** "then's holdings at then's prices" means a within-window fill shows up in ΔMktVal (value moved partly because you added money). ΔP&L naturally cancels cost, so it isolates pure gain change even under reading A — a useful property to surface in the header/help or docs.
- Depends on nothing outside `performance.py` and the existing `common` valuation helpers; no schema changes, no new dependencies.
