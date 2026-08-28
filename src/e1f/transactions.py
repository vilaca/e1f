#!/usr/bin/env python
"""e1f transactions — ingest and list broker ETF trades in SQLite.

Usage:
    e1f transactions trade-republic path/to/transactions.csv
    e1f transactions tr path/to/transactions.csv
    e1f transactions xtb path/to/cash-operations.xlsx
    e1f transactions list
"""

import argparse
import math
import re
import sqlite3
import sys
import warnings
from collections.abc import Generator
from contextlib import closing, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd

from e1f.common import DEFAULT_CONFIG, DEFAULT_DB, XTB_EXCHANGE_SUFFIX, ConfigManager

BROKER_TRADE_REPUBLIC = "trade_republic"
BROKER_XTB = "xtb"
_ISIN_RE = re.compile(r"^[A-Z]{2}[A-Z0-9]{10}$")
_MAX_SAFE_FLOAT_INTEGER = 2**53 - 1
TR_ETF_TRADE_TYPES = frozenset({"BUY", "SELL", "SAVINGS_PLAN"})
TR_ETF_ASSET_CLASSES = frozenset({"FUND"})

XTB_BUY_TYPES = frozenset({"stock purchase", "stocks/etf purchase", "ações/etf compra"})
XTB_SELL_TYPES = frozenset({"stock sale", "stocks/etf sale", "ações/etf vende"})
XTB_ETF_CATEGORY = "etf"
_XTB_COMMENT_RE = re.compile(
    r"(?:OPEN|CLOSE)\s+(?:BUY|SELL)\s+"
    r"([0-9]+(?:\.[0-9]+)?(?:/[0-9]+(?:\.[0-9]+)?)?)\s+@\s+"
    r"([0-9]+(?:\.[0-9]+)?)",
    re.IGNORECASE,
)
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

_TRANSACTION_SIDES = ("BUY", "SELL", "SAVINGS_PLAN")
_TRANSACTIONS_TABLE_SQL = """
    CREATE TABLE {table} (
        broker TEXT NOT NULL,
        transaction_id TEXT NOT NULL,
        datetime TEXT,
        symbol TEXT,
        side TEXT NOT NULL CHECK (side IN ('BUY', 'SELL', 'SAVINGS_PLAN')),
        shares REAL NOT NULL CHECK (
            typeof(shares) IN ('integer', 'real') AND shares > 0.0 AND shares < 1e308
        ),
        price REAL NOT NULL CHECK (
            typeof(price) IN ('integer', 'real') AND price > 0.0 AND price < 1e308
        ),
        fee REAL,
        tax REAL,
        PRIMARY KEY (broker, transaction_id)
    )
"""


@dataclass(frozen=True)
class ImportSummary:
    """Result counts from a broker export ingest."""

    inserted: int
    skipped: int
    filtered: int
    errors: int
    missing_isins: tuple[tuple[str, str], ...] = ()
    unmapped_tickers: tuple[tuple[str, str], ...] = ()


def init_transactions_database(db_path: str) -> None:
    with closing(sqlite3.connect(db_path)) as conn:
        schema_row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'transactions'"
        ).fetchone()
        if schema_row is None:
            conn.execute(_TRANSACTIONS_TABLE_SQL.format(table="transactions"))
            conn.commit()
            return
        schema = " ".join(str(schema_row[0]).split())
        # Heuristic marker for our schema; a false negative only triggers a safe rebuild.
        if all(
            fragment in schema
            for fragment in (
                "side TEXT NOT NULL CHECK",
                "shares REAL NOT NULL CHECK",
                "price REAL NOT NULL CHECK",
            )
        ):
            return

        invalid = conn.execute(
            """
            SELECT broker, transaction_id
            FROM transactions
            WHERE side IS NULL
               OR side NOT IN ('BUY', 'SELL', 'SAVINGS_PLAN')
               OR shares IS NULL
               OR typeof(shares) NOT IN ('integer', 'real')
               OR shares <= 0.0
               OR shares >= 1e308
               OR price IS NULL
               OR typeof(price) NOT IN ('integer', 'real')
               OR price <= 0.0
               OR price >= 1e308
            LIMIT 1
            """
        ).fetchone()
        if invalid is not None:
            raise ValueError(
                "cannot harden transactions table: invalid existing row "
                f"{invalid[0]!r}/{invalid[1]!r}; side must be one of {_TRANSACTION_SIDES}, "
                "and shares/price must be finite positive numbers"
            )

        conn.execute("BEGIN IMMEDIATE")
        conn.execute(_TRANSACTIONS_TABLE_SQL.format(table="transactions_hardened"))
        conn.execute(
            """
            INSERT INTO transactions_hardened
            SELECT broker, transaction_id, datetime, symbol, side, shares, price, fee, tax
            FROM transactions
            """
        )
        conn.execute("DROP TABLE transactions")
        conn.execute("ALTER TABLE transactions_hardened RENAME TO transactions")
        conn.commit()


def list_transaction_rows(db_path: str) -> list[tuple[Any, ...]]:
    """Return stored transactions ordered by datetime."""
    with closing(sqlite3.connect(db_path)) as conn:
        if conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='transactions'"
        ).fetchone() is None:
            return []
        return conn.execute(
            """
            SELECT broker, datetime, symbol, side, shares, price, fee, tax
            FROM transactions
            ORDER BY datetime, transaction_id
            """
        ).fetchall()


def _parse_str(value: object) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    return str(value).strip()


def _is_isin(value: str) -> bool:
    return bool(_ISIN_RE.match(value))


def _normalize_tr_type(value: object) -> str:
    return _parse_str(value).upper().replace(" ", "_")


def _parse_float(value: object, field: str, *, required: bool = False) -> float | None:
    """Finite numeric field; blank is allowed only when optional."""
    text = _parse_str(value)
    if not text:
        if required:
            raise ValueError(f"{field} is required")
        return None
    try:
        parsed = float(text)
    except ValueError:
        raise ValueError(f"{field} is not numeric: {text!r}") from None
    if not math.isfinite(parsed):
        raise ValueError(f"{field} must be finite")
    if required and parsed <= 0.0:
        raise ValueError(f"{field} must be positive")
    return parsed


def _format_datetime(value: object) -> str:
    if isinstance(value, pd.Timestamp):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    return _parse_str(value)


def _parse_xtb_id(value: object) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    if isinstance(value, float):
        if (
            not math.isfinite(value)
            or not value.is_integer()
            or abs(value) > _MAX_SAFE_FLOAT_INTEGER
        ):
            return ""
        return str(int(value))
    text = _parse_str(value)
    if not text:
        return ""
    spreadsheet_integer = re.fullmatch(r"([0-9]+)\.0+", text)
    return spreadsheet_integer.group(1) if spreadsheet_integer else text


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


def _register_xtb_ticker(mapping: dict[str, str], isin: str, ticker: str, exchange: str) -> None:
    base = _parse_str(ticker).upper()
    if not base:
        return
    mapping[base] = isin
    suffix = XTB_EXCHANGE_SUFFIX.get(_parse_str(exchange).upper())
    if suffix:
        mapping[f"{base}.{suffix}"] = isin


def build_ticker_to_isin(config_path: str = DEFAULT_CONFIG) -> dict[str, str]:
    """Map XTB tickers (and ``TICKER.EXCHANGE`` forms) to ISINs from the ETF config."""
    mapping: dict[str, str] = {}
    for isin, data in ConfigManager(config_path).config.get("etfs", {}).items():
        listings = data.get("listings")
        if listings:
            for entry in listings:
                _register_xtb_ticker(
                    mapping,
                    isin,
                    _parse_str(entry.get("ticker")),
                    _parse_str(entry.get("exchange")),
                )
            continue
        exchange = _parse_str(data.get("exchange")).upper()
        for ticker in data.get("tickers", []):
            _register_xtb_ticker(mapping, isin, ticker, exchange)
    return mapping


def resolve_xtb_ticker(ticker: object, mapping: dict[str, str]) -> str:
    raw = _parse_str(ticker).upper()
    if not raw:
        return ""
    if _is_isin(raw):
        return raw
    if raw in mapping:
        return mapping[raw]
    base = raw.split(".", 1)[0]
    return mapping.get(base, "")


def parse_xtb_trade_comment(comment: object) -> tuple[float, float] | None:
    match = _XTB_COMMENT_RE.search(_parse_str(comment))
    if not match:
        return None
    shares = float(match.group(1).split("/")[0])
    price = float(match.group(2))
    return shares, price


def xtb_shares_and_price(row: pd.Series) -> tuple[float, float] | None:
    """Share count from Comment; unit price from Amount/shares when cash is present."""
    trade = parse_xtb_trade_comment(row["comment"])
    if trade is None:
        return None
    shares, comment_price = trade
    if shares <= 0:
        return None
    amount = _parse_float(row.get("amount"), "amount")
    if amount is not None and amount != 0:
        return shares, abs(amount) / shares
    return shares, comment_price


def normalize_xtb_side(value: object) -> str:
    trade_type = _parse_str(value).lower()
    if trade_type in XTB_BUY_TYPES:
        return "BUY"
    if trade_type in XTB_SELL_TYPES:
        return "SELL"
    return ""


def is_xtb_etf_trade_row(row: pd.Series) -> bool:
    if _parse_str(row["category"]).lower() != XTB_ETF_CATEGORY:
        return False
    return normalize_xtb_side(row["type"]) in {"BUY", "SELL"}


@contextmanager
def _suppress_openpyxl_default_style_warning() -> Generator[None, None, None]:
    """XTB exports trigger a harmless openpyxl default-style warning."""
    with warnings.catch_warnings():
        warnings.filterwarnings(
            "ignore",
            message="Workbook contains no default style*",
            category=UserWarning,
        )
        yield


def load_xtb_cash_operations(path: Path) -> pd.DataFrame:
    """Load the Cash Operations sheet from an XTB account-history Excel export."""
    if not path.is_file():
        raise FileNotFoundError(f"Excel file not found: {path}")

    with _suppress_openpyxl_default_style_warning():
        workbook = pd.ExcelFile(path)
        sheet = next(
            (name for name in workbook.sheet_names if "cash oper" in str(name).lower()),
            None,
        )
        if sheet is None:
            raise ValueError("no Cash Operations sheet found in XTB export")

        preview = pd.read_excel(path, sheet_name=sheet, header=None, nrows=40)
        header_row = _find_xtb_header_row(preview)
        if header_row is None:
            raise ValueError("could not find Cash Operations header row in XTB export")

        df = pd.read_excel(path, sheet_name=sheet, header=header_row)
    df.columns = [_parse_str(col).lower() for col in df.columns]
    required = {"type", "ticker", "category", "time", "amount", "id", "comment"}
    missing = required - set(df.columns)
    if missing:
        missing_list = ", ".join(sorted(missing))
        raise ValueError(f"missing required XTB Cash Operations columns: {missing_list}")

    df = df[df["type"].notna()]
    df = df[df["type"].astype(str).str.strip().str.lower() != "total"]
    return df.reset_index(drop=True)


def _find_xtb_header_row(preview: pd.DataFrame) -> int | None:
    for idx, row in preview.iterrows():
        cells = {_parse_str(value).lower() for value in row if _parse_str(value)}
        if {"type", "ticker", "id", "comment"}.issubset(cells):
            return int(str(idx))
    return None


def _isins_from_dataframe(df: pd.DataFrame) -> dict[str, str]:
    """Return unique ISIN -> security name from ingested ETF trade rows."""
    found: dict[str, str] = {}
    for _, row in _etf_trade_rows(df).iterrows():
        symbol = _parse_str(row["symbol"])
        if symbol not in found:
            found[symbol] = _parse_str(row["name"])
    return found


def missing_config_isins(
    symbols: dict[str, str],
    config_path: str = DEFAULT_CONFIG,
) -> tuple[tuple[str, str], ...]:
    """ISINs present in the ingest but absent from the ETF universe config."""
    configured = set(ConfigManager(config_path).config.get("etfs", {}).keys())
    return tuple(
        (isin, symbols[isin])
        for isin in sorted(symbols)
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


def format_unmapped_tickers(unmapped: tuple[tuple[str, str], ...]) -> str:
    """Human-readable list of XTB tickers with no ISIN match in the ETF config."""
    if not unmapped:
        return ""
    lines = ["", "Tickers not mapped to any ISIN in etf_universe.yaml:"]
    for ticker, name in unmapped:
        suffix = f"  {name}" if name else ""
        lines.append(f"  {ticker}{suffix}")
    lines.extend(["", "  Add each ETF with e1f config add <ISIN>, then re-import."])
    return "\n".join(lines)


class TradeRepublicImporter:
    """Parse Trade Republic Transaktionsexport CSV and store canonical rows."""

    def __init__(
        self,
        db_path: str = DEFAULT_DB,
        config_path: str = DEFAULT_CONFIG,
    ) -> None:
        self.db_path = db_path
        self.config_path = config_path
        init_transactions_database(db_path)

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
            _parse_float(row["shares"], "shares", required=True),
            _parse_float(row["price"], "price", required=True),
            _parse_float(row["fee"], "fee"),
            _parse_float(row["tax"], "tax"),
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

                try:
                    values = self._row_to_values(row)
                except ValueError:
                    errors += 1
                    continue
                cursor = conn.execute(_INSERT_SQL, values)
                if cursor.rowcount == 1:
                    inserted += 1
                else:
                    skipped += 1
            conn.commit()

        return ImportSummary(
            inserted=inserted,
            skipped=skipped,
            filtered=filtered,
            errors=errors,
            missing_isins=missing_config_isins(_isins_from_dataframe(df), self.config_path),
        )


class XtbImporter:
    """Parse XTB Cash Operations Excel export and store canonical rows."""

    def __init__(
        self,
        db_path: str = DEFAULT_DB,
        config_path: str = DEFAULT_CONFIG,
    ) -> None:
        self.db_path = db_path
        self.config_path = config_path
        init_transactions_database(db_path)

    def import_excel(self, excel_path: str | Path) -> ImportSummary:
        path = Path(excel_path)
        df = load_xtb_cash_operations(path)
        ticker_to_isin = build_ticker_to_isin(self.config_path)

        inserted = 0
        skipped = 0
        filtered = 0
        errors = 0
        resolved_names: dict[str, str] = {}
        unmapped: dict[str, str] = {}

        with closing(sqlite3.connect(self.db_path)) as conn:
            for _, row in df.iterrows():
                if not is_xtb_etf_trade_row(row):
                    filtered += 1
                    continue

                side = normalize_xtb_side(row["type"])
                try:
                    trade = xtb_shares_and_price(row)
                except ValueError:
                    errors += 1
                    continue
                if trade is None:
                    filtered += 1
                    continue

                isin = resolve_xtb_ticker(row["ticker"], ticker_to_isin)
                if not isin:
                    ticker = _parse_str(row["ticker"]).upper()
                    if ticker and ticker not in unmapped:
                        unmapped[ticker] = _parse_str(row.get("instrument", ""))
                    filtered += 1
                    continue

                transaction_id = _parse_xtb_id(row["id"])
                if not transaction_id:
                    errors += 1
                    continue

                shares, price = trade
                instrument = _parse_str(row.get("instrument", ""))
                if isin not in resolved_names and instrument:
                    resolved_names[isin] = instrument

                values = (
                    BROKER_XTB,
                    transaction_id,
                    _format_datetime(row["time"]),
                    isin,
                    side,
                    shares,
                    price,
                    None,
                    None,
                )
                cursor = conn.execute(_INSERT_SQL, values)
                if cursor.rowcount == 1:
                    inserted += 1
                else:
                    skipped += 1
            conn.commit()

        return ImportSummary(
            inserted=inserted,
            skipped=skipped,
            filtered=filtered,
            errors=errors,
            missing_isins=missing_config_isins(resolved_names, self.config_path),
            unmapped_tickers=tuple((ticker, unmapped[ticker]) for ticker in sorted(unmapped)),
        )


def _cmd_list(db_path: str) -> int:
    rows = list_transaction_rows(db_path)
    if not rows:
        print("No transactions in database")
        print("Ingest some: e1f transactions trade-republic path/to/transactions.csv")
        print("             e1f transactions xtb path/to/cash-operations.xlsx")
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


def _print_import_summary(broker_label: str, summary: ImportSummary) -> None:
    print(
        f"✓ {broker_label} ingest complete: "
        f"{summary.inserted} inserted, "
        f"{summary.skipped} skipped (duplicates), "
        f"{summary.filtered} filtered (non-ETF trades or unmapped tickers), "
        f"{summary.errors} errors"
        f"{format_unmapped_tickers(summary.unmapped_tickers)}"
        f"{format_missing_isins(summary.missing_isins)}"
    )


def _cmd_trade_republic(csv_path: str, db_path: str, config_path: str) -> int:
    importer = TradeRepublicImporter(db_path=db_path, config_path=config_path)
    summary = importer.import_csv(csv_path)
    _print_import_summary("Trade Republic", summary)
    return 1 if summary.errors else 0


def _cmd_xtb(excel_path: str, db_path: str, config_path: str) -> int:
    importer = XtbImporter(db_path=db_path, config_path=config_path)
    summary = importer.import_excel(excel_path)
    _print_import_summary("XTB", summary)
    return 1 if summary.errors else 0


def _build_parser() -> argparse.ArgumentParser:
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
    e1f transactions tr ~/Downloads/transactions.csv

  # Ingest XTB Cash Operations Excel export
  e1f transactions xtb ~/Downloads/EUR_38472916_2006-01-01_2026-08-15.xlsx

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
        aliases=["tr"],
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

    xtb_parser = subparsers.add_parser(
        "xtb",
        help="Ingest XTB Cash Operations Excel export",
    )
    xtb_parser.add_argument("excel_path", help="Path to the XTB Excel export file")
    xtb_parser.add_argument("--db", "-d", default=DEFAULT_DB, help="Database file path")
    xtb_parser.add_argument(
        "--config",
        "-c",
        default=DEFAULT_CONFIG,
        help="ETF universe config for ticker→ISIN and missing-ISIN report",
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if not args.command:
        parser.print_help()
        return 1

    try:
        if args.command == "list":
            return _cmd_list(args.db)
        if args.command in {"trade-republic", "tr"}:
            return _cmd_trade_republic(args.csv_path, args.db, args.config)
        if args.command == "xtb":
            return _cmd_xtb(args.excel_path, args.db, args.config)
        parser.error(f"unsupported command: {args.command!r}")
        return 1  # unreachable; keeps mypy happy
    except Exception as e:  # noqa: BLE001 — CLI top-level; all errors become exit code 1
        print(f"✗ Error: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
