# ADR-0043 — Prefer EUR, then USD, when the fund name has no currency hint

**Scope:** ftgo listing pick in `DataExtractor._resolve_ftgo` when
`fund_currency_from_name` returns nothing. Does not change pinning, the
share-class-currency preference, or FX conversion. Supersedes ADR-0002's
"fall back to the first match" clause.

## Context

FT Markets returns every listing of an ISIN (LSE pence, Xetra EUR, LSE USD,
…). ADR-0002 prefers the listing quoted in the share-class currency parsed from
the fund name, then the first remaining match. Many UCITS names carry no
currency token (`X MSCI WORLD HEALTH CARE`, `Xtrackers … 1C`). FT Markets often
lists the LSE **GBX** (pence) line first, so the pin was GBX even when a Xetra
EUR line sat one row down.

GBX is not an ISO currency ftgo quotes an FX spot pair for (ADR-0010): a held
GBX pin fails conversion rather than mis-scaling by 100. The universe is
UCITS funds valued in EUR. An EUR listing is the native quote and needs no FX.
When there is no EUR line, the usual remaining share-class quote is USD
(`EURUSD` is the FX pair already fetched).

## Decision

When the name has no share-class currency:

1. Prefer a listing quoted in the portfolio base currency (`EUR`).
2. Else a listing quoted in `USD`.
3. Else the first listing whose quote currency is not in
   `UNSUPPORTED_FX_CURRENCIES` (`GBX` / `GBp`).
4. Else the first remaining match (GBX only if it is the only line).

A name that *does* parse (e.g. `"… USD (Acc)"`) still pins that currency, even
when an EUR listing exists. That is still ADR-0002: true NAV, not a venue
overlay.

Already-pinned sidecar rows are not re-resolved; delete the entry and fetch
again to pick under this rule.

## Rationale

- **UCITS / base currency** — European accumulating ETFs in this universe have
  a EUR venue line. Pinning it stores closes already in the valuation currency,
  so `convert_to_eur` is identity. GBX is an LSE quoting convention, not the
  share class.
- **USD second** — when there is no EUR listing, USD is the usual remaining
  UCITS share-class quote and already has an FX rule (`EURUSD`). Other
  overlays (CHF, GBP, …) are rarer and only win if neither EUR nor USD exists.
- **Fail-loud stays** — if the only listing is GBX, we still pin it; holding it
  still raises until GBX/GBP support exists. This ADR only stops *choosing*
  GBX when a convertible listing is sitting next to it.

## Consequences

- New ISINs whose names omit a currency token pin EUR when it exists, otherwise
  USD, rather than whichever line FT Markets listed first (often LSE pence).
- USD (or other) share classes that *name* their currency still pin that
  listing. Names that omit it and offer both USD and EUR pin EUR.
- Existing sidecar pins are unchanged until deleted and re-fetched.
