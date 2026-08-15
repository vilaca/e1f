# ADR-0004 — Broker transaction import (Trade Republic CSV)

**Scope:** `transactions` command, SQLite `transactions` table, broker export ingestion and listing

## Context

e1f already maintains an ETF universe (`config`) and historical prices (`fetch`) in
SQLite. Users need a way to bring in their own broker activity without manually
transcribing trades.

Trade Republic provides an official CSV transaction export (Transaktionsexport).
It covers brokerage and cash activity with stable column names and a UUID
`transaction_id` suitable for idempotent imports.

## Decision

Add an `e1f transactions` command. v1 supports **Trade Republic CSV only** and
**listing** stored trades:

- **`transactions list`** — print ETF trades already in the DB (`--db` override).
- **`transactions trade-republic`** — parse the official Transaktionsexport into
  a canonical `transactions` table in the shared SQLite DB (`DEFAULT_DB`, same
  file as `prices`; schema in `src/e1f/transactions.py`, frozen in
  `tests/test_contracts.py`).
- Deduplicate on `(broker, transaction_id)` using `ON CONFLICT DO NOTHING`.
- Ingest **ETF buy/sell rows only** from the export (filter logic in
  `is_etf_trade_row()` in `src/e1f/transactions.py`). Cash, dividends, and
  non-ETF trades are skipped.
- Store ingested rows even when the ISIN is not yet in `etf_universe.yaml`.
  Ingest does **not** modify the ETF config.
- **Ingest and list only** for this command: no P&L or mark-to-market (holdings
  derivation is `ADR-0005`).

Excel and generic column mapping are out of scope until a broker that exports xlsx
is supported or a second profile is added.

## Rationale

- **Transactions as source of truth** — holdings and performance metrics are derived
  views; storing raw broker rows keeps v1 small and auditable.
- **Named broker profile** — Trade Republic's export has a fixed schema; a dedicated
  parser is simpler and more reliable than user-defined column mapping.
- **Universe decoupling** — ingested ETF trades may reference ISINs outside the
  configured price universe; ingest must not silently rewrite config.
- **Shared DB** — one SQLite file keeps prices and transactions joinable for later
  analysis commands without a second persistence layer.

## Consequences

- Re-importing the same CSV is safe: duplicate `(broker, transaction_id)` rows
  are skipped.
- `transactions list` reads back ingested rows; it does not modify the DB.
- Ingest does not modify `etf_universe.yaml`, but prints ISINs from ingested ETF
  trades that are absent from the config so the user can run `e1f config add`.
- Price fetch remains universe-driven (`e1f fetch`); imported ISINs without config
  entries have transactions but no automatic price history until added via
  `e1f config add`.
- Follow-up work (separate ADRs/commands): P&L and market value vs `prices`,
  additional broker profiles, Excel support where brokers provide it. Holdings
  and average cost per share are covered by `ADR-0005`.
