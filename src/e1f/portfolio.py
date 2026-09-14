#!/usr/bin/env python
"""e1f portfolio — ETF holdings and average cost from stored transactions.

Usage:
    e1f portfolio
    e1f portfolio --db data/e1f.db --config data/etf_universe.yaml
    e1f portfolio --as-of 2025-12-31 --sort value --reverse
    e1f portfolio --diff 30
"""

import argparse
import sqlite3
import sys
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

from e1f.common import (
    DEFAULT_CONFIG,
    DEFAULT_CURRENCY_META,
    DEFAULT_DB,
    ConfigManager,
    MetricContract,
    Status,
    _explain_metric,
    annual_fee_estimate,
    convert_to_eur,
    pinned_quote_currency,
    weighted_ter_cost,
)

BUY_SIDES = frozenset({"BUY", "SAVINGS_PLAN"})
_SHARE_EPSILON = 1e-9
# Canonical tokens (ADR-0037): cost=Total paid, value=Value€.
SORT_FIELDS = (
    "broker", "isin", "name", "class", "ccy", "dist", "ter", "fee_yr",
    "weight", "units", "avg", "last_px", "cost", "value",
)
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
    as_of: str | None = None,
) -> list[tuple[str, str, str, str, float, float, float]]:
    with closing(sqlite3.connect(db_path)) as conn:
        if conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='transactions'"
        ).fetchone() is None:
            return []
        if as_of is not None:
            return conn.execute(
                """
                SELECT broker, datetime, symbol, side, shares, price, fee
                FROM transactions
                WHERE substr(datetime, 1, 10) <= ?
                ORDER BY datetime, transaction_id
                """,
                (as_of,),
            ).fetchall()
        return conn.execute(
            """
            SELECT broker, datetime, symbol, side, shares, price, fee
            FROM transactions
            ORDER BY datetime, transaction_id
            """
        ).fetchall()


def _latest_close(db_path: str, isin: str, as_of: str | None = None) -> tuple[str, float] | None:
    """``(date, close)`` of the most recent priced day for ``isin`` (native currency).

    When ``as_of`` is given, restricts to dates on/before that day.
    """
    with closing(sqlite3.connect(db_path)) as conn:
        if conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='prices'"
        ).fetchone() is None:
            return None
        if as_of is not None:
            row = conn.execute(
                "SELECT date, close FROM prices"
                " WHERE isin = ? AND close IS NOT NULL AND date <= ?"
                " ORDER BY date DESC LIMIT 1",
                (isin, as_of),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT date, close FROM prices"
                " WHERE isin = ? AND close IS NOT NULL ORDER BY date DESC LIMIT 1",
                (isin,),
            ).fetchone()
    return (str(row[0])[:10], float(row[1])) if row else None


def _last_known_price(db_path: str, isin: str, as_of: str | None = None) -> float | None:
    """Latest close in native currency (the ``Last px`` column); None if unpriced."""
    latest = _latest_close(db_path, isin, as_of)
    return latest[1] if latest else None


def _eur_value(
    db_path: str, currency_meta_path: str, isin: str, shares: float, as_of: str | None = None
) -> float | None:
    """EUR market value ``shares × latest close × FX``; None when it can't be valued.

    FX uses the rate as of the close's own date (ADR-0010). None when there is no
    price, no pinned trade currency, or no FX rate — never a silent mis-conversion,
    matching ``common.value_on`` and the ``performance`` valuation contract.
    When ``as_of`` is given, uses the latest close on/before that date.
    """
    latest = _latest_close(db_path, isin, as_of)
    if latest is None:
        return None
    price_date, close = latest
    quote = pinned_quote_currency(isin, currency_meta_path)
    if quote is None:
        return None
    try:
        return convert_to_eur(close * shares, quote, price_date, db_path)
    except ValueError:
        return None


def _eur_value_estimated(
    db_path: str, currency_meta_path: str, isin: str, shares: float, as_of: str
) -> tuple[float | None, bool]:
    """EUR value and whether the backing close predates ``as_of`` (stale/estimated)."""
    latest = _latest_close(db_path, isin, as_of)
    if latest is None:
        return None, False
    price_date, close = latest
    quote = pinned_quote_currency(isin, currency_meta_path)
    if quote is None:
        return None, False
    try:
        value = convert_to_eur(close * shares, quote, price_date, db_path)
        return value, price_date < as_of
    except ValueError:
        return None, False


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


def _fund_meta(
    config_path: str, symbol: str, currency_meta_path: str = DEFAULT_CURRENCY_META
) -> tuple[str, str, str, str, float | None]:
    data = ConfigManager(config_path).get(symbol) or {}
    asset_class = str(data.get("asset_class") or "")[:12]
    fund_ccy = str(data.get("fund_currency") or "")
    trade_ccy = pinned_quote_currency(symbol, currency_meta_path) or fund_ccy
    ccy = (
        f"{trade_ccy}({fund_ccy})" if trade_ccy and fund_ccy and trade_ccy != fund_ccy
        else (trade_ccy or fund_ccy)
    )
    distribution = str(data.get("distribution") or "")
    ter = data.get("ter")
    ter_float = float(ter) if isinstance(ter, (int, float)) else None
    ter_text = f"{ter_float:.2f}%" if ter_float is not None else ""
    return asset_class, ccy, distribution, ter_text, ter_float


def yearly_fee_est(ter_float: float | None, market_value: float | None) -> float | None:
    """Backward-compatible command helper delegated to the shared fee primitive."""
    return annual_fee_estimate(ter_float, market_value)


def _distribution_label(distribution: str) -> str:
    if distribution == "Accumulating":
        return "ACC"
    if distribution == "Distributing":
        return "Dist"
    return distribution


_BROKER_LABELS = {"trade_republic": "tr"}
_ASSET_CLASS_LABELS = {"Real Estate": "REITs", "Equity": "Eqty"}
_BROKER_COL = 4
_TABLE_WIDTH = _BROKER_COL + 158  # remaining columns + inter-column spaces


def _broker_label(broker: str) -> str:
    return _BROKER_LABELS.get(broker, broker)


def _asset_class_label(asset_class: str) -> str:
    return _ASSET_CLASS_LABELS.get(asset_class, asset_class)


def _sort_key(
    holding: Holding,
    sort_by: str,
    *,
    config_path: str,
    total_invested: float,
    eur_values: dict[tuple[str, str], float | None],
    last_prices: dict[str, float | None],
    currency_meta_path: str,
) -> tuple[Any, ...] | str | float:
    if sort_by == "broker":
        return (holding.broker, holding.symbol)
    if sort_by == "isin":
        return holding.symbol
    if sort_by == "name":
        return _etf_name(config_path, holding.symbol).lower()
    if sort_by == "class":
        return _asset_class_label(
            _fund_meta(config_path, holding.symbol, currency_meta_path)[0]
        ).lower()
    if sort_by == "ccy":
        return _fund_meta(config_path, holding.symbol, currency_meta_path)[1].lower()
    if sort_by == "dist":
        return _distribution_label(
            _fund_meta(config_path, holding.symbol, currency_meta_path)[2]
        ).lower()
    if sort_by == "weight":
        return holding_weight_pct(holding, total_invested)
    if sort_by == "cost":
        return holding.total_paid
    if sort_by == "units":
        return holding.shares
    if sort_by == "avg":
        return holding.avg_cost
    if sort_by == "value":
        value = eur_values.get((holding.broker, holding.symbol))
        return float("-inf") if value is None else value
    if sort_by == "last_px":
        px = last_prices.get(holding.symbol)
        return float("-inf") if px is None else px
    if sort_by == "ter":
        ter = (ConfigManager(config_path).get(holding.symbol) or {}).get("ter")
        return float(ter) if isinstance(ter, (int, float)) else float("-inf")
    if sort_by == "fee_yr":
        ter = (ConfigManager(config_path).get(holding.symbol) or {}).get("ter")
        ter_float = float(ter) if isinstance(ter, (int, float)) else None
        fee = yearly_fee_est(ter_float, eur_values.get((holding.broker, holding.symbol)))
        return fee if fee is not None else float("-inf")
    raise ValueError(f"unsupported sort field: {sort_by}")


def sort_holdings(
    holdings: list[Holding],
    *,
    sort_by: str = "broker",
    reverse: bool = False,
    config_path: str,
    total_invested: float,
    eur_values: dict[tuple[str, str], float | None],
    last_prices: dict[str, float | None] | None = None,
    currency_meta_path: str = DEFAULT_CURRENCY_META,
) -> list[Holding]:
    """Return holdings ordered by the requested column."""
    prices = last_prices or {}
    return sorted(
        holdings,
        key=lambda holding: _sort_key(
            holding,
            sort_by,
            config_path=config_path,
            total_invested=total_invested,
            eur_values=eur_values,
            last_prices=prices,
            currency_meta_path=currency_meta_path,
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


def _table_header(*, show_broker: bool, show_cost_basis: bool, show_status: bool) -> str:
    header = "\n"
    if show_broker:
        header += f"{'Brkr':<{_BROKER_COL}} "
    header += (
        f"{'ISIN':<14} {'Name':<32} {'Class':<6} "
        f"{'CCY':<8} {'Dist':<4} {'TER':>6}"
    )
    if show_cost_basis:
        header += (
            f" {'Fee/yr':>8} {'Weight':>7} {'Units':>10} {'Avg paid':>10}"
            f" {'Last px':>8} {'Dir':>4} {'Total':>8} {'Value€':>9}"
        )
    else:
        header += f" {'Weight':>7}"
    if show_status:
        header += f" {'Status':>{_STATUS_COL}}"
    return header


@dataclass(frozen=True)
class PortfolioDiffRow:
    """Per-ISIN signed delta between two portfolio snapshots."""

    isin: str
    name: str
    delta_units: float
    delta_cost: float
    delta_value: float | None   # None when either endpoint is unpriceable
    estimated: bool             # at least one endpoint's close is carried forward
    delta_weight: float | None  # %-pt change in cost-basis book weight; None for TOTAL
    delta_avg: float | None     # change in avg cost per share; None if not held at both
    delta_last_px: float | None # change in native-currency last close; None if unpriced

    @property
    def valuable(self) -> bool:
        return self.delta_value is not None


@dataclass
class _IsinsPoint:
    """Per-ISIN aggregated position at one snapshot date."""

    units: float
    cost: float
    eur_value: float | None
    estimated: bool
    last_px: float | None  # native currency, None if unpriced

    @property
    def avg_cost(self) -> float | None:
        return None if self.units <= 0 else self.cost / self.units


def _isin_snapshot(
    db_path: str, currency_meta_path: str, as_of: str
) -> dict[str, _IsinsPoint]:
    """ISIN → aggregated position across brokers at ``as_of``."""
    rows = _load_trade_rows(db_path, as_of)
    holdings = compute_holdings(rows)
    result: dict[str, _IsinsPoint] = {}
    for h in holdings:
        value, estimated = _eur_value_estimated(
            db_path, currency_meta_path, h.symbol, h.shares, as_of
        )
        last_px = _last_known_price(db_path, h.symbol, as_of)
        if h.symbol in result:
            prev = result[h.symbol]
            merged_value = (
                None if (prev.eur_value is None or value is None)
                else prev.eur_value + value
            )
            result[h.symbol] = _IsinsPoint(
                units=prev.units + h.shares,
                cost=prev.cost + h.total_paid,
                eur_value=merged_value,
                estimated=prev.estimated or estimated,
                last_px=last_px,  # same ISIN → same price regardless of broker
            )
        else:
            result[h.symbol] = _IsinsPoint(
                units=h.shares,
                cost=h.total_paid,
                eur_value=value,
                estimated=estimated,
                last_px=last_px,
            )
    return result


def _portfolio_diff_rows(
    start: dict[str, _IsinsPoint],
    end: dict[str, _IsinsPoint],
    config_path: str,
) -> list[PortfolioDiffRow]:
    start_total_cost = sum(p.cost for p in start.values())
    end_total_cost = sum(p.cost for p in end.values())

    result: list[PortfolioDiffRow] = []
    for isin in sorted(set(start) | set(end)):
        s = start.get(isin)
        e = end.get(isin)

        s_units = s.units if s else 0.0
        s_cost = s.cost if s else 0.0
        e_units = e.units if e else 0.0
        e_cost = e.cost if e else 0.0

        # ΔValue: None when either held endpoint is unpriceable.
        if (s is not None and s.eur_value is None) or (e is not None and e.eur_value is None):
            delta_value: float | None = None
        else:
            start_val = 0.0 if s is None else (s.eur_value or 0.0)
            end_val = 0.0 if e is None else (e.eur_value or 0.0)
            delta_value = end_val - start_val

        # ΔWeight%: cost-basis share of the whole book at each endpoint.
        s_weight = (100.0 * s_cost / start_total_cost) if start_total_cost > 0 else 0.0
        e_weight = (100.0 * e_cost / end_total_cost) if end_total_cost > 0 else 0.0
        delta_weight: float | None = e_weight - s_weight

        # ΔAvg paid: meaningful only when held at both endpoints.
        if s is not None and e is not None:
            delta_avg: float | None = (e.avg_cost or 0.0) - (s.avg_cost or 0.0)
        else:
            delta_avg = None

        # ΔLast px: None when either endpoint has no price.
        s_px = s.last_px if s else None
        e_px = e.last_px if e else None
        if s_px is not None and e_px is not None:
            delta_last_px: float | None = e_px - s_px
        else:
            delta_last_px = None

        result.append(PortfolioDiffRow(
            isin=isin,
            name=_etf_name(config_path, isin),
            delta_units=e_units - s_units,
            delta_cost=e_cost - s_cost,
            delta_value=delta_value,
            estimated=(s.estimated if s else False) or (e.estimated if e else False),
            delta_weight=delta_weight,
            delta_avg=delta_avg,
            delta_last_px=delta_last_px,
        ))
    return result


def _fmt_signed_money(value: float | None, *, flag: bool = False) -> str:
    if value is None:
        return "—"
    prefix = "+" if value > 0 else ""
    return f"{prefix}{value:,.2f}" + ("~" if flag else "")


def _fmt_signed_units(value: float) -> str:
    prefix = "+" if value > 0 else ""
    return f"{prefix}{value:.4f}"


def _fmt_signed_pct(value: float | None) -> str:
    if value is None:
        return "—"
    prefix = "+" if value > 0 else ""
    return f"{prefix}{value:.2f}%"


_DIFF_HEADER = (
    f"\n{'ISIN':<14} {'Name':<32} {'ΔUnits':>12} {'ΔCost€':>12} {'ΔValue€':>12}"
    f" {'ΔWgt%':>7} {'ΔAvg paid':>10} {'ΔLast px':>10}"
)
_DIFF_RULE_WIDTH = len(_DIFF_HEADER.lstrip("\n"))


def _format_diff_row(row: PortfolioDiffRow) -> str:
    units = _fmt_signed_units(row.delta_units)
    cost = _fmt_signed_money(row.delta_cost)
    value = _fmt_signed_money(row.delta_value, flag=row.estimated)
    weight = _fmt_signed_pct(row.delta_weight)
    avg = _fmt_signed_money(row.delta_avg)
    last_px = _fmt_signed_money(row.delta_last_px)
    return (
        f"{row.isin:<14} {row.name[:32]:<32} {units:>12} {cost:>12} {value:>12}"
        f" {weight:>7} {avg:>10} {last_px:>10}"
    )


def _diff_sort_key(row: PortfolioDiffRow, sort_by: str) -> str | float:
    if sort_by == "isin":
        return row.isin
    if sort_by == "name":
        return row.name.lower()
    if sort_by == "units":
        return row.delta_units
    if sort_by == "cost":
        return row.delta_cost
    if sort_by == "value":
        return float("-inf") if row.delta_value is None else row.delta_value
    if sort_by == "weight":
        return float("-inf") if row.delta_weight is None else row.delta_weight
    if sort_by == "avg":
        return float("-inf") if row.delta_avg is None else row.delta_avg
    if sort_by == "last_px":
        return float("-inf") if row.delta_last_px is None else row.delta_last_px
    return row.isin


def _cmd_portfolio_diff(
    db_path: str,
    config_path: str,
    *,
    start: str,
    end: str,
    currency_meta_path: str = DEFAULT_CURRENCY_META,
    sort_by: str = "isin",
    reverse: bool = False,
) -> int:
    start_snap = _isin_snapshot(db_path, currency_meta_path, start)
    end_snap = _isin_snapshot(db_path, currency_meta_path, end)

    if not start_snap and not end_snap:
        print("No ETF holdings in database")
        print("Ingest trades: e1f transactions trade-republic path/to/transactions.csv")
        return 0

    rows = _portfolio_diff_rows(start_snap, end_snap, config_path)
    rows = sorted(rows, key=lambda r: _diff_sort_key(r, sort_by), reverse=reverse)

    valuable = [r for r in rows if r.valuable]
    total_cost = sum(r.delta_cost for r in rows)
    total_value: float | None = (
        sum(r.delta_value for r in valuable if r.delta_value is not None)
        if valuable else None
    )
    total_row = PortfolioDiffRow(
        isin="TOTAL", name="",
        delta_units=sum(r.delta_units for r in rows),
        delta_cost=total_cost,
        delta_value=total_value,
        estimated=any(r.estimated for r in valuable),
        delta_weight=None,
        delta_avg=None,
        delta_last_px=None,
    )
    excluded = [r.isin for r in rows if not r.valuable]

    print(f"\nPortfolio holdings change {start} → {end}")
    print(_DIFF_HEADER)
    print("-" * _DIFF_RULE_WIDTH)
    for row in rows:
        print(_format_diff_row(row))
    print("-" * _DIFF_RULE_WIDTH)
    print(_format_diff_row(total_row))

    if any(r.estimated for r in valuable):
        print(
            "\n~ ΔValue€ estimated: at least one window endpoint used a "
            "carried-forward close (fetch to refresh)."
        )
    if excluded:
        print(
            "\n⚠ excluded from ΔValue€ (held but unpriceable at an endpoint): "
            + ", ".join(sorted(excluded))
        )
    return 0


def _cmd_portfolio(
    db_path: str,
    config_path: str,
    *,
    as_of: str,
    currency_meta_path: str = DEFAULT_CURRENCY_META,
    sort_by: str = "broker",
    reverse: bool = False,
    show_cost_basis: bool = False,
    show_status: bool = False,
    explain: bool = False,
    show_broker: bool = False,
) -> int:
    show_status = show_status or explain  # --explain implies status visibility (ADR-0014)
    rows = _load_trade_rows(db_path, as_of)
    holdings = compute_holdings(rows)

    if not holdings:
        print("No ETF holdings in database")
        print("Ingest trades: e1f transactions trade-republic path/to/transactions.csv")
        return 0

    total_invested = sum(holding.total_paid for holding in holdings)
    eur_values = {
        (holding.broker, holding.symbol): _eur_value(
            db_path, currency_meta_path, holding.symbol, holding.shares, as_of
        )
        for holding in holdings
    }
    total_market_value = sum(v for v in eur_values.values() if v is not None)
    last_prices = {
        holding.symbol: _last_known_price(db_path, holding.symbol, as_of) for holding in holdings
    }
    holdings = sort_holdings(
        holdings,
        sort_by=sort_by,
        reverse=reverse,
        config_path=config_path,
        total_invested=total_invested,
        eur_values=eur_values,
        last_prices=last_prices,
        currency_meta_path=currency_meta_path,
    )

    header = _table_header(
        show_broker=show_broker,
        show_cost_basis=show_cost_basis,
        show_status=show_status,
    )
    print(header)
    rule = _TABLE_WIDTH if show_cost_basis else _TABLE_WIDTH - 58
    if show_status:
        rule += _STATUS_COL + 1
    if not show_broker:
        rule -= _BROKER_COL + 1
    print("-" * rule)
    fee_inputs: list[tuple[float | None, float | None]] = []
    excluded: list[str] = []
    for holding in holdings:
        name = _etf_name(config_path, holding.symbol)
        asset_class, fund_currency, distribution, ter, ter_float = _fund_meta(
            config_path, holding.symbol, currency_meta_path
        )
        weight = holding_weight_pct(holding, total_invested)
        value = eur_values[(holding.broker, holding.symbol)]
        if value is None:
            excluded.append(holding.symbol)
        fee = yearly_fee_est(ter_float, value)
        fee_inputs.append((ter_float, value))
        row = ""
        if show_broker:
            row += f"{_broker_label(holding.broker):<{_BROKER_COL}} "
        row += (
            f"{holding.symbol:<14} {name:<32} "
            f"{_asset_class_label(asset_class):<6} {fund_currency:<8} "
            f"{_distribution_label(distribution):<4} {ter:>6}"
        )
        if show_cost_basis:
            fee_str = f"€{fee:.2f}" if fee is not None else "—"
            last_px = _last_known_price(db_path, holding.symbol, as_of)
            last_px_str = f"{last_px:>8.2f}" if last_px is not None else f"{'—':>8}"
            dir_str = "n/a" if last_px is None else ("+" if last_px >= holding.avg_cost else "-")
            value_str = f"{value:>9.2f}" if value is not None else f"{'—':>9}"
            row += (
                f" {fee_str:>8} {weight:>6.1f}% {holding.shares:>10.4f} {holding.avg_cost:>10.4f}"
                f" {last_px_str} {dir_str:>4} {holding.total_paid:>8.2f} {value_str}"
            )
        else:
            row += f" {weight:>6.1f}%"
        if show_status:
            row += f" {Status.CALCULATED.value:>{_STATUS_COL}}"
        print(row)
    weighted_ter, annual_fee = weighted_ter_cost(fee_inputs)
    total_fee_est = annual_fee or 0.0
    has_any_fee = annual_fee is not None
    total = f"\nTotal: {len(holdings)} holdings"
    if show_cost_basis:
        total += f", {total_invested:.2f} total paid"
        if total_market_value > 0:
            total += f", €{total_market_value:.2f} market value"
        if has_any_fee:
            total += f", ~€{total_fee_est:.2f}/yr in fees"
    if weighted_ter is not None:
        total += f", {weighted_ter:.3f}% weighted avg TER"
    print(total)
    if excluded:
        print(
            "\n⚠ excluded from market value / fee / weighted TER (no price or FX): "
            + ", ".join(sorted(set(excluded)))
        )
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
  e1f portfolio --as-of 2025-12-31 --sort value --reverse
  e1f portfolio --diff 30
  e1f portfolio --sort weight --reverse
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
        "--currency-meta",
        default=DEFAULT_CURRENCY_META,
        help="Currency metadata YAML (pinned ftgo resolutions)",
    )
    parser.add_argument(
        "--as-of",
        default=datetime.now(UTC).date().isoformat(),
        metavar="YYYY-MM-DD",
        help="Show holdings as of this date (default: today)",
    )
    parser.add_argument(
        "--diff",
        metavar="N",
        default=None,
        help="Show signed change over the last N calendar days instead of a snapshot "
        "(composes with --as-of: window is [as_of − N, as_of]). N ≥ 1.",
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
    parser.add_argument(
        "--show-broker",
        action="store_true",
        help="Show broker column",
    )

    return parser


def _validate_as_of(as_of: str) -> None:
    try:
        date.fromisoformat(as_of)
    except ValueError as exc:
        raise ValueError(f"--as-of must be YYYY-MM-DD: {as_of}") from exc


def _validate_positive_int(raw: str | None, flag: str) -> int | None:
    if raw is None:
        return None
    try:
        n = int(raw)
    except ValueError as exc:
        raise ValueError(f"{flag} must be a positive integer, got: {raw!r}") from exc
    if n < 1:
        raise ValueError(f"{flag} must be ≥ 1, got: {n}")
    return n


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    try:
        _validate_as_of(args.as_of)
        diff_n = _validate_positive_int(args.diff, "--diff")
        if diff_n is not None:
            end = args.as_of
            start = (date.fromisoformat(end) - timedelta(days=diff_n)).isoformat()
            return _cmd_portfolio_diff(
                args.db,
                args.config,
                start=start,
                end=end,
                currency_meta_path=args.currency_meta,
                sort_by=args.sort,
                reverse=args.reverse,
            )
        return _cmd_portfolio(
            args.db,
            args.config,
            as_of=args.as_of,
            currency_meta_path=args.currency_meta,
            sort_by=args.sort,
            reverse=args.reverse,
            show_cost_basis=args.show_cost_basis,
            show_status=args.show_status,
            explain=args.explain,
            show_broker=args.show_broker,
        )
    except Exception as e:  # noqa: BLE001 — CLI top-level; all errors become exit code 1
        print(f"✗ Error: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
