# ADR-0005 — Portfolio holdings derived from transactions

**Scope:** `portfolio` command, average-cost holdings from the `transactions` table

## Context

e1f stores ETF buy/sell rows from broker CSV ingest in SQLite (`ADR-0004`).
Users need a view of what they currently hold and the average price paid per
share, without maintaining a separate holdings file.

## Decision

Add an `e1f portfolio` command that:

- Reads `transactions` ordered by `datetime`.
- Applies average-cost accounting per broker and `symbol` using `side` from the
  `transactions` schema (`ADR-0004`); fees on buys are included in cost basis.
- Prints open positions (output in `src/e1f/portfolio.py`, `_cmd_portfolio`).

## Rationale

- **Derived view** — holdings are computed from the transaction ledger; no second
  source of truth.
- **Names from config** — `etf_universe.yaml` is the canonical ETF metadata;
  ISINs without config entries still appear in output.
- **Average cost** — simple, auditable cost basis for v1; FIFO and mark-to-market
  are follow-up work.

## Consequences

- Re-ingest or edit transactions changes reported holdings on the next
  `e1f portfolio` run.
- Holdings ignore ISINs with no net shares (fully sold).
- Follow-up work (separate ADRs/commands): market value using `prices`, P&L,
  multi-currency handling.
