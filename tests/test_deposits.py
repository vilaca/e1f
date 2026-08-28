"""Deposits: organic-vs-reported, ROIC, and per-deposit impact (ADR-0033 Phase C)."""

import sqlite3
from contextlib import closing

import pytest
import yaml

from e1f import deposits as dep, performance as perf
from e1f.common import load_trades, position_timeline
from e1f.deposits import DepositImpact

EUR_ISIN = "IE00EUR000001"
USD_ISIN = "IE00USD000001"


def _seed(tmp_path, *, transactions, prices=(), fx=(), currencies=None, names=None):
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
    config.write_text(yaml.dump({"etfs": {i: {"name": n} for i, n in (names or {}).items()}}))
    meta = tmp_path / "meta.yaml"
    meta.write_text(
        yaml.dump({i: {"currency": c, "symbol": f"{i}:X:{c}", "xid": "1"}
                   for i, c in (currencies or {}).items()})
    )
    return str(db), str(config), str(meta)


def _buy(txid, day, isin, shares, price, fee=0.0, broker="tr"):
    return (broker, txid, day, isin, "BUY", shares, price, fee, 0.0)


def _args(db, config, meta, *extra):
    return ["--db", db, "--config", config, "--currency-meta", meta, *extra]


# ---------------------------------------------------------------------------
# Attribution math
# ---------------------------------------------------------------------------

def test_deposit_impacts_eur_two_buys_and_summary(tmp_path):
    db, config, meta = _seed(
        tmp_path,
        transactions=[
            _buy("t1", "2024-01-01", EUR_ISIN, 10.0, 10.0),  # amount 100 → 140 @14
            _buy("t2", "2024-02-01", EUR_ISIN, 5.0, 12.0),   # amount  60 →  70 @14
        ],
        prices=[(EUR_ISIN, "2024-12-31", 14.0)],
        currencies={EUR_ISIN: "EUR"},
        names={EUR_ISIN: "Euro Fund"},
    )
    d1, d2 = dep.deposit_impacts(db, config, meta, "2024-12-31")
    assert (d1.amount, d1.value, d1.gain) == pytest.approx((100.0, 140.0, 40.0))
    assert d1.ret_pct == pytest.approx(40.0)
    assert (d2.amount, d2.value, d2.gain) == pytest.approx((60.0, 70.0, 10.0))
    # P&L shares: 40/50 and 10/50.
    assert d1.pnl_share == pytest.approx(80.0) and d2.pnl_share == pytest.approx(20.0)

    s = dep.summarize([d1, d2])
    assert s.invested == pytest.approx(160.0)
    assert s.reported == pytest.approx(210.0)
    assert s.organic_gain == pytest.approx(50.0)
    assert s.roic == pytest.approx(31.25)


def test_deposit_amount_includes_fee(tmp_path):
    db, config, meta = _seed(
        tmp_path,
        transactions=[_buy("t1", "2024-01-01", EUR_ISIN, 10.0, 10.0, fee=1.0)],
        prices=[(EUR_ISIN, "2024-12-31", 11.0)],
        currencies={EUR_ISIN: "EUR"},
        names={EUR_ISIN: "Euro Fund"},
    )
    d = dep.deposit_impacts(db, config, meta, "2024-12-31")[0]
    assert d.amount == pytest.approx(101.0)  # 10×10 + 1 fee
    assert d.value == pytest.approx(110.0) and d.gain == pytest.approx(9.0)


def test_deposit_usd_valued_via_fx(tmp_path):
    db, config, meta = _seed(
        tmp_path,
        transactions=[_buy("t1", "2024-01-01", USD_ISIN, 10.0, 9.0)],  # EUR 90 paid
        prices=[(USD_ISIN, "2024-12-31", 120.0)],
        fx=[("EUR", "USD", "2024-12-31", 1.2)],
        currencies={USD_ISIN: "USD"},
        names={USD_ISIN: "Dollar Fund"},
    )
    d = dep.deposit_impacts(db, config, meta, "2024-12-31")[0]
    assert d.amount == pytest.approx(90.0)
    assert d.value == pytest.approx(1000.0)  # 10 × 120 / 1.2


def test_reported_value_reconciles_with_performance_total_multi_currency(tmp_path):
    db, config, meta = _seed(
        tmp_path,
        transactions=[
            _buy("t1", "2024-01-01", EUR_ISIN, 10.0, 10.0),
            _buy("t2", "2024-01-01", USD_ISIN, 5.0, 12.0),
        ],
        prices=[
            (EUR_ISIN, "2024-12-31", 14.0),
            (USD_ISIN, "2024-12-31", 120.0),
        ],
        fx=[("EUR", "USD", "2024-12-31", 1.2)],
        currencies={EUR_ISIN: "EUR", USD_ISIN: "USD"},
        names={EUR_ISIN: "Euro Fund", USD_ISIN: "Dollar Fund"},
    )
    as_of = "2024-12-31"
    impacts = dep.deposit_impacts(db, config, meta, as_of)
    summary = dep.summarize(impacts)
    timeline = position_timeline(load_trades(db))
    total = perf._snapshot_total(db, config, meta, timeline, as_of)

    assert summary is not None
    assert total is not None
    assert summary.reported == pytest.approx(total.market_value)
    assert summary.invested == pytest.approx(total.cost)
    assert summary.organic_gain == pytest.approx(total.pnl)
    assert sum(impact.value for impact in impacts if impact.value is not None) == pytest.approx(
        summary.reported
    )


def test_deposit_unvaluable_excluded_from_summary(tmp_path):
    db, config, meta = _seed(
        tmp_path,
        transactions=[
            _buy("t1", "2024-01-01", EUR_ISIN, 10.0, 10.0),
            _buy("t2", "2024-01-01", USD_ISIN, 10.0, 9.0),  # no price → unvaluable
        ],
        prices=[(EUR_ISIN, "2024-12-31", 14.0)],
        currencies={EUR_ISIN: "EUR", USD_ISIN: "USD"},
        names={EUR_ISIN: "Euro Fund", USD_ISIN: "Dollar Fund"},
    )
    impacts = dep.deposit_impacts(db, config, meta, "2024-12-31")
    usd = next(i for i in impacts if i.isin == USD_ISIN)
    assert usd.value is None and usd.gain is None and usd.pnl_share is None
    s = dep.summarize(impacts)
    assert s.invested == pytest.approx(100.0)  # USD deposit excluded


def test_deposit_impacts_skips_future_trades(tmp_path):
    db, config, meta = _seed(
        tmp_path,
        transactions=[
            _buy("t1", "2024-01-01", EUR_ISIN, 10.0, 10.0),
            _buy("t2", "2025-06-01", EUR_ISIN, 10.0, 10.0),  # after as-of
            ("tr", "t3", "2025-03-01", EUR_ISIN, "SELL", 5.0, 11.0, 0.0, 0.0),
        ],
        prices=[(EUR_ISIN, "2024-12-31", 14.0)],
        currencies={EUR_ISIN: "EUR"},
        names={EUR_ISIN: "Euro Fund"},
    )
    impacts = dep.deposit_impacts(db, config, meta, "2024-12-31")
    assert [i.date for i in impacts] == ["2024-01-01"]


def test_deposit_impacts_refuses_book_with_sell(tmp_path):
    db, config, meta = _seed(
        tmp_path,
        transactions=[
            _buy("t1", "2024-01-01", EUR_ISIN, 10.0, 10.0),
            ("tr", "t2", "2024-03-01", EUR_ISIN, "SELL", 5.0, 11.0, 0.0, 0.0),
        ],
        prices=[(EUR_ISIN, "2024-12-31", 14.0)],
        currencies={EUR_ISIN: "EUR"},
        names={EUR_ISIN: "Euro Fund"},
    )
    with pytest.raises(ValueError, match=r"buy-and-hold book.*1 SELL"):
        dep.deposit_impacts(db, config, meta, "2024-12-31")


def test_assign_pnl_shares_none_when_total_zero():
    winner = DepositImpact("d", "A", "", amount=100.0, value=110.0)   # +10
    loser = DepositImpact("d", "B", "", amount=100.0, value=90.0)     # -10
    dep._assign_pnl_shares([winner, loser])
    assert winner.pnl_share is None and loser.pnl_share is None  # net P&L 0


def test_sort_impacts_by_gain_desc():
    a = DepositImpact("2024-01-01", "A", "", amount=100.0, value=110.0)  # +10
    b = DepositImpact("2024-01-02", "B", "", amount=100.0, value=130.0)  # +30
    c = DepositImpact("2024-01-03", "C", "", amount=100.0, value=None)   # unvaluable
    ordered = dep.sort_impacts([a, b, c], sort_by="gain", reverse=True)
    assert [i.isin for i in ordered] == ["B", "A", "C"]  # None sinks to the bottom


# ---------------------------------------------------------------------------
# Command
# ---------------------------------------------------------------------------

def test_cmd_deposits_renders_summary_and_reconciles(tmp_path, capsys):
    db, config, meta = _seed(
        tmp_path,
        transactions=[
            _buy("t1", "2024-01-01", EUR_ISIN, 10.0, 10.0),
            _buy("t2", "2024-02-01", EUR_ISIN, 5.0, 12.0),
        ],
        prices=[(EUR_ISIN, "2024-12-31", 14.0)],
        currencies={EUR_ISIN: "EUR"},
        names={EUR_ISIN: "Euro Fund"},
    )
    code = dep.main(_args(db, config, meta, "--as-of", "2024-12-31"))
    out = capsys.readouterr().out
    assert code == 0
    assert "Invested (contributions)" in out and "160.00" in out
    assert "Market value (reported)" in out and "210.00" in out
    assert "Organic gain (market)" in out and "+50.00" in out
    assert "ROIC" in out and "+31.2%" in out
    assert "Euro Fund" in out and "+80.0%" in out  # a deposit's P&L share


def test_cmd_deposits_reports_sell_as_unsupported(tmp_path, capsys):
    db, config, meta = _seed(
        tmp_path,
        transactions=[
            _buy("t1", "2024-01-01", EUR_ISIN, 10.0, 10.0),
            ("tr", "t2", "2024-03-01", EUR_ISIN, "SELL", 5.0, 11.0, 0.0, 0.0),
        ],
    )
    code = dep.main(_args(db, config, meta, "--as-of", "2024-12-31"))
    assert code == 1
    assert "requires a buy-and-hold book" in capsys.readouterr().out


def test_cmd_deposits_unvaluable_warning(tmp_path, capsys):
    db, config, meta = _seed(
        tmp_path,
        transactions=[
            _buy("t1", "2024-01-01", EUR_ISIN, 10.0, 10.0),
            _buy("t2", "2024-01-01", USD_ISIN, 10.0, 9.0),
        ],
        prices=[(EUR_ISIN, "2024-12-31", 14.0)],
        currencies={EUR_ISIN: "EUR", USD_ISIN: "USD"},
        names={EUR_ISIN: "Euro Fund", USD_ISIN: "Dollar Fund"},
    )
    dep.main(_args(db, config, meta, "--as-of", "2024-12-31"))
    out = capsys.readouterr().out
    assert "⚠ excluded from totals" in out and USD_ISIN in out


def test_cmd_deposits_no_deposits(tmp_path, capsys):
    db, config, meta = _seed(tmp_path, transactions=[])
    code = dep.main(_args(db, config, meta))
    assert code == 0
    assert "No deposits (BUY transactions)" in capsys.readouterr().out


def test_cmd_deposits_bad_as_of(tmp_path, capsys):
    db, config, meta = _seed(tmp_path, transactions=[])
    assert dep.main(_args(db, config, meta, "--as-of", "nope")) == 1
    assert "must be YYYY-MM-DD" in capsys.readouterr().out
