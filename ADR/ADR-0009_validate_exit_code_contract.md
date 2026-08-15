# ADR-0009 — `validate` exit-code contract

**Scope:** `e1f validate` exit codes and its error/warning taxonomy

## Context

`validate` reports config/DB sync, history depth, and price-data quality.
It previously exited `0` regardless of what it found, so it printed integrity
problems but could not gate anything — a CI step or `&&` chain treated a corrupt
DB the same as a clean one. As price-data integrity checks grew (duplicates,
nulls, non-positive closes, weekend rows, invalid dates), a machine-readable pass
/fail signal became necessary.

## Decision

`validate` classifies every finding as an **error** or a **warning** and sets the
exit code from that split:

- **Exit 1 — errors:** duplicate `(isin, date)` keys, null closes, non-positive
  closes, weekend rows, invalid/unparseable dates, malformed pinned quote-currency
  metadata, or a config/DB desync (ISINs in config but missing from the DB, or
  orphaned in the DB). These are corruption or drift that make the stored series
  untrustworthy.
- **Exit 0 — warnings (or clean):** over-limit missing-business-day gaps, large
  day-over-day price moves, and short / sparse / cash-like history. These are
  worth surfacing but are legitimately explainable (holidays, thin trading, young
  funds), so they never fail the command.

The taxonomy is documented in the `validate --help` epilog and README; this ADR
records the *why*.

## Rationale

- **Gate on corruption, not on judgement calls** — errors are objective data
  defects; warnings need a human to decide whether they matter for a given ETF.
- **Non-zero is actionable** — a `1` always means "fix the data", never "look at
  this maybe", so `validate` can sit in a pipeline.

## Consequences

- Scripts calling `validate` must treat `1` as fatal; a warnings-only DB stays
  `0`.
- Reclassifying a check between error and warning is a contract change and should
  update this ADR.
