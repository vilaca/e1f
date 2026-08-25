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
from typing import Any

from e1f.common import (
    DEFAULT_CONFIG,
    DEFAULT_DB,
    ConfigManager,
    MetricContract,
    Status,
    _explain_metric,
)

BUY_SIDES = frozenset({"BUY", "SAVINGS_PLAN"})
_SHARE_EPSILON = 1e-9
SORT_FIELDS = ("broker", "isin", "name", "weight", "total", "units", "avg", "ter")
_STATUS_COL = 11


# Provenance contract (ADR-0014). Holdings are derived exactly from stored
# transactions — no market data, no look-through — so every holding is CALCULATED;
# ``Status`` / ``MetricContract`` and the ``--explain`` helper live in ``common``
# (ADR-0013 decision 8), this instance stays here.
HOLDINGS_CONTRACT = MetricContract(
    method_version="average_cost_v1",
    requires=(),  # complete: derived fully from the stored transaction history
    does_not_require=("price data", "FX rates", "look-through holdings"),
    supports=("net shares", "average cost", "total paid", "cost-basis weight"),
    limitations=(
        "average-cost accounting (not FIFO/LIFO); realized-gain tax lots not tracked",
        "weight is a share of cost basis, not market value",
        "fund metadata (asset class, currency, distribution, TER) shown only where "
        "the config carries it",
    ),
)


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


def holding_weight_pct(holding: Holding, total_invested: float) -> float:
    """Share of total cost basis attributed to this holding, as a percentage."""
    if total_invested <= 0:
        return 0.0
    return 100.0 * holding.total_paid / total_invested


def _etf_name(config_path: str, symbol: str) -> str:
    data = ConfigManager(config_path).get(symbol)
    if not data:
        return ""
    return str(data.get("name", ""))[:40]


def _fund_meta(config_path: str, symbol: str) -> tuple[str, str, str, str]:
    data = ConfigManager(config_path).get(symbol) or {}
    asset_class = str(data.get("asset_class") or "")[:12]
    fund_currency = str(data.get("fund_currency") or "")
    distribution = str(data.get("distribution") or "")
    ter = data.get("ter")
    ter_text = f"{float(ter):.2f}%" if isinstance(ter, (int, float)) else ""
    return asset_class, fund_currency, distribution, ter_text


def _distribution_label(distribution: str) -> str:
    if distribution == "Accumulating":
        return "ACC"
    if distribution == "Distributing":
        return "Dist"
    return distribution


_BROKER_LABELS = {"trade_republic": "tr"}
_BROKER_COL = 4
_TABLE_WIDTH = _BROKER_COL + 130  # remaining columns + inter-column spaces


def _broker_label(broker: str) -> str:
    return _BROKER_LABELS.get(broker, broker)


def _sort_key(
    holding: Holding,
    sort_by: str,
    *,
    config_path: str,
    total_invested: float,
) -> tuple[Any, ...] | str | float:
    if sort_by == "broker":
        return (holding.broker, holding.symbol)
    if sort_by == "isin":
        return holding.symbol
    if sort_by == "name":
        return _etf_name(config_path, holding.symbol).lower()
    if sort_by == "weight":
        return holding_weight_pct(holding, total_invested)
    if sort_by == "total":
        return holding.total_paid
    if sort_by == "units":
        return holding.shares
    if sort_by == "avg":
        return holding.avg_cost
    if sort_by == "ter":
        ter = (ConfigManager(config_path).get(holding.symbol) or {}).get("ter")
        return float(ter) if isinstance(ter, (int, float)) else -1.0
    raise ValueError(f"unsupported sort field: {sort_by}")


def sort_holdings(
    holdings: list[Holding],
    *,
    sort_by: str = "broker",
    reverse: bool = False,
    config_path: str,
    total_invested: float,
) -> list[Holding]:
    """Return holdings ordered by the requested column."""
    return sorted(
        holdings,
        key=lambda holding: _sort_key(
            holding,
            sort_by,
            config_path=config_path,
            total_invested=total_invested,
        ),
        reverse=reverse,
    )


def _has_config_entry(config_path: str, symbol: str) -> bool:
    return ConfigManager(config_path).get(symbol) is not None


def render_holdings_explain(
    holdings: list[Holding], config_path: str, total_invested: float
) -> list[str]:
    """Reconstruct the holdings provenance block from the computed holdings.

    Portfolio holdings share one identical contract and status, so ``--explain``
    emits a single block (not one per row, ADR-0014 decision 4) and reports config
    metadata completeness across the set. Nothing is read from a persisted log.
    """
    missing = sorted(h.symbol for h in holdings if not _has_config_entry(config_path, h.symbol))
    completeness = (
        f"config metadata present for all {len(holdings)} holdings"
        if not missing
        else f"{len(missing)} of {len(holdings)} holdings not in config "
        f"(metadata blank): {', '.join(missing)}"
    )
    lines = ["\nProvenance (--explain) — reconstructed from source, not a log:"]
    lines.extend(_explain_metric(
        "Holdings (average-cost)",
        Status.CALCULATED,
        f"{len(holdings)} holdings ; €{total_invested:,.2f} total cost basis",
        f"net BUY/SELL per broker+symbol from stored transactions ; {completeness}",
        "average-cost accounting ; weight = total_paid / Σ total_paid",
        HOLDINGS_CONTRACT,
    ))
    return lines


def _cmd_portfolio(
    db_path: str,
    config_path: str,
    *,
    sort_by: str = "broker",
    reverse: bool = False,
    show_cost_basis: bool = False,
    show_status: bool = False,
    explain: bool = False,
) -> int:
    show_status = show_status or explain  # --explain implies status visibility (ADR-0014)
    rows = _load_trade_rows(db_path)
    holdings = compute_holdings(rows)

    if not holdings:
        print("No ETF holdings in database")
        print("Ingest trades: e1f transactions trade-republic path/to/transactions.csv")
        return 0

    total_invested = sum(holding.total_paid for holding in holdings)
    holdings = sort_holdings(
        holdings,
        sort_by=sort_by,
        reverse=reverse,
        config_path=config_path,
        total_invested=total_invested,
    )

    header = (
        f"\n{'Brkr':<{_BROKER_COL}} {'ISIN':<14} {'Name':<32} {'Asset class':<12} "
        f"{'CCY':<4} {'Dist':<4} {'TER':>6} {'Weight':>7}"
    )
    if show_cost_basis:
        header += f" {'Units':>10} {'Avg paid':>10} {'Total paid':>10}"
    if show_status:
        header += f" {'Status':>{_STATUS_COL}}"
    print(header)
    rule = _TABLE_WIDTH if show_cost_basis else _TABLE_WIDTH - 42
    if show_status:
        rule += _STATUS_COL + 1
    print("-" * rule)
    for holding in holdings:
        name = _etf_name(config_path, holding.symbol)
        asset_class, fund_currency, distribution, ter = _fund_meta(
            config_path, holding.symbol
        )
        weight = holding_weight_pct(holding, total_invested)
        row = (
            f"{_broker_label(holding.broker):<{_BROKER_COL}} {holding.symbol:<14} {name:<32} "
            f"{asset_class:<12} {fund_currency:<4} "
            f"{_distribution_label(distribution):<4} {ter:>6} {weight:>6.1f}%"
        )
        if show_cost_basis:
            row += (
                f" {holding.shares:>10.4f} {holding.avg_cost:>10.4f}"
                f" {holding.total_paid:>10.4f}"
            )
        if show_status:
            row += f" {Status.CALCULATED.value:>{_STATUS_COL}}"
        print(row)
    total = f"\nTotal: {len(holdings)} holdings"
    if show_cost_basis:
        total += f", {total_invested:.4f} total paid"
    print(total)
    if explain:
        for line in render_holdings_explain(holdings, config_path, total_invested):
            print(line)
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="e1f portfolio",
        description="Show ETF holdings and average cost per share from transactions",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Provenance (ADR-0014, off by default): --show-status adds a Status column
(uniformly CALCULATED — holdings are exact from transactions); --explain adds a
provenance block with config-metadata completeness and implies --show-status.

Examples:
  e1f portfolio
  e1f portfolio --db data/e1f.db --config data/etf_universe.yaml
  e1f portfolio --sort weight --reverse
  e1f portfolio --sort total --reverse
  e1f portfolio --show-status
  e1f portfolio --explain
        """,
    )
    parser.add_argument("--db", "-d", default=DEFAULT_DB, help="Database file path")
    parser.add_argument(
        "--config",
        "-c",
        default=DEFAULT_CONFIG,
        help="ETF universe config for security names",
    )
    parser.add_argument(
        "--sort",
        choices=SORT_FIELDS,
        default="broker",
        help="Sort holdings by column (default: broker, then ISIN)",
    )
    parser.add_argument(
        "--reverse",
        "-r",
        action="store_true",
        help="Descending sort order",
    )
    parser.add_argument(
        "--show-cost-basis",
        action="store_true",
        help="Show units, average paid, and total paid columns",
    )
    parser.add_argument(
        "--show-status",
        action="store_true",
        help="Add a provenance Status column (ADR-0014)",
    )
    parser.add_argument(
        "--explain",
        action="store_true",
        help="Add a provenance block (Status/contract/limited-by; implies --show-status)",
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    try:
        return _cmd_portfolio(
            args.db,
            args.config,
            sort_by=args.sort,
            reverse=args.reverse,
            show_cost_basis=args.show_cost_basis,
            show_status=args.show_status,
            explain=args.explain,
        )
    except Exception as e:  # noqa: BLE001 — CLI top-level; all errors become exit code 1
        print(f"✗ Error: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
