# ADR-0063 — `pace`: how far behind this month's deposits are

**Scope:** a stable command, `e1f pace`, that compares the cash deposited so far this
calendar month with a monthly reference taken from earlier months, and reports the
shortfall (or surplus). It reads `transactions` only; no prices or FX.

## Context

On 2026-11-16 the user asked for a command that "looks at previous months deposits and
figures out for the current month how behind we are". The book is fed by a monthly
contribution, so the useful number is how much of it is still to come.

## Decisions

### 1. A deposit is BUY / SAVINGS_PLAN cash

Amount is `shares × price + fee`, as `deposits` computes it (ADR-0033), summed per
calendar month by trade date. SELLs are ignored: the command measures cash put in,
and needs no valuation, so it works on a book `deposits` refuses.

### 2. The reference is the mean of the previous full months

The mean of the previous `--months` (default 6) full calendar months, counted from
the first month with a deposit. A month with no buy inside that span counts as 0, so a
skipped month lowers the reference instead of being hidden. The current month is
never in the mean. `--target EUR` replaces the reference with a fixed amount.

### 3. Behind = reference − deposited so far

No pro-rating by day of month: deposits arrive on the broker's schedule, so a
day-fraction pace would mark a plan that buys on the 15th as behind on the 10th. The
output shows the full-month gap; negative is reported as "Ahead".

### 4. Context lines: deviation and year to date

Each previous month is listed with its deviation from the reference mean, so a lumpy
history is visible next to the mean it produces. A year-to-date line totals deposits
from 1 January of the as-of year to the as-of day (`PaceResult.year_total`); it is
shown even when the pace is unavailable.

### 5. Typed outcomes

`PaceResult.status`: `CALCULATED` (full lookback or `--target`), `BOUNDED` with reason
`SHORT_HISTORY` (fewer previous months than asked for; disclosed), `UNAVAILABLE` with
reason `NO_PREVIOUS_MONTHS` (no deposit before this month and no `--target`).
