"""Tests for config subcommands."""

import os
import sqlite3
from contextlib import closing

import pandas as pd
import pytest
import yaml

import e1f.config as config_cmd
from e1f.common import OpenFIGIResolver

ISIN_A = "AA0000000001"
ISIN_B = "BB0000000002"
ISIN_C = "CC0000000003"

RESOLVED = {
    "name": "Test ETF",
    "tickers": ["TST"],
    "exchange": "NA",
    "figi": "F",
}


def etf(isin):
    return {
        "name": f"ETF {isin}",
        "tickers": ["T"],
        "exchange": "NA",
        "figi": "F",
        "asset_class": "Equity",
    }


@pytest.fixture
def paths(tmp_path):
    return {
        "config": str(tmp_path / "universe.yaml"),
        "db": str(tmp_path / "prices.db"),
        "meta": str(tmp_path / "currency.yaml"),
    }


def write_config(path, isins):
    with open(path, "w") as f:
        yaml.dump({"etfs": {isin: etf(isin) for isin in isins}}, f)


def write_db(path, isin_closes):
    with closing(sqlite3.connect(path)) as conn:
        conn.execute(
            "CREATE TABLE prices (isin TEXT, date TEXT, close REAL, "
            "PRIMARY KEY (isin, date))"
        )
        for isin, closes in isin_closes.items():
            dates = pd.bdate_range(end="2026-08-12", periods=len(closes))
            conn.executemany(
                "INSERT INTO prices VALUES (?, ?, ?)",
                [
                    (isin, date.strftime("%Y-%m-%d %H:%M:%S"), float(close))
                    for date, close in zip(dates, closes, strict=False)
                ],
            )
        conn.commit()


def write_meta(path, isins):
    with open(path, "w") as f:
        yaml.dump(
            {
                isin: {
                    "xid": "x",
                    "symbol": "T:LSE:USD",
                    "currency": "USD",
                }
                for isin in isins
            },
            f,
        )


def read_config_isins(path):
    with open(path) as f:
        return set(yaml.safe_load(f)["etfs"])


def read_db_isins(path):
    with closing(sqlite3.connect(path)) as conn:
        return {row[0] for row in conn.execute("SELECT DISTINCT isin FROM prices")}


def mock_resolver(monkeypatch, result=RESOLVED):
    monkeypatch.setattr(OpenFIGIResolver, "resolve", lambda self, isin: result)
    monkeypatch.setattr("e1f.common.enrich_fund_metadata", lambda isin, info: info)


def test_list_empty(paths, capsys):
    write_config(paths["config"], [])
    assert config_cmd.main(["--config", paths["config"], "list"]) == 0
    assert "No ETFs in configuration" in capsys.readouterr().out


def test_list_shows_all_etfs(paths, capsys):
    write_config(paths["config"], [ISIN_A, ISIN_B])
    assert config_cmd.main(["--config", paths["config"], "list"]) == 0
    out = capsys.readouterr().out
    assert ISIN_A in out and ISIN_B in out and "Total: 2 ETFs" in out
    assert "Asset class" in out
    assert "Equity" in out


def test_add_and_update(paths, monkeypatch, capsys):
    write_config(paths["config"], [])
    mock_resolver(monkeypatch)
    assert (
        config_cmd.main(["--config", paths["config"], "add", ISIN_A, ISIN_B]) == 0
    )
    assert read_config_isins(paths["config"]) == {ISIN_A, ISIN_B}

    assert config_cmd.main(["--config", paths["config"], "update", ISIN_A]) == 0
    assert config_cmd.main(["--config", paths["config"], "update", ISIN_C]) == 1


def test_update_without_isins_updates_all(paths, monkeypatch, capsys):
    write_config(paths["config"], [ISIN_A, ISIN_B])
    mock_resolver(monkeypatch)

    assert config_cmd.main(["--config", paths["config"], "update"]) == 0
    assert "✓ Updated 2/2 ETFs" in capsys.readouterr().out


def test_update_without_isins_on_empty_config(paths, capsys):
    write_config(paths["config"], [])
    assert config_cmd.main(["--config", paths["config"], "update"]) == 0
    assert "No ETFs in configuration" in capsys.readouterr().out


def test_add_partial_failure_returns_1(paths, monkeypatch):
    write_config(paths["config"], [])
    monkeypatch.setattr(
        OpenFIGIResolver,
        "resolve",
        lambda self, isin: RESOLVED if isin == ISIN_A else None,
    )
    assert (
        config_cmd.main(["--config", paths["config"], "add", ISIN_A, ISIN_B]) == 1
    )
    assert read_config_isins(paths["config"]) == {ISIN_A}


def test_remove_deletes_everywhere(paths, capsys):
    write_config(paths["config"], [ISIN_A, ISIN_B])
    write_db(paths["db"], {ISIN_A: [100, 101], ISIN_B: [100, 101]})
    write_meta(paths["meta"], [ISIN_A, ISIN_B])

    rc = config_cmd.main(
        [
            "--config",
            paths["config"],
            "remove",
            ISIN_A,
            ISIN_C,
            "--db",
            paths["db"],
            "--currency-meta",
            paths["meta"],
        ]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert f"{ISIN_C}: not found in any file" in out
    assert read_config_isins(paths["config"]) == {ISIN_B}
    assert read_db_isins(paths["db"]) == {ISIN_B}
    with open(paths["meta"]) as f:
        assert set(yaml.safe_load(f)) == {ISIN_B}


def test_remove_without_db(paths):
    write_config(paths["config"], [ISIN_A])
    write_meta(paths["meta"], [])
    rc = config_cmd.main(
        [
            "--config",
            paths["config"],
            "remove",
            ISIN_A,
            "--db",
            paths["db"],
            "--currency-meta",
            paths["meta"],
        ]
    )
    assert rc == 0
    assert read_config_isins(paths["config"]) == set()
    assert not os.path.exists(paths["db"])


def test_trim_keeps_intersection(paths, capsys):
    write_config(paths["config"], [ISIN_A, ISIN_B])
    write_db(paths["db"], {ISIN_B: [100], ISIN_C: [100]})
    write_meta(paths["meta"], [ISIN_B, ISIN_C])

    rc = config_cmd.main(
        [
            "--config",
            paths["config"],
            "trim",
            "--db",
            paths["db"],
            "--currency-meta",
            paths["meta"],
        ]
    )
    assert rc == 0
    assert read_config_isins(paths["config"]) == {ISIN_B}
    assert read_db_isins(paths["db"]) == {ISIN_B}
    with open(paths["meta"]) as f:
        assert set(yaml.safe_load(f)) == {ISIN_B}


def test_trim_in_sync_is_noop(paths, capsys):
    write_config(paths["config"], [ISIN_A])
    write_db(paths["db"], {ISIN_A: [100]})
    write_meta(paths["meta"], [ISIN_A])
    rc = config_cmd.main(
        [
            "--config",
            paths["config"],
            "trim",
            "--db",
            paths["db"],
            "--currency-meta",
            paths["meta"],
        ]
    )
    assert rc == 0
    assert "Nothing to trim" in capsys.readouterr().out


def test_trim_refuses_without_db(paths, capsys):
    write_config(paths["config"], [ISIN_A])
    write_meta(paths["meta"], [ISIN_A])
    rc = config_cmd.main(
        [
            "--config",
            paths["config"],
            "trim",
            "--db",
            paths["db"],
            "--currency-meta",
            paths["meta"],
        ]
    )
    assert rc == 1
    assert "refusing to trim" in capsys.readouterr().out
    assert read_config_isins(paths["config"]) == {ISIN_A}


def test_no_subcommand_prints_help(paths, capsys):
    write_config(paths["config"], [])
    assert config_cmd.main(["--config", paths["config"]]) == 1
    assert "usage" in capsys.readouterr().out.lower()
