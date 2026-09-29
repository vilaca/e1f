# ADR-0056 — Daily bars in `prices`: ftgo open/high/low/volume beside each close

**Scope:** `fetch` stores each day's open, high, low and volume from ftgo next to the
close, in four nullable `prices` columns. It also gains `--backfill`, which adds them
to history already stored without changing any close. This ADR covers storage only:
no command reads the bars yet. The first consumer will be ADR-0055's deferred
"Intraday lows" item, and its estimate is a separate decision.

## Context

ftgo's `get_historical_prices` returns open, high, low, close and volume in one
response, but `fetch` kept only the close. ADR-0055 therefore bounds a buy-limit
order from closes alone; with the day's low stored, the bound could become an
estimate.

A probe of stored listings (2026-09-29) found that bar quality depends on the listing
and the era. A day with high = low and zero volume is a carried price, not a trade.

| Listing (ftgo symbol) | Stored from | Bars usable from¹ | Before that |
|---|---|---|---|
| Amundi Prime All Country World (`WEBN:GER`) | 2024-06 | listing | — |
| iShares Core MSCI World (`IWDA:LSE`) | 2009-09 | 2015 | 6–100% of days flat; close outside [low, high] on 6–47% |
| iShares Core S&P 500 (`CSPX:LSE`) | 2010-09 | 2015 | 35–93% flat; close outside on 9–40% |
| SPDR MSCI All Country World Market (`SPYI:GER`) | 2011-05 | 2018 | 21–81% flat |
| Amundi Global Luxury (`GLUX:GER`) | 2010-03 | 2019 | 26–98% flat |

¹ First calendar year with under 5% flat days.

None of the five funds checked had a missing bar field. Bars are therefore usable
for recent history and unreliable for older London listings. Deciding which days to
trust belongs to the consumer, not to storage.

## Decisions

### 1. Four nullable columns on `prices`, not a separate table

`open REAL`, `high REAL`, `low REAL`, `volume INTEGER`, all NULL when a day has no
bar. A day stays one row, so `config remove`, `--replace` and every reader that
selects by column name work unchanged. A separate table would need its own removal,
replace and orphan handling. `_init_database` adds any missing column to an existing
DB, and no close is touched. ADR-0002 records the migration, and
`test_prices_schema_contract` pins the schema.

### 2. ftgo is the only bar source

Rows from the `--fallback` yfinance path store closes only. yfinance quotes by
ticker, which may be a different listing from the pinned ftgo one (ADR-0001,
ADR-0002), and it is not used as a bar source. `--backfill` refuses `--fallback`.

### 3. Stored as returned, whole or not at all

Bars are stored exactly as ftgo returns them, with no consistency filtering. Flat or
inconsistent days (Context) are the consumer's to judge, and filtering them at
ingest would hide provenance. A day missing any of the four fields stores no bar at
all, so "has a bar" is one test: `low IS NOT NULL`.

### 4. A bar belongs to the close it was fetched with

- **Default upsert** (incremental fetch; `--backfill` applies the same rule, §5): a
  stored close never changes. A fetched bar attaches to a stored row only when that row has no bar and
  its close equals the fetched close. Otherwise the bar describes a different print,
  such as an upstream revision or a yfinance close, and it is dropped.
- **`--force` and `--replace`** overwrite the whole row, so the bar is always the one
  fetched with the new close. A yfinance close under `--fallback` clears it.

### 5. `--backfill` attaches bars to stored days and nothing else

It skips the cache and fetches from `--start` via ftgo. It then updates stored rows
only, applying the rule in §4: a row gains a bar when it has none and its close
equals the fetched close. It never inserts a day. A stored series can come from an
earlier pin of the same ISIN, and inserting days from the current pin would splice
two listings into one series. The first real backfill did exactly that:

- It inserted 79 Xetra EUR rows into LU0908508731's history, which is stored mostly
  in GBX pence from an earlier London pin (ADR-0043).
- 38 of those rows came before the stored start and 41 fell on UK bank holidays,
  creating 78 scale jumps.
- Those rows were removed, and a series with an earlier pin is repaired with
  `--replace` (ADR-0008).

Filling gaps in a series stays the job of `--force` and `--replace`, whose checks
live in ADR-0008. No stored value is changed or removed, so `--backfill` needs no
override or preflight. It is mutually exclusive with `--force` and `--replace`,
which rewrite rows.

### 6. Bar coverage in the fetch summary

Each per-ISIN summary line ends with `daily bars N/T`: stored days with a bar out of
all stored days. This makes gaps visible, whether from yfinance rows, revised closes
(for example a close stored while the market was still open), history from an
earlier pin, or history not yet backfilled.

## Rationale

- **No extra request.** The call `fetch` already makes returns the bars.
- **Closes stay immutable on the default path.** Every result computed so far is
  preserved, and the bars are purely additive.
- **Each row comes from one print.** Refusing a bar fetched with a different close
  keeps the low comparable with the close, which the `limitbuy` estimate needs.

## Consequences

- `src/e1f/fetch.py` changes in five places: the schema and its migration,
  `_fetch_ftgo` keeping the bar, `_price_rows` and the upserts, `--backfill`, and
  bar coverage in the summary line.
- `tests/test_fetch.py` covers:
  - the migration and the whole-bar rule;
  - that yfinance never stores a bar;
  - the attach, skip, keep and overwrite upsert cases;
  - a backfill over the full range that changes no close and adds no day;
  - the CLI refusal and mutual exclusion.
- `tests/test_contracts.py` pins the new schema.
- The README's price-sources section describes the bars and `--backfill`.
- No reader uses the bars yet, and close-based results are unchanged.

## Deferred

- **Using the lows in `limitbuy`:** an estimate beside ADR-0055's bound, with a
  per-day quality gate. It gets its own ADR.
- **`validate` checks for bars**, such as a close outside [low, high]. These wait for
  the consumer to define which days it trusts.
- **FX bars:** `fx_rates` keeps the close rate only.
