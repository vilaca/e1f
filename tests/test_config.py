"""Tests for config subcommands."""

import os
import sqlite3
from contextlib import closing

import pandas as pd
import pytest
import yaml

import e1f.config as config_cmd
from e1f.common import CurrencyMetadata, OpenFIGIResolver

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


def write_trade(path, isin, *, side="BUY", shares=1.0):
    with closing(sqlite3.connect(path)) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS transactions ("
            "transaction_id TEXT PRIMARY KEY, datetime TEXT, symbol TEXT, "
            "side TEXT, shares REAL)"
        )
        conn.execute(
            "INSERT INTO transactions VALUES (?, '2026-01-01', ?, ?, ?)",
            (f"{side}-{isin}", isin, side, shares),
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
    monkeypatch.setattr("e1f.common.universe.enrich_fund_metadata", lambda isin, info: info)


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


def test_remove_refuses_to_strand_live_holding(paths, capsys):
    write_config(paths["config"], [ISIN_A])
    write_db(paths["db"], {ISIN_A: [100]})
    write_trade(paths["db"], ISIN_A)
    write_meta(paths["meta"], [ISIN_A])

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

    assert rc == 1
    assert "Refusing to remove live holding" in capsys.readouterr().out
    assert read_config_isins(paths["config"]) == {ISIN_A}
    assert read_db_isins(paths["db"]) == {ISIN_A}
    with open(paths["meta"]) as stream:
        assert set(yaml.safe_load(stream)) == {ISIN_A}


def test_remove_force_retains_transactions_while_removing_valuation_data(paths, capsys):
    write_config(paths["config"], [ISIN_A])
    write_db(paths["db"], {ISIN_A: [100]})
    write_trade(paths["db"], ISIN_A)
    write_meta(paths["meta"], [ISIN_A])

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
            "--force",
        ]
    )

    assert rc == 0
    assert "forcing removal of live holding" in capsys.readouterr().out
    assert read_config_isins(paths["config"]) == set()
    assert read_db_isins(paths["db"]) == set()
    with closing(sqlite3.connect(paths["db"])) as conn:
        symbols = {row[0] for row in conn.execute("SELECT symbol FROM transactions")}
    assert symbols == {ISIN_A}


def test_remove_allows_fully_closed_position(paths):
    write_config(paths["config"], [ISIN_A])
    write_db(paths["db"], {ISIN_A: [100]})
    write_trade(paths["db"], ISIN_A, shares=2.0)
    write_trade(paths["db"], ISIN_A, side="SELL", shares=2.0)
    write_meta(paths["meta"], [ISIN_A])

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
    assert read_db_isins(paths["db"]) == set()


def test_remove_live_holding_refuses_entire_batch(paths):
    write_config(paths["config"], [ISIN_A, ISIN_B])
    write_db(paths["db"], {ISIN_A: [100], ISIN_B: [200]})
    write_trade(paths["db"], ISIN_A)
    write_meta(paths["meta"], [ISIN_A, ISIN_B])

    rc = config_cmd.main(
        [
            "--config",
            paths["config"],
            "remove",
            ISIN_A,
            ISIN_B,
            "--db",
            paths["db"],
            "--currency-meta",
            paths["meta"],
        ]
    )

    assert rc == 1
    assert read_config_isins(paths["config"]) == {ISIN_A, ISIN_B}
    assert read_db_isins(paths["db"]) == {ISIN_A, ISIN_B}


def test_remove_rolls_back_all_stores_when_metadata_write_fails(paths, monkeypatch):
    write_config(paths["config"], [ISIN_A])
    write_db(paths["db"], {ISIN_A: [100]})
    write_meta(paths["meta"], [ISIN_A])
    original_save = CurrencyMetadata.save
    calls = 0

    def fail_first_save(self, path):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("simulated metadata write failure")
        return original_save(self, path)

    monkeypatch.setattr(CurrencyMetadata, "save", fail_first_save)
    with pytest.raises(OSError, match="simulated metadata write failure"):
        config_cmd.main(
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

    assert read_config_isins(paths["config"]) == {ISIN_A}
    assert read_db_isins(paths["db"]) == {ISIN_A}
    with open(paths["meta"]) as stream:
        assert set(yaml.safe_load(stream)) == {ISIN_A}


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


def test_trim_refuses_entire_batch_when_it_would_strand_live_holding(paths, capsys):
    write_config(paths["config"], [ISIN_A, ISIN_B])
    write_db(paths["db"], {ISIN_A: [100], ISIN_B: [200], ISIN_C: [300]})
    write_trade(paths["db"], ISIN_A)
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

    assert rc == 1
    assert "Refusing to trim live holding" in capsys.readouterr().out
    assert read_config_isins(paths["config"]) == {ISIN_A, ISIN_B}
    assert read_db_isins(paths["db"]) == {ISIN_A, ISIN_B, ISIN_C}
    with open(paths["meta"]) as stream:
        assert set(yaml.safe_load(stream)) == {ISIN_B, ISIN_C}


def test_trim_force_retains_transactions_while_removing_valuation_data(paths, capsys):
    write_config(paths["config"], [ISIN_A])
    write_db(paths["db"], {ISIN_A: [100]})
    write_trade(paths["db"], ISIN_A)
    write_meta(paths["meta"], [])

    rc = config_cmd.main(
        [
            "--config",
            paths["config"],
            "trim",
            "--db",
            paths["db"],
            "--currency-meta",
            paths["meta"],
            "--force",
        ]
    )

    assert rc == 0
    assert "forcing trim of live holding" in capsys.readouterr().out
    assert read_config_isins(paths["config"]) == set()
    assert read_db_isins(paths["db"]) == set()
    with closing(sqlite3.connect(paths["db"])) as conn:
        symbols = {row[0] for row in conn.execute("SELECT symbol FROM transactions")}
    assert symbols == {ISIN_A}


def test_trim_allows_fully_closed_position(paths):
    write_config(paths["config"], [ISIN_A])
    write_db(paths["db"], {ISIN_A: [100]})
    write_trade(paths["db"], ISIN_A, shares=2.0)
    write_trade(paths["db"], ISIN_A, side="SELL", shares=2.0)
    write_meta(paths["meta"], [])

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
    assert read_config_isins(paths["config"]) == set()
    assert read_db_isins(paths["db"]) == set()


def test_trim_preserves_needed_fx_pair_metadata(paths):
    write_config(paths["config"], [ISIN_A])
    write_db(paths["db"], {ISIN_A: [100]})
    write_meta(paths["meta"], [ISIN_A])
    with open(paths["meta"]) as f:
        metadata = yaml.safe_load(f)
    metadata["fx_pairs"] = {"EURUSD": {"xid": "usd", "symbol": "EURUSD"}}
    with open(paths["meta"], "w") as f:
        yaml.dump(metadata, f)

    assert config_cmd.main(
        [
            "--config",
            paths["config"],
            "trim",
            "--db",
            paths["db"],
            "--currency-meta",
            paths["meta"],
        ]
    ) == 0

    with open(paths["meta"]) as f:
        trimmed = yaml.safe_load(f)
    assert set(trimmed) == {ISIN_A, "fx_pairs"}
    assert trimmed["fx_pairs"] == {"EURUSD": {"xid": "usd", "symbol": "EURUSD"}}


def test_trim_rolls_back_all_stores_when_metadata_write_fails(paths, monkeypatch):
    write_config(paths["config"], [ISIN_A, ISIN_B])
    write_db(paths["db"], {ISIN_B: [100], ISIN_C: [100]})
    write_meta(paths["meta"], [ISIN_B, ISIN_C])
    original_save = CurrencyMetadata.save
    calls = 0

    def fail_first_save(self, path):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("simulated metadata write failure")
        return original_save(self, path)

    monkeypatch.setattr(CurrencyMetadata, "save", fail_first_save)
    with pytest.raises(OSError, match="simulated metadata write failure"):
        config_cmd.main(
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

    assert read_config_isins(paths["config"]) == {ISIN_A, ISIN_B}
    assert read_db_isins(paths["db"]) == {ISIN_B, ISIN_C}
    with open(paths["meta"]) as stream:
        assert set(yaml.safe_load(stream)) == {ISIN_B, ISIN_C}


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


def _add_snapshot_tables(db_path, *, snapshot_fund, alias_raw_name, fx_pairs):
    """Seed the trim-cleanup satellite tables (holdings_snapshot/holding,
    security_alias, fx_rates) so trim's cascade paths are exercised."""
    with closing(sqlite3.connect(db_path)) as conn:
        conn.execute(
            "CREATE TABLE holdings_snapshot (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "fund_id TEXT, as_of TEXT, source TEXT, tier TEXT, retrieved_at TEXT, "
            "reported_holding_count INTEGER)"
        )
        conn.execute(
            "CREATE TABLE holding (snapshot_id INTEGER, dimension TEXT, raw_name TEXT, "
            "normalized_name TEXT, weight REAL, rank INTEGER)"
        )
        conn.execute(
            "CREATE TABLE security_alias (raw_name TEXT PRIMARY KEY, canonical_name TEXT, "
            "canonical_key TEXT, reviewed_at TEXT)"
        )
        conn.execute("CREATE TABLE fx_rates (base TEXT, quote TEXT, date TEXT, rate REAL)")
        cur = conn.execute(
            "INSERT INTO holdings_snapshot (fund_id, as_of, source, tier, retrieved_at) "
            "VALUES (?, 'x', 's', 't', 'r')",
            (snapshot_fund,),
        )
        conn.execute(
            "INSERT INTO holding VALUES (?, 'security', ?, NULL, 1.0, 1)",
            (cur.lastrowid, "KeptName"),
        )
        # An alias that no surviving holding references — orphaned once trim runs.
        conn.execute(
            "INSERT INTO security_alias VALUES (?, NULL, NULL, NULL)", (alias_raw_name,)
        )
        conn.executemany(
            "INSERT INTO fx_rates VALUES (?, ?, '2024-01-01', 1.0)", fx_pairs
        )
        conn.commit()


def test_trim_cascades_snapshot_alias_and_fx(paths, capsys):
    # config={A}, db={A,B}, meta={A}: B is trimmed from the DB, dragging its
    # snapshot/holding rows, the orphaned alias, and the stale GBP fx pair.
    write_config(paths["config"], [ISIN_A])
    write_db(paths["db"], {ISIN_A: [100], ISIN_B: [100]})
    write_meta(paths["meta"], [ISIN_A])  # A gets currency USD
    _add_snapshot_tables(
        paths["db"],
        snapshot_fund=ISIN_B,
        alias_raw_name="OrphanCorp",
        fx_pairs=[("EUR", "USD"), ("EUR", "GBP")],
    )

    rc = config_cmd.main(
        ["--config", paths["config"], "trim",
         "--db", paths["db"], "--currency-meta", paths["meta"]]
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert "Removing from holdings_snapshot" in out
    assert "orphaned alias" in out
    assert "Removing from fx_rates" in out and "EURGBP" in out

    with closing(sqlite3.connect(paths["db"])) as conn:
        assert conn.execute("SELECT COUNT(*) FROM holdings_snapshot").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM holding").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM security_alias").fetchone()[0] == 0
        fx = {(r[0], r[1]) for r in conn.execute("SELECT base, quote FROM fx_rates")}
        assert fx == {("EUR", "USD")}  # USD kept (A holds it), GBP pruned


def test_trim_without_currency_meta_file(paths, capsys):
    # No currency-meta file: trim treats it as empty rather than crashing.
    write_config(paths["config"], [ISIN_A])
    write_db(paths["db"], {ISIN_A: [100]})
    rc = config_cmd.main(
        ["--config", paths["config"], "trim",
         "--db", paths["db"], "--currency-meta", paths["meta"]]
    )
    assert rc == 0
    # A is in config+db but absent from the (empty) meta → trimmed out entirely.
    assert read_config_isins(paths["config"]) == set()


def test_remove_without_currency_meta_file(paths, capsys):
    # remove tolerates a missing currency-meta file (FileNotFoundError fallback).
    write_config(paths["config"], [ISIN_A])
    rc = config_cmd.main(
        ["--config", paths["config"], "remove", ISIN_A,
         "--db", paths["db"], "--currency-meta", paths["meta"]]
    )
    assert rc == 0
    assert read_config_isins(paths["config"]) == set()


def test_no_subcommand_prints_help(paths, capsys):
    write_config(paths["config"], [])
    assert config_cmd.main(["--config", paths["config"]]) == 1
    assert "usage" in capsys.readouterr().out.lower()
