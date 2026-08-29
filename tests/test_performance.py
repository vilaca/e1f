"""Performance: XIRR/TWR/risk math, EUR valuation, and the table command (ADR-0011)."""

import re
import sqlite3
from contextlib import closing

import pytest
import yaml

from e1f import performance as perf
from e1f import portfolio as portfolio_mod
from e1f.common import (
    PositionEvent,
    aggregate_value_series,
    build_series,
    close_asof,
    contribution_to_return,
    load_trades,
    portfolio_return_series,
    position_timeline,
    wealth_and_returns,
)
from e1f.performance import (
    HoldingSeries,
    annualize,
    extended_metrics,
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
# Extended metrics (ADR-0033): drawdown shape + daily extremes
# ---------------------------------------------------------------------------

# A clean peak(1.2)→trough(0.9, -25%)→recovery(1.32) wealth path from one buy.
_DD_POINTS = [
    ("2024-01-01", 100.0, 100.0),  # r=0    wealth=1.00
    ("2024-01-02", 120.0, 0.0),    # r=+0.20 wealth=1.20 (peak)
    ("2024-01-03", 90.0, 0.0),     # r=-0.25 wealth=0.90 (-25% drawdown)
    ("2024-01-04", 132.0, 0.0),    # r=+0.4667 wealth=1.32 (recovers past the peak)
]


def test_extended_metrics_drawdown_recovery_and_extremes():
    twr = risk_metrics(_DD_POINTS).twr
    ext = extended_metrics(_DD_POINTS, twr)

    assert ext.max_drawdown == pytest.approx(-0.25)
    assert ext.max_dd_peak_date == "2024-01-02"
    assert ext.max_dd_recovery_date == "2024-01-04"
    assert ext.max_dd_ongoing is False
    assert ext.max_dd_duration_days == 2  # 01-02 → 01-04, calendar days
    assert ext.underwater_days == 2       # single episode
    assert ext.days_since_high == 0       # 01-04 is a new high — back at peak
    # Recovery factor = TWR / |MaxDD| = 0.32 / 0.25.
    assert ext.recovery_factor == pytest.approx(0.32 / 0.25)
    assert ext.best_day == pytest.approx(132.0 / 90.0 - 1.0)
    assert ext.best_day_date == "2024-01-04"
    assert ext.worst_day == pytest.approx(-0.25)
    assert ext.worst_day_date == "2024-01-03"
    assert ext.gain_loss_ratio == pytest.approx((132.0 / 90.0 - 1.0) / 0.25)


def test_extended_metrics_maxdd_agrees_with_risk_metrics():
    # The two must never disagree — same time-weighted return recurrence.
    risk = risk_metrics(_DD_POINTS)
    ext = extended_metrics(_DD_POINTS, risk.twr)
    assert ext.max_drawdown == pytest.approx(risk.max_drawdown)


def test_extended_metrics_ongoing_drawdown_measured_to_last_day():
    points = [
        ("2024-01-01", 100.0, 100.0),  # wealth 1.00
        ("2024-01-02", 120.0, 0.0),    # wealth 1.20 (peak)
        ("2024-01-05", 90.0, 0.0),     # wealth 0.90 (-25%), never recovers
    ]
    twr = risk_metrics(points).twr
    ext = extended_metrics(points, twr)
    assert ext.max_dd_ongoing is True
    assert ext.max_dd_peak_date == "2024-01-02"
    assert ext.max_dd_recovery_date == "2024-01-05"  # last day, not a real recovery
    assert ext.max_dd_duration_days == 3
    assert ext.underwater_days == 3
    assert ext.days_since_high == 3  # deepest episode is the open one — they coincide
    # TWR = -0.10 while still down 25% → a negative recovery factor.
    assert ext.recovery_factor == pytest.approx(-0.10 / 0.25)


def test_extended_metrics_no_drawdown_has_no_recovery_factor():
    points = [
        ("2024-01-01", 100.0, 100.0),  # wealth 1.00
        ("2024-01-02", 110.0, 0.0),    # wealth 1.10
        ("2024-01-03", 120.0, 0.0),    # wealth 1.20 — monotonic, never below peak
    ]
    ext = extended_metrics(points, risk_metrics(points).twr)
    assert ext.max_drawdown == pytest.approx(0.0)
    assert ext.max_dd_duration_days == 0
    assert ext.underwater_days == 0
    assert ext.max_dd_ongoing is False
    assert ext.max_dd_peak_date is None and ext.max_dd_recovery_date is None
    assert ext.recovery_factor is None          # nothing to recover from
    assert ext.days_since_high == 0             # monotonic up — every day a new high
    # Worst "day" is the +0% first return, so Max Gain / Max Loss is undefined.
    assert ext.gain_loss_ratio is None


def test_extended_metrics_deepest_of_several_episodes_wins():
    # Two drawdowns: -10% (recovers), then a deeper -20% (recovers). Deepest drives
    # the duration/peak fields; underwater sums both episodes.
    points = [
        ("2024-01-01", 100.0, 100.0),  # 1.00
        ("2024-01-02", 90.0, 0.0),     # 0.90  -10% (episode A opens at peak 01-01)
        ("2024-01-03", 100.0, 0.0),    # 1.00  recovers A (2 days)
        ("2024-01-04", 80.0, 0.0),     # 0.80  -20% (episode B opens at peak 01-03)
        ("2024-01-06", 100.0, 0.0),    # 1.00  recovers B (3 days)
    ]
    ext = extended_metrics(points, risk_metrics(points).twr)
    assert ext.max_drawdown == pytest.approx(-0.20)
    assert ext.max_dd_peak_date == "2024-01-03"      # deeper episode's peak
    assert ext.max_dd_recovery_date == "2024-01-06"
    assert ext.max_dd_duration_days == 3
    assert ext.underwater_days == 5                   # 2 (A) + 3 (B)
    assert ext.days_since_high == 0                   # recovers on 01-06 (a new high)


def test_extended_metrics_days_since_high_tracks_current_not_deepest():
    # Deepest drawdown (-20%) recovers, then a shallower (-10%) dip stays open at the
    # end. MaxDD Duration still describes the deepest (recovered) episode, but Days
    # Since High tracks the *current* running peak (01-05), not the deepest one.
    points = [
        ("2024-01-01", 100.0, 100.0),  # 1.00
        ("2024-01-02", 80.0, 0.0),     # 0.80  -20% (episode A opens at peak 01-01)
        ("2024-01-03", 100.0, 0.0),    # 1.00  recovers A (2 days), new peak 01-03
        ("2024-01-05", 110.0, 0.0),    # 1.10  new peak 01-05
        ("2024-01-15", 99.0, 0.0),     # 0.99  -10% off 01-05, still open at the end
    ]
    ext = extended_metrics(points, risk_metrics(points).twr)
    assert ext.max_drawdown == pytest.approx(-0.20)   # deepest episode
    assert ext.max_dd_ongoing is False                # deepest one recovered
    assert ext.max_dd_peak_date == "2024-01-01"
    assert ext.max_dd_duration_days == 2              # 01-01 → 01-03
    assert ext.days_since_high == 10                 # 01-05 → 01-15, the current dip


def test_extended_metrics_empty_series_is_all_none():
    ext = extended_metrics([], None)
    assert ext.max_drawdown is None
    assert ext.max_dd_duration_days is None
    assert ext.underwater_days is None
    assert ext.days_since_high is None
    assert ext.max_dd_ongoing is False
    assert ext.best_month is None and ext.worst_month is None
    assert ext.best_month_label is None and ext.worst_month_label is None
    assert ext.trailing_1m is None and ext.trailing_3m is None and ext.trailing_6m is None
    assert ext.recovery_factor is None
    assert ext.best_day is None and ext.worst_day is None
    assert ext.gain_loss_ratio is None


# A two-calendar-month path: Jan +10%, then Feb -20% then +5% (chain-linked -16%).
_SPAN_POINTS = [
    ("2024-01-01", 100.0, 100.0),  # r=0     (Jan)
    ("2024-01-31", 110.0, 0.0),    # r=+0.10 (Jan) → Jan month +10%
    ("2024-02-15", 88.0, 0.0),     # r=-0.20 (Feb)
    ("2024-02-29", 92.4, 0.0),     # r=+0.05 (Feb) → Feb month -16%
]


def test_monthly_returns_chain_links_by_calendar_month():
    _wealth, returns = perf._wealth_and_returns(_SPAN_POINTS)
    monthly = perf._monthly_returns(returns)
    assert monthly == [
        ("2024-01", pytest.approx(0.10)),
        ("2024-02", pytest.approx(-0.16)),
    ]
    assert perf._monthly_returns([]) == []


def test_subtract_months_clamps_to_month_length():
    from datetime import date

    assert perf._subtract_months(date(2024, 3, 31), 1) == date(2024, 2, 29)  # leap clamp
    assert perf._subtract_months(date(2024, 3, 15), 3) == date(2023, 12, 15)  # crosses year
    assert perf._subtract_months(date(2024, 1, 15), 1) == date(2023, 12, 15)


def test_trailing_return_windows_and_inception_gate():
    wealth_path = [("2024-01-15", 1.05), ("2024-02-15", 1.10), ("2024-03-15", 1.20)]
    # 1M: base = wealth on/before 2024-02-15 (1.10) → 1.20/1.10 − 1.
    assert perf._trailing_return(wealth_path, "2024-01-01", 1) == pytest.approx(1.20 / 1.10 - 1)
    # 2M: base = wealth on/before 2024-01-15 (1.05).
    assert perf._trailing_return(wealth_path, "2024-01-01", 2) == pytest.approx(1.20 / 1.05 - 1)
    # 3M: start 2023-12-15 predates inception → None.
    assert perf._trailing_return(wealth_path, "2024-01-01", 3) is None
    assert perf._trailing_return([], "2024-01-01", 1) is None


def test_extended_metrics_monthly_and_trailing():
    ext = extended_metrics(_SPAN_POINTS, risk_metrics(_SPAN_POINTS).twr)
    assert ext.best_month == pytest.approx(0.10) and ext.best_month_label == "2024-01"
    assert ext.worst_month == pytest.approx(-0.16) and ext.worst_month_label == "2024-02"
    # anchor 2024-02-29; 1M start 2024-01-29 → base is inception seed 1.0 (no earlier day).
    assert ext.trailing_1m == pytest.approx(0.924 - 1.0)
    assert ext.trailing_3m is None and ext.trailing_6m is None


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

def _seed(tmp_path, *, transactions=(), prices=(), fx=(), currencies=None, names=None, ters=None):
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
    names, ters = names or {}, ters or {}
    etfs = {}
    for isin in set(names) | set(ters):
        entry = {}
        if isin in names:
            entry["name"] = names[isin]
        if isin in ters:
            entry["ter"] = ters[isin]
        etfs[isin] = entry
    config.write_text(yaml.dump({"etfs": etfs}))
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


def test_sort_rows_by_twr_and_pnl_pct():
    low = _perf_row("A", 110.0)   # pnl_pct = 10
    high = _perf_row("B", 150.0)  # pnl_pct = 50
    low.twr, high.twr = 0.05, 0.20
    by_twr = [r.isin for r in sort_rows([low, high], sort_by="twr", reverse=True)]
    assert by_twr == ["B", "A"]
    by_pct = [r.isin for r in sort_rows([low, high], sort_by="pnl_pct", reverse=True)]
    assert by_pct == ["B", "A"]


def test_sort_rows_by_ctr_uses_contrib_map():
    a, b = _perf_row("A", 100.0), _perf_row("B", 100.0)
    ordered = sort_rows([a, b], sort_by="ctr", reverse=True, ctr_by_isin={"A": 0.02, "B": 0.10})
    assert [r.isin for r in ordered] == ["B", "A"]


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


# ---------------------------------------------------------------------------
# --series mode: daily cumulative totals — ADR-0030
# ---------------------------------------------------------------------------

# A held EUR fund priced across a Christmas week: a market holiday (2024-12-25)
# and a weekend (12-28/29) have no close, so they are not trading days.
_SERIES_PRICES = [
    (EUR_ISIN, "2024-12-24", 11.0),  # Tue
    (EUR_ISIN, "2024-12-26", 11.5),  # Thu (25th is a holiday — no close)
    (EUR_ISIN, "2024-12-27", 11.8),  # Fri
    (EUR_ISIN, "2024-12-30", 12.0),  # Mon (weekend 28/29 — no close)
    (EUR_ISIN, "2024-12-31", 12.2),  # Tue
]
_SERIES_TRADING_DAYS = ["2024-12-24", "2024-12-26", "2024-12-27", "2024-12-30", "2024-12-31"]


def _seed_series(tmp_path, *, prices=None, transactions=None, **kw):
    return _seed(
        tmp_path,
        transactions=transactions or [_buy("t1", "2024-01-01", EUR_ISIN, 100.0, 10.0)],
        prices=_SERIES_PRICES if prices is None else prices,
        currencies={EUR_ISIN: "EUR"},
        names={EUR_ISIN: "Euro Fund"},
        **kw,
    )


def _series_dates(out):
    """Dates on the data rows (lines that begin with a YYYY-MM-DD)."""
    return [ln.split()[0] for ln in out.splitlines() if re.match(r"^\d{4}-\d{2}-\d{2}\s", ln)]


def _row_numbers(line, *, drop_index=None):
    """Numeric tokens after the leading label, flags/commas/percent stripped."""
    parts = line.split()[1:]
    if drop_index is not None:
        parts = parts[:drop_index] + parts[drop_index + 1 :]
    values = []
    for tok in parts:
        cleaned = re.sub(r"[,+~*]", "", tok).rstrip("%")
        try:
            values.append(float(cleaned))
        except ValueError:
            values.append(cleaned)
    return values


def test_series_main_lists_trading_days_only(tmp_path, capsys):
    db, config, meta = _seed_series(tmp_path)
    code = perf.main(_args(db, config, meta, "--as-of", "2024-12-31", "--series", "10"))
    out = capsys.readouterr().out
    assert code == 0
    assert "Portfolio performance series 2024-12-21 → 2024-12-31" in out
    assert _series_dates(out) == _SERIES_TRADING_DAYS
    # holiday + weekend never appear
    assert "2024-12-25" not in out and "2024-12-28" not in out and "2024-12-29" not in out


def test_series_main_drops_pnlctr_keeps_rate_columns(tmp_path, capsys):
    db, config, meta = _seed_series(tmp_path)
    perf.main(_args(db, config, meta, "--as-of", "2024-12-31", "--series", "10"))
    out = capsys.readouterr().out
    # Unlike --diff, the rate columns are present; P&Lctr (always 100% for the book) is gone.
    assert "XIRR" in out and "TWR" in out and "CAGR" in out and "MaxDD" in out
    assert "P&Lctr" not in out
    # rows are dates, not ISINs
    assert EUR_ISIN not in out


def test_series_main_reverse_shows_newest_first(tmp_path, capsys):
    db, config, meta = _seed_series(tmp_path)
    perf.main(_args(db, config, meta, "--as-of", "2024-12-31", "--series", "10", "--reverse"))
    dates = _series_dates(capsys.readouterr().out)
    assert dates[0] == "2024-12-31" and dates[-1] == "2024-12-24"


def test_series_main_composes_with_as_of(tmp_path, capsys):
    db, config, meta = _seed_series(tmp_path)
    perf.main(_args(db, config, meta, "--as-of", "2024-12-27", "--series", "10"))
    dates = _series_dates(capsys.readouterr().out)
    # end is 2024-12-27 → later trading days excluded
    assert dates == ["2024-12-24", "2024-12-26", "2024-12-27"]


def test_series_main_invariance_row_equals_as_of_total(tmp_path, capsys):
    """Each series row's TOTAL must equal performance --as-of <that day>'s TOTAL."""
    db, config, meta = _seed_series(tmp_path)

    perf.main(_args(db, config, meta, "--as-of", "2024-12-31", "--series", "10"))
    series_out = capsys.readouterr().out
    series_line = next(ln for ln in series_out.splitlines() if ln.startswith("2024-12-27"))

    perf.main(_args(db, config, meta, "--as-of", "2024-12-27"))
    total_line = next(ln for ln in capsys.readouterr().out.splitlines() if ln.startswith("TOTAL"))

    # snapshot TOTAL carries an extra P&Lctr column (index 4) the series drops;
    # the series adds Daily TWR (index 7) plus WTER + Fee€/yr the snapshot lacks.
    # Compare the nine shared columns (MktVal … TWR, Vol, MaxDD).
    series_vals = _row_numbers(series_line)
    series_vals = series_vals[:7] + series_vals[8:10]
    total_vals = _row_numbers(total_line, drop_index=4)
    assert len(series_vals) == len(total_vals)
    for got, want in zip(series_vals, total_vals, strict=True):
        if isinstance(want, float):
            assert got == pytest.approx(want, abs=0.01)
        else:
            assert got == want


def test_series_main_carry_forward_flagged(tmp_path, capsys):
    second = "IE00EUR000002"
    db, config, meta = _seed(
        tmp_path,
        transactions=[
            _buy("t1", "2024-01-01", EUR_ISIN, 100.0, 10.0),
            _buy("t2", "2024-01-01", second, 50.0, 20.0),
        ],
        prices=[
            (EUR_ISIN, "2024-12-30", 12.0),
            (EUR_ISIN, "2024-12-31", 12.2),
            (second, "2024-12-30", 21.0),  # no close on 12-31 → carried forward
        ],
        currencies={EUR_ISIN: "EUR", second: "EUR"},
        names={EUR_ISIN: "Euro Fund", second: "Second Fund"},
    )
    perf.main(_args(db, config, meta, "--as-of", "2024-12-31", "--series", "5"))
    out = capsys.readouterr().out
    assert "~" in out
    assert "carried forward" in out


def test_series_main_short_history_flagged(tmp_path, capsys):
    db, config, meta = _seed_series(tmp_path)  # prices start long after the 2024-01-01 buy
    perf.main(_args(db, config, meta, "--as-of", "2024-12-31", "--series", "10"))
    out = capsys.readouterr().out
    assert "*" in out
    assert "short history" in out


def test_series_and_diff_mutually_exclusive(tmp_path, capsys):
    db, config, meta = _seed_series(tmp_path)
    args = _args(db, config, meta, "--as-of", "2024-12-31", "--series", "5", "--diff", "5")
    code = perf.main(args)
    out = capsys.readouterr().out
    assert code == 1
    assert "mutually exclusive" in out


def test_series_invalid_n_rejected(tmp_path, capsys):
    db, config, meta = _seed_series(tmp_path)
    for bad in ("0", "-3", "abc"):
        code = perf.main(_args(db, config, meta, "--series", bad))
        assert code != 0, f"expected non-zero exit for --series {bad!r}"


def test_series_no_transactions_message(tmp_path, capsys):
    db, config, meta = _seed(tmp_path)
    code = perf.main(_args(db, config, meta, "--series", "30"))
    out = capsys.readouterr().out
    assert code == 0
    assert "No ETF holdings in database" in out


def test_series_empty_window_message(tmp_path, capsys):
    db, config, meta = _seed_series(tmp_path)
    # window ends before any close/holding exists
    code = perf.main(_args(db, config, meta, "--as-of", "2024-06-01", "--series", "10"))
    out = capsys.readouterr().out
    assert code == 0
    assert "No priced trading days in window" in out


def test_series_day_before_first_holding_is_skipped(tmp_path, capsys):
    # A close exists on 2024-11-15, before the 2024-11-20 buy → that day has nothing
    # valuable and gets no row.
    db, config, meta = _seed(
        tmp_path,
        transactions=[_buy("t1", "2024-11-20", EUR_ISIN, 100.0, 10.0)],
        prices=[(EUR_ISIN, "2024-11-15", 9.0), (EUR_ISIN, "2024-11-21", 10.5)],
        currencies={EUR_ISIN: "EUR"},
        names={EUR_ISIN: "Euro Fund"},
    )
    perf.main(_args(db, config, meta, "--as-of", "2024-11-21", "--series", "10"))
    dates = _series_dates(capsys.readouterr().out)
    assert dates == ["2024-11-21"]


# --series unit seams (no full command) --------------------------------------


def test_trading_days_excludes_days_without_close(tmp_path):
    db, _config, _meta = _seed_series(tmp_path)
    timeline = position_timeline(load_trades(db))
    days = perf._trading_days(db, timeline, "2024-12-21", "2024-12-31")
    assert days == _SERIES_TRADING_DAYS


def test_series_rows_totals_match_snapshot_total(tmp_path):
    db, config, meta = _seed_series(tmp_path)
    timeline = position_timeline(load_trades(db))
    rows = perf._series_rows(db, config, meta, timeline, start="2024-12-21", end="2024-12-31")
    assert [p.day for p in rows] == _SERIES_TRADING_DAYS
    for point in rows:
        assert point.total.isin == "TOTAL"
        assert point.total.market_value == pytest.approx(
            perf._snapshot_total(db, config, meta, timeline, point.day).market_value
        )


# --series Daily TWR — ADR-0040


def test_series_daily_twr_is_hand_computed_close_to_close(tmp_path, capsys):
    """2024-12-27 Daily TWR is 11.8/11.5 − 1 (no contribution that day)."""
    db, config, meta = _seed_series(tmp_path)
    timeline = position_timeline(load_trades(db))
    rows = perf._series_rows(db, config, meta, timeline, start="2024-12-21", end="2024-12-31")
    by_day = {point.day: point for point in rows}
    assert by_day["2024-12-24"].daily_twr is None  # first close; no prior EUR value
    assert by_day["2024-12-26"].daily_twr == pytest.approx(11.5 / 11.0 - 1)
    assert by_day["2024-12-27"].daily_twr == pytest.approx(11.8 / 11.5 - 1)

    perf.main(_args(db, config, meta, "--as-of", "2024-12-31", "--series", "10"))
    out = capsys.readouterr().out
    assert "Daily TWR" in out
    assert "increment that compounds into TWR" in out
    line = next(ln for ln in out.splitlines() if ln.startswith("2024-12-27"))
    assert _row_numbers(line)[7] == pytest.approx(2.61, abs=0.01)


def test_series_daily_twr_reconciles_with_portfolio_return_series(tmp_path):
    """Each printed Daily TWR equals the shared book's return dated that day."""
    db, config, meta = _seed_series(tmp_path)
    timeline = position_timeline(load_trades(db))
    by_day = dict(portfolio_return_series(db, meta, "2024-12-31"))
    for point in perf._series_rows(
        db, config, meta, timeline, start="2024-12-21", end="2024-12-31"
    ):
        if point.daily_twr is None:
            assert point.day not in by_day
        else:
            assert point.daily_twr == pytest.approx(by_day[point.day])


# --isin filter: restrict the book to one holding — ADR-0038

_ISIN_SECOND = "IE00EUR000002"


def _seed_two_fund_series(tmp_path):
    """EUR on the Christmas-week closes; second fund has a unique 12-23 close."""
    return _seed(
        tmp_path,
        transactions=[
            _buy("t1", "2024-01-01", EUR_ISIN, 100.0, 10.0),
            _buy("t2", "2024-01-01", _ISIN_SECOND, 50.0, 20.0),
        ],
        prices=[
            *_SERIES_PRICES,
            (_ISIN_SECOND, "2024-12-23", 20.0),
            (_ISIN_SECOND, "2024-12-31", 21.0),
        ],
        currencies={EUR_ISIN: "EUR", _ISIN_SECOND: "EUR"},
        names={EUR_ISIN: "Euro Fund", _ISIN_SECOND: "Second Fund"},
    )


def test_normalize_isin_strips_and_uppers():
    assert perf._normalize_isin(None) is None
    assert perf._normalize_isin(" ie00eur000001 ") == "IE00EUR000001"
    with pytest.raises(ValueError, match="non-empty"):
        perf._normalize_isin("  ")


def test_restrict_timeline_unknown_lists_holdings(tmp_path):
    db, config, _meta = _seed_two_fund_series(tmp_path)
    timeline = position_timeline(load_trades(db))
    with pytest.raises(ValueError, match="not a holding") as exc:
        perf._restrict_timeline(timeline, "IE00NOTHELD01", config)
    assert EUR_ISIN in str(exc.value) and _ISIN_SECOND in str(exc.value)


def test_series_isin_uses_only_that_funds_trading_days(tmp_path, capsys):
    db, config, meta = _seed_two_fund_series(tmp_path)
    perf.main(
        _args(db, config, meta, "--as-of", "2024-12-31", "--series", "10", "--isin", EUR_ISIN)
    )
    assert _series_dates(capsys.readouterr().out) == _SERIES_TRADING_DAYS

    perf.main(
        _args(db, config, meta, "--as-of", "2024-12-31", "--series", "10", "--isin", _ISIN_SECOND)
    )
    assert _series_dates(capsys.readouterr().out) == ["2024-12-23", "2024-12-31"]


def test_series_isin_invariance_row_equals_as_of_isin_total(tmp_path, capsys):
    """Each --series --isin X row equals --as-of D --isin X's TOTAL (two-fund book)."""
    db, config, meta = _seed_two_fund_series(tmp_path)

    perf.main(
        _args(db, config, meta, "--as-of", "2024-12-31", "--series", "10", "--isin", EUR_ISIN)
    )
    series_line = next(
        ln for ln in capsys.readouterr().out.splitlines() if ln.startswith("2024-12-27")
    )

    perf.main(_args(db, config, meta, "--as-of", "2024-12-27", "--isin", EUR_ISIN))
    total_line = next(ln for ln in capsys.readouterr().out.splitlines() if ln.startswith("TOTAL"))

    series_vals = _row_numbers(series_line)
    series_vals = series_vals[:7] + series_vals[8:10]
    total_vals = _row_numbers(total_line, drop_index=4)
    assert len(series_vals) == len(total_vals)
    for got, want in zip(series_vals, total_vals, strict=True):
        if isinstance(want, float):
            assert got == pytest.approx(want, abs=0.01)
        else:
            assert got == want


def test_series_isin_banner_names_the_holding(tmp_path, capsys):
    db, config, meta = _seed_two_fund_series(tmp_path)
    perf.main(
        _args(db, config, meta, "--as-of", "2024-12-31", "--series", "10", "--isin", EUR_ISIN)
    )
    out = capsys.readouterr().out
    assert f"Euro Fund ({EUR_ISIN}) performance series" in out
    assert "Portfolio performance series" not in out
    assert _ISIN_SECOND not in out


def test_series_isin_accepts_lowercase(tmp_path, capsys):
    db, config, meta = _seed_series(tmp_path)
    code = perf.main(
        _args(
            db, config, meta, "--as-of", "2024-12-31", "--series", "10", "--isin", EUR_ISIN.lower()
        )
    )
    out = capsys.readouterr().out
    assert code == 0
    assert _series_dates(out) == _SERIES_TRADING_DAYS
    assert EUR_ISIN in out


def test_series_isin_unknown_exits_1_and_lists_holdings(tmp_path, capsys):
    db, config, meta = _seed_two_fund_series(tmp_path)
    code = perf.main(
        _args(db, config, meta, "--series", "10", "--isin", "IE00NOTHELD01")
    )
    out = capsys.readouterr().out
    assert code == 1
    assert "not a holding" in out
    assert EUR_ISIN in out and _ISIN_SECOND in out


def test_series_isin_empty_book_still_says_no_holdings(tmp_path, capsys):
    db, config, meta = _seed(tmp_path)
    code = perf.main(_args(db, config, meta, "--series", "10", "--isin", EUR_ISIN))
    out = capsys.readouterr().out
    assert code == 0
    assert "No ETF holdings in database" in out
    assert "not a holding" not in out


def test_snapshot_isin_hides_the_other_holding(tmp_path, capsys):
    db, config, meta = _seed_two_fund_series(tmp_path)
    code = perf.main(_args(db, config, meta, "--as-of", "2024-12-31", "--isin", EUR_ISIN))
    out = capsys.readouterr().out
    assert code == 0
    assert EUR_ISIN in out and "Euro Fund" in out
    assert _ISIN_SECOND not in out


# --series weighted TER + estimated annual cost columns — ADR-0031

_TER_SECOND = "IE00EUR000002"


def _seed_ter(tmp_path, *, ters, second_close):
    """Two EUR funds, both bought at cost 1000; EUR_ISIN appreciates to MktVal 3000."""
    return _seed(
        tmp_path,
        transactions=[
            _buy("t1", "2024-01-01", EUR_ISIN, 100.0, 10.0),
            _buy("t2", "2024-01-01", _TER_SECOND, 100.0, 10.0),
        ],
        prices=[
            (EUR_ISIN, "2024-12-31", 30.0),          # MktVal 3000 (cost 1000)
            (_TER_SECOND, "2024-12-31", second_close),
        ],
        currencies={EUR_ISIN: "EUR", _TER_SECOND: "EUR"},
        names={EUR_ISIN: "Euro Fund", _TER_SECOND: "Second Fund"},
        ters=ters,
    )


def _last_series_values(out):
    line = [ln for ln in out.splitlines() if re.match(r"^\d{4}-\d{2}-\d{2}\s", ln)][-1]
    return _row_numbers(line)


def test_series_ter_columns_are_market_value_weighted(tmp_path, capsys):
    # cost-weighted TER would be (0.5+0.1)/2 = 0.300%; value-weighted is
    # (0.5*3000 + 0.1*1000)/4000 = 0.400%. The table must show the value-weighted one.
    db, config, meta = _seed_ter(
        tmp_path, ters={EUR_ISIN: 0.5, _TER_SECOND: 0.1}, second_close=10.0
    )
    perf.main(_args(db, config, meta, "--as-of", "2024-12-31", "--series", "1"))
    out = capsys.readouterr().out
    vals = _last_series_values(out)
    assert vals[10] == pytest.approx(0.400)   # WTER %
    assert vals[11] == pytest.approx(16.0)   # Fee€/yr = 0.5%*3000 + 0.1%*1000
    assert "0.300%" not in out               # not cost-weighted
    assert "market-value-weighted TER" in out


def test_series_ter_missing_metadata_dilutes(tmp_path, capsys):
    # Second fund has no TER: it contributes 0 fee but still sits in the denominator.
    db, config, meta = _seed_ter(tmp_path, ters={EUR_ISIN: 0.5}, second_close=10.0)
    perf.main(_args(db, config, meta, "--as-of", "2024-12-31", "--series", "1"))
    vals = _last_series_values(capsys.readouterr().out)
    assert vals[11] == pytest.approx(15.0)   # 0.5%*3000 only
    assert vals[10] == pytest.approx(0.375)   # 100*15/4000, diluted below 0.5%


def test_series_ter_na_when_no_metadata(tmp_path, capsys):
    db, config, meta = _seed_series(tmp_path)  # names only, no TER
    perf.main(_args(db, config, meta, "--as-of", "2024-12-31", "--series", "10"))
    out = capsys.readouterr().out
    assert "WTER" in out  # header still present
    vals = _last_series_values(out)
    assert vals[10] == "n/a" and vals[11] == "n/a"
    assert "market-value-weighted TER" not in out  # footnote suppressed


def test_weighted_ter_cost_value_weighted_and_dilution():
    rows = [_perf_row("A", 3000.0), _perf_row("B", 1000.0), _perf_row("C", None)]
    wter, fee = perf._weighted_ter_cost(rows, {"A": 0.5, "B": 0.1, "C": 0.9})
    assert wter == pytest.approx(0.4) and fee == pytest.approx(16.0)  # C unvaluable, ignored
    assert portfolio_mod.weighted_ter_cost(
        [(0.5, 3000.0), (0.1, 1000.0), (0.9, None)]
    ) == pytest.approx((wter, fee))

    wter2, fee2 = perf._weighted_ter_cost(rows, {"A": 0.5, "B": None, "C": None})
    assert fee2 == pytest.approx(15.0) and wter2 == pytest.approx(0.375)  # B dilutes

    assert perf._weighted_ter_cost(rows, {"A": None, "B": None, "C": None}) == (None, None)


def test_portfolio_and_performance_reconcile_fees_multi_currency_multi_broker(
    tmp_path,
    capsys,
):
    db, config, meta = _seed(
        tmp_path,
        transactions=[
            _buy("tr-eur", "2024-01-01", EUR_ISIN, 60.0, 10.0, broker="tr"),
            _buy("xtb-eur", "2024-01-01", EUR_ISIN, 40.0, 10.0, broker="xtb"),
            _buy("xtb-usd", "2024-01-01", USD_ISIN, 10.0, 90.0, broker="xtb"),
        ],
        prices=[
            (EUR_ISIN, "2024-12-31", 30.0),
            (USD_ISIN, "2024-12-31", 120.0),
        ],
        fx=[("EUR", "USD", "2024-12-31", 1.2)],
        currencies={EUR_ISIN: "EUR", USD_ISIN: "USD"},
        names={EUR_ISIN: "Euro Fund", USD_ISIN: "Dollar Fund"},
        ters={EUR_ISIN: 0.5, USD_ISIN: 0.1},
    )
    as_of = "2024-12-31"

    assert portfolio_mod.main(
        _args(db, config, meta, "--show-cost-basis", "--show-broker")
    ) == 0
    portfolio_output = capsys.readouterr().out
    market_value = float(
        re.search(r"€([\d,.]+) market value", portfolio_output).group(1).replace(",", "")
    )
    annual_fee = float(
        re.search(r"~€([\d,.]+)/yr in fees", portfolio_output).group(1).replace(",", "")
    )
    weighted_ter = float(
        re.search(r"([\d.]+)% weighted avg TER", portfolio_output).group(1)
    )

    timeline = position_timeline(load_trades(db))
    points = perf._series_rows(
        db,
        config,
        meta,
        timeline,
        start=as_of,
        end=as_of,
    )
    assert len(points) == 1
    point = points[0]
    assert point.total.market_value == pytest.approx(market_value)
    assert point.weighted_ter == pytest.approx(weighted_ter)
    assert point.annual_cost == pytest.approx(annual_fee)
    assert (market_value, weighted_ter, annual_fee) == pytest.approx((4000.0, 0.4, 16.0))


# ---------------------------------------------------------------------------
# --metrics command (ADR-0033, Phase A)
# ---------------------------------------------------------------------------

def _ext_metrics(**overrides):
    base = dict(
        max_drawdown=-0.1, max_dd_duration_days=1, max_dd_peak_date="2024-01-01",
        max_dd_recovery_date="2024-01-02", max_dd_ongoing=False, underwater_days=1,
        days_since_high=0, recovery_factor=1.0, best_day=0.1, best_day_date="2024-01-02",
        worst_day=-0.1, worst_day_date="2024-01-01", gain_loss_ratio=1.0,
        best_month=0.1, best_month_label="2024-01", worst_month=-0.1,
        worst_month_label="2024-01", trailing_1m=0.05, trailing_3m=None, trailing_6m=None,
    )
    base.update(overrides)
    return perf.ExtendedMetrics(**base)


def test_maxdd_duration_note_variants():
    assert "no drawdown" in perf._maxdd_duration_note(_ext_metrics(max_dd_peak_date=None))
    assert "still underwater" in perf._maxdd_duration_note(_ext_metrics(max_dd_ongoing=True))
    assert "recovery 2024-01-02" in perf._maxdd_duration_note(_ext_metrics())


def test_main_metrics_report_renders(tmp_path, capsys):
    # One EUR holding, peak 06-01 → -25% trough 09-01 → recovery 12-31.
    db, config, meta = _seed(
        tmp_path,
        transactions=[_buy("t1", "2024-01-01", EUR_ISIN, 100.0, 10.0)],
        prices=[
            (EUR_ISIN, "2024-01-01", 10.0),
            (EUR_ISIN, "2024-06-01", 12.0),
            (EUR_ISIN, "2024-09-01", 9.0),
            (EUR_ISIN, "2024-12-31", 13.0),
        ],
        currencies={EUR_ISIN: "EUR"},
        names={EUR_ISIN: "Euro Fund"},
    )
    code = perf.main(_args(db, config, meta, "--as-of", "2024-12-31", "--metrics"))
    out = capsys.readouterr().out
    assert code == 0
    assert "Portfolio metrics as of 2024-12-31" in out
    assert "Max Drawdown" in out and "-25.0%" in out
    assert "MaxDD Duration" in out and "213d" in out
    assert "peak 2024-06-01 → recovery 2024-12-31" in out
    assert "Days Since High" in out and "at high" in out  # recovered to a new high 12-31
    assert "Recovery Factor" in out and "1.20" in out
    assert "Best Day" in out and "+44.44%" in out
    assert "Worst Day" in out and "-25.00%" in out
    assert "Max Gain / Max Loss" in out and "1.78" in out
    assert "Best Month" in out and "2024-12" in out    # 13/9 − 1 in December
    assert "Worst Month" in out and "2024-09" in out    # −25% in September
    assert "Trailing returns" in out and "6 Months" in out and "+8.33%" in out  # 1.3/1.2 − 1


def test_main_metrics_short_history_estimated_and_excluded_notes(tmp_path, capsys):
    # EUR priced (short window, stale as of 08-25) + USD held but never priced.
    db, config, meta = _seed(
        tmp_path,
        transactions=[
            _buy("t1", "2026-08-01", EUR_ISIN, 100.0, 10.0),
            _buy("t2", "2026-08-01", USD_ISIN, 10.0, 90.0),
        ],
        prices=[(EUR_ISIN, "2026-08-01", 10.0), (EUR_ISIN, "2026-08-20", 11.0)],
        currencies={EUR_ISIN: "EUR", USD_ISIN: "USD"},
        names={EUR_ISIN: "Euro Fund", USD_ISIN: "Dollar Fund"},
    )
    code = perf.main(_args(db, config, meta, "--as-of", "2026-08-25", "--metrics"))
    out = capsys.readouterr().out
    assert code == 0
    assert "* < 1y or short history" in out
    assert "~ MktVal carried forward" in out
    assert "⚠ excluded" in out and USD_ISIN in out


def test_main_metrics_no_holdings(tmp_path, capsys):
    db, config, meta = _seed(tmp_path)
    code = perf.main(_args(db, config, meta, "--metrics"))
    out = capsys.readouterr().out
    assert code == 0
    assert "No ETF holdings in database" in out


def test_main_metrics_no_priceable_holdings(tmp_path, capsys):
    db, config, meta = _seed(
        tmp_path,
        transactions=[_buy("t1", "2024-01-01", EUR_ISIN, 100.0, 10.0)],
        currencies={EUR_ISIN: "EUR"},
        names={EUR_ISIN: "Euro Fund"},
    )  # no prices at all → nothing valuable
    code = perf.main(_args(db, config, meta, "--as-of", "2024-12-31", "--metrics"))
    out = capsys.readouterr().out
    assert code == 0
    assert "No priceable holdings as of 2024-12-31" in out


def test_main_metrics_diff_does_not_compose(tmp_path, capsys):
    db, config, meta = _seed(tmp_path)
    code = perf.main(_args(db, config, meta, "--metrics", "--diff", "7"))
    out = capsys.readouterr().out
    assert code == 1
    assert "does not compose with --diff" in out


def test_main_metrics_series_composes(tmp_path, capsys):
    db, config, meta = _seed(
        tmp_path,
        transactions=[_buy("t1", "2024-01-01", EUR_ISIN, 100.0, 10.0)],
        prices=[
            (EUR_ISIN, "2024-01-01", 10.0),
            (EUR_ISIN, "2024-01-02", 12.0),
            (EUR_ISIN, "2024-01-03", 9.0),
            (EUR_ISIN, "2024-01-04", 13.0),
        ],
        currencies={EUR_ISIN: "EUR"},
        names={EUR_ISIN: "Euro Fund"},
    )
    code = perf.main(
        _args(db, config, meta, "--as-of", "2024-01-04", "--metrics", "--series", "10")
    )
    out = capsys.readouterr().out
    assert code == 0
    assert "Portfolio metrics series" in out
    assert "MaxDD" in out and "DDdur" in out and "SinceHi" in out and "RecFac" in out
    for day in ("2024-01-01", "2024-01-02", "2024-01-03", "2024-01-04"):
        assert day in out
    assert "-25.0%" in out  # last day's cumulative MaxDD (the 12 → 9 trough)


def test_main_metrics_series_reverse_newest_first(tmp_path, capsys):
    db, config, meta = _seed(
        tmp_path,
        transactions=[_buy("t1", "2024-01-01", EUR_ISIN, 100.0, 10.0)],
        prices=[
            (EUR_ISIN, "2024-01-01", 10.0),
            (EUR_ISIN, "2024-01-02", 11.0),
            (EUR_ISIN, "2024-01-03", 12.0),
        ],
        currencies={EUR_ISIN: "EUR"},
        names={EUR_ISIN: "Euro Fund"},
    )
    perf.main(
        _args(db, config, meta, "--as-of", "2024-01-03", "--metrics", "--series", "10", "-r")
    )
    out = capsys.readouterr().out
    dated = [ln for ln in out.splitlines() if ln.startswith("2024-01-")]
    assert dated[0].startswith("2024-01-03") and dated[-1].startswith("2024-01-01")


def test_main_metrics_series_estimated_note(tmp_path, capsys):
    # USD leg priced only on 01-01 → carried forward on 01-02/03 → estimated days.
    db, config, meta = _seed(
        tmp_path,
        transactions=[
            _buy("t1", "2024-01-01", EUR_ISIN, 100.0, 10.0),
            _buy("t2", "2024-01-01", USD_ISIN, 10.0, 90.0),
        ],
        prices=[
            (EUR_ISIN, "2024-01-01", 10.0),
            (EUR_ISIN, "2024-01-02", 11.0),
            (EUR_ISIN, "2024-01-03", 12.0),
            (USD_ISIN, "2024-01-01", 100.0),
        ],
        fx=[("EUR", "USD", "2024-01-01", 1.0)],
        currencies={EUR_ISIN: "EUR", USD_ISIN: "USD"},
        names={EUR_ISIN: "Euro Fund", USD_ISIN: "Dollar Fund"},
    )
    perf.main(
        _args(db, config, meta, "--as-of", "2024-01-03", "--metrics", "--series", "10")
    )
    out = capsys.readouterr().out
    assert "~ some days' MktVal is carried forward" in out


def test_main_metrics_series_no_priced_days(tmp_path, capsys):
    db, config, meta = _seed(
        tmp_path,
        transactions=[_buy("t1", "2024-01-01", EUR_ISIN, 100.0, 10.0)],
        prices=[(EUR_ISIN, "2020-01-01", 10.0)],  # only price predates the window
        currencies={EUR_ISIN: "EUR"},
        names={EUR_ISIN: "Euro Fund"},
    )
    code = perf.main(
        _args(db, config, meta, "--as-of", "2024-01-10", "--metrics", "--series", "3")
    )
    out = capsys.readouterr().out
    assert code == 0
    assert "No priced trading days" in out


# ---------------------------------------------------------------------------
# Return contribution (ADR-0033): contribution_to_return + --contrib
# ---------------------------------------------------------------------------

_A, _B = "IE00A0000001", "IE00B0000001"

# Fund A: +20% on day 2 then flat. Fund B: flat then +10% on day 3. Equal €100 buys.
_CONTRIB_SEED = dict(
    transactions=[_buy("a", "2024-01-01", _A, 10.0, 10.0), _buy("b", "2024-01-01", _B, 10.0, 10.0)],
    prices=[
        (_A, "2024-01-01", 10.0), (_A, "2024-01-02", 12.0), (_A, "2024-01-03", 12.0),
        (_B, "2024-01-01", 10.0), (_B, "2024-01-02", 10.0), (_B, "2024-01-03", 11.0),
    ],
    currencies={_A: "EUR", _B: "EUR"},
    names={_A: "Fund A", _B: "Fund B"},
)


def test_contribution_to_return_reconciles_with_twr(tmp_path):
    db, _config, meta = _seed(tmp_path, **_CONTRIB_SEED)
    timeline = position_timeline(load_trades(db))
    holdings = [build_series(db, isin, timeline[isin], "2024-01-03", meta) for isin in (_A, _B)]
    contributions = contribution_to_return(holdings, "2024-01-01", "2024-01-03", db)

    _wp, returns = wealth_and_returns(
        aggregate_value_series(holdings, "2024-01-01", "2024-01-03", db)
    )
    twr = 1.0
    for _day, r in returns:
        twr *= 1.0 + r
    twr -= 1.0

    assert contributions is not None
    assert sum(contributions.values()) == pytest.approx(twr)  # Cariño: sums to the book TWR
    # A's +20% move landed on a day it weighed 50% early; B's +10% later. Both positive.
    assert contributions[_A] == pytest.approx(0.102291, abs=1e-5)
    assert contributions[_B] == pytest.approx(0.047708, abs=1e-5)


def test_contribution_to_return_none_when_no_priced_subperiod(tmp_path):
    db, _config, meta = _seed(
        tmp_path,
        transactions=[_buy("t1", "2024-01-01", EUR_ISIN, 10.0, 10.0)],
        currencies={EUR_ISIN: "EUR"},
        names={EUR_ISIN: "Euro Fund"},
    )  # bought but never priced → no valuable day → no defined return
    timeline = position_timeline(load_trades(db))
    holdings = [build_series(db, EUR_ISIN, timeline[EUR_ISIN], "2024-01-03", meta)]
    assert contribution_to_return(holdings, "2024-01-01", "2024-01-03", db) is None


def test_contribution_to_return_none_on_total_loss(tmp_path):
    db, _config, meta = _seed(
        tmp_path,
        transactions=[_buy("t1", "2024-01-01", EUR_ISIN, 10.0, 10.0)],
        prices=[(EUR_ISIN, "2024-01-01", 10.0), (EUR_ISIN, "2024-01-02", 0.0)],
        currencies={EUR_ISIN: "EUR"},
        names={EUR_ISIN: "Euro Fund"},
    )  # priced to zero → R = −100% → log-linking singular
    timeline = position_timeline(load_trades(db))
    holdings = [build_series(db, EUR_ISIN, timeline[EUR_ISIN], "2024-01-02", meta)]
    assert contribution_to_return(holdings, "2024-01-01", "2024-01-02", db) is None


def test_main_contrib_renders_and_reconciles(tmp_path, capsys):
    # Two priced funds + a USD leg never priced (excluded from totals).
    seed = dict(_CONTRIB_SEED)
    seed["transactions"] = [*seed["transactions"], _buy("u", "2024-01-01", USD_ISIN, 10.0, 9.0)]
    seed["currencies"] = {**seed["currencies"], USD_ISIN: "USD"}
    seed["names"] = {**seed["names"], USD_ISIN: "Dollar Fund"}
    db, config, meta = _seed(tmp_path, **seed)

    code = perf.main(_args(db, config, meta, "--as-of", "2024-01-03", "--contrib"))
    out = capsys.readouterr().out
    assert code == 0
    assert "Per-holding return contribution as of 2024-01-03" in out
    assert "Ctr%" in out and "TOTAL" in out
    assert "Cariño-linked" in out
    # TOTAL TWR and the summed Ctr% both read +15.00% — the reconciliation is visible.
    assert out.count("+15.00%") == 2
    assert "⚠ excluded" in out and USD_ISIN in out


def test_main_contrib_total_loss_shows_unavailable(tmp_path, capsys):
    db, config, meta = _seed(
        tmp_path,
        transactions=[_buy("t1", "2024-01-01", EUR_ISIN, 10.0, 10.0)],
        prices=[(EUR_ISIN, "2024-01-01", 10.0), (EUR_ISIN, "2024-01-02", 0.0)],
        currencies={EUR_ISIN: "EUR"},
        names={EUR_ISIN: "Euro Fund"},
    )
    code = perf.main(_args(db, config, meta, "--as-of", "2024-01-02", "--contrib"))
    out = capsys.readouterr().out
    assert code == 0
    assert "Contribution unavailable" in out  # None contributions → Ctr n/a


def test_main_contrib_no_holdings(tmp_path, capsys):
    db, config, meta = _seed(tmp_path)
    assert perf.main(_args(db, config, meta, "--contrib")) == 0
    assert "No ETF holdings in database" in capsys.readouterr().out


def test_main_contrib_no_priceable(tmp_path, capsys):
    db, config, meta = _seed(
        tmp_path,
        transactions=[_buy("t1", "2024-01-01", EUR_ISIN, 10.0, 10.0)],
        currencies={EUR_ISIN: "EUR"},
        names={EUR_ISIN: "Euro Fund"},
    )
    code = perf.main(_args(db, config, meta, "--as-of", "2024-01-02", "--contrib"))
    out = capsys.readouterr().out
    assert code == 0
    assert "No priceable holdings as of 2024-01-02" in out


def test_main_contrib_does_not_compose_with_metrics(tmp_path, capsys):
    db, config, meta = _seed(tmp_path)
    code = perf.main(_args(db, config, meta, "--contrib", "--metrics"))
    out = capsys.readouterr().out
    assert code == 1 and "does not compose" in out


def test_main_contrib_sort_ctr_ascending(tmp_path, capsys):
    db, config, meta = _seed(tmp_path, **_CONTRIB_SEED)
    code = perf.main(
        _args(db, config, meta, "--as-of", "2024-01-03", "--contrib", "--sort", "ctr")
    )
    out = capsys.readouterr().out
    assert code == 0
    # B's Ctr% is smaller than A's, so ascending puts B first.
    assert out.index(_B) < out.index(_A)
