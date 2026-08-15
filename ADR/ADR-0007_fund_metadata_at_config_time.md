# ADR-0007 — Fund metadata fetched at config time

**Scope:** `config add` / `config update`, `portfolio` output, fund reference fields in `etf_universe.yaml`

## Context

Portfolio analysis needs fund-level facts beyond ticker resolution: total expense ratio
(TER), distribution policy (accumulating vs distributing), the fund's share-class
currency, and the underlying asset class. OpenFIGI resolves listings but its short names
often omit share-class hints (e.g. `(Acc)`, `USDA`) and its instrument classification
does not describe the ETF's underlying exposure. FT Markets (ftgo) fund names are more
descriptive but can list multiple share classes under the same ticker search.

## Decision

When `e1f config add` or `e1f config update` resolves an ISIN, enrich the YAML entry with:

- **`fund_currency`** — OpenFIGI name first; ftgo listing names (all listings scanned)
  fill gaps; justETF ISIN profile as last resort.
- **`distribution`** — OpenFIGI name first; ftgo name when OpenFIGI is silent. OpenFIGI
  wins when ftgo sibling listings disagree. justETF fills remaining gaps.
- **`ter`** — ftgo `get_fund_stats` (`Ongoing charge` / `Net expense ratio`) on the
  share-class-matched xid; justETF ISIN profile when ftgo returns `--` (common for newer
  Amundi listings).
- **`asset_class`** — the broad first component of justETF's `Investment focus`
  (for example, `Equity` from `Equity, United States` or `Bonds` from
  `Bonds, World, Aggregate, All maturities`).

Source priority for fields available from multiple providers is
**OpenFIGI → ftgo → justETF**. Asset class comes from justETF because OpenFIGI and
ftgo classify the traded instrument rather than its underlying exposure. yfinance is
not used for fund metadata (prices only, opt-in via ADR-0001).

Enrichment is best-effort: missing fields are omitted rather than blocking config writes.
Explicit `⚠` warnings when a fallback source is used.

## Rationale

- **ISIN fidelity** — OpenFIGI and justETF resolve by ISIN; ftgo searches by ISIN with
  richer fund names and official ongoing charges where FT Markets has them.
- **No yfinance for metadata** — listing quote currency ≠ fund currency; TER coverage
  is patchy and provider-dependent.
- **Pin once** — fund facts change rarely; storing them beside tickers avoids live API
  calls on every portfolio run.

## Consequences

- Existing ETFs need `e1f config update` to backfill or refresh metadata.
- justETF HTML parsing may break if their page structure changes; failures omit fields.
- `common.py` calls ftgo and optionally justETF during config enrichment.
