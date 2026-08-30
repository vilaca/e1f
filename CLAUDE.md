# e1f

ETF universe config and historical price fetching into SQLite.

## Running checks

```bash
./scripts/check.sh              # all gates: lint, layers, shell, actions, types, dead, package, mutation, test
./scripts/check.sh lint         # one gate (lint | layers | shell | actions | types | dead | package | mutation | test)
uv run pytest                   # tests only, no coverage floor
```

The script is the single definition of "green". CI runs the same gates via `scripts/check.sh` (`actions` in a separate job; see `.github/workflows/ci.yml`).

## Layout

```
src/e1f/
  cli.py            — entry point; routes top-level commands
  autocomplete.py   — Bash/Zsh shell completion
  config.py         — OpenFIGI resolution, YAML management
  fetch.py          — ftgo/yfinance price + FX fetching, SQLite
  funds.py          — configured universe with TER + windowed performance metrics
  validate.py       — config/DB sync and price-data quality
  transactions.py   — broker export ingest (Trade Republic CSV, XTB Excel)
  portfolio.py      — holdings from transactions; EUR market value
  performance.py    — EUR valuation, XIRR/TWR/risk metrics
  benchmark.py      — portfolio vs benchmark ETFs
  deposits.py       — organic-vs-reported value + ROIC
  correlation.py    — return co-movement redundancy + clustering
  rebalance.py      — buy-only target rebalance + DCA plan
  scenario.py       — CRUD for named ISIN:pct baskets
  glossary.py       — metric definitions (data/glossary.md)
  backtest.py       — contribution-timing strategy evaluation (ADR-0019 through ADR-0023)
  lookthrough.py    — refresh cached yfinance look-through snapshots for held funds
  concentration.py  — within-fund concentration (security, sector, asset-class)
  overlap.py        — cross-fund single-name exposure floor
  seasonality.py    — calendar-month effects and pre-specified/frozen-OOS rules (ADR-0026 through ADR-0028)
  common/           — shared primitives; commands import from `e1f.common` (ADR-0025)
  experimental/     — isolated experimental tier; one-way import boundary (ADR-0024)
data/
  etf_universe.yaml      — ETF config (ISINs, names, tickers)
  currency_metadata.yaml — pinned ftgo resolutions incl. FX pairs
  glossary.md            — metric glossary read by the `glossary` command (ADR-0034)
  e1f.db                 — SQLite DB: prices, fx_rates, transactions (gitignored)
ADR/    — decision log; one ADR per decision, no gaps in numbering
tests/  — pytest suite; 90% coverage floor enforced
```

## Key decisions

See `ADR/` for the full decision log.

## Skills

- `/doc-check` — audit user/agent docs, ADRs, and the metric glossary for drift, stale claims, dead links, and convention breaks

## Conventions

- One home per fact: the *why* lives in an ADR, code shapes live in code, README describes behaviour without duplicating argparse definitions.
- Financial timing/fill conventions are governed by their ADR. Any change must update or supersede that ADR and add or update a pinned-date regression test with hand-computed expected fills, terminal wealth, or equivalent numerics; property tests complement but do not replace date pins.
- Before calling a change done: `./scripts/check.sh` must be green.
- Coverage floor is 90%; ratchet up, never down without a recorded reason.
- New modules must satisfy the layer contract in `ADR-0003`; experimental modules live under `e1f.experimental` behind the one-way boundary in `ADR-0024` (no stable module may import them).
- Destructive commands must preflight the whole candidate set before mutation, provide an explicit override for deliberate data loss, preserve rollback, and test refusal, override, and failure paths.
- User-visible partial or unavailable results must have typed semantic outcome data and a disclosure test; do not rely only on full output snapshots or log-string matching.
- Every new stable metric/column must update `data/glossary.md` and its `Where` command/flag contract. Shared financial primitives used by multiple commands need a same-fixture reconciliation test when their outputs must agree.
- Application code (`src/`) keeps all imports at the top of the file (ruff `PLC0415`); tests may import locally.
