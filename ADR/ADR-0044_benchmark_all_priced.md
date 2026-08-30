# ADR-0044 — `benchmark --all`: every priced ISIN as a benchmark

**Scope:** an optional `--all` flag on `e1f benchmark` that replaces the
default seven (and `--against`) with every ISIN stored in `prices`. No new
metrics. Default and `--against` paths are unchanged.

## Context

`benchmark` compares the book to a **selected** set: seven broad accumulating
ETFs by default, or an explicit `--against` list (ADR-0033 Phase B, ADR-0039).
The price DB often holds the rest of the configured universe (and any other
fetched ISIN). Scoring the book against those series meant typing every ISIN
into `--against`.

## Decisions

**`--all` swaps the candidate list.** When the flag is present, the rows are
every distinct ISIN in `prices`, sorted by ISIN (then `--sort` if given). Real
holdings stay marked `*`. Default (flag absent) is still the seven; `--against`
is still an explicit subset.

**`--all` and `--against` are mutually exclusive.** Passing both exits 1.
An empty prices table prints "No price series" and exits 0 (nothing to score
against). Unconvertible or unpriced-as-of rows stay UNAVAILABLE, never dropped.

**Same stats, same book.** Each row is still `benchmark_stats` of the
portfolio return series vs that ISIN's EUR returns over their overlap. The
book is still holdings. This is not `funds` (stand-alone fund TWR) and not a
paper portfolio.

## Implementation

`_priced_isins` lives in `src/e1f/benchmark.py`. `common` is not extended.
`--against` argparse default becomes unset (`None`) so an explicit `--against`
can be distinguished from the seven-fund fallback.

## Invariance

On a fixture with a held fund and a second priced ISIN that is not a holding,
`e1f benchmark --all` prints both; `e1f benchmark` (no flag) does not print
the unheld extra. `--all --against X` is an error.
