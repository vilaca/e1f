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
