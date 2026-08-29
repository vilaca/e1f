# ADR-0040 — `Daily TWR` column on `performance --series`

**Scope:** add one column to the `performance --series` table (ADR-0030) — that
day's time-weighted sub-period return (`Daily TWR`) — so the path shows the
increment that compounds into the existing cumulative `TWR`. Snapshot, `--diff`,
`--metrics`, and `--contrib` are unchanged.

## Context

`--series` already prints cumulative-since-inception `TWR` on every row (each
row equals `--as-of` that day). Reading *how the book moved that day* meant
subtracting adjacent TWRs by hand, which is easy to get wrong when a
contribution or a gap-bridged weekend sits between two printed days. The daily
return is already computed inside `wealth_and_returns`; it was just not shown.

## Decisions

**Same sub-period return as cumulative TWR.** `Daily TWR` on day `D` is the
`wealth_and_returns` return dated `D`: `r_D = V_D/(V_prev+CF_D) − 1`. It is
gap-bridged (a weekend or missing FX close is one period), contributions are
start-of-day, and `--isin` uses the restricted book. `n/a` when no return is
dated `D` (no defined denominator). This is the same series `--metrics` Best/Worst
Day extrema come from.

**Next to `TWR`, signed.** The column sits immediately after cumulative `TWR`.
Values are signed (`+0.32%` / `-1.10%`) at two decimals, matching Best/Worst Day,
because a daily print is small and either sign. Shared snapshot columns stay
equal `--as-of D`'s TOTAL; invariance tests skip this series-only column.

**`--series` only.** The snapshot table has no date axis, so a daily increment
does not belong there. `--metrics --series` already has Best/Worst; it does not
gain this column.

**No new valuation math.** `_series_point` reads the return dated `D` from the
same holdings/`_aggregate_series` path `_total_row` uses. `common` is unchanged.

## Implementation

`SeriesPoint` gains `daily_twr: float | None`.

`_daily_twr(holdings, day, db_path)` — last `wealth_and_returns` return dated
exactly `day`, or `None`.

## Invariance

On a fixture with no mid-gap contributions, `Daily TWR` on 2024-12-27 equals
the hand-computed close-to-close return (11.8/11.5 − 1). For any series row,
`daily_twr` equals the `D`-dated return of that day's snapshot aggregate
series. Adjacent-row `(1+TWR_D)/(1+TWR_{D-1}) − 1` is *not* the definition —
a contribution day with no close can sit between two printed rows.
