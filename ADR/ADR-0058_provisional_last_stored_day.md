# ADR-0058 — `fetch`: the last stored day is provisional

**Scope:** an incremental `fetch` overwrites the last stored day of each price series
and FX series with the value it re-reads. Every earlier day keeps its stored value
(ADR-0056 §4). This is a one-time repair of the rows the old behaviour left wrong.

## Context

An incremental fetch starts at the last stored day, because ftgo returns nothing
when start equals end, so it always re-reads that day. The default upsert then kept
the stored row. A fetch run during trading hours stores the day's live price as its
close, and since ADR-0056 also the day's partial bar (the low so far). No later
fetch corrected either.

A preflight on 2026-09-30 re-downloaded everything into a copy of the DB and
compared it with the real one:

- **57 closes differ, all on five dates.** These were 2026-08-25, 08-28, 08-31,
  09-11 and 09-16. The largest difference was 1.63%, and 32 funds were affected on
  2026-09-16. Every earlier close matches upstream exactly.
- **One EUR/USD rate differs,** on 2026-09-14: 1.16 stored against 1.15 upstream.
- **Three days are missing** (2026-07-24, for three funds).

The pattern fits fetches run while markets were open. Every command that reads
these days was affected.

## Decisions

### 1. The last stored day is overwritten; earlier days are settled

On an incremental fetch, fetched rows dated on or after the last stored day are
written whole:

- **Prices:** close, open, high, low and volume.
- **FX:** the rate.

Rows before that day follow the default rule unchanged. A stored close is kept, a
missing day is added, and a bar attaches only to its own close. The summary line's
`replaced` count shows when the provisional day changed.

### 2. No market-hours or timezone gating

Refusing to store today's row before the venue closes would need per-venue close
times, publication lags and holiday behaviour. Re-reading the last day is already
part of every incremental fetch, and it needs none of that. It also picks up a late
upstream correction to the last day.

### 3. What stays as it was

- **`--force` and `--replace`** already overwrite whole rows.
- **`--backfill`** still changes no close (ADR-0056 §5).
- **The cache.** A second fetch on the same day is still a cache hit. ftgo also
  returns nothing when start equals end, so a live price stored during trading hours
  is corrected by the next fetch on a later day. Until then, the last row can be a
  live price, which is also what a current valuation wants.

### 4. One-time repair

The rows found in the Context were repaired from the preflight copy, which
re-downloaded everything with `fetch --force`:

- 57 closes, each with the bar that belongs to it;
- 3 missing days;
- 1 FX rate.

The copy differed from the real DB in nothing else: no existing bar, other FX rate
or transaction row changed. A backup of the DB as it was before the repair is kept.

## Consequences

- `src/e1f/fetch.py`: `_save_prices` and `_save_fx` take the provisional day and
  split rows into settled and fresh. `fetch` and `_refresh_fx_pair` pass the day.
- `tests/test_fetch.py` covers:
  - a provisional close and its missing bar being overwritten while the settled
    day before it is kept;
  - a partial bar recorded during trading hours being replaced by the day's final bar;
  - the same rule for FX.
- ADR-0056 §4 and the README note the exception.
