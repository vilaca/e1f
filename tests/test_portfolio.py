"""Portfolio holdings: average-cost positions from transactions."""

import sqlite3
from contextlib import closing

import yaml
import pytest

from e1f import portfolio as portfolio_mod
from e1f import transactions as transactions_mod
from e1f.portfolio import (
    Holding,
    compute_holdings,
    holding_weight_pct,
    sort_holdings,
    yearly_fee_est,
    _broker_label,
    _distribution_label,
    _eur_value,
    _last_known_price,
)
from e1f.transactions import BROKER_TRADE_REPUBLIC

ISIN_ETF = "IE00B4L5Y983"
ISIN_USD = "IE00USD000001"
ISIN_SECOND = "IE00EUR000002"
BROKER_OTHER = "other_broker"


def _seed_valued(tmp_path, *, transactions, prices, fx=(), currencies, names, ters=None):
    """DB + config + currency-meta with prices and FX, for market-value tests."""
    db = tmp_path / "valued.db"
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
        conn.executemany("INSERT INTO transactions VALUES (?,?,?,?,?,?,?,?,?)", transactions)
        conn.executemany("INSERT INTO prices VALUES (?,?,?)", prices)
        conn.executemany("INSERT INTO fx_rates VALUES (?,?,?,?)", fx)
        conn.commit()
    ters = ters or {}
    etfs = {}
    for isin in set(names) | set(ters):
        entry = {}
        if isin in names:
            entry["name"] = names[isin]
        if isin in ters:
            entry["ter"] = ters[isin]
        etfs[isin] = entry
    config = tmp_path / "config.yaml"
    config.write_text(yaml.dump({"etfs": etfs}))
    meta = tmp_path / "meta.yaml"
    meta.write_text(
        yaml.dump({isin: {"currency": c, "symbol": f"{isin}:X:{c}", "xid": "1"}
                   for isin, c in currencies.items()})
    )
    return str(db), str(config), str(meta)


def _vbuy(txid, day, isin, shares, price, *, broker=BROKER_TRADE_REPUBLIC, fee=0.0):
    return (broker, txid, day, isin, "BUY", shares, price, fee, 0.0)


def _row(dt, symbol, side, shares, price, fee, broker=BROKER_TRADE_REPUBLIC):
    return (broker, dt, symbol, side, shares, price, fee)


def test_compute_holdings_single_buy():
    rows = [_row("2024-01-01", ISIN_ETF, "BUY", 2.0, 100.0, 1.0)]
    assert compute_holdings(rows) == [
        Holding(
            broker=BROKER_TRADE_REPUBLIC,
            symbol=ISIN_ETF,
            shares=2.0,
            avg_cost=100.5,
            total_paid=201.0,
        )
    ]


def test_compute_holdings_savings_plan_counts_as_buy():
    rows = [_row("2024-01-01", ISIN_ETF, "SAVINGS_PLAN", 1.0, 50.0, 0.0)]
    assert compute_holdings(rows) == [
        Holding(
            broker=BROKER_TRADE_REPUBLIC,
            symbol=ISIN_ETF,
            shares=1.0,
            avg_cost=50.0,
            total_paid=50.0,
        )
    ]


def test_compute_holdings_two_buys_average_cost():
    rows = [
        _row("2024-01-01", ISIN_ETF, "BUY", 1.0, 100.0, 1.0),
        _row("2024-02-01", ISIN_ETF, "BUY", 1.0, 120.0, 0.0),
    ]
    assert compute_holdings(rows) == [
        Holding(
            broker=BROKER_TRADE_REPUBLIC,
            symbol=ISIN_ETF,
            shares=2.0,
            avg_cost=110.5,
            total_paid=221.0,
        )
    ]


def test_compute_holdings_sell_reduces_position():
    rows = [
        _row("2024-01-01", ISIN_ETF, "BUY", 2.0, 100.0, 0.0),
        _row("2024-02-01", ISIN_ETF, "SELL", 1.0, 110.0, 0.0),
    ]
    assert compute_holdings(rows) == [
        Holding(
            broker=BROKER_TRADE_REPUBLIC,
            symbol=ISIN_ETF,
            shares=1.0,
            avg_cost=100.0,
            total_paid=100.0,
        )
    ]


def test_compute_holdings_sell_closes_position():
    rows = [
        _row("2024-01-01", ISIN_ETF, "BUY", 1.0, 100.0, 0.0),
        _row("2024-02-01", ISIN_ETF, "SELL", 1.0, 110.0, 0.0),
    ]
    assert compute_holdings(rows) == []


def test_compute_holdings_keeps_brokers_separate():
    rows = [
        _row("2024-01-01", ISIN_ETF, "BUY", 1.0, 100.0, 0.0, broker=BROKER_TRADE_REPUBLIC),
        _row("2024-01-01", ISIN_ETF, "BUY", 2.0, 90.0, 0.0, broker=BROKER_OTHER),
    ]
    assert compute_holdings(rows) == [
        Holding(
            broker=BROKER_OTHER,
            symbol=ISIN_ETF,
            shares=2.0,
            avg_cost=90.0,
            total_paid=180.0,
        ),
        Holding(
            broker=BROKER_TRADE_REPUBLIC,
            symbol=ISIN_ETF,
            shares=1.0,
            avg_cost=100.0,
            total_paid=100.0,
        ),
    ]


def test_compute_holdings_skips_zero_or_negative_shares():
    rows = [
        _row("2024-01-01", ISIN_ETF, "BUY", 0.0, 100.0, 0.0),
        _row("2024-01-02", ISIN_ETF, "BUY", -1.0, 100.0, 0.0),
    ]
    assert compute_holdings(rows) == []


def test_compute_holdings_sell_without_position_is_ignored():
    rows = [_row("2024-01-01", ISIN_ETF, "SELL", 1.0, 100.0, 0.0)]
    assert compute_holdings(rows) == []


def test_holding_weight_pct():
    holdings = [
        Holding(BROKER_TRADE_REPUBLIC, ISIN_ETF, 1.0, 100.0, 300.0),
        Holding(BROKER_OTHER, ISIN_ETF, 1.0, 100.0, 100.0),
    ]
    total = 400.0
    assert holding_weight_pct(holdings[0], total) == 75.0
    assert holding_weight_pct(holdings[1], total) == 25.0
    assert holding_weight_pct(holdings[0], 0.0) == 0.0


def test_yearly_fee_est():
    assert yearly_fee_est(0.20, 1000.0) == pytest.approx(2.0)  # 0.20% of €1000 market value
    assert yearly_fee_est(None, 1000.0) is None
    assert yearly_fee_est(0.20, 0.0) is None
    assert yearly_fee_est(0.20, None) is None  # unvaluable holding → no fee


def test_distribution_label():
    assert _distribution_label("Accumulating") == "ACC"
    assert _distribution_label("Distributing") == "Dist"
    assert _distribution_label("") == ""


def test_broker_label():
    assert _broker_label("trade_republic") == "tr"
    assert _broker_label("xtb") == "xtb"


def test_sort_holdings_by_weight_desc():
    holdings = [
        Holding(BROKER_TRADE_REPUBLIC, "AAA", 1.0, 10.0, 100.0),
        Holding(BROKER_OTHER, "BBB", 1.0, 10.0, 300.0),
        Holding(BROKER_TRADE_REPUBLIC, "CCC", 1.0, 10.0, 100.0),
    ]
    sorted_holdings = sort_holdings(
        holdings,
        sort_by="weight",
        reverse=True,
        config_path="unused",
        total_invested=500.0,
        eur_values={},
    )
    assert [h.symbol for h in sorted_holdings] == ["BBB", "AAA", "CCC"]


def test_last_known_price_reads_latest_close(tmp_path):
    from contextlib import closing
    import sqlite3

    db = tmp_path / "t.db"
    with closing(sqlite3.connect(db)) as conn:
        conn.execute(
            "CREATE TABLE prices (isin TEXT, date TEXT, close REAL, "
            "PRIMARY KEY (isin, date))"
        )
        conn.executemany(
            "INSERT INTO prices VALUES (?, ?, ?)",
            [
                (ISIN_ETF, "2024-01-01", 100.0),
                (ISIN_ETF, "2024-01-03", 105.0),  # latest date
                (ISIN_ETF, "2024-01-02", None),   # NULL close ignored
            ],
        )
        conn.commit()

    assert _last_known_price(str(db), ISIN_ETF) == pytest.approx(105.0)
    assert _last_known_price(str(db), "ZZ0000000000") is None


def test_last_known_price_no_prices_table(tmp_path):
    from contextlib import closing
    import sqlite3

    db = tmp_path / "t.db"
    with closing(sqlite3.connect(db)) as conn:
        conn.execute("CREATE TABLE other (x INTEGER)")
        conn.commit()
    assert _last_known_price(str(db), ISIN_ETF) is None


def test_main_portfolio_corrupt_db_returns_error(tmp_path, capsys):
    db = tmp_path / "corrupt.db"
    db.write_bytes(b"this is not a sqlite database")
    config = tmp_path / "config.yaml"
    config.write_text(yaml.dump({"etfs": {}}))

    code = portfolio_mod.main(["--db", str(db), "--config", str(config)])
    out = capsys.readouterr().out
    assert code == 1
    assert "✗ Error:" in out


@pytest.mark.parametrize("sort_by", ["broker", "isin", "units", "avg", "cost"])
def test_sort_holdings_config_free_fields(sort_by):
    holdings = [
        Holding(BROKER_TRADE_REPUBLIC, "CCC", 3.0, 30.0, 90.0),
        Holding(BROKER_OTHER, "AAA", 1.0, 10.0, 10.0),
        Holding(BROKER_TRADE_REPUBLIC, "BBB", 2.0, 20.0, 40.0),
    ]
    result = sort_holdings(
        holdings,
        sort_by=sort_by,
        reverse=False,
        config_path="unused",
        total_invested=140.0,
        eur_values={},
    )
    # These fields all sort AAA < BBB < CCC for this fixture.
    assert [h.symbol for h in result] == ["AAA", "BBB", "CCC"]


def test_sort_holdings_by_name_ter_and_fee(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text(
        yaml.dump(
            {
                "etfs": {
                    "AAA": {"name": "Zeta Fund", "ter": 0.05},
                    "BBB": {"name": "Alpha Fund", "ter": 0.30},
                }
            }
        )
    )
    holdings = [
        Holding(BROKER_TRADE_REPUBLIC, "AAA", 1.0, 10.0, 100.0),
        Holding(BROKER_TRADE_REPUBLIC, "BBB", 1.0, 10.0, 100.0),
    ]

    by_name = sort_holdings(
        holdings, sort_by="name", reverse=False,
        config_path=str(config), total_invested=200.0, eur_values={},
    )
    assert [h.symbol for h in by_name] == ["BBB", "AAA"]  # Alpha before Zeta

    by_ter = sort_holdings(
        holdings, sort_by="ter", reverse=False,
        config_path=str(config), total_invested=200.0, eur_values={},
    )
    assert [h.symbol for h in by_ter] == ["AAA", "BBB"]  # 0.05 before 0.30

    # fee_yr sorts on the market-value fee (ADR-0032): equal €100 values here, so
    # the order follows TER — 0.05% (€0.05) before 0.30% (€0.30).
    values = {(BROKER_TRADE_REPUBLIC, "AAA"): 100.0, (BROKER_TRADE_REPUBLIC, "BBB"): 100.0}
    by_fee = sort_holdings(
        holdings, sort_by="fee_yr", reverse=False,
        config_path=str(config), total_invested=200.0, eur_values=values,
    )
    assert [h.symbol for h in by_fee] == ["AAA", "BBB"]


def test_sort_holdings_missing_ter_sorts_last_ascending(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text(yaml.dump({"etfs": {"AAA": {"name": "Has TER", "ter": 0.10}}}))
    holdings = [
        Holding(BROKER_TRADE_REPUBLIC, "AAA", 1.0, 10.0, 100.0),
        Holding(BROKER_TRADE_REPUBLIC, "BBB", 1.0, 10.0, 100.0),  # no config entry → -1.0
    ]
    by_ter = sort_holdings(
        holdings, sort_by="ter", reverse=False,
        config_path=str(config), total_invested=200.0, eur_values={},
    )
    assert [h.symbol for h in by_ter] == ["BBB", "AAA"]  # missing (−∞) sorts first ascending


def test_sort_holdings_unsupported_field_raises():
    holdings = [Holding(BROKER_TRADE_REPUBLIC, "AAA", 1.0, 10.0, 100.0)]
    with pytest.raises(ValueError, match="unsupported sort field"):
        sort_holdings(
            holdings, sort_by="bogus", reverse=False,
            config_path="unused", total_invested=100.0, eur_values={},
        )


def test_sort_holdings_by_value_none_last_when_reversed():
    holdings = [
        Holding(BROKER_TRADE_REPUBLIC, "AAA", 1.0, 10.0, 100.0),
        Holding(BROKER_TRADE_REPUBLIC, "BBB", 1.0, 10.0, 100.0),
        Holding(BROKER_TRADE_REPUBLIC, "CCC", 1.0, 10.0, 100.0),
    ]
    values = {
        (BROKER_TRADE_REPUBLIC, "AAA"): 50.0,
        (BROKER_TRADE_REPUBLIC, "BBB"): 200.0,
        (BROKER_TRADE_REPUBLIC, "CCC"): None,
    }
    result = sort_holdings(
        holdings, sort_by="value", reverse=True,
        config_path="unused", total_invested=300.0, eur_values=values,
    )
    assert [h.symbol for h in result] == ["BBB", "AAA", "CCC"]


def test_sort_holdings_by_last_px_and_class(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text(
        yaml.dump(
            {
                "etfs": {
                    "AAA": {"name": "A", "asset_class": "Equity"},
                    "BBB": {"name": "B", "asset_class": "Real Estate"},
                }
            }
        )
    )
    holdings = [
        Holding(BROKER_TRADE_REPUBLIC, "AAA", 1.0, 10.0, 100.0),
        Holding(BROKER_TRADE_REPUBLIC, "BBB", 1.0, 10.0, 100.0),
    ]
    by_px = sort_holdings(
        holdings, sort_by="last_px", reverse=True,
        config_path=str(config), total_invested=200.0, eur_values={},
        last_prices={"AAA": 10.0, "BBB": 50.0},
    )
    assert [h.symbol for h in by_px] == ["BBB", "AAA"]

    by_class = sort_holdings(
        holdings, sort_by="class", reverse=False,
        config_path=str(config), total_invested=200.0, eur_values={},
    )
    assert [h.symbol for h in by_class] == ["AAA", "BBB"]  # Eqty < REITs

    by_dist = sort_holdings(
        holdings, sort_by="dist", reverse=False,
        config_path=str(config), total_invested=200.0, eur_values={},
    )
    assert [h.symbol for h in by_dist] == ["AAA", "BBB"]

    by_ccy = sort_holdings(
        holdings, sort_by="ccy", reverse=False,
        config_path=str(config), total_invested=200.0, eur_values={},
    )
    assert {h.symbol for h in by_ccy} == {"AAA", "BBB"}


def test_main_portfolio_sort_by_cost(tmp_path, capsys):
    db = tmp_path / "t.db"
    config = tmp_path / "config.yaml"
    config.write_text(yaml.dump({"etfs": {ISIN_ETF: {"name": "Test ETF"}}}))

    from contextlib import closing
    import sqlite3
    from e1f.transactions import BROKER_TRADE_REPUBLIC, BROKER_XTB, init_transactions_database

    other_isin = "IE00B4K48X80"
    init_transactions_database(str(db))
    with closing(sqlite3.connect(db)) as conn:
        conn.executemany(
            "INSERT INTO transactions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (BROKER_TRADE_REPUBLIC, "1", "2024-01-01", ISIN_ETF, "BUY", 1.0, 100.0, 0.0, 0.0),
                (BROKER_XTB, "2", "2024-01-02", other_isin, "BUY", 1.0, 300.0, 0.0, 0.0),
            ],
        )
        conn.commit()

    code = portfolio_mod.main(
        ["--db", str(db), "--config", str(config), "--sort", "cost", "--reverse"],
    )
    out = capsys.readouterr().out

    assert code == 0
    assert out.index(other_isin) < out.index(ISIN_ETF)


def test_main_portfolio_shows_weight_column(tmp_path, capsys):
    db = tmp_path / "t.db"
    config = tmp_path / "config.yaml"
    config.write_text(yaml.dump({"etfs": {ISIN_ETF: {"name": "Test ETF"}}}))

    from contextlib import closing
    import sqlite3
    from e1f.transactions import BROKER_TRADE_REPUBLIC, BROKER_XTB, init_transactions_database

    init_transactions_database(str(db))
    with closing(sqlite3.connect(db)) as conn:
        conn.executemany(
            "INSERT INTO transactions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (BROKER_TRADE_REPUBLIC, "1", "2024-01-01", ISIN_ETF, "BUY", 1.0, 100.0, 0.0, 0.0),
                (BROKER_XTB, "2", "2024-01-02", ISIN_ETF, "BUY", 1.0, 300.0, 0.0, 0.0),
            ],
        )
        conn.commit()

    code = portfolio_mod.main(["--db", str(db), "--config", str(config)])
    out = capsys.readouterr().out

    assert code == 0
    assert "Weight" in out
    assert "25.0%" in out
    assert "75.0%" in out
    assert "Units" not in out
    assert "Avg paid" not in out
    assert "Total paid" not in out


def test_main_portfolio_empty(tmp_path, capsys):
    db = tmp_path / "t.db"
    config = tmp_path / "config.yaml"
    config.write_text(yaml.dump({"etfs": {}}))
    code = portfolio_mod.main(["--db", str(db), "--config", str(config)])
    out = capsys.readouterr().out
    assert code == 0
    assert "No ETF holdings" in out


def test_main_portfolio_shows_name_from_config(tmp_path, capsys):
    from pathlib import Path

    db = tmp_path / "t.db"
    config = tmp_path / "config.yaml"
    config.write_text(
        yaml.dump(
            {
                "etfs": {
                    ISIN_ETF: {
                        "name": "Core MSCI World USD (Acc)",
                        "asset_class": "Equity",
                        "fund_currency": "USD",
                        "distribution": "Accumulating",
                        "ter": 0.22,
                    },
                }
            }
        )
    )
    fixture = Path(__file__).resolve().parent / "fixtures" / "trade_republic_sample.csv"
    assert transactions_mod.main(
        ["trade-republic", str(fixture), "--db", str(db), "--config", str(config)]
    ) == 0

    code = portfolio_mod.main(
        ["--db", str(db), "--config", str(config), "--show-cost-basis", "--show-broker"]
    )
    out = capsys.readouterr().out
    assert code == 0
    assert f"{_broker_label(BROKER_TRADE_REPUBLIC):<4}" in out
    assert "trade_republic" not in out
    assert ISIN_ETF in out
    assert "Core MSCI World USD (Acc)" in out
    assert "Class" in out
    assert "Eqty" in out
    assert "USD" in out
    assert "ACC" in out
    assert "Accumulating" not in out
    assert "0.22%" in out
    assert "Avg paid" in out
    assert "Total" in out
    assert "Weight" in out
    assert "Total: 1 holdings" in out


def test_main_portfolio_displays_money_with_four_decimals(tmp_path, capsys):
    db = tmp_path / "t.db"
    config = tmp_path / "config.yaml"
    config.write_text(yaml.dump({"etfs": {ISIN_ETF: {"name": "Test ETF"}}}))

    from e1f.transactions import BROKER_XTB, init_transactions_database
    import sqlite3
    from contextlib import closing

    init_transactions_database(str(db))
    rows = [
        (
            BROKER_XTB, "1", "2026-07-24 14:45:49", ISIN_ETF, "BUY",
            0.7839, 9.87 / 0.7839, None, None,
        ),
        (
            BROKER_XTB, "2", "2026-07-24 14:45:50", ISIN_ETF, "BUY",
            7.0, 88.13 / 7.0, None, None,
        ),
        (
            BROKER_XTB, "3", "2026-07-29 14:26:03", ISIN_ETF, "BUY",
            7.0, 87.51 / 7.0, None, None,
        ),
        (
            BROKER_XTB, "4", "2026-07-29 14:26:11", ISIN_ETF, "BUY",
            0.9987, 12.49 / 0.9987, None, None,
        ),
    ]
    with closing(sqlite3.connect(db)) as conn:
        conn.executemany(
            "INSERT INTO transactions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        conn.commit()

    code = portfolio_mod.main(
        ["--db", str(db), "--config", str(config), "--show-cost-basis"]
    )
    out = capsys.readouterr().out
    assert code == 0
    assert "15.7826" in out
    assert "12.5455" in out
    assert "198.00" in out


def test_main_portfolio_shows_yearly_fee_estimate(tmp_path, capsys):
    db = tmp_path / "t.db"
    config = tmp_path / "config.yaml"
    config.write_text(yaml.dump({"etfs": {ISIN_ETF: {"name": "Test ETF", "ter": 0.20}}}))
    meta = tmp_path / "meta.yaml"
    meta.write_text(
        yaml.dump({ISIN_ETF: {"currency": "EUR", "symbol": f"{ISIN_ETF}:X:EUR", "xid": "1"}})
    )

    from contextlib import closing
    import sqlite3
    from e1f.transactions import BROKER_TRADE_REPUBLIC, init_transactions_database

    init_transactions_database(str(db))
    with closing(sqlite3.connect(db)) as conn:
        conn.execute(
            "INSERT INTO transactions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (BROKER_TRADE_REPUBLIC, "1", "2024-01-01", ISIN_ETF, "BUY", 1.0, 1000.0, 0.0, 0.0),
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS prices (isin TEXT, date TEXT, close REAL, "
            "PRIMARY KEY (isin, date))"
        )
        conn.execute("INSERT INTO prices VALUES (?, ?, ?)", (ISIN_ETF, "2024-01-02", 1000.0))
        conn.commit()

    args = ["--db", str(db), "--config", str(config), "--currency-meta", str(meta)]
    # Fee/yr is only shown with --show-cost-basis
    code = portfolio_mod.main(args)
    out = capsys.readouterr().out
    assert code == 0
    assert "Fee/yr" not in out

    code = portfolio_mod.main([*args, "--show-cost-basis"])
    out = capsys.readouterr().out
    assert code == 0
    assert "Fee/yr" in out
    assert "€2.00" in out          # 0.20% of €1000 market value = €2.00
    assert "~€2.00/yr in fees" in out


def test_main_portfolio_unknown_isin_shows_blank_name(tmp_path, capsys):
    db = tmp_path / "t.db"
    config = tmp_path / "config.yaml"
    config.write_text(yaml.dump({"etfs": {}}))

    from contextlib import closing

    import sqlite3

    from e1f.transactions import BROKER_TRADE_REPUBLIC, init_transactions_database

    init_transactions_database(str(db))
    with closing(sqlite3.connect(db)) as conn:
        conn.execute(
            "INSERT INTO transactions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (BROKER_TRADE_REPUBLIC, "1", "2024-01-01", ISIN_ETF, "BUY", 1.0, 100.0, 0.0, 0.0),
        )
        conn.commit()

    code = portfolio_mod.main(["--db", str(db), "--config", str(config)])
    out = capsys.readouterr().out
    assert code == 0
    assert ISIN_ETF in out
    assert "Total: 1 holdings" in out
    assert "Unknown ETF" not in out

def test_main_portfolio_help(capsys):
    with pytest.raises(SystemExit) as exc:
        portfolio_mod.main(["--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "e1f portfolio" in out
    assert "--sort" in out
    assert "--reverse" in out
    assert "--show-cost-basis" in out
    assert "--show-status" in out
    assert "--explain" in out


def test_retired_sort_token_total_is_rejected():
    with pytest.raises(SystemExit):
        portfolio_mod.main(["--sort", "total"])


# ---------------------------------------------------------------------------
# Provenance disclosure (ADR-0014): --show-status, --explain
# ---------------------------------------------------------------------------


def _seed_two(tmp_path, *, config_isins):
    from contextlib import closing
    import sqlite3
    from e1f.transactions import BROKER_TRADE_REPUBLIC, init_transactions_database

    db = tmp_path / "t.db"
    config = tmp_path / "config.yaml"
    config.write_text(yaml.dump({"etfs": {i: {"name": f"Fund {i}"} for i in config_isins}}))
    unknown = "IE00UNKNOWN001"
    init_transactions_database(str(db))
    with closing(sqlite3.connect(db)) as conn:
        conn.executemany(
            "INSERT INTO transactions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (BROKER_TRADE_REPUBLIC, "1", "2024-01-01", ISIN_ETF, "BUY", 1.0, 100.0, 0.0, 0.0),
                (BROKER_TRADE_REPUBLIC, "2", "2024-01-02", unknown, "BUY", 1.0, 300.0, 0.0, 0.0),
            ],
        )
        conn.commit()
    return str(db), str(config), unknown


def test_main_default_has_no_status_column(tmp_path, capsys):
    db, config, _unknown = _seed_two(tmp_path, config_isins=[ISIN_ETF])
    portfolio_mod.main(["--db", db, "--config", config])
    out = capsys.readouterr().out
    assert "Status" not in out
    assert "CALCULATED" not in out


def test_main_show_status_adds_calculated_column(tmp_path, capsys):
    db, config, _unknown = _seed_two(tmp_path, config_isins=[ISIN_ETF])
    portfolio_mod.main(["--db", db, "--config", config, "--show-status"])
    out = capsys.readouterr().out
    assert "Status" in out
    assert out.count("CALCULATED") == 2  # both holdings, uniformly CALCULATED


def test_main_explain_implies_status_and_reports_metadata_gap(tmp_path, capsys):
    db, config, unknown = _seed_two(tmp_path, config_isins=[ISIN_ETF])
    portfolio_mod.main(["--db", db, "--config", config, "--explain"])
    out = capsys.readouterr().out
    assert "Status" in out                       # --explain implies the column
    assert "reconstructed from source, not a log" in out
    assert "method = average_cost_v1" in out
    assert "Not limited by: price data" in out
    # the holding whose ISIN is absent from config is named as a metadata gap
    assert "1 of 2 holdings not in config" in out
    assert unknown in out


def test_main_explain_all_metadata_present(tmp_path, capsys):
    db, config, _unknown = _seed_two(tmp_path, config_isins=[ISIN_ETF, "IE00UNKNOWN001"])
    portfolio_mod.main(["--db", db, "--config", config, "--explain"])
    out = capsys.readouterr().out
    assert "config metadata present for all 2 holdings" in out


# ---------------------------------------------------------------------------
# FX-converted market value + market-value-weighted fee/TER — ADR-0032
# ---------------------------------------------------------------------------

def _pargs(db, config, meta, *extra):
    return ["--db", db, "--config", config, "--currency-meta", meta, *extra]


def test_eur_value_fx_converts_usd(tmp_path):
    db, _config, meta = _seed_valued(
        tmp_path,
        transactions=[_vbuy("t1", "2024-01-01", ISIN_USD, 10.0, 90.0)],
        prices=[(ISIN_USD, "2024-06-01", 120.0)],
        fx=[("EUR", "USD", "2024-06-01", 1.2)],
        currencies={ISIN_USD: "USD"},
        names={ISIN_USD: "Dollar Fund"},
    )
    # 10 * 120 USD / 1.2 = 1000 EUR
    assert _eur_value(db, meta, ISIN_USD, 10.0) == pytest.approx(1000.0)


def test_eur_value_none_when_unpriced_or_no_fx(tmp_path):
    db, _config, meta = _seed_valued(
        tmp_path,
        transactions=[_vbuy("t1", "2024-01-01", ISIN_USD, 10.0, 90.0)],
        prices=[(ISIN_USD, "2024-06-01", 120.0)],  # priced but no FX rate seeded
        fx=[],
        currencies={ISIN_USD: "USD"},
        names={ISIN_USD: "Dollar Fund"},
    )
    assert _eur_value(db, meta, ISIN_USD, 10.0) is None        # no FX rate
    assert _eur_value(db, meta, "IE00NOPRICE00", 10.0) is None  # no price / not pinned


def test_main_portfolio_value_and_fee_fx_converted(tmp_path, capsys):
    db, config, meta = _seed_valued(
        tmp_path,
        transactions=[_vbuy("t1", "2024-01-01", ISIN_USD, 10.0, 90.0)],
        prices=[(ISIN_USD, "2024-06-01", 120.0)],
        fx=[("EUR", "USD", "2024-06-01", 1.2)],
        currencies={ISIN_USD: "USD"},
        names={ISIN_USD: "Dollar Fund"},
        ters={ISIN_USD: 0.30},
    )
    code = portfolio_mod.main(_pargs(db, config, meta, "--show-cost-basis"))
    out = capsys.readouterr().out
    assert code == 0
    assert "1000.00" in out                 # Value€ = FX-converted EUR
    assert "~€3.00/yr in fees" in out        # 0.30% of €1000
    assert "0.300% weighted avg TER" in out


def test_main_portfolio_fee_on_market_value_not_cost(tmp_path, capsys):
    # Cost basis €1000, market value €2000: the fee must weight by market value.
    db, config, meta = _seed_valued(
        tmp_path,
        transactions=[_vbuy("t1", "2024-01-01", ISIN_ETF, 100.0, 10.0)],  # cost 1000
        prices=[(ISIN_ETF, "2024-06-01", 20.0)],                          # MktVal 2000
        currencies={ISIN_ETF: "EUR"},
        names={ISIN_ETF: "Euro Fund"},
        ters={ISIN_ETF: 0.50},
    )
    code = portfolio_mod.main(_pargs(db, config, meta, "--show-cost-basis"))
    out = capsys.readouterr().out
    assert code == 0
    assert "€2000.00 market value" in out
    assert "~€10.00/yr in fees" in out   # 0.50% of €2000, not €5 on cost
    assert "/yr in fees" in out and "~€5.00/yr" not in out


def test_main_portfolio_weighted_ter_is_market_value_weighted(tmp_path, capsys):
    # value-weighted: (0.5*3000 + 0.1*1000)/4000 = 0.400%; cost-weighted would be 0.300%.
    db, config, meta = _seed_valued(
        tmp_path,
        transactions=[
            _vbuy("t1", "2024-01-01", ISIN_ETF, 100.0, 10.0),     # cost 1000
            _vbuy("t2", "2024-01-01", ISIN_SECOND, 100.0, 10.0),  # cost 1000
        ],
        prices=[
            (ISIN_ETF, "2024-06-01", 30.0),     # MktVal 3000
            (ISIN_SECOND, "2024-06-01", 10.0),  # MktVal 1000
        ],
        currencies={ISIN_ETF: "EUR", ISIN_SECOND: "EUR"},
        names={ISIN_ETF: "Euro Fund", ISIN_SECOND: "Second Fund"},
        ters={ISIN_ETF: 0.50, ISIN_SECOND: 0.10},
    )
    portfolio_mod.main(_pargs(db, config, meta))
    out = capsys.readouterr().out
    assert "0.400% weighted avg TER" in out
    assert "0.300%" not in out


def test_main_portfolio_excluded_when_no_price(tmp_path, capsys):
    db, config, meta = _seed_valued(
        tmp_path,
        transactions=[_vbuy("t1", "2024-01-01", ISIN_ETF, 10.0, 10.0)],
        prices=[],  # unfetched
        currencies={ISIN_ETF: "EUR"},
        names={ISIN_ETF: "Euro Fund"},
        ters={ISIN_ETF: 0.20},
    )
    code = portfolio_mod.main(_pargs(db, config, meta, "--show-cost-basis"))
    out = capsys.readouterr().out
    assert code == 0
    assert "⚠ excluded from market value / fee / weighted TER" in out
    assert ISIN_ETF in out
    assert "weighted avg TER" not in out  # no market value → cannot weight
