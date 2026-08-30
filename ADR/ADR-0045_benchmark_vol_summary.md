# ADR-0045 — `benchmark` book summary + table TWR / Vol / MaxDD

**Scope:** print the book's own TWR / Vol / MaxDD above the `benchmark` table,
and add TWR / Vol / MaxDD columns on each overlap row for *that ETF*. Book-side
overlap TWR / Vol / MaxDD are not table columns (the Book line already answers
those). Does not change Beta / R² / TE / IR / RelStr. Out% is the ETF-minus-book
gap (the previous book-minus-ETF figure, negated) so it reads with the row's
TWR. Sharpe stays €STR-gated (ADR-0033). Supersedes ADR-0041's book-side table
`TWR` column; `port_twr` is still computed for Out% / RelStr. The ETF-leg
columns are named `TWR` / `Vol` / `MaxDD` (no `b` prefix) because the book
figures live on the Book line.

## Context

Each table row's comparison spans only that ETF's shared window, so there is
no single TWR on the table. Comparing seven overlapping windows without seeing
the book's own since-inception return and bumpiness meant running `performance`
alongside. TE is vol of *active* return, not of either leg, so a high-vol
satellite and a low-vol bond fund can print similar TE for different reasons.
Printing the book's overlap TWR / Vol / MaxDD next to the Book line duplicated
the book's quantities (and invited reading an overlap number as since-inception).

## Decisions

**A book line above the table.** Computed from the full portfolio return
series (the same `portfolio_return_series` the rows align against), not from
any one overlap:

| Print | Definition |
|---|---|
| `TWR` | Chain-linked product of the book's daily returns |
| `Vol` | Sample stdev ×√252 (same as `performance` / `funds`) |
| `MaxDD` | Wealth-index peak-to-trough (same as `performance` / `funds`) |
| `n` / dates | Count of those returns and their first → last date |

This is the book's history. The table keeps per-row `n` (overlap count, not
this line's n).

**`Vol` / `MaxDD` / `TWR` on every row** are that ETF's aligned daily returns
over the shared window. `n/a` when the row is UNAVAILABLE. Sort tokens: `vol` /
`maxdd` / `twr` (canonical, ADR-0037).

Order: `… IR  Vol  MaxDD  TWR  RelStr  Out%`.

**Not added as columns.** Book-side overlap TWR / Vol / MaxDD (Book line
covers the book). Sharpe / Sortino (€STR). Capture ratios (convention).

## Invariance

On returns `+10%, −10%`: TWR = `1.10×0.90 − 1` = −1%; MaxDD = `0.99/1.10 − 1`
= −10%; Vol = `stdev([0.10, −0.10]) × √252`. The book line on a one-fund
buy-and-hold fixture whose closes run 100 → 126 prints TWR = 26% (price
ratio), independent of which `--against` row is shown. On a fixture whose book
compounds +21% and the ETF +10.25% over the same two days, the printed row
shows `TWR` = 10.3% and `Out%` = −10.8%, not the book's 21.0%.
