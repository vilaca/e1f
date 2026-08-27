"""Performance: XIRR/TWR/risk math, EUR valuation, and the table command (ADR-0011)."""

import sqlite3
from contextlib import closing

import pytest
import yaml

from e1f import performance as perf
from e1f.common import PositionEvent, close_asof, load_trades, position_timeline
from e1f.performance import (
    HoldingSeries,
    annualize,
    risk_metrics,
    sort_rows,
)

EUR_ISIN = "IE00EUR000001"
USD_ISIN = "IE00USD000001"


# The pure XIRR solver graduated to ``common`` (ADR-0019); its unit tests live in
# tests/test_common.py now. ``performance`` re-exports ``xirr`` unchanged.


# ---------------------------------------------------------------------------
# Risk metrics + annualization
# ---------------------------------------------------------------------------

def test_risk_metrics_twr_and_flat_drawdown():
    # 1000 in on day one, rising to 1200 with no later contributions => TWR 20%.
    series = [
        ("2024-01-01", 1000.0, 1000.0),
        ("2024-07-01", 1100.0, 0.0),
        ("2024-12-31", 1200.0, 0.0),
    ]
    metrics = risk_metrics(series)
    assert metrics.twr == pytest.approx(0.20)
    assert metrics.volatility is not None and metrics.volatility > 0.0
    assert metrics.max_drawdown == pytest.approx(0.0)  # monotonic rise


def test_risk_metrics_drawdown_on_wealth_index_not_raw_value():
    # A dip then partial recovery; contributions must not mask the drawdown.
    series = [
        ("2024-01-01", 1000.0, 1000.0),
        ("2024-06-01", 800.0, 0.0),   # -20%
        ("2024-09-01", 1900.0, 1000.0),  # +1000 contributed, not a recovery
    ]
    metrics = risk_metrics(series)
    assert metrics.max_drawdown == pytest.approx(-0.20)


def test_risk_metrics_empty_series_is_all_none():
    metrics = risk_metrics([])
    assert metrics == perf.RiskMetrics(twr=None, volatility=None, max_drawdown=None)


def test_risk_metrics_single_return_has_no_volatility():
    # One valued day (bought and valued the same day): one return, no stdev.
    metrics = risk_metrics([("2024-01-01", 1000.0, 1000.0)])
    assert metrics.twr == pytest.approx(0.0)
    assert metrics.volatility is None  # need >= 2 returns for a stdev


def test_annualize_identity_over_one_year():
    assert annualize(0.20, 365) == pytest.approx(0.20)


def test_annualize_guards_undefined_inputs():
    assert annualize(None, 365) is None
    assert annualize(0.2, 0) is None
    assert annualize(-1.5, 365) is None  # total loss beyond -100%


# ---------------------------------------------------------------------------
# Valuation helpers (position / close / value on a date)
# ---------------------------------------------------------------------------

def _events(*triples):
    """PositionEvents from (date, shares_held, cost_basis); cash_flow = cost delta."""
    events, prev_cost = [], 0.0
    for day, shares, cost in triples:
        events.append(
            PositionEvent(date=day, cash_flow=cost - prev_cost, shares_held=shares, cost_basis=cost)
        )
        prev_cost = cost
    return events


def test_position_asof_steps_through_events():
    events = _events(("2024-01-01", 1.0, 100.0), ("2024-03-01", 3.0, 320.0))
    assert perf._position_asof(events, "2023-12-31") == (0.0, 0.0)
    assert perf._position_asof(events, "2024-02-01") == (1.0, 100.0)
    assert perf._position_asof(events, "2024-09-01") == (3.0, 320.0)


def test_close_asof_nearest_prior():
    series = HoldingSeries(
        "I", [], ["2024-01-05", "2024-06-01"], [100.0, 120.0], "EUR"
    )
    assert close_asof(series, "2024-01-04") is None
    assert close_asof(series, "2024-03-01") == 100.0
    assert close_asof(series, "2024-06-01") == 120.0


def _fx_db(tmp_path, rows=()):
    db = tmp_path / "fx.db"
    with closing(sqlite3.connect(str(db))) as conn:
        conn.execute(
            "CREATE TABLE fx_rates (base TEXT, quote TEXT, date TEXT, rate REAL, "
            "PRIMARY KEY (base, quote, date))"
        )
        conn.executemany("INSERT INTO fx_rates VALUES (?, ?, ?, ?)", rows)
        conn.commit()
    return str(db)


def test_value_on_eur_passthrough_no_db():
    series = HoldingSeries(
        "I", _events(("2024-01-01", 10.0, 1000.0)),
        ["2024-01-01", "2024-06-01"], [100.0, 120.0], "EUR",
    )
    assert perf._value_on(series, "2024-06-01", "/no/db") == 1200.0


def test_value_on_usd_converts_via_fx(tmp_path):
    db = _fx_db(tmp_path, [("EUR", "USD", "2024-06-01", 1.2)])
    series = HoldingSeries(
        "I", _events(("2024-01-01", 10.0, 900.0)), ["2024-06-01"], [120.0], "USD"
    )
    assert perf._value_on(series, "2024-06-01", db) == pytest.approx(1000.0)  # 10*120/1.2


def test_value_on_zero_before_first_buy():
    series = HoldingSeries(
        "I", _events(("2024-01-01", 10.0, 1000.0)), ["2024-01-01"], [100.0], "EUR"
    )
    assert perf._value_on(series, "2023-12-31", "/no/db") == 0.0


def test_value_on_none_when_unpriced_or_no_currency_or_no_fx(tmp_path):
    events = _events(("2024-01-01", 10.0, 1000.0))
    priced = HoldingSeries("I", events, ["2024-06-01"], [120.0], "EUR")
    assert perf._value_on(priced, "2024-03-01", "/no/db") is None  # held, before first price
    no_ccy = HoldingSeries("I", events, ["2024-06-01"], [120.0], None)
    assert perf._value_on(no_ccy, "2024-06-01", "/no/db") is None
    usd = HoldingSeries(
        "I", _events(("2024-01-01", 10.0, 900.0)), ["2024-06-01"], [120.0], "USD"
    )
    assert perf._value_on(usd, "2024-06-01", _fx_db(tmp_path)) is None  # no FX rate -> None


# ---------------------------------------------------------------------------
# DB fixture + row building
# ---------------------------------------------------------------------------

def _seed(tmp_path, *, transactions=(), prices=(), fx=(), currencies=None, names=None):
    db = tmp_path / "e1f.db"
    with closing(sqlite3.connect(str(db))) as conn:
        conn.execute(
            "CREATE TABLE transactions (broker TEXT, transaction_id TEXT, datetime TEXT, "
            "symbol TEXT, side TEXT, shares REAL, price REAL, fee REAL, tax REAL, "
            "PRIMARY KEY (broker, transaction_id))"
        )
        conn.execute(
            "CREATE TABLE prices (isin TEXT, date TEXT, close REAL, PRIMARY KEY (isin, date))"
        )
        conn.execute(
            "CREATE TABLE fx_rates (base TEXT, quote TEXT, date TEXT, rate REAL, "
            "PRIMARY KEY (base, quote, date))"
        )
        conn.executemany(
            "INSERT INTO transactions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", transactions
        )
        conn.executemany("INSERT INTO prices VALUES (?, ?, ?)", prices)
        conn.executemany("INSERT INTO fx_rates VALUES (?, ?, ?, ?)", fx)
        conn.commit()
    config = tmp_path / "config.yaml"
    config.write_text(yaml.dump({"etfs": {isin: {"name": n} for isin, n in (names or {}).items()}}))
    meta = tmp_path / "meta.yaml"
    meta.write_text(
        yaml.dump({isin: {"currency": c, "symbol": f"{isin}:X:{c}", "xid": "1"}
                   for isin, c in (currencies or {}).items()})
    )
    return str(db), str(config), str(meta)


def _buy(txid, day, isin, shares, price_eur, fee=0.0, broker="tr"):
    return (broker, txid, day, isin, "BUY", shares, price_eur, fee, 0.0)


def _row_for(db, config, meta, isin, as_of):
    events = [e for e in position_timeline(load_trades(db))[isin] if e.date <= as_of]
    series = perf._build_series(db, isin, events, as_of, meta)
    return perf._build_row(isin, series, as_of, config, db)


def test_build_row_eur_holding_full_metrics(tmp_path):
    db, config, meta = _seed(
        tmp_path,
        transactions=[_buy("t1", "2024-01-01", EUR_ISIN, 100.0, 10.0)],
        prices=[
            (EUR_ISIN, "2024-01-01", 10.0),
            (EUR_ISIN, "2024-07-01", 11.0),
            (EUR_ISIN, "2024-12-31", 12.0),
        ],
        currencies={EUR_ISIN: "EUR"},
        names={EUR_ISIN: "Euro Fund"},
    )
    row = _row_for(db, config, meta, EUR_ISIN, "2024-12-31")
    assert row.market_value == pytest.approx(1200.0)
    assert row.cost == pytest.approx(1000.0)
    assert row.pnl == pytest.approx(200.0)
    assert row.pnl_pct == pytest.approx(20.0)
    assert row.xirr == pytest.approx(0.20, rel=1e-3)
    assert row.twr == pytest.approx(0.20, rel=1e-3)
    assert row.cagr == pytest.approx(0.20, rel=1e-3)
    assert row.max_drawdown == pytest.approx(0.0)
    assert row.short_history is False  # 365-day window


def test_build_row_usd_asymmetry_cost_eur_value_converted(tmp_path):
    # Cost leg stays EUR (900); value leg converts USD close via FX. Day-0 P&L appears.
    db, config, meta = _seed(
        tmp_path,
        transactions=[_buy("t1", "2024-01-01", USD_ISIN, 10.0, 90.0)],
        prices=[(USD_ISIN, "2024-01-01", 100.0), (USD_ISIN, "2024-12-31", 120.0)],
        fx=[("EUR", "USD", "2024-01-01", 1.0), ("EUR", "USD", "2024-12-31", 1.2)],
        currencies={USD_ISIN: "USD"},
        names={USD_ISIN: "Dollar Fund"},
    )
    row = _row_for(db, config, meta, USD_ISIN, "2024-12-31")
    assert row.cost == pytest.approx(900.0)               # EUR, unconverted
    assert row.market_value == pytest.approx(1000.0)      # 10 * 120 / 1.2
    assert row.pnl == pytest.approx(100.0)


def test_build_row_unvaluable_when_no_price(tmp_path):
    db, config, meta = _seed(
        tmp_path,
        transactions=[_buy("t1", "2024-01-01", EUR_ISIN, 100.0, 10.0)],
        currencies={EUR_ISIN: "EUR"},
        names={EUR_ISIN: "Euro Fund"},
    )
    row = _row_for(db, config, meta, EUR_ISIN, "2024-12-31")
    assert row.valuable is False
    assert row.market_value is None
    assert row.pnl is None and row.pnl_pct is None
    assert row.cost == pytest.approx(1000.0)


def test_build_row_none_when_not_held_at_as_of(tmp_path):
    db, config, meta = _seed(
        tmp_path,
        transactions=[_buy("t1", "2024-06-01", EUR_ISIN, 100.0, 10.0)],
        prices=[(EUR_ISIN, "2024-06-01", 10.0)],
        currencies={EUR_ISIN: "EUR"},
        names={EUR_ISIN: "Euro Fund"},
    )
    # as_of precedes the only contribution => not held yet.
    events = [e for e in position_timeline(load_trades(db))[EUR_ISIN] if e.date <= "2024-01-01"]
    series = perf._build_series(db, EUR_ISIN, events, "2024-01-01", meta)
    assert perf._build_row(EUR_ISIN, series, "2024-01-01", config, db) is None


def test_build_row_not_estimated_when_priced_on_as_of(tmp_path):
    db, config, meta = _seed(
        tmp_path,
        transactions=[_buy("t1", "2024-01-01", EUR_ISIN, 100.0, 10.0)],
        prices=[(EUR_ISIN, "2024-01-01", 10.0), (EUR_ISIN, "2024-12-31", 12.0)],
        currencies={EUR_ISIN: "EUR"},
        names={EUR_ISIN: "Euro Fund"},
    )
    row = _row_for(db, config, meta, EUR_ISIN, "2024-12-31")
    assert row.estimated is False
    assert row.price_date == "2024-12-31"


def test_build_row_estimated_when_price_carried_forward(tmp_path):
    db, config, meta = _seed(
        tmp_path,
        transactions=[_buy("t1", "2024-01-01", EUR_ISIN, 100.0, 10.0)],
        prices=[(EUR_ISIN, "2024-01-01", 10.0), (EUR_ISIN, "2024-12-20", 12.0)],
        currencies={EUR_ISIN: "EUR"},
        names={EUR_ISIN: "Euro Fund"},
    )
    # No close on the as-of day: value carried forward from 2024-12-20.
    row = _row_for(db, config, meta, EUR_ISIN, "2024-12-31")
    assert row.estimated is True
    assert row.price_date == "2024-12-20"
    assert row.market_value == pytest.approx(1200.0)


# ---------------------------------------------------------------------------
# Sorting
# ---------------------------------------------------------------------------

def _perf_row(isin, value):
    return perf.PerformanceRow(
        isin=isin, name=isin.lower(), cost=100.0, market_value=value,
        xirr=None, twr=None, volatility=None, max_drawdown=None, cagr=None,
        short_history=False,
    )


def test_sort_rows_value_reverse_with_none_last():
    rows = [_perf_row("A", 300.0), _perf_row("B", None), _perf_row("C", 500.0)]
    ordered = [r.isin for r in sort_rows(rows, sort_by="value", reverse=True)]
    assert ordered == ["C", "A", "B"]  # None (-inf) sinks to the bottom


def test_sort_rows_by_isin_default():
    rows = [_perf_row("C", 1.0), _perf_row("A", 2.0), _perf_row("B", 3.0)]
    assert [r.isin for r in sort_rows(rows)] == ["A", "B", "C"]


def test_assign_pnl_contributions_shares_sum_to_100():
    # cost=100 each: pnl = +100, -50 -> net +50; shares 200% and -100%.
    rows = [_perf_row("A", 200.0), _perf_row("B", 50.0)]
    perf._assign_pnl_contributions(rows)
    assert rows[0].pnl_contribution == pytest.approx(200.0)
    assert rows[1].pnl_contribution == pytest.approx(-100.0)
    assert sum(r.pnl_contribution for r in rows) == pytest.approx(100.0)


def test_assign_pnl_contributions_none_when_no_pnl_or_zero_total():
    # Unvaluable row gets None; a zero net total leaves every share undefined.
    rows = [_perf_row("A", 200.0), _perf_row("B", 0.0), _perf_row("C", None)]
    perf._assign_pnl_contributions(rows)
    # net pnl = +100 (A) - 100 (B) + None (C) = 0 -> all None
    assert [r.pnl_contribution for r in rows] == [None, None, None]


# ---------------------------------------------------------------------------
# Command (main / _cmd_performance) end to end
# ---------------------------------------------------------------------------

def _args(db, config, meta, *extra):
    return ["--db", db, "--config", config, "--currency-meta", meta, *extra]


def test_main_two_holdings_totals(tmp_path, capsys):
    db, config, meta = _seed(
        tmp_path,
        transactions=[
            _buy("t1", "2024-01-01", EUR_ISIN, 100.0, 10.0),
            _buy("t2", "2024-01-01", USD_ISIN, 10.0, 90.0),
        ],
        prices=[
            (EUR_ISIN, "2024-01-01", 10.0), (EUR_ISIN, "2024-12-31", 12.0),
            (USD_ISIN, "2024-01-01", 100.0), (USD_ISIN, "2024-12-31", 120.0),
        ],
        fx=[("EUR", "USD", "2024-01-01", 1.0), ("EUR", "USD", "2024-12-31", 1.2)],
        currencies={EUR_ISIN: "EUR", USD_ISIN: "USD"},
        names={EUR_ISIN: "Euro Fund", USD_ISIN: "Dollar Fund"},
    )
    code = perf.main(_args(db, config, meta, "--as-of", "2024-12-31"))
    out = capsys.readouterr().out
    assert code == 0
    assert EUR_ISIN in out and USD_ISIN in out
    assert "TOTAL" in out
    assert "1,200.00" in out          # EUR holding market value
    assert "2,200.00" in out          # total market value (1200 + 1000)
    # P&Lctr: EUR pnl +200, USD pnl +100, net +300 -> 66.7% / 33.3%, TOTAL 100%.
    assert "P&Lctr" in out
    assert "66.7%" in out
    assert "33.3%" in out
    assert "100.0%" in out             # TOTAL contribution


def test_main_unvaluable_row_excluded_from_total_with_warning(tmp_path, capsys):
    db, config, meta = _seed(
        tmp_path,
        transactions=[
            _buy("t1", "2024-01-01", EUR_ISIN, 100.0, 10.0),
            _buy("t2", "2024-01-01", USD_ISIN, 10.0, 90.0),  # no prices -> unvaluable
        ],
        prices=[(EUR_ISIN, "2024-01-01", 10.0), (EUR_ISIN, "2024-12-31", 12.0)],
        currencies={EUR_ISIN: "EUR", USD_ISIN: "USD"},
        names={EUR_ISIN: "Euro Fund", USD_ISIN: "Dollar Fund"},
    )
    code = perf.main(_args(db, config, meta, "--as-of", "2024-12-31"))
    out = capsys.readouterr().out
    assert code == 0
    assert "n/a" in out
    assert "excluded from TOTAL" in out and USD_ISIN in out


def test_main_short_history_flagged(tmp_path, capsys):
    db, config, meta = _seed(
        tmp_path,
        transactions=[_buy("t1", "2026-06-01", EUR_ISIN, 100.0, 10.0)],
        prices=[(EUR_ISIN, "2026-06-01", 10.0), (EUR_ISIN, "2026-08-01", 11.0)],
        currencies={EUR_ISIN: "EUR"},
        names={EUR_ISIN: "Euro Fund"},
    )
    code = perf.main(_args(db, config, meta, "--as-of", "2026-08-01"))
    out = capsys.readouterr().out
    assert code == 0
    assert "*" in out
    assert "short history" in out


def test_main_estimated_price_flagged(tmp_path, capsys):
    db, config, meta = _seed(
        tmp_path,
        transactions=[_buy("t1", "2024-01-01", EUR_ISIN, 100.0, 10.0)],
        prices=[(EUR_ISIN, "2024-01-01", 10.0), (EUR_ISIN, "2024-12-20", 12.0)],
        currencies={EUR_ISIN: "EUR"},
        names={EUR_ISIN: "Euro Fund"},
    )
    code = perf.main(_args(db, config, meta, "--as-of", "2024-12-31"))
    out = capsys.readouterr().out
    assert code == 0
    assert "1,200.00~" in out                     # MktVal marked as carried forward
    # Single stale date collapses to one summary line, no per-ISIN listing.
    assert "MktVal estimated: no close on 2024-12-31" in out
    assert "freshest data is 2024-12-20 (11d stale) for all holdings" in out
    assert EUR_ISIN not in out.split("MktVal estimated")[1]


def test_main_estimated_prices_mixed_dates_listed_per_isin(tmp_path, capsys):
    other = "IE00OTHER0001"
    db, config, meta = _seed(
        tmp_path,
        transactions=[
            _buy("t1", "2024-01-01", EUR_ISIN, 100.0, 10.0),
            _buy("t2", "2024-01-01", other, 100.0, 10.0),
        ],
        prices=[
            (EUR_ISIN, "2024-01-01", 10.0),
            (EUR_ISIN, "2024-12-20", 12.0),
            (other, "2024-01-01", 10.0),
            (other, "2024-12-27", 12.0),
        ],
        currencies={EUR_ISIN: "EUR", other: "EUR"},
        names={EUR_ISIN: "Euro Fund", other: "Other Fund"},
    )
    perf.main(_args(db, config, meta, "--as-of", "2024-12-31"))
    out = capsys.readouterr().out
    # Differing stale dates fall back to the per-ISIN listing.
    assert "MktVal estimated from the latest price before 2024-12-31" in out
    assert f"{EUR_ISIN}  2024-12-20 (11d stale)" in out
    assert f"{other}  2024-12-27 (4d stale)" in out


def test_main_no_estimate_note_when_priced_on_as_of(tmp_path, capsys):
    db, config, meta = _seed(
        tmp_path,
        transactions=[_buy("t1", "2024-01-01", EUR_ISIN, 100.0, 10.0)],
        prices=[(EUR_ISIN, "2024-01-01", 10.0), (EUR_ISIN, "2024-12-31", 12.0)],
        currencies={EUR_ISIN: "EUR"},
        names={EUR_ISIN: "Euro Fund"},
    )
    perf.main(_args(db, config, meta, "--as-of", "2024-12-31"))
    out = capsys.readouterr().out
    assert "~" not in out
    assert "estimated from the latest price" not in out


def test_main_as_of_values_historical_snapshot(tmp_path, capsys):
    db, config, meta = _seed(
        tmp_path,
        transactions=[_buy("t1", "2024-01-01", EUR_ISIN, 100.0, 10.0)],
        prices=[
            (EUR_ISIN, "2024-01-01", 10.0),
            (EUR_ISIN, "2024-06-01", 11.0),
            (EUR_ISIN, "2024-12-31", 20.0),
        ],
        currencies={EUR_ISIN: "EUR"},
        names={EUR_ISIN: "Euro Fund"},
    )
    perf.main(_args(db, config, meta, "--as-of", "2024-06-01"))
    out = capsys.readouterr().out
    assert "1,100.00" in out       # valued at the 2024-06-01 close, not the 2024-12-31 one
    assert "2,000.00" not in out


def test_main_no_transactions_message(tmp_path, capsys):
    db, config, meta = _seed(tmp_path)
    code = perf.main(_args(db, config, meta))
    out = capsys.readouterr().out
    assert code == 0
    assert "No ETF holdings in database" in out


def test_main_as_of_before_any_holding(tmp_path, capsys):
    db, config, meta = _seed(
        tmp_path,
        transactions=[_buy("t1", "2024-06-01", EUR_ISIN, 100.0, 10.0)],
        prices=[(EUR_ISIN, "2024-06-01", 10.0)],
        currencies={EUR_ISIN: "EUR"},
        names={EUR_ISIN: "Euro Fund"},
    )
    code = perf.main(_args(db, config, meta, "--as-of", "2024-01-01"))
    out = capsys.readouterr().out
    assert code == 0
    assert "No holdings as of 2024-01-01" in out


def test_main_invalid_as_of_errors(tmp_path, capsys):
    db, config, meta = _seed(tmp_path)
    code = perf.main(_args(db, config, meta, "--as-of", "not-a-date"))
    out = capsys.readouterr().out
    assert code == 1
    assert "must be YYYY-MM-DD" in out


def test_main_sort_value_reverse_orders_rows(tmp_path, capsys):
    small, big = "IE00SMALL00001", "IE00BIG0000001"
    db, config, meta = _seed(
        tmp_path,
        transactions=[
            _buy("t1", "2024-01-01", small, 1.0, 10.0),
            _buy("t2", "2024-01-01", big, 100.0, 10.0),
        ],
        prices=[
            (small, "2024-01-01", 10.0), (small, "2024-12-31", 10.0),
            (big, "2024-01-01", 10.0), (big, "2024-12-31", 10.0),
        ],
        currencies={small: "EUR", big: "EUR"},
        names={small: "Small", big: "Big"},
    )
    perf.main(_args(db, config, meta, "--as-of", "2024-12-31", "--sort", "value", "--reverse"))
    out = capsys.readouterr().out
    assert out.index(big) < out.index(small)  # larger market value first


def test_main_help_exits_zero(capsys):
    with pytest.raises(SystemExit) as excinfo:
        perf.main(["--help"])
    assert excinfo.value.code == 0
    assert "performance" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Provenance disclosure (ADR-0014): row_status, --show-status, --explain
# ---------------------------------------------------------------------------


def _status_row(**overrides):
    base = dict(
        isin="X", name="n", cost=1.0, market_value=10.0, xirr=0.1, twr=0.1,
        volatility=0.2, max_drawdown=-0.1, cagr=0.1, short_history=False,
    )
    base.update(overrides)
    return perf.PerformanceRow(**base)


def test_row_status_calculated_when_valuable():
    assert perf.row_status(_status_row(market_value=10.0)) is perf.Status.CALCULATED


def test_row_status_unavailable_when_not_valuable():
    assert perf.row_status(_status_row(market_value=None)) is perf.Status.UNAVAILABLE


def _two_holdings_one_unvaluable(tmp_path):
    unk = "IE00UNK000001"
    return _seed(
        tmp_path,
        transactions=[
            _buy("t1", "2024-01-01", EUR_ISIN, 100.0, 10.0),
            _buy("t2", "2024-12-01", unk, 5.0, 20.0),  # no price -> unvaluable
        ],
        prices=[(EUR_ISIN, "2024-01-01", 10.0), (EUR_ISIN, "2024-12-31", 12.0)],
        currencies={EUR_ISIN: "EUR"},
        names={EUR_ISIN: "Euro Fund"},
    ), unk


def test_main_default_has_no_status_column(tmp_path, capsys):
    db, config, meta = _seed(
        tmp_path,
        transactions=[_buy("t1", "2024-01-01", EUR_ISIN, 100.0, 10.0)],
        prices=[(EUR_ISIN, "2024-01-01", 10.0), (EUR_ISIN, "2024-12-31", 12.0)],
        currencies={EUR_ISIN: "EUR"},
        names={EUR_ISIN: "Euro Fund"},
    )
    perf.main(_args(db, config, meta, "--as-of", "2024-12-31"))
    out = capsys.readouterr().out
    assert "Status" not in out
    assert "CALCULATED" not in out


def test_main_show_status_adds_column_with_both_states(tmp_path, capsys):
    (db, config, meta), _unk = _two_holdings_one_unvaluable(tmp_path)
    perf.main(_args(db, config, meta, "--as-of", "2024-12-31", "--show-status"))
    out = capsys.readouterr().out
    assert "Status" in out
    assert "CALCULATED" in out    # the valuable holding + TOTAL
    assert "UNAVAILABLE" in out   # the unvaluable holding


def test_main_explain_implies_status_and_prints_blocks(tmp_path, capsys):
    (db, config, meta), unk = _two_holdings_one_unvaluable(tmp_path)
    perf.main(_args(db, config, meta, "--as-of", "2024-12-31", "--explain"))
    out = capsys.readouterr().out
    assert "Status" in out                          # --explain implies the column
    assert "reconstructed from source, not a log" in out
    assert "method = eur_valuation_v1" in out
    assert "method = xirr_twr_v1" in out
    assert "Result:" in out and "Inputs:" in out and "Limited by:" in out
    assert "Not limited by: look-through holdings" in out
    # the unvaluable holding's block names its UNAVAILABLE valuation
    assert "no close/FX on or before the as-of date" in out
    assert unk in out


# ---------------------------------------------------------------------------
# --diff mode: unit seam (_diff_rows) — ADR-0029
# ---------------------------------------------------------------------------

def _ep_row(isin, *, value, cost=100.0, estimated=False):
    """Minimal PerformanceRow for _diff_rows unit tests."""
    return perf.PerformanceRow(
        isin=isin, name=isin.lower(), cost=cost, market_value=value,
        xirr=None, twr=None, volatility=None, max_drawdown=None, cagr=None,
        short_history=False, estimated=estimated,
    )


def test_diff_rows_held_through_produces_signed_delta():
    start = {"A": _ep_row("A", value=1000.0, cost=900.0)}
    end = {"A": _ep_row("A", value=1200.0, cost=900.0)}
    rows = perf._diff_rows(start, end)
    assert len(rows) == 1
    r = rows[0]
    assert r.isin == "A"
    assert r.delta_market_value == pytest.approx(200.0)
    assert r.delta_cost == pytest.approx(0.0)
    assert r.delta_pnl == pytest.approx(200.0)
    assert r.valuable is True
    assert r.estimated is False


def test_diff_rows_new_in_window_start_is_zero():
    start: dict[str, perf.PerformanceRow] = {}
    end = {"B": _ep_row("B", value=500.0, cost=450.0)}
    rows = perf._diff_rows(start, end)
    assert len(rows) == 1
    r = rows[0]
    assert r.delta_market_value == pytest.approx(500.0)
    assert r.delta_cost == pytest.approx(450.0)
    assert r.delta_pnl == pytest.approx(50.0)


def test_diff_rows_sold_in_window_end_is_zero():
    start = {"C": _ep_row("C", value=800.0, cost=700.0)}
    end: dict[str, perf.PerformanceRow] = {}
    rows = perf._diff_rows(start, end)
    assert len(rows) == 1
    r = rows[0]
    assert r.delta_market_value == pytest.approx(-800.0)
    assert r.delta_cost == pytest.approx(-700.0)
    assert r.delta_pnl == pytest.approx(-100.0)


def test_diff_rows_held_but_unpriceable_at_end_is_unavailable():
    start = {"D": _ep_row("D", value=1000.0, cost=900.0)}
    end = {"D": _ep_row("D", value=None, cost=900.0)}  # held but no price
    rows = perf._diff_rows(start, end)
    assert len(rows) == 1
    r = rows[0]
    assert r.valuable is False
    assert r.delta_market_value is None
    assert r.delta_pnl is None


def test_diff_rows_held_but_unpriceable_at_start_is_unavailable():
    start = {"E": _ep_row("E", value=None, cost=900.0)}
    end = {"E": _ep_row("E", value=1200.0, cost=900.0)}
    rows = perf._diff_rows(start, end)
    assert rows[0].valuable is False


def test_diff_rows_union_of_isins():
    start = {"A": _ep_row("A", value=100.0), "B": _ep_row("B", value=200.0)}
    end = {"B": _ep_row("B", value=210.0), "C": _ep_row("C", value=300.0)}
    isins = [r.isin for r in perf._diff_rows(start, end)]
    assert sorted(isins) == ["A", "B", "C"]


def test_diff_rows_estimated_flag_propagates_from_either_endpoint():
    start = {"F": _ep_row("F", value=1000.0, estimated=True)}
    end = {"F": _ep_row("F", value=1100.0, estimated=False)}
    rows = perf._diff_rows(start, end)
    assert rows[0].estimated is True

    start2 = {"G": _ep_row("G", value=1000.0, estimated=False)}
    end2 = {"G": _ep_row("G", value=1100.0, estimated=True)}
    rows2 = perf._diff_rows(start2, end2)
    assert rows2[0].estimated is True


def test_diff_rows_empty_when_no_isins():
    assert perf._diff_rows({}, {}) == []


# ---------------------------------------------------------------------------
# --diff mode: command seam (main + capsys) — ADR-0029
# ---------------------------------------------------------------------------

def _sell(txid, day, isin, shares, price_eur, fee=0.0, broker="tr"):
    return (broker, txid, day, isin, "SELL", shares, price_eur, fee, 0.0)


def test_diff_main_two_holdings_header_and_totals(tmp_path, capsys):
    db, config, meta = _seed(
        tmp_path,
        transactions=[
            _buy("t1", "2024-01-01", EUR_ISIN, 100.0, 10.0),   # cost 1000
            _buy("t2", "2024-01-01", USD_ISIN, 10.0, 90.0),    # cost 900 EUR
        ],
        prices=[
            (EUR_ISIN, "2024-12-24", 11.0),   # start: 1100
            (EUR_ISIN, "2024-12-31", 12.0),   # end:   1200
            (USD_ISIN, "2024-12-24", 110.0),  # start: 10*110/1.1 = 1000
            (USD_ISIN, "2024-12-31", 120.0),  # end:   10*120/1.2 = 1000
        ],
        fx=[
            ("EUR", "USD", "2024-12-24", 1.1),
            ("EUR", "USD", "2024-12-31", 1.2),
        ],
        currencies={EUR_ISIN: "EUR", USD_ISIN: "USD"},
        names={EUR_ISIN: "Euro Fund", USD_ISIN: "Dollar Fund"},
    )
    code = perf.main(_args(db, config, meta, "--as-of", "2024-12-31", "--diff", "7"))
    out = capsys.readouterr().out
    assert code == 0
    assert "Performance change 2024-12-24 → 2024-12-31 (EUR)" in out
    # EUR_ISIN: +100 (1200-1100), USD_ISIN: 0, TOTAL: +100
    assert "+100.00" in out
    assert "TOTAL" in out
    # Rate columns (XIRR, TWR, etc.) must NOT appear
    assert "XIRR" not in out and "TWR" not in out and "CAGR" not in out


def test_diff_main_new_position_in_window(tmp_path, capsys):
    new_isin = "IE00NEW000001"
    db, config, meta = _seed(
        tmp_path,
        transactions=[
            _buy("t1", "2024-01-01", EUR_ISIN, 100.0, 10.0),
            _buy("t2", "2024-12-28", new_isin, 50.0, 20.0),   # bought inside window
        ],
        prices=[
            (EUR_ISIN, "2024-12-24", 11.0),
            (EUR_ISIN, "2024-12-31", 12.0),
            (new_isin, "2024-12-28", 20.0),
            (new_isin, "2024-12-31", 22.0),
        ],
        currencies={EUR_ISIN: "EUR", new_isin: "EUR"},
        names={EUR_ISIN: "Euro Fund", new_isin: "New Fund"},
    )
    code = perf.main(_args(db, config, meta, "--as-of", "2024-12-31", "--diff", "7"))
    out = capsys.readouterr().out
    assert code == 0
    assert new_isin in out
    # new_isin: start=0, end=50*22=1100 → delta = +1,100.00
    assert "+1,100.00" in out


def test_diff_main_sold_position_appears_with_negative_delta(tmp_path, capsys):
    sold_isin = "IE00SOLD00001"
    db, config, meta = _seed(
        tmp_path,
        transactions=[
            _buy("t1", "2024-01-01", EUR_ISIN, 100.0, 10.0),
            _buy("t2", "2024-01-01", sold_isin, 50.0, 20.0),    # cost 1000
            _sell("t3", "2024-12-27", sold_isin, 50.0, 22.0),   # sell all inside window
        ],
        prices=[
            (EUR_ISIN, "2024-12-24", 11.0),
            (EUR_ISIN, "2024-12-31", 12.0),
            (sold_isin, "2024-12-24", 22.0),  # start: 50*22 = 1100
        ],
        currencies={EUR_ISIN: "EUR", sold_isin: "EUR"},
        names={EUR_ISIN: "Euro Fund", sold_isin: "Sold Fund"},
    )
    code = perf.main(_args(db, config, meta, "--as-of", "2024-12-31", "--diff", "7"))
    out = capsys.readouterr().out
    assert code == 0
    assert sold_isin in out
    # sold_isin: start=1100, end=0 → delta = -1,100.00
    assert "-1,100.00" in out


def test_diff_main_carry_forward_endpoint_flagged_estimated(tmp_path, capsys):
    db, config, meta = _seed(
        tmp_path,
        transactions=[_buy("t1", "2024-01-01", EUR_ISIN, 100.0, 10.0)],
        prices=[
            (EUR_ISIN, "2024-12-20", 11.0),   # no close on 2024-12-24 (start) — carry
            (EUR_ISIN, "2024-12-31", 12.0),
        ],
        currencies={EUR_ISIN: "EUR"},
        names={EUR_ISIN: "Euro Fund"},
    )
    code = perf.main(_args(db, config, meta, "--as-of", "2024-12-31", "--diff", "7"))
    out = capsys.readouterr().out
    assert code == 0
    assert "~" in out
    assert "estimated" in out and "carried-forward" in out


def test_diff_main_same_close_both_endpoints_all_zero(tmp_path, capsys):
    db, config, meta = _seed(
        tmp_path,
        transactions=[_buy("t1", "2024-01-01", EUR_ISIN, 100.0, 10.0)],
        prices=[
            (EUR_ISIN, "2024-12-20", 11.0),  # close applies to both endpoints via carry
        ],
        currencies={EUR_ISIN: "EUR"},
        names={EUR_ISIN: "Euro Fund"},
    )
    # Both 2024-12-24 and 2024-12-31 carry forward from 2024-12-20 → same price
    code = perf.main(_args(db, config, meta, "--as-of", "2024-12-31", "--diff", "7"))
    out = capsys.readouterr().out
    assert code == 0
    # Delta is 0; no error
    assert "0.00" in out


def test_diff_main_unpriceable_endpoint_shows_dash_and_excluded(tmp_path, capsys):
    unpriced = "IE00UNP000001"
    db, config, meta = _seed(
        tmp_path,
        transactions=[
            _buy("t1", "2024-01-01", EUR_ISIN, 100.0, 10.0),
            _buy("t2", "2024-01-01", unpriced, 50.0, 20.0),  # held but never priced
        ],
        prices=[
            (EUR_ISIN, "2024-12-24", 11.0),
            (EUR_ISIN, "2024-12-31", 12.0),
        ],
        currencies={EUR_ISIN: "EUR", unpriced: "EUR"},
        names={EUR_ISIN: "Euro Fund", unpriced: "Unpriced Fund"},
    )
    code = perf.main(_args(db, config, meta, "--as-of", "2024-12-31", "--diff", "7"))
    out = capsys.readouterr().out
    assert code == 0
    assert unpriced in out
    assert "—" in out                      # em dash for unavailable row
    assert "excluded from TOTAL" in out
    assert unpriced in out.split("excluded from TOTAL")[1]


def test_diff_main_rate_columns_absent(tmp_path, capsys):
    db, config, meta = _seed(
        tmp_path,
        transactions=[_buy("t1", "2024-01-01", EUR_ISIN, 100.0, 10.0)],
        prices=[
            (EUR_ISIN, "2024-12-24", 11.0),
            (EUR_ISIN, "2024-12-31", 12.0),
        ],
        currencies={EUR_ISIN: "EUR"},
        names={EUR_ISIN: "Euro Fund"},
    )
    code = perf.main(_args(db, config, meta, "--as-of", "2024-12-31", "--diff", "7"))
    out = capsys.readouterr().out
    assert code == 0
    for absent in ("XIRR", "TWR", "Vol", "MaxDD", "CAGR", "P&Lctr", "P&L%"):
        assert absent not in out, f"rate column {absent!r} should not appear in diff output"


def test_diff_main_composes_with_as_of(tmp_path, capsys):
    db, config, meta = _seed(
        tmp_path,
        transactions=[_buy("t1", "2024-01-01", EUR_ISIN, 100.0, 10.0)],
        prices=[
            (EUR_ISIN, "2024-06-24", 11.0),
            (EUR_ISIN, "2024-07-01", 13.0),
        ],
        currencies={EUR_ISIN: "EUR"},
        names={EUR_ISIN: "Euro Fund"},
    )
    # Window: 2024-06-24 → 2024-07-01
    code = perf.main(_args(db, config, meta, "--as-of", "2024-07-01", "--diff", "7"))
    out = capsys.readouterr().out
    assert code == 0
    assert "Performance change 2024-06-24 → 2024-07-01 (EUR)" in out
    # delta = 100*(13-11) = +200
    assert "+200.00" in out


def test_diff_main_invalid_n_rejected(tmp_path, capsys):
    db, config, meta = _seed(tmp_path)
    for bad in ("0", "-1", "1.5", "foo"):
        code = perf.main(_args(db, config, meta, "--diff", bad))
        assert code != 0, f"expected non-zero exit for --diff {bad!r}"


def test_diff_main_show_status_adds_column(tmp_path, capsys):
    db, config, meta = _seed(
        tmp_path,
        transactions=[_buy("t1", "2024-01-01", EUR_ISIN, 100.0, 10.0)],
        prices=[
            (EUR_ISIN, "2024-12-24", 11.0),
            (EUR_ISIN, "2024-12-31", 12.0),
        ],
        currencies={EUR_ISIN: "EUR"},
        names={EUR_ISIN: "Euro Fund"},
    )
    code = perf.main(
        _args(db, config, meta, "--as-of", "2024-12-31", "--diff", "7", "--show-status")
    )
    out = capsys.readouterr().out
    assert code == 0
    assert "Status" in out
    assert "CALCULATED" in out


def test_diff_main_explain_adds_provenance_note(tmp_path, capsys):
    db, config, meta = _seed(
        tmp_path,
        transactions=[_buy("t1", "2024-01-01", EUR_ISIN, 100.0, 10.0)],
        prices=[
            (EUR_ISIN, "2024-12-24", 11.0),
            (EUR_ISIN, "2024-12-31", 12.0),
        ],
        currencies={EUR_ISIN: "EUR"},
        names={EUR_ISIN: "Euro Fund"},
    )
    code = perf.main(_args(db, config, meta, "--as-of", "2024-12-31", "--diff", "7", "--explain"))
    out = capsys.readouterr().out
    assert code == 0
    assert "Status" in out       # --explain implies show-status
    assert "reading-A" in out


def test_diff_main_invariance_diff_equals_end_minus_start(tmp_path, capsys):
    """--diff N TOTAL must equal (--as-of end TOTAL) minus (--as-of start TOTAL)."""
    db, config, meta = _seed(
        tmp_path,
        transactions=[
            _buy("t1", "2024-01-01", EUR_ISIN, 100.0, 10.0),
            _buy("t2", "2024-01-01", USD_ISIN, 10.0, 90.0),
        ],
        prices=[
            (EUR_ISIN, "2024-12-24", 11.0),
            (EUR_ISIN, "2024-12-31", 12.0),
            (USD_ISIN, "2024-12-24", 110.0),
            (USD_ISIN, "2024-12-31", 120.0),
        ],
        fx=[
            ("EUR", "USD", "2024-12-24", 1.1),
            ("EUR", "USD", "2024-12-31", 1.2),
        ],
        currencies={EUR_ISIN: "EUR", USD_ISIN: "USD"},
        names={EUR_ISIN: "Euro Fund", USD_ISIN: "Dollar Fund"},
    )

    def _total_market_value(out: str) -> float:
        for line in out.splitlines():
            if line.startswith("TOTAL"):
                # columns: ISIN Name MktVal€ Cost€ P&L€ ...
                # or diff:  ISIN Name ΔMktVal€ ΔCost€ ΔP&L€ ...
                parts = line.split()
                # find the first numeric token after TOTAL
                for tok in parts[1:]:
                    cleaned = tok.replace(",", "").replace("+", "").replace("~", "")
                    try:
                        return float(cleaned)
                    except ValueError:
                        continue
        raise AssertionError(f"no TOTAL line found in:\n{out}")

    perf.main(_args(db, config, meta, "--as-of", "2024-12-31"))
    end_mv = _total_market_value(capsys.readouterr().out)

    perf.main(_args(db, config, meta, "--as-of", "2024-12-24"))
    start_mv = _total_market_value(capsys.readouterr().out)

    perf.main(_args(db, config, meta, "--as-of", "2024-12-31", "--diff", "7"))
    diff_mv = _total_market_value(capsys.readouterr().out)

    assert diff_mv == pytest.approx(end_mv - start_mv, abs=0.01)
