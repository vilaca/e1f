# ADR-0059 — `fetch`: weekend rows from the source are dropped

**Scope:** `fetch` never stores a price row dated on a Saturday or Sunday, from ftgo
or yfinance. `--replace` treats a stored weekend row as corruption, the way it
already treats an unparseable date (ADR-0008), so it removes one without
`--allow-shrink`.

## Context

`validate` counts weekend rows as an error (ADR-0009). On 2026-10-29 it failed on
one row: Invesco US Technology S&P (`IE00B3VSSL01`, XLKS:LSE:USD) on Saturday
2010-10-02, close 54.65, between 51.06 on Friday and 50.53 on Monday. That is a 7%
jump and reversal no other fund shows.

A `fetch IE00B3VSSL01 --replace` into a copy of the DB stored the same row again.
ftgo itself serves it, so no repair by re-downloading could remove it, and
`validate` would keep failing.

## Decisions

### 1. Weekend rows are dropped where fetched rows become stored rows

`_price_rows` drops any row dated on a Saturday or Sunday, so every write path
(default, `--force`, `--replace`, `--backfill`) and both sources follow one rule.
No venue in the universe trades on a weekend, so such a row is a source error, not
a price. Each drop is logged as a warning naming the ISIN and the dates.

### 2. `--replace` doesn't count a stored weekend row as coverage

The shrink guard compares the fetched dates with the stored ones. A stored weekend
row would never be in the fetched set after decision 1, so the guard would refuse
the very repair that removes it. Weekend rows are left out of the guard, like NULL
and unparseable dates. Every stored weekday still has to be covered.

### 3. What stays as it was

- **FX rates** are not filtered; `validate` checks weekend rows in prices only.
- **Holidays** are not filtered. Each venue has its own calendar, and a holiday
  row is not an error to `validate`.

## Consequences

- `src/e1f/fetch.py`: `_price_rows` drops weekend rows; `_replace_prices` leaves
  them out of the shrink guard.
- `tests/test_fetch.py` covers the drop and its warning, `--replace` removing a
  stored weekend row that upstream still serves, and the guard still refusing to
  drop stored weekdays.
- The stored row was removed with `e1f fetch IE00B3VSSL01 --replace`.
