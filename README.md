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

The tool exposes three subcommands around a shared config/DB:

1. **`e1f config`** — build the ETF universe YAML from ISINs (via OpenFIGI).
2. **`e1f fetch`** — populate the SQLite price DB.
3. **`e1f transactions`** — ingest ETF trades from broker CSV and list stored trades.

```bash
# 1. Add ETFs by ISIN (auto-resolves name, tickers, exchange, FIGI)
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

# 3. Ingest broker transactions into the SQLite DB
e1f transactions trade-republic ~/Downloads/transactions.csv
e1f transactions trade-republic transactions.csv --db data/e1f.db
e1f transactions list
```

Defaults (from `src/e1f/common.py`): config `data/etf_universe.yaml`, database
`data/e1f.db`, fetch start date `2000-01-01` (earlier than any ETF inception, so
the first fetch returns each ETF's full history). Paths resolve against the
project root, so commands work from any directory. Flag overrides are per command
— `e1f config --help`, `e1f fetch --help`, `e1f transactions --help`.

## Price sources

ftgo resolves securities **by ISIN** and pins the first match in
`data/currency_metadata.yaml`, preferring the listing quoted in the fund's own
share-class currency, so the fetched security can't drift as FT Markets search
ordering changes. When `--fallback` is set and ftgo has no data for an ISIN, fetch tries
yfinance using the tickers from the config (trying `.L` and `.DE` suffixes for
London/Xetra listings).

The SQLite DB holds a `prices` table (schema in `src/e1f/fetch.py`) and a
`transactions` table (schema in `src/e1f/transactions.py`); both can be read
directly with any SQLite client or `pandas.read_sql`.

Broker CSV ingest (v1: Trade Republic) stores **ETF buy/sell rows only** in
`transactions`; cash, dividends, and non-ETF trades are skipped. Ingest does not
modify `etf_universe.yaml`. After ingest it lists ETF ISINs from the file that
are not yet in the config, with a ready-to-run `e1f config add …` line.
Re-ingesting the same file is idempotent (duplicate `transaction_id` rows are
skipped). Use `e1f transactions list` to view stored trades. See
`ADR/ADR-0004_broker_transaction_import.md`.

All sources (OpenFIGI, ftgo, yfinance) are fetched with retry-on-failure:
rate limits (HTTP 429) and server errors are retried with backoff, honoring
the server's `Retry-After` header when present and falling back to
exponential backoff otherwise.

## Development

```bash
uv sync --extra dev
./scripts/check.sh          # full suite: lint, layers, types, tests + coverage
uv run pytest               # tests only
```
