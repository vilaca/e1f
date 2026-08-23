# ADR-0010 — Currency and FX foundation

**Scope:** the `fx_rates` table, FX fetching inside `e1f fetch`, and the
`fx_rate_asof` / `convert_to_eur` helpers in `common`.

## Context

`prices.close` is stored in each fund's **native quote currency** — the currency
of the ftgo listing that was pinned in `data/currency_metadata.yaml` (ADR-0002),
not necessarily the fund's share-class currency. These diverge: e.g.
`IE0003XJA0J9` and `IE00BM67HK77` carry `fund_currency: USD` in
`etf_universe.yaml` but their pinned listings quote in **EUR** (Frankfurt/Xetra).
So the only trustworthy statement of a stored price's currency is the pinned
`currency` field, never `fund_currency`.

There was no FX table and no currency normalization anywhere: `prices` and
`transactions` were never joined, and nothing could sum a mixed-currency
portfolio into one base currency. This is the prerequisite for all downstream
performance (ADR-0011) and forecast (ADR-0012) work — none of it can value a
portfolio without a daily FX series.

Settled scope for the whole effort (not re-litigated here): base currency is
**EUR**; the portfolio is **buy-and-hold** (no sells, no realized P&L, no tax
lots); all held ETFs are **accumulating** (price close is an accurate
total-return proxy — no distribution ingest). `transactions.price` is already
EUR for both brokers (Trade Republic trades in EUR; the XTB Cash Operations
export is account-currency EUR), so cost basis needs no conversion — only the
price/NAV side does.

## Decision

### 1. A dedicated `fx_rates` table

```sql
CREATE TABLE fx_rates (
    base  TEXT,   -- 'EUR' today; explicit so direction is unambiguous
    quote TEXT,   -- e.g. 'USD'
    date  TEXT,   -- 'YYYY-MM-DD'
    rate  REAL,   -- ftgo-native: quote units per 1 base (EURUSD ≈ 1.16)
    PRIMARY KEY (base, quote, date)
)
```

Rates are stored **exactly as ftgo returns them** (quote units per one base
unit), with no inversion at write time. A price in currency `C` converts to EUR
by `price / rate` for the pair `(EUR, C)`. Storing `(base, quote)` explicitly
keeps the conversion direction self-documenting and future-proofs a non-EUR base
without a schema change.

The FX table is separate from `prices` rather than overloading it with FX pairs
as pseudo-ISINs, so the ISIN namespace stays clean and the schema states the
`(base, quote, date)` grain directly.

### 2. FX follows the same source hierarchy as prices

ftgo is primary, yfinance (`<BASE><QUOTE>=X`, e.g. `EURUSD=X`) is the fallback —
identical to ADR-0001. ftgo serves FX spot rates as first-class `Currencies`
instruments (`get_xid("EURUSD")` → a pinned xid; `get_historical_prices` returns
the same daily schema as NAV prices), so no divergent source path is needed. The
FX xid is pinned like an ISIN resolution, under an `fx_pairs` map inside
`data/currency_metadata.yaml` (extending ADR-0002's pinned-resolution sidecar),
so the FX security can't drift as FT Markets search ordering changes.

The set of pairs to fetch is derived from the **held** ISINs
(`common.portfolio_isins`) mapped through their pinned quote currency — **not**
`fund_currency`. Today that is `{USD}` → the single pair `EURUSD`. The set grows
automatically as the held portfolio takes on funds priced in other currencies.

**Fail loud on an unsupported held currency.** A held quote currency we have no
FX rule for is a correctness hazard, not something to paper over: `GBX` (pence)
is not an ISO currency ftgo quotes a spot pair for — it needs a ÷100
normalization to GBP first — so it raises rather than silently mis-converting.
GBX/GBP support is deferred until a fund priced that way is actually held.

### 3. FX is fetched automatically inside `e1f fetch`

A bulk fetch (`e1f fetch`, with or without `--portfolio`; i.e. no explicit ISIN
and not `--replace`) refreshes the needed FX pairs after prices, reusing the same
cache/incremental/upsert machinery (default keeps stored rates and adds missing
dates; `--force` overwrites). This keeps the daily FX series current with prices
by default — the whole point of a daily series. Targeted single-ISIN fetches and
`--replace` repairs skip FX to stay fast and atomic. With no `transactions`
table (nothing held) FX refresh is a no-op.

### 4. Missing FX dates: nearest-prior forward-fill

Valuing on a date with a price but no same-day FX rate (weekends, holidays,
mismatched FX vs. exchange calendars) uses the most recent rate **on or before**
that date — a forward-only fill, mirroring how `fetch()` already forward-fills
prices. Rates are never carried backward and never interpolated, so valuation
never uses a future rate. A date earlier than the first stored rate has no prior
rate and cannot be valued: `fx_rate_asof` raises rather than back-filling from
the first known rate.

### 5. Read/convert helper ships with the data layer

`common.fx_rate_asof(db_path, quote, date, base='EUR')` returns the nearest-prior
rate via a single `WHERE date <= ? ORDER BY date DESC LIMIT 1` lookup (identity
`1.0` when `quote == base`), and `common.convert_to_eur(amount, quote, date,
db_path)` applies it as `amount / rate`. Both live in `common` (the bottom layer,
ADR-0003) so ADR-0011's valuation can build on a tested primitive. This ADR stops
short of portfolio valuation itself (summing holdings) — that is ADR-0011.

The forward-fill policy (#4) and the fail-loud policy (#2) are exercised by code
and tests here, not left as prose for a later session to reinterpret.

### 6. Trade-date FX cross-check is deferred

Trade Republic's CSV carries `fx_rate` / `original_currency` / `original_amount`;
ingest discards them. Persisting them as a sanity check against the daily series
is **out of scope** for ADR-0010 and left to a future ADR. Reasons: valuation is
driven by the daily series, not trade-date rates; both brokers already record
`transactions.price` in EUR; and the XTB Cash Operations sheet has **no**
counterpart fields (its only conversion rates live on the unused Closed Positions
sheet), so persisting the TR fields would produce an asymmetric, half-populated
column — a data-quality feature that deserves its own design, not a rider here.
The transactions canonical schema (ADR-0004) is therefore untouched.

## Rationale

- **Store ftgo-native rates** — inverting to an EUR-per-unit convention at ingest
  would add one more place to get the direction wrong; storing the source value
  verbatim keeps a single, auditable conversion rule at read time.
- **Same source hierarchy as prices** — one source story for the whole DB; ftgo
  demonstrably serves daily FX, so a special case would be complexity without
  benefit.
- **Currency from the pinned resolution, not `fund_currency`** — the pinned
  currency is what the stored close is actually denominated in; `fund_currency`
  is the share class's currency and provably diverges, which would silently
  mis-scale converted values.
- **Fail loud on unknown currencies** — a wrong FX conversion is expensive and
  hard to spot; refusing to convert a currency we don't model turns a silent
  error into a loud, recoverable one.
- **Forward-fill only** — the honest "as-of" rate never depends on future
  information; it matches the existing price fill so FX and NAV share one mental
  model.

## Consequences

- The `fx_rates` schema is frozen as a data contract (`tests/test_contracts.py`);
  changing it requires updating that test and noting the migration here.
- A held fund priced in a currency without an FX rule (e.g. GBX) makes `fetch`
  and any valuation raise until support is added — intentional.
- Valuation before the first stored FX date is not possible; such dates drop out
  of any downstream series rather than being back-filled.
- `data/currency_metadata.yaml` gains an `fx_pairs` map alongside its ISIN
  entries; `validate` is unaffected (it only looks up config ISINs).
