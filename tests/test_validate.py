"""Tests for the top-level validate command."""

import sqlite3
from contextlib import closing

import numpy as np
import pandas as pd
import pytest
import yaml

import e1f.validate as validate_cmd

ISIN_A = "AA0000000001"
ISIN_B = "BB0000000002"


def etf(isin):
    return {"name": f"ETF {isin}", "tickers": ["T"], "exchange": "NA", "figi": "F"}


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
    """Write close prices on recent consecutive business days."""
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


def good_prices(isin, n=1100, seed=0):
    rng = np.random.default_rng(seed)
    return list(100 * np.exp(np.cumsum(rng.normal(0, 0.01, n))))


def test_validate_healthy(paths, capsys):
    write_config(paths["config"], [ISIN_A, ISIN_B])
    write_db(
        paths["db"],
        {
            ISIN_A: good_prices(ISIN_A, seed=1),
            ISIN_B: good_prices(ISIN_B, seed=2),
        },
    )
    rc = validate_cmd.main(["--config", paths["config"], "--db", paths["db"]])
    assert rc == 0
    out = capsys.readouterr().out
    assert "=== Data Integrity ===" in out
    assert "Duplicate keys:       0" in out
    assert "Null closes:          0" in out
    assert "Non-positive closes:  0" in out
    assert "Weekend rows:         0" in out
    assert "config and DB in sync" in out
    assert "None — all ETFs look good" in out


def test_validate_rejects_malformed_quote_currency(paths, monkeypatch, capsys):
    write_config(paths["config"], [ISIN_A, ISIN_B])
    write_db(
        paths["db"],
        {
            ISIN_A: good_prices(ISIN_A, seed=1),
            ISIN_B: good_prices(ISIN_B, seed=2),
        },
    )
    write_meta(paths["meta"], [ISIN_A, ISIN_B])
    with open(paths["meta"]) as f:
        metadata = yaml.safe_load(f)
    metadata[ISIN_B] = {"xid": "x", "symbol": "T:MUN", "currency": "MUN"}
    with open(paths["meta"], "w") as f:
        yaml.dump(metadata, f)
    monkeypatch.setattr(validate_cmd, "DEFAULT_CURRENCY_META", paths["meta"])

    rc = validate_cmd.main(["--config", paths["config"], "--db", paths["db"]])

    assert rc == 1
    out = capsys.readouterr().out
    assert f"{ISIN_B}: MUN" in out
    assert "Validation failed" in out


def test_quality_report_counts_missing_business_days():
    prices = pd.DataFrame(
        {
            "isin": [ISIN_A, ISIN_A, ISIN_B, ISIN_B],
            "date": ["2025-12-23", "2025-12-29", "2026-08-11", "2026-08-12"],
            "close": [100.0, 101.0, 50.0, 51.0],
        }
    )

    report = validate_cmd.quality_report(prices)

    assert report["max_missing_business_days"] == 3
    assert report["missing_business_days_by_isin"] == {ISIN_A: 3, ISIN_B: 0}


def test_quality_report_missing_business_days_with_weekend_endpoint():
    prices = pd.DataFrame(
        {
            "isin": [ISIN_A, ISIN_A],
            "date": ["2026-08-01", "2026-08-04"],
            "close": [100.0, 101.0],
        }
    )

    report = validate_cmd.quality_report(prices)

    assert report["max_missing_business_days"] == 1
    assert report["missing_business_days_by_isin"] == {ISIN_A: 1}


def test_quality_report_handles_invalid_and_nat_dates():
    prices = pd.DataFrame(
        {
            "isin": [ISIN_A, ISIN_A, ISIN_A],
            "date": ["2026-08-10", None, "not-a-date"],
            "close": [100.0, 101.0, 102.0],
        }
    )

    report = validate_cmd.quality_report(prices)

    assert report["rows"] == 3
    assert report["invalid_dates"] == 2
    assert report["duplicates"] == 0
    assert report["duplicate_isins"] == []
    assert report["max_missing_business_days"] == 0
    assert report["missing_business_days_by_isin"] == {ISIN_A: 0}


def test_quality_report_all_invalid_dates_no_phantom_duplicates():
    prices = pd.DataFrame(
        {
            "isin": [ISIN_A, ISIN_A, ISIN_A],
            "date": ["junk1", "junk2", "junk3"],
            "close": [1.0, 2.0, 3.0],
        }
    )

    report = validate_cmd.quality_report(prices)

    assert report["invalid_dates"] == 3
    assert report["duplicates"] == 0


def test_validate_reports_price_integrity_issues(paths, capsys):
    write_config(paths["config"], [ISIN_A])
    write_db(paths["db"], {ISIN_A: [100, -5, 200]})
    with closing(sqlite3.connect(paths["db"])) as conn:
        conn.execute(
            "INSERT INTO prices VALUES (?, ?, ?)",
            (ISIN_A, "2026-08-29 00:00:00", None),
        )
        conn.commit()

    rc = validate_cmd.main(["--config", paths["config"], "--db", paths["db"]])

    assert rc == 1
    out = capsys.readouterr().out
    assert "Null closes:          1" in out
    assert "Non-positive closes:  1" in out
    weekend_line = next(line for line in out.splitlines() if "Weekend rows:" in line)
    assert f"1 [{ISIN_A}]" in weekend_line
    assert f"      12 days  {ISIN_A}  ETF {ISIN_A}" in out
    price_change_line = next(
        line for line in out.splitlines() if "Largest price change:" in line
    )
    assert f"[{ISIN_A}]" in price_change_line
    assert "Validation failed" in out
    assert "None — all ETFs look good" not in out


def test_validate_exits_1_on_duplicate_keys_without_crashing(paths, capsys):
    write_config(paths["config"], [ISIN_A])
    write_db(paths["db"], {ISIN_A: good_prices(ISIN_A, seed=1)})
    with closing(sqlite3.connect(paths["db"])) as conn:
        dupe_date = conn.execute(
            "SELECT date FROM prices WHERE isin = ? LIMIT 1", (ISIN_A,)
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO prices VALUES (?, ?, ?)",
            (ISIN_A, dupe_date[:10], 123.0),
        )
        conn.commit()

    rc = validate_cmd.main(["--config", paths["config"], "--db", paths["db"]])

    assert rc == 1
    out = capsys.readouterr().out
    assert f"Duplicate keys:       1 [{ISIN_A}]" in out
    assert "Invalid dates:        0" in out
    assert "=== History Breakdown ===" in out
    assert "Validation failed" in out


def test_validate_reports_invalid_dates_as_error(paths, capsys):
    write_config(paths["config"], [ISIN_A])
    write_db(paths["db"], {ISIN_A: good_prices(ISIN_A, seed=1)})
    with closing(sqlite3.connect(paths["db"])) as conn:
        conn.execute("INSERT INTO prices VALUES (?, ?, ?)", (ISIN_A, None, 100.0))
        conn.commit()

    rc = validate_cmd.main(["--config", paths["config"], "--db", paths["db"]])

    assert rc == 1
    out = capsys.readouterr().out
    assert f"Invalid dates:        1 [{ISIN_A}]" in out
    assert "Validation failed" in out


def test_validate_reports_config_isins_missing_from_db(paths, capsys):
    write_config(paths["config"], [ISIN_A, ISIN_B])
    write_db(paths["db"], {ISIN_A: good_prices(ISIN_A, seed=1)})

    rc = validate_cmd.main(["--config", paths["config"], "--db", paths["db"]])

    assert rc == 1
    out = capsys.readouterr().out
    assert "In config, missing from DB" in out and ISIN_B in out


def test_validate_empty_history_prints_none_not_bare_header(paths, capsys):
    write_config(paths["config"], [ISIN_A])
    write_db(paths["db"], {ISIN_A: []})

    rc = validate_cmd.main(["--config", paths["config"], "--db", paths["db"]])

    out = capsys.readouterr().out
    assert "=== History Breakdown ===" in out
    assert "no dated price history" in out
    assert rc == 1


def test_validate_flags_orphans_and_short_history(paths, capsys):
    write_config(paths["config"], [ISIN_A])
    write_db(
        paths["db"],
        {
            ISIN_A: good_prices(ISIN_A, n=50),
            ISIN_B: good_prices(ISIN_B, seed=3),
        },
    )
    rc = validate_cmd.main(["--config", paths["config"], "--db", paths["db"]])
    assert rc == 1
    out = capsys.readouterr().out
    assert "orphans" in out and ISIN_B in out
    assert "Short history" in out and ISIN_A in out


def test_validate_warning_only_returns_success(paths, capsys):
    write_config(paths["config"], [ISIN_A])
    write_db(paths["db"], {ISIN_A: good_prices(ISIN_A, n=50)})

    rc = validate_cmd.main(["--config", paths["config"], "--db", paths["db"]])

    assert rc == 0
    assert "Validation passed with warnings" in capsys.readouterr().out


def test_validate_integrity_warning_without_flags_is_not_contradictory(paths, capsys):
    closes = good_prices(ISIN_A, seed=1)
    closes[500] *= 2
    write_config(paths["config"], [ISIN_A])
    write_db(paths["db"], {ISIN_A: closes})

    rc = validate_cmd.main(["--config", paths["config"], "--db", paths["db"]])

    assert rc == 0
    out = capsys.readouterr().out
    assert "Validation passed with warnings" in out
    assert "None here — see Data Integrity warnings above." in out
    assert "\n  None.\n" not in out


def test_validate_without_db(paths, capsys):
    write_config(paths["config"], [ISIN_A])
    rc = validate_cmd.main(["--config", paths["config"], "--db", paths["db"]])
    assert rc == 1
    assert "run 'e1f fetch' first" in capsys.readouterr().out
