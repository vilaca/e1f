#!/usr/bin/env python
"""e1f portfolio — ETF holdings and average cost from stored transactions.

Usage:
    e1f portfolio
    e1f portfolio --db data/e1f.db --config data/etf_universe.yaml
"""

import argparse
import sqlite3
import sys
from contextlib import closing
from dataclasses import dataclass

from e1f.common import DEFAULT_CONFIG, DEFAULT_DB, ConfigManager

BUY_SIDES = frozenset({"BUY", "SAVINGS_PLAN"})
_SHARE_EPSILON = 1e-9


@dataclass(frozen=True)
class Holding:
    """Net ETF position derived from transaction history."""

    broker: str
    symbol: str
    shares: float
    avg_cost: float
    total_paid: float


def _load_trade_rows(
    db_path: str,
) -> list[tuple[str, str, str, str, float, float, float]]:
    with closing(sqlite3.connect(db_path)) as conn:
        if conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='transactions'"
        ).fetchone() is None:
            return []
        return conn.execute(
            """
            SELECT broker, datetime, symbol, side, shares, price, fee
            FROM transactions
            ORDER BY datetime, transaction_id
            """
        ).fetchall()


def compute_holdings(
    rows: list[tuple[str, str, str, str, float, float, float]],
) -> list[Holding]:
    """Derive open positions using average-cost accounting per broker and symbol."""
    state: dict[tuple[str, str], tuple[float, float]] = {}

    for broker, _dt, symbol, side, shares, price, fee in rows:
        qty = shares or 0.0
        if qty <= 0:
            continue
        unit_price = price or 0.0
        trade_fee = fee or 0.0
        key = (broker, symbol)
        held, cost = state.get(key, (0.0, 0.0))

        if side in BUY_SIDES:
            state[key] = (held + qty, cost + qty * unit_price + trade_fee)
        elif side == "SELL":
            if held <= _SHARE_EPSILON:
                continue
            sell_qty = min(qty, held)
            avg = cost / held
            state[key] = (held - sell_qty, cost - avg * sell_qty)

    holdings: list[Holding] = []
    for broker, symbol in sorted(state):
        held, cost = state[(broker, symbol)]
        if held <= _SHARE_EPSILON:
            continue
        holdings.append(
            Holding(
                broker=broker,
                symbol=symbol,
                shares=held,
                avg_cost=cost / held,
                total_paid=cost,
            )
        )
    return holdings


def _etf_name(config_path: str, symbol: str) -> str:
    data = ConfigManager(config_path).get(symbol)
    if not data:
        return ""
    return str(data.get("name", ""))[:40]


def _cmd_portfolio(db_path: str, config_path: str) -> int:
    rows = _load_trade_rows(db_path)
    holdings = compute_holdings(rows)

    if not holdings:
        print("No ETF holdings in database")
        print("Ingest trades: e1f transactions trade-republic path/to/transactions.csv")
        return 0

    print(
        f"\n{'Broker':<16} {'ISIN':<14} {'Name':<40} {'Shares':>10} "
        f"{'Avg paid':>10} {'Total paid':>12}"
    )
    print("-" * 116)
    for holding in holdings:
        name = _etf_name(config_path, holding.symbol)
        print(
            f"{holding.broker:<16} {holding.symbol:<14} {name:<40} "
            f"{holding.shares:>10.4f} {holding.avg_cost:>10.2f} "
            f"{holding.total_paid:>12.2f}"
        )
    print(f"\nTotal: {len(holdings)} holdings")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="e1f portfolio",
        description="Show ETF holdings and average cost per share from transactions",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  e1f portfolio
  e1f portfolio --db data/e1f.db --config data/etf_universe.yaml
        """,
    )
    parser.add_argument("--db", "-d", default=DEFAULT_DB, help="Database file path")
    parser.add_argument(
        "--config",
        "-c",
        default=DEFAULT_CONFIG,
        help="ETF universe config for security names",
    )

    args = parser.parse_args(argv)

    try:
        return _cmd_portfolio(args.db, args.config)
    except Exception as e:  # noqa: BLE001 — CLI top-level; all errors become exit code 1
        print(f"✗ Error: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
