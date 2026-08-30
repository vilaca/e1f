# ADR-0041 — `TWR` / `bTWR` columns on `e1f benchmark`

**Scope:** add two columns to the `benchmark` table — the book's and the
benchmark's cumulative time-weighted return over that row's shared window.
No new valuation. RelStr and Out% stay as they are (they already use these
two numbers).

## Context

`benchmark` already chain-links both legs over the overlap (`port_twr`,
`bench_twr`) to compute RelStr and Out%, then hides the inputs. Reading
"World +2.1% Out%" without seeing *what* compounded on each side meant
reconstructing the TWRs from RelStr, or running `performance` (a different
window: since inception, not this row's overlap). ADR-0033 Phase B deferred
showing them; the glossary still said "not shown as a column."

## Decisions

**Both legs, not one.** Each row's overlap can differ, so the book's TWR is
not a single number for the table. `TWR` is the book's cumulative return over
that row's shared dates; `bTWR` is the benchmark's over the same dates. Out%
is `TWR − bTWR` and RelStr is `(1+TWR)/(1+bTWR)` by construction.

**Same definition as `performance` TWR**, restricted to the aligned overlap:
`wealth_and_returns` / `_cumulative` on the shared daily returns. Not
since-inception, not calendar-year. `n/a` when the row is UNAVAILABLE.

**Next to RelStr / Out%.** Order is `… IR  TWR  bTWR  RelStr  Out%`. Sort
tokens: `twr` (canonical, ADR-0037) and `btwr` (command-local).

**No new math.** `BenchmarkStats.port_twr` / `bench_twr` already exist.

## Invariance

On a fixture whose book compounds +21% and the benchmark +10.25% over the
same two days, the printed `TWR` / `bTWR` / `Out%` / `RelStr` equal
`0.21` / `0.1025` / `0.21−0.1025` / `1.21/1.1025`.
