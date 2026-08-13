# e1f

Build a UCITS ETF universe from ISINs and fetch historical prices into SQLite.
Prices come from ftgo (FT Markets) with a yfinance fallback.

## Setup

Requires Python 3.14.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

This installs the `e1f` command.

## Workflow

The tool exposes two subcommands around a shared config/DB:

1. **`e1f config`** — build the ETF universe YAML from ISINs (via OpenFIGI).
2. **`e1f fetch`** — populate the SQLite price DB (ftgo, with a yfinance fallback).

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
```

Defaults: config `config/etf_universe.yaml`, database `data/e1f.db`,
start date `2000-01-01` (earlier than any UCITS ETF, so the first fetch returns
each ETF's full history from inception). Paths resolve against the project root,
so the commands work from any directory. Override with `--config`, `--db`, and
`--start` (see each command's `--help`).

## Price sources

ftgo resolves securities **by ISIN** and pins the first match in
`data/currency_metadata.yaml`, preferring the listing quoted in the fund's own
share-class currency, so the fetched security can't drift as FT Markets search
ordering changes. When ftgo has no data for an ISIN, fetching falls back to
yfinance using the tickers from the config (trying `.L` and `.DE` suffixes for
London/Xetra listings).

The SQLite DB stores a single `prices` table (`isin`, `date`, `close`), so it
can be read directly with any SQLite client or `pandas.read_sql`.

All sources (OpenFIGI, ftgo, yfinance) are fetched with retry-on-failure:
rate limits (HTTP 429) and server errors are retried with backoff, honoring
the server's `Retry-After` header when present and falling back to
exponential backoff otherwise.

## Development

```bash
pip install -e '.[dev]'
pytest
```
