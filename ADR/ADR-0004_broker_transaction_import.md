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

Add an `e1f transactions` command with **Trade Republic CSV** ingest and
**listing** stored trades (XTB Excel is `ADR-0006`):

- **`transactions list`** — print ETF trades already in the DB (`--db` override).
- **`transactions trade-republic`** (alias **`tr`**) — parse the official
  Transaktionsexport into a canonical `transactions` table in the shared SQLite
  DB (`DEFAULT_DB`, same file as `prices`; schema in `src/e1f/transactions.py`,
  frozen in `tests/test_contracts.py`).
- Deduplicate on `(broker, transaction_id)` using `ON CONFLICT DO NOTHING`.
- Ingest **ETF buy/sell rows only** from the export (filter logic in
  `is_etf_trade_row()` in `src/e1f/transactions.py`). Cash, dividends, and
  non-ETF trades are skipped.
- Store ingested rows even when the ISIN is not yet in `etf_universe.yaml`.
  Ingest does **not** modify the ETF config.
- Enforce the canonical financial row at the SQLite boundary: `side` is one of
  `BUY` / `SELL` / `SAVINGS_PLAN`, and `shares` / `price` are finite positive
  `REAL` values. The import parser rejects malformed rows before insertion; the
  table constraints protect future importers and direct writes as well.
- On opening a legacy unconstrained `transactions` table, migrate it
  transactionally when every existing row satisfies those invariants. Refuse
  migration and identify the first bad `(broker, transaction_id)` when it does
  not; never discard or coerce source-of-truth rows.
- **Ingest and list only** for this command: no P&L or mark-to-market (holdings
  derivation is `ADR-0005`; XTB Excel is `ADR-0006`).

Generic column mapping remains out of scope.

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
- Existing valid databases are hardened in place on the next broker import.
  Existing invalid rows must be repaired explicitly before another import; the
  failed migration leaves the original table unchanged.
- `transactions list` reads back ingested rows; it does not modify the DB.
- Ingest does not modify `etf_universe.yaml`, but prints ISINs from ingested ETF
  trades that are absent from the config so the user can run `e1f config add`.
- Price fetch remains universe-driven (`e1f fetch`); imported ISINs without config
  entries have transactions but no automatic price history until added via
  `e1f config add`.
- Follow-up work (separate ADRs/commands): P&L and market value vs `prices`.
  Holdings and average cost per share are covered by `ADR-0005`. XTB Cash
  Operations Excel is covered by `ADR-0006`.
