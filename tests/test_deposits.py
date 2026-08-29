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


def test_sort_impacts_by_pnl_desc():
    a = DepositImpact("2024-01-01", "A", "", amount=100.0, value=110.0)  # +10
    b = DepositImpact("2024-01-02", "B", "", amount=100.0, value=130.0)  # +30
    c = DepositImpact("2024-01-03", "C", "", amount=100.0, value=None)   # unvaluable
    ordered = dep.sort_impacts([a, b, c], sort_by="pnl", reverse=True)
    assert [i.isin for i in ordered] == ["B", "A", "C"]  # None sinks to the bottom


def test_sort_impacts_by_name_and_pnl_ctr():
    a = DepositImpact("d", "A", "Zeta", amount=100.0, value=110.0)
    b = DepositImpact("d", "B", "Alpha", amount=100.0, value=130.0)
    dep._assign_pnl_shares([a, b])
    by_name = dep.sort_impacts([a, b], sort_by="name")
    assert [i.isin for i in by_name] == ["B", "A"]
    by_ctr = dep.sort_impacts([a, b], sort_by="pnl_ctr", reverse=True)
    assert [i.isin for i in by_ctr] == ["B", "A"]  # +30 is the larger share


# ---------------------------------------------------------------------------
# Grouping (deposit vintages: period × ISIN)
# ---------------------------------------------------------------------------

def test_group_impacts_by_year_sums_and_reconciles(tmp_path):
    db, config, meta = _seed(
        tmp_path,
        transactions=[
            _buy("t1", "2023-03-01", EUR_ISIN, 10.0, 10.0),  # 2023: 100
            _buy("t2", "2023-09-01", EUR_ISIN, 5.0, 8.0),    # 2023:  40  (15 sh total)
            _buy("t3", "2024-02-01", EUR_ISIN, 5.0, 12.0),   # 2024:  60  ( 5 sh)
        ],
        prices=[(EUR_ISIN, "2024-12-31", 14.0)],
        currencies={EUR_ISIN: "EUR"},
        names={EUR_ISIN: "Euro Fund"},
    )
    impacts = dep.deposit_impacts(db, config, meta, "2024-12-31")
    y2023, y2024 = dep.group_impacts(impacts, "year")
    assert (y2023.date, y2023.isin) == ("2023", EUR_ISIN)
    assert (y2023.amount, y2023.value, y2023.gain) == pytest.approx((140.0, 210.0, 70.0))
    assert (y2024.date, y2024.amount, y2024.value) == ("2024", pytest.approx(60.0),
                                                        pytest.approx(70.0))
    # %P&L over the grouped rows: 70/80 and 10/80.
    assert y2023.pnl_share == pytest.approx(87.5) and y2024.pnl_share == pytest.approx(12.5)
    # Grouping is a partition: grouped totals equal the ungrouped summary.
    summary = dep.summarize(impacts)
    assert sum(g.value for g in (y2023, y2024)) == pytest.approx(summary.reported)
    assert sum(g.amount for g in (y2023, y2024)) == pytest.approx(summary.invested)


def test_group_impacts_by_month_keeps_funds_separate(tmp_path):
    db, config, meta = _seed(
        tmp_path,
        transactions=[
            _buy("t1", "2024-01-05", EUR_ISIN, 10.0, 10.0),
            _buy("t2", "2024-01-20", USD_ISIN, 5.0, 12.0),
        ],
        prices=[(EUR_ISIN, "2024-12-31", 14.0), (USD_ISIN, "2024-12-31", 120.0)],
        fx=[("EUR", "USD", "2024-12-31", 1.2)],
        currencies={EUR_ISIN: "EUR", USD_ISIN: "USD"},
        names={EUR_ISIN: "Euro Fund", USD_ISIN: "Dollar Fund"},
    )
    impacts = dep.deposit_impacts(db, config, meta, "2024-12-31")
    grouped = dep.group_impacts(impacts, "month")
    # Same month, two funds → two rows (period × ISIN), not one merged row.
    assert [(g.date, g.isin) for g in grouped] == [
        ("2024-01", EUR_ISIN), ("2024-01", USD_ISIN)
    ]


def test_all_total_row_is_the_grand_total():
    valuable_a = DepositImpact("2024", "A", "Fund A", amount=100.0, value=140.0)  # +40
    unvaluable = DepositImpact("2024", "C", "Fund C", amount=50.0, value=None)
    dep._assign_pnl_shares([valuable_a, unvaluable])
    row = dep._total_row([valuable_a, unvaluable], label="── ALL ──")
    assert row.name == "── ALL ──" and row.isin == ""
    assert (row.amount, row.value, row.gain) == pytest.approx((100.0, 140.0, 40.0))
    assert row.pnl_share == pytest.approx(100.0)  # unvaluable excluded, valuable sum = 100%


def test_group_impacts_unvaluable_bucket_is_none(tmp_path):
    db, config, meta = _seed(
        tmp_path,
        transactions=[
            _buy("t1", "2024-01-01", EUR_ISIN, 10.0, 10.0),
            _buy("t2", "2024-06-01", USD_ISIN, 10.0, 9.0),  # no price → unvaluable
        ],
        prices=[(EUR_ISIN, "2024-12-31", 14.0)],
        currencies={EUR_ISIN: "EUR", USD_ISIN: "USD"},
        names={EUR_ISIN: "Euro Fund", USD_ISIN: "Dollar Fund"},
    )
    impacts = dep.deposit_impacts(db, config, meta, "2024-12-31")
    grouped = {g.isin: g for g in dep.group_impacts(impacts, "year")}
    assert grouped[USD_ISIN].value is None and grouped[USD_ISIN].pnl_share is None
    assert grouped[EUR_ISIN].value == pytest.approx(140.0)


def test_subtotal_row_sums_valuable_only():
    valuable_a = DepositImpact("2024", "A", "Fund A", amount=100.0, value=140.0)  # +40
    valuable_b = DepositImpact("2024", "B", "Fund B", amount=60.0, value=70.0)    # +10
    unvaluable = DepositImpact("2024", "C", "Fund C", amount=50.0, value=None)
    dep._assign_pnl_shares([valuable_a, valuable_b, unvaluable])  # 80% / 20% / —
    sub = dep._subtotal_row([valuable_a, valuable_b, unvaluable])
    assert sub.date == "" and sub.isin == ""  # period column blank in the total row
    assert (sub.amount, sub.value, sub.gain) == pytest.approx((160.0, 210.0, 50.0))
    assert sub.ret_pct == pytest.approx(31.25)  # 50 / 160
    assert sub.pnl_share == pytest.approx(100.0)  # the whole (one-period) book


def test_period_key_week_is_iso8601():
    assert dep._period_key("2024-01-01", "week") == "2024-W01"
    assert dep._period_key("2024-01-07", "week") == "2024-W01"  # Sunday, same week
    assert dep._period_key("2024-01-08", "week") == "2024-W02"
    # ISO week-numbering year, not the calendar year: Mon 2024-12-30 is 2025-W01.
    assert dep._period_key("2024-12-29", "week") == "2024-W52"
    assert dep._period_key("2024-12-30", "week") == "2025-W01"


def test_group_impacts_by_week_sums_same_iso_week(tmp_path):
    db, config, meta = _seed(
        tmp_path,
        transactions=[
            _buy("t1", "2024-01-01", EUR_ISIN, 10.0, 10.0),  # W01 Mon: 100
            _buy("t2", "2024-01-07", EUR_ISIN, 5.0, 8.0),    # W01 Sun:  40  (15 sh)
            _buy("t3", "2024-01-08", EUR_ISIN, 5.0, 12.0),   # W02 Mon:  60  ( 5 sh)
        ],
        prices=[(EUR_ISIN, "2024-12-31", 14.0)],
        currencies={EUR_ISIN: "EUR"},
        names={EUR_ISIN: "Euro Fund"},
    )
    impacts = dep.deposit_impacts(db, config, meta, "2024-12-31")
    w01, w02 = dep.group_impacts(impacts, "week")
    assert (w01.date, w01.isin) == ("2024-W01", EUR_ISIN)
    assert (w01.amount, w01.value, w01.gain) == pytest.approx((140.0, 210.0, 70.0))
    assert (w02.date, w02.amount, w02.value) == ("2024-W02", pytest.approx(60.0),
                                                    pytest.approx(70.0))
    summary = dep.summarize(impacts)
    assert sum(g.value for g in (w01, w02)) == pytest.approx(summary.reported)
    assert sum(g.amount for g in (w01, w02)) == pytest.approx(summary.invested)


def test_group_impacts_week_uses_iso_year_across_calendar_boundary(tmp_path):
    db, config, meta = _seed(
        tmp_path,
        transactions=[
            _buy("t1", "2024-12-29", EUR_ISIN, 10.0, 10.0),  # Sun → 2024-W52
            _buy("t2", "2024-12-31", EUR_ISIN, 5.0, 12.0),   # Tue → 2025-W01
        ],
        prices=[(EUR_ISIN, "2024-12-31", 14.0)],
        currencies={EUR_ISIN: "EUR"},
        names={EUR_ISIN: "Euro Fund"},
    )
    impacts = dep.deposit_impacts(db, config, meta, "2024-12-31")
    w52, w01 = dep.group_impacts(impacts, "week")
    assert (w52.date, w52.amount, w52.value) == ("2024-W52", pytest.approx(100.0),
                                                   pytest.approx(140.0))
    assert (w01.date, w01.amount, w01.value) == ("2025-W01", pytest.approx(60.0),
                                                   pytest.approx(70.0))


def test_group_subtotals_reconcile_across_periods(tmp_path):
    db, config, meta = _seed(
        tmp_path,
        transactions=[
            _buy("t1", "2023-03-01", EUR_ISIN, 10.0, 10.0),  # 2023
            _buy("t2", "2024-02-01", EUR_ISIN, 5.0, 12.0),   # 2024
            _buy("t3", "2024-05-01", USD_ISIN, 5.0, 12.0),   # 2024 (USD 60 paid)
        ],
        prices=[(EUR_ISIN, "2024-12-31", 14.0), (USD_ISIN, "2024-12-31", 120.0)],
        fx=[("EUR", "USD", "2024-12-31", 1.2)],
        currencies={EUR_ISIN: "EUR", USD_ISIN: "USD"},
        names={EUR_ISIN: "Euro Fund", USD_ISIN: "Dollar Fund"},
    )
    impacts = dep.deposit_impacts(db, config, meta, "2024-12-31")
    grouped = dep.group_impacts(impacts, "year")
    by_period: dict[str, list] = {}
    for row in grouped:
        by_period.setdefault(row.date, []).append(row)
    subtotals = [dep._subtotal_row(m) for m in by_period.values()]
    summary = dep.summarize(impacts)
    # Value subtotals sum to the reported market value; %P&L subtotals sum to 100%.
    assert sum(s.value for s in subtotals) == pytest.approx(summary.reported)
    assert sum(s.amount for s in subtotals) == pytest.approx(summary.invested)
    assert sum(s.pnl_share for s in subtotals) == pytest.approx(100.0)


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


def test_cmd_deposits_group_year_renders_period_rows(tmp_path, capsys):
    db, config, meta = _seed(
        tmp_path,
        transactions=[
            _buy("t1", "2023-03-01", EUR_ISIN, 10.0, 10.0),
            _buy("t2", "2024-02-01", EUR_ISIN, 5.0, 12.0),
        ],
        prices=[(EUR_ISIN, "2024-12-31", 14.0)],
        currencies={EUR_ISIN: "EUR"},
        names={EUR_ISIN: "Euro Fund"},
    )
    code = dep.main(_args(db, config, meta, "--as-of", "2024-12-31", "--group", "year"))
    out = capsys.readouterr().out
    assert code == 0
    assert "Per-year impact" in out
    assert "2023" in out and "2024" in out  # each period is its own group heading
    assert out.count("── total ──") == 2  # a subtotal per period
    assert out.count("Amount€") == 2  # the header repeats at the start of each group
    # The top summary block is dropped under --group; the grand total lives in the table.
    assert "Invested (contributions)" not in out
    assert "── ALL ──" in out and "160.00" in out  # grand total in-table


def test_cmd_deposits_group_year_sort_pnl_reverse_orders_periods_and_funds(
    tmp_path, capsys
):
    # 2023: EUR +40. 2024: EUR +10, USD +440. --reverse → 2024 first; --sort pnl
    # → USD above EUR inside 2024.
    db, config, meta = _seed(
        tmp_path,
        transactions=[
            _buy("t1", "2023-03-01", EUR_ISIN, 10.0, 10.0),
            _buy("t2", "2024-02-01", EUR_ISIN, 5.0, 12.0),
            _buy("t3", "2024-05-01", USD_ISIN, 5.0, 12.0),
        ],
        prices=[(EUR_ISIN, "2024-12-31", 14.0), (USD_ISIN, "2024-12-31", 120.0)],
        fx=[("EUR", "USD", "2024-12-31", 1.2)],
        currencies={EUR_ISIN: "EUR", USD_ISIN: "USD"},
        names={EUR_ISIN: "Euro Fund", USD_ISIN: "Dollar Fund"},
    )
    code = dep.main(_args(
        db, config, meta, "--as-of", "2024-12-31",
        "--group", "year", "--sort", "pnl", "--reverse",
    ))
    out = capsys.readouterr().out
    assert code == 0
    i_2024, i_2023 = out.index("\n2024\n"), out.index("\n2023\n")
    assert i_2024 < i_2023
    section_2024 = out[i_2024:i_2023]
    assert section_2024.index(USD_ISIN) < section_2024.index(EUR_ISIN)


def test_cmd_deposits_group_skips_subtotal_when_period_has_no_valuable_fund(
    tmp_path, capsys
):
    db, config, meta = _seed(
        tmp_path,
        transactions=[
            _buy("t1", "2023-03-01", USD_ISIN, 10.0, 9.0),  # unvaluable (no price)
            _buy("t2", "2024-02-01", EUR_ISIN, 10.0, 10.0),
        ],
        prices=[(EUR_ISIN, "2024-12-31", 14.0)],
        currencies={EUR_ISIN: "EUR", USD_ISIN: "USD"},
        names={EUR_ISIN: "Euro Fund", USD_ISIN: "Dollar Fund"},
    )
    dep.main(_args(db, config, meta, "--as-of", "2024-12-31", "--group", "year"))
    out = capsys.readouterr().out
    assert "2023" in out and USD_ISIN in out  # unvaluable vintage still listed
    assert out.count("── total ──") == 1  # only the 2024 (valuable) section


def test_cmd_deposits_group_week_renders_iso_week_headings(tmp_path, capsys):
    db, config, meta = _seed(
        tmp_path,
        transactions=[
            _buy("t1", "2024-01-01", EUR_ISIN, 10.0, 10.0),
            _buy("t2", "2024-01-08", EUR_ISIN, 5.0, 12.0),
        ],
        prices=[(EUR_ISIN, "2024-12-31", 14.0)],
        currencies={EUR_ISIN: "EUR"},
        names={EUR_ISIN: "Euro Fund"},
    )
    code = dep.main(_args(db, config, meta, "--as-of", "2024-12-31", "--group", "week"))
    out = capsys.readouterr().out
    assert code == 0
    assert "Per-week impact" in out
    assert "2024-W01" in out and "2024-W02" in out
    assert "Invested (contributions)" not in out
    assert "── ALL ──" in out


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


def test_retired_sort_token_gain_is_rejected():
    with pytest.raises(SystemExit):
        dep.main(["--sort", "gain"])
