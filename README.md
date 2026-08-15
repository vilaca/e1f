# e1f

Build an ETF universe from ISINs and fetch historical prices into SQLite.
Prices come from ftgo (FT Markets) with an optional yfinance fallback (`--fallback`).

## Setup

Requires Python 3.11+.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

This installs the `e1f` command.

## Workflow

The tool exposes four subcommands around a shared config/DB:

1. **`e1f config`** — build the ETF universe YAML from ISINs (via OpenFIGI).
2. **`e1f fetch`** — populate the SQLite price DB.
3. **`e1f transactions`** — ingest ETF trades from broker exports (Trade Republic CSV, XTB Excel) and list stored trades.
4. **`e1f portfolio`** — open ETF holdings per broker from `transactions`.

```bash
# 1. Add ETFs by ISIN (OpenFIGI resolution; config shape in src/e1f/common.py)
e1f config add IE00BM67HK77
e1f config add IE00BM67HK77 IE00BDBRDM35 IE00BKM4GZ66
e1f config list
e1f config update IE00BM67HK77

# Remove ETFs from config, DB, and currency metadata
e1f config remove IE00BM67HK77
e1f config trim        # keep only ISINs present in config, DB, and metadata

# Check config/DB sync, history depth, and data quality
e1f config validate

# 2. Fetch prices into the SQLite DB
e1f fetch                 # all ETFs in the config
e1f fetch IE00BM67HK77    # a single ISIN
e1f fetch --force         # ignore the cache and re-download
e1f fetch IE00BM67HK77 --replace  # atomically replace one ISIN's stored series (repair)

# 3. Ingest broker transactions into the SQLite DB
e1f transactions trade-republic ~/Downloads/transactions.csv
e1f transactions tr ~/Downloads/transactions.csv
e1f transactions trade-republic transactions.csv --db data/e1f.db
e1f transactions xtb ~/Downloads/EUR_38472916_2006-01-01_2026-08-15.xlsx
e1f transactions list
e1f portfolio
```

Defaults (from `src/e1f/common.py`): config `data/etf_universe.yaml`, database
`data/e1f.db`, fetch start date `2000-01-01` (earlier than any ETF inception, so
the first fetch returns each ETF's full history). Paths resolve against the
project root, so commands work from any directory. Flag overrides are per command
— `e1f config --help`, `e1f fetch --help`, `e1f transactions --help`,
`e1f portfolio --help`.

## Price sources

ftgo resolves securities **by ISIN** and pins the first match in
`data/currency_metadata.yaml`, preferring the listing quoted in the fund's own
share-class currency, so the fetched security can't drift as FT Markets search
ordering changes. When `--fallback` is set and ftgo has no data for an ISIN, fetch tries
yfinance using the tickers from the config (trying `.L` and `.DE` suffixes for
London/Xetra listings).

Fetch is incremental by default (`--force` re-downloads and overwrites matching
dates). `e1f fetch <ISIN> --replace` repairs one series by deleting its stored rows and
re-inserting the fetched range; unless `--allow-shrink` is given it refuses to
drop any stored date (shorter range, narrower window, or interior hole), so a
truncated response can't silently wipe history
(`ADR/ADR-0008_price_series_replace_repair.md`).

`e1f config validate` distinguishes **errors** from **warnings**: it exits `1`
only on errors (duplicate keys, null or non-positive closes, weekend rows, invalid
dates, or a config/DB desync) and `0` when clean or when only warnings remain
(over-limit business-day gaps, large price moves, short/sparse/cash-like history).
See `e1f config validate --help` for the full taxonomy and
`ADR/ADR-0009_validate_exit_code_contract.md` for the why.

The SQLite DB holds a `prices` table (schema in `src/e1f/fetch.py`) and a
`transactions` table (schema in `src/e1f/transactions.py`); both can be read
directly with any SQLite client or `pandas.read_sql`.

Broker ingest stores **ETF buy/sell rows only** in `transactions` (Trade Republic
CSV via `e1f transactions trade-republic` or `tr`; XTB Excel Cash Operations via
`e1f transactions xtb` — see `ADR/ADR-0004_broker_transaction_import.md` and
`ADR/ADR-0006_xtb_cash_operations_excel_import.md`). Cash, dividends, and non-ETF
trades are skipped. Ingest does not modify `etf_universe.yaml`. After ingest it
lists ETF ISINs from the file that are not yet in the config, with a ready-to-run
`e1f config add …` line (XTB also reports unmapped tickers). Re-ingesting the
same file is idempotent (duplicate `(broker, transaction_id)` rows are skipped).
Use `e1f transactions list` to view stored trades.

Holdings and cost basis: `ADR/ADR-0005_portfolio_holdings_from_transactions.md`
(output in `src/e1f/portfolio.py`).

All sources (OpenFIGI, ftgo, yfinance) are fetched with retry-on-failure:
rate limits (HTTP 429) and server errors are retried with backoff, honoring
the server's `Retry-After` header when present and falling back to
exponential backoff otherwise.

## Development

```bash
uv sync --extra dev
./scripts/check.sh          # full suite: lint, layers, shell, actions, types, dead, test + coverage
uv run pytest               # tests only
```
