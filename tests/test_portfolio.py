"""Portfolio holdings: average-cost positions from transactions."""

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
)
from e1f.transactions import BROKER_TRADE_REPUBLIC

ISIN_ETF = "IE00B4L5Y983"
BROKER_OTHER = "other_broker"


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
    assert yearly_fee_est(0.20, 1000.0) == pytest.approx(2.0)
    assert yearly_fee_est(None, 1000.0) is None
    assert yearly_fee_est(0.20, 0.0) is None


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
    )
    assert [h.symbol for h in sorted_holdings] == ["BBB", "AAA", "CCC"]


def test_main_portfolio_sort_by_total(tmp_path, capsys):
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
        ["--db", str(db), "--config", str(config), "--sort", "total", "--reverse"],
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
        ["--db", str(db), "--config", str(config), "--show-cost-basis"]
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

    from contextlib import closing
    import sqlite3
    from e1f.transactions import BROKER_TRADE_REPUBLIC, init_transactions_database

    init_transactions_database(str(db))
    with closing(sqlite3.connect(db)) as conn:
        conn.execute(
            "INSERT INTO transactions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (BROKER_TRADE_REPUBLIC, "1", "2024-01-01", ISIN_ETF, "BUY", 1.0, 1000.0, 0.0, 0.0),
        )
        conn.commit()

    # Fee/yr is only shown with --show-cost-basis
    code = portfolio_mod.main(["--db", str(db), "--config", str(config)])
    out = capsys.readouterr().out
    assert code == 0
    assert "Fee/yr" not in out

    code = portfolio_mod.main(["--db", str(db), "--config", str(config), "--show-cost-basis"])
    out = capsys.readouterr().out
    assert code == 0
    assert "Fee/yr" in out
    assert "€2.00" in out          # 0.20% of €1000 = €2.00
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
