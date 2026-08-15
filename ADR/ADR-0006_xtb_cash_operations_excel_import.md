# ADR-0006 — XTB Cash Operations Excel import

**Scope:** `transactions xtb` command, XTB account-history Excel export

## Context

ADR-0004 added Trade Republic CSV ingest. XTB’s account-history export is
**Excel only** (Cash Operations sheet), with tickers such as `WEBN.DE` rather
than ISINs in the file.

## Decision

Add `e1f transactions xtb <file.xlsx>` that:

- Reads the **Cash Operations** sheet from an XTB account-history workbook
  (header row detected automatically).
- Ingests rows with `Category` = ETF and `Type` = stock purchase / stock sale
  (filter logic in `is_xtb_etf_trade_row()` in `src/e1f/transactions.py`).
- Parses share count from the `Comment` field (e.g.
  `OPEN BUY 7/7.9987 @ 12.502`) and unit price from `abs(Amount) / shares` so
  stored cost matches XTB cash lines.
- Maps `Ticker` to ISIN via `etf_universe.yaml` (`build_ticker_to_isin()` in
  `src/e1f/transactions.py`; per-listing `listings` from OpenFIGI when present).
  Rows with no resolvable ISIN are skipped and listed in the ingest report.
- Stores canonical rows in the shared `transactions` table with
  `broker=xtb` and dedup on `(broker, transaction_id)` from the export `ID`
  column.

Requires `openpyxl` (pandas Excel reader). Fee and tax are stored as SQL `NULL`
when absent (same as empty fee/tax cells in the Trade Republic CSV).

## Rationale

- **Second broker profile** — same canonical schema and portfolio derivation as
  Trade Republic; separate parser per fixed export shape (ADR-0004).
- **ISIN via config** — XTB exports tickers, not ISINs; the ETF universe YAML
  is already the ticker metadata source for fetch.
- **Excel dependency** — justified because XTB does not offer an equivalent
  single CSV for this report.

## Consequences

- ETFs traded on XTB but not in `etf_universe.yaml` are filtered until the user
  runs `e1f config add` with a resolvable ticker.
- Re-import is idempotent on `(broker, transaction_id)`.
- Old semicolon CSV cash-ops exports (legacy XTB format) are out of scope unless
  added as a separate profile later.
