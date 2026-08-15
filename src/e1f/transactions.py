#!/usr/bin/env python
"""e1f transactions — ingest and list broker ETF trades in SQLite.

Usage:
    e1f transactions trade-republic path/to/transactions.csv
    e1f transactions list
"""

import argparse
import re
import sqlite3
import sys
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd

from e1f.common import DEFAULT_CONFIG, DEFAULT_DB, ConfigManager

BROKER_TRADE_REPUBLIC = "trade_republic"
_ISIN_RE = re.compile(r"^[A-Z]{2}[A-Z0-9]{10}$")
TR_ETF_TRADE_TYPES = frozenset({"BUY", "SELL", "SAVINGS_PLAN"})
TR_ETF_ASSET_CLASSES = frozenset({"FUND"})

TR_REQUIRED_COLUMNS = (
    "datetime",
    "date",
    "account_type",
    "category",
    "type",
    "asset_class",
    "name",
    "symbol",
    "shares",
    "price",
    "amount",
    "fee",
    "tax",
    "currency",
    "original_amount",
    "original_currency",
    "fx_rate",
    "description",
    "transaction_id",
    "counterparty_name",
    "counterparty_iban",
    "payment_reference",
    "mcc_code",
)

_INSERT_SQL = """
    INSERT INTO transactions (
        broker, transaction_id, datetime, symbol, side, shares, price, fee, tax
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT (broker, transaction_id) DO NOTHING
"""


@dataclass(frozen=True)
class ImportSummary:
    """Result counts from a broker CSV ingest."""

    inserted: int
    skipped: int
    filtered: int
    errors: int
    missing_isins: tuple[tuple[str, str], ...] = ()


def _parse_str(value: object) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    return str(value).strip()


def _is_isin(value: str) -> bool:
    return bool(_ISIN_RE.match(value))


def _normalize_tr_type(value: object) -> str:
    return _parse_str(value).upper().replace(" ", "_")


def is_etf_trade_row(row: pd.Series) -> bool:
    """True for Trade Republic ETF buy/sell rows (including savings plans)."""
    if _parse_str(row["category"]) != "TRADING":
        return False
    if _normalize_tr_type(row["type"]) not in TR_ETF_TRADE_TYPES:
        return False
    if _parse_str(row["asset_class"]) not in TR_ETF_ASSET_CLASSES:
        return False
    return _is_isin(_parse_str(row["symbol"]))


def _etf_trade_rows(df: pd.DataFrame) -> pd.DataFrame:
    return df[df.apply(is_etf_trade_row, axis=1)]


def _isins_from_dataframe(df: pd.DataFrame) -> dict[str, str]:
    """Return unique ISIN -> security name from ingested ETF trade rows."""
    found: dict[str, str] = {}
    for _, row in _etf_trade_rows(df).iterrows():
        symbol = _parse_str(row["symbol"])
        if symbol not in found:
            found[symbol] = _parse_str(row["name"])
    return found


def missing_config_isins(
    df: pd.DataFrame,
    config_path: str = DEFAULT_CONFIG,
) -> tuple[tuple[str, str], ...]:
    """ISINs present in the CSV but absent from the ETF universe config."""
    configured = set(ConfigManager(config_path).config.get("etfs", {}).keys())
    isins = _isins_from_dataframe(df)
    return tuple(
        (isin, isins[isin])
        for isin in sorted(isins)
        if isin not in configured
    )


def format_missing_isins(missing: tuple[tuple[str, str], ...]) -> str:
    """Human-readable list of ISINs to add via ``e1f config add``."""
    if not missing:
        return ""
    lines = ["", "ISINs not in etf_universe.yaml (add with e1f config add):"]
    for isin, name in missing:
        suffix = f"  {name}" if name else ""
        lines.append(f"  {isin}{suffix}")
    isin_args = " ".join(isin for isin, _ in missing)
    lines.extend(["", f"  e1f config add {isin_args}"])
    return "\n".join(lines)


def _parse_float(value: object) -> float | None:
    text = _parse_str(value)
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


class TradeRepublicImporter:
    """Parse Trade Republic Transaktionsexport CSV and store canonical rows."""

    def __init__(
        self,
        db_path: str = DEFAULT_DB,
        config_path: str = DEFAULT_CONFIG,
    ) -> None:
        self.db_path = db_path
        self.config_path = config_path
        self._init_database()

    def _init_database(self) -> None:
        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS transactions (
                    broker TEXT NOT NULL,
                    transaction_id TEXT NOT NULL,
                    datetime TEXT,
                    symbol TEXT,
                    side TEXT,
                    shares REAL,
                    price REAL,
                    fee REAL,
                    tax REAL,
                    PRIMARY KEY (broker, transaction_id)
                )
                """
            )
            conn.commit()

    @staticmethod
    def _validate_columns(df: pd.DataFrame) -> None:
        missing = [col for col in TR_REQUIRED_COLUMNS if col not in df.columns]
        if missing:
            missing_list = ", ".join(missing)
            raise ValueError(f"missing required Trade Republic columns: {missing_list}")

    @staticmethod
    def _row_to_values(row: pd.Series) -> tuple[Any, ...]:
        return (
            BROKER_TRADE_REPUBLIC,
            _parse_str(row["transaction_id"]),
            _parse_str(row["datetime"]),
            _parse_str(row["symbol"]),
            _normalize_tr_type(row["type"]),
            _parse_float(row["shares"]),
            _parse_float(row["price"]),
            _parse_float(row["fee"]),
            _parse_float(row["tax"]),
        )

    def import_csv(self, csv_path: str | Path) -> ImportSummary:
        """Read a Trade Republic CSV and upsert rows into the transactions table."""
        path = Path(csv_path)
        if not path.is_file():
            raise FileNotFoundError(f"CSV file not found: {path}")

        df = pd.read_csv(path, dtype=str, keep_default_na=False)
        self._validate_columns(df)

        inserted = 0
        skipped = 0
        filtered = 0
        errors = 0

        with closing(sqlite3.connect(self.db_path)) as conn:
            for _, row in df.iterrows():
                if not is_etf_trade_row(row):
                    filtered += 1
                    continue

                transaction_id = _parse_str(row["transaction_id"])
                if not transaction_id:
                    errors += 1
                    continue

                cursor = conn.execute(_INSERT_SQL, self._row_to_values(row))
                if cursor.rowcount == 1:
                    inserted += 1
                else:
                    skipped += 1
            conn.commit()

        etf_trades = _etf_trade_rows(df)
        return ImportSummary(
            inserted=inserted,
            skipped=skipped,
            filtered=filtered,
            errors=errors,
            missing_isins=missing_config_isins(etf_trades, self.config_path),
        )

    def list_rows(self) -> list[tuple[Any, ...]]:
        """Return stored transactions ordered by datetime."""
        with closing(sqlite3.connect(self.db_path)) as conn:
            return conn.execute(
                """
                SELECT broker, datetime, symbol, side, shares, price, fee, tax
                FROM transactions
                ORDER BY datetime, transaction_id
                """
            ).fetchall()


def _cmd_list(db_path: str) -> int:
    rows = TradeRepublicImporter(db_path=db_path).list_rows()
    if not rows:
        print("No transactions in database")
        print("Ingest some: e1f transactions trade-republic path/to/transactions.csv")
        return 0

    print(
        f"\n{'Broker':<16} {'Datetime':<28} {'Symbol':<14} {'Side':<14} "
        f"{'Shares':>10} {'Price':>10} {'Fee':>6} {'Tax':>6}"
    )
    print("-" * 112)
    for broker, dt, symbol, side, shares, price, fee, tax in rows:
        shares_s = "" if shares is None else f"{shares:.4f}"
        price_s = "" if price is None else f"{price:.2f}"
        fee_s = "" if fee is None else f"{fee:.2f}"
        tax_s = "" if tax is None else f"{tax:.2f}"
        print(
            f"{broker:<16} {dt:<28} {symbol:<14} {side:<14} "
            f"{shares_s:>10} {price_s:>10} {fee_s:>6} {tax_s:>6}"
        )
    print(f"\nTotal: {len(rows)} transactions")
    return 0


def _cmd_trade_republic(csv_path: str, db_path: str, config_path: str) -> int:
    importer = TradeRepublicImporter(db_path=db_path, config_path=config_path)
    summary = importer.import_csv(csv_path)
    print(
        f"✓ Trade Republic ingest complete: "
        f"{summary.inserted} inserted, "
        f"{summary.skipped} skipped (duplicates), "
        f"{summary.filtered} filtered (non-ETF trades), "
        f"{summary.errors} errors"
        f"{format_missing_isins(summary.missing_isins)}"
    )
    return 1 if summary.errors else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="e1f transactions",
        description="Ingest and list broker ETF trades in SQLite",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # List stored transactions
  e1f transactions list

  # Ingest Trade Republic Transaktionsexport CSV
  e1f transactions trade-republic ~/Downloads/transactions.csv

  # Use a custom database path
  e1f transactions trade-republic transactions.csv --db data/e1f.db

  # Compare ingested ISINs against the ETF universe config
  e1f transactions trade-republic transactions.csv --config data/etf_universe.yaml
        """,
    )

    subparsers = parser.add_subparsers(dest="command")

    list_parser = subparsers.add_parser("list", help="List stored transactions")
    list_parser.add_argument("--db", "-d", default=DEFAULT_DB, help="Database file path")

    tr_parser = subparsers.add_parser(
        "trade-republic",
        help="Ingest Trade Republic Transaktionsexport CSV",
    )
    tr_parser.add_argument("csv_path", help="Path to the CSV export file")
    tr_parser.add_argument("--db", "-d", default=DEFAULT_DB, help="Database file path")
    tr_parser.add_argument(
        "--config",
        "-c",
        default=DEFAULT_CONFIG,
        help="ETF universe config for missing-ISIN report",
    )

    args = parser.parse_args(argv)

    if not args.command:
        parser.print_help()
        return 1

    try:
        if args.command == "list":
            return _cmd_list(args.db)
        if args.command == "trade-republic":
            return _cmd_trade_republic(args.csv_path, args.db, args.config)
        parser.error(f"unsupported command: {args.command!r}")
        return 1  # unreachable; keeps mypy happy
    except Exception as e:  # noqa: BLE001 — CLI top-level; all errors become exit code 1
        print(f"✗ Error: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
