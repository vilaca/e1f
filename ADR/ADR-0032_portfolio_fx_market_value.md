# ADR-0032 — `portfolio` FX-converts market value; fee + weighted TER on market value

**Scope:** `portfolio`'s market-value (`Value€`) column now converts to EUR via
FX, and the estimated annual fee and weighted-average TER weight by EUR market
value instead of cost basis. Supersedes ADR-0029's note that "`portfolio`'s
market-value column uses a single latest close with no FX conversion".

## Context

`portfolio` showed a `Value` column as `latest close × shares` in the fund's
**native** currency, with no FX conversion — so a USD- or GBP-priced fund's value
was numerically wrong in a EUR book, and any total mixing it with EUR holdings was
meaningless. The estimated annual fee and weighted-average TER weighted by **cost
basis** (`total_paid`), which disagreed with what a fund actually charges: TER is
levied on AUM (market value), not on what you originally paid. This surfaced as
`portfolio` reporting ~€8.09/yr where `performance --series` (ADR-0031, correctly
on EUR market value) reported ~€8.17/yr for the same book.

## Decisions

**Market value is FX-converted EUR.** Each holding's value is
`shares × latest close × FX`, converted with the shared `convert_to_eur` using
the FX rate as of the close's own date (ADR-0010). The column is relabelled
`Value€`; `Last px` stays the native close. When a holding has no price, no pinned
trade currency, or no FX rate, its value is `—` and it is excluded from the market
value / fee / weighted-TER totals (never a silent zero), matching `performance`'s
valuation contract. The reused primitive keeps `portfolio`'s value identical to
`performance`'s to the cent.

**Fee and weighted TER weight by market value.** `Fee€/yr = Σ(terᵢ/100 × MktValᵢ)`
and `weighted avg TER = Σ(terᵢ × MktValᵢ)/Σ MktValᵢ` (= `100 × ΣFee / Σ MktVal`),
consistent with ADR-0031. A holding with no TER metadata contributes 0 to the fee
but stays in the denominator (dilutes). The weighted TER now **requires** price +
FX data; with none it is omitted rather than shown cost-weighted.

**The `Weight` column stays cost-basis.** It is documented as a share of cost
basis (the holdings' own frame) and was not in question; only the value, fee, and
weighted TER move to market value.

**`fee_yr` sort follows the market-value fee.** Sorting reuses the same
market-value fee, so the order matches the displayed column. `eur_values` is
computed once per command and threaded into `sort_holdings`.

**Reuse, not duplication.** `convert_to_eur` / `pinned_quote_currency` come from
`common` (the same primitives `performance` and `overlap` use); no cross-command
import (ADR-0003). `_latest_close` returns the close *and its date* for the FX
lookup; `_last_known_price` is a thin wrapper over it for the native `Last px`.

## Consequences

`portfolio` now reads price + FX data even in its default view (to weight the
TER), where before it read none. This is the cost of a correct market-value TER;
a book with no fetched prices shows holdings and cost basis as before, omits the
weighted TER, and warns which holdings were excluded. ADR-0029's deferral of
date-aware valuation in `portfolio` is retired for the value column (the fee
question forced it); `portfolio --diff` remains deferred.
