# ADR-0039 — Add SPDR MSCI ACWI IMI to the default `benchmark` set

**Scope:** add `IE00B3YLTY66` (SPDR MSCI ACWI IMI Acc) to
`_DEFAULT_BENCHMARK_LABELS` in `src/e1f/benchmark.py`. No new metrics, flags, or
valuation. Supersedes ADR-0033 Phase B's "six broad benchmarks" count.

## Context

The default `e1f benchmark` set (ADR-0033 Phase B) includes SPDR MSCI ACWI
(`IE00B44Z5B48`, large+mid) but not the IMI share class the book already holds
and that `backtest` / `seasonality` use as the all-world series. Comparing the
book to ACWI and not to ACWI IMI meant the small-cap sleeve had no default peer.

The fund is already in `data/etf_universe.yaml` (USD, Accumulating) and
`data/currency_metadata.yaml`; no `config add` / fetch-contract change.

## Decisions

**Add `IE00B3YLTY66` to the default set**, labelled `SPDR MSCI ACWI IMI (Acc)`
(tickers SPYI / IMID), listed immediately after SPDR MSCI ACWI so the two ACWI
share classes sit together. `--against` still overrides the whole list.

**Still an investable accumulating ETF**, same contract as the other defaults
(net of TER, overlap window, `*` if held). No new column or floor.

## Rationale

ACWI IMI is the all-country *investable market* index (large+mid+small). ACWI
alone is large+mid. Both belong in a default set that already carries two other
all-world funds (WEBN, VWCE); omitting the IMI class left the book's small-cap
exposure without a named peer.

## Consequences

`e1f benchmark` with no `--against` prints seven rows. Held `*` / UNAVAILABLE
handling is unchanged. ADR-0033's Phase B text stays as the original six; this
ADR is the count change.
