# ADR-0001 — ftgo as primary price source; yfinance as opt-in fallback

**Scope:** `fetch` command, price data sourcing

## Context

Two sources can provide historical ETF prices: **ftgo** (FT Markets API,
ISIN-based, high-quality European listings) and **yfinance** (Yahoo Finance,
ticker-based, broad but noisier). The question is how to expose both without
making the fetch behaviour unpredictable.

## Decision

ftgo is the sole default source. yfinance is available only via an explicit
`--fallback` flag, and is tried only when ftgo returns no data for a given ISIN.

## Rationale

- **Reproducibility** — ftgo resolves by ISIN (one canonical security); yfinance
  resolves by ticker, which is exchange-specific and can silently return a
  different listing on different runs.
- **Currency correctness** — ftgo results are pinned to the fund's own share-class
  currency (see ADR-0002); Yahoo tickers are exchange-quoted and may introduce
  an FX overlay (e.g. a USD-class ETF quoted in GBX on LSE).
- **Opt-in, not silent fallback** — the original design had an automatic fallback.
  This was made opt-in so users know when data comes from an inferior source and
  can decide whether the result is fit for analysis.

## Consequences

- Fetches are reproducible by default. The `--fallback` flag documents intent.
- ISINs not listed on FT Markets require `--fallback` to get any data; the CLI
  warns when this happens.
- yfinance retry logic (rate-limit handling, `.L`/`.DE` suffix probing) is kept
  but isolated behind the flag.

## Deferred (not in this ADR)

- **Broker-venue price sources (Lang & Schwarz / Tradegate).** ftgo and yfinance
  serve the *official exchange close* (LSE/Xetra, stamped ~17:30 CET), converted to
  EUR via the `fx_rates` series (ADR-0010). Brokers price off their own market
  makers instead: Trade Republic quotes **Lang & Schwarz** (LS Exchange), XTB a
  different venue again. On a trending day these sit ~0.5–1% off the exchange close
  — same-day, not stale — so an e1f valuation will never match a broker app to the
  cent. This is inherent, not a bug: the exchange close is the neutral reference,
  and matching one broker would diverge from another. Confirmed empirically for
  `IE000YYE6WK5` (VanEck Defense): LS/Tradegate live ≈ 53.67 EUR vs e1f's prior-day
  USD-close-derived 53.97 EUR, a 0.62% gap that tracked the week's decline.
  - **Accessible source.** `ls-tc.de` (the LS feed directly) is bot-blocked
    (503/410 to non-browser clients); **onvista** and Tradegate real-time pages
    return the live LS quote cleanly and are the practical scrape target if this is
    ever built.
  - **Shape if pursued.** An *optional* source behind a flag (mirroring
    `--fallback`), never the default — the exchange-close baseline stays canonical.
    LS/Tradegate are live-quote venues with no official daily OHLC, so it is a
    scheduled last-price snapshot at a fixed cutoff, not a backfillable historical
    series (contrast ADR-0008's `--replace` repair, which assumes a real series).
  - **What generalizing across ISINs needs.** (1) A per-ISIN venue-instrument
    mapping, pinned in the currency-metadata sidecar alongside the ftgo `xid`
    (ADR-0002) — the venue's internal instrument id is not the ISIN. (2) Native EUR
    quotes, so no FX overlay (bypasses the ADR-0010 conversion, removing the
    FX-precision term entirely). (3) A per-broker venue map (TR→LS, XTB→its venue)
    if the goal is to reconcile against a *specific* broker rather than to add one
    more generic quote. (4) A provenance status distinct from CALCULATED, since a
    scraped intraday snapshot is a weaker claim than a settled exchange close
    (ADR-0014 vocabulary).
  - **Recommendation.** Not worth building for a sub-1% cosmetic gap on a
    valuation that already agrees on cost basis and gain; recorded here so the
    trade-off is not re-derived. Revisit only if broker-exact reconciliation
    becomes a first-class requirement.
