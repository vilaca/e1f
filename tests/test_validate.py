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


def test_validate_venue_mapping_excludes_reserved_fx_pairs(paths, monkeypatch):
    write_config(paths["config"], [ISIN_A, ISIN_B])
    write_db(
        paths["db"],
        {
            ISIN_A: good_prices(ISIN_A, seed=1),
            ISIN_B: good_prices(ISIN_B, seed=2),
        },
    )
    write_meta(paths["meta"], [ISIN_A, ISIN_B])
    with open(paths["meta"]) as stream:
        metadata = yaml.safe_load(stream)
    metadata["fx_pairs"] = {"EURUSD": {"xid": "fx", "symbol": "EURUSD"}}
    with open(paths["meta"], "w") as stream:
        yaml.dump(metadata, stream)
    seen_venues = {}

    def capture_venues(_price_df, venues):
        seen_venues.update(venues)
        return {}

    monkeypatch.setattr(validate_cmd, "consensus_gaps", capture_venues)
    rc = validate_cmd.main(
        [
            "--config",
            paths["config"],
            "--db",
            paths["db"],
            "--currency-meta",
            paths["meta"],
        ]
    )

    assert rc == 0
    assert seen_venues == {ISIN_A: "LSE", ISIN_B: "LSE"}


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


# ---------------------------------------------------------------------------
# Interior single-day gaps (consensus_gaps), voted within an exchange venue
# ---------------------------------------------------------------------------

def _long_prices(rows):
    return pd.DataFrame(rows, columns=["isin", "date", "close"])


def test_consensus_gaps_flags_within_venue_hole():
    # 5 LSE funds over 5 days; E1 misses day 3 that its peers have → flagged.
    days = ["2024-01-01", "2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05"]
    rows = [
        (isin, d, 100.0)
        for isin in ["E1", "E2", "E3", "E4", "E5"]
        for d in days
        if not (isin == "E1" and d == "2024-01-03")
    ]
    venues = {i: "LSE" for i in ["E1", "E2", "E3", "E4", "E5"]}
    assert validate_cmd.consensus_gaps(_long_prices(rows), venues) == {"E1": ["2024-01-03"]}


def test_consensus_gaps_cross_venue_holiday_not_flagged():
    # GER funds trade 2024-01-03; every LSE fund is closed that day (a UK holiday).
    # Voting per venue, no LSE fund is flagged — this is the false-positive guard.
    all_days = ["2024-01-01", "2024-01-02", "2024-01-03", "2024-01-04"]
    lse_days = ["2024-01-01", "2024-01-02", "2024-01-04"]  # LSE misses 01-03
    rows = [(i, d, 100.0) for i in ["G1", "G2", "G3"] for d in all_days]
    rows += [(i, d, 100.0) for i in ["L1", "L2", "L3"] for d in lse_days]
    venues = {**{i: "GER" for i in ["G1", "G2", "G3"]},
              **{i: "LSE" for i in ["L1", "L2", "L3"]}}
    assert validate_cmd.consensus_gaps(_long_prices(rows), venues) == {}


def test_consensus_gaps_thin_venue_cannot_vote():
    # Only two funds on the venue → below MIN_COVERING → no gap established.
    rows = [("A", d, 100.0) for d in ["2024-01-01", "2024-01-02", "2024-01-03"]]
    rows += [("B", "2024-01-01", 100.0), ("B", "2024-01-03", 100.0)]  # B misses 01-02
    assert validate_cmd.consensus_gaps(_long_prices(rows), {"A": "PAR", "B": "PAR"}) == {}


def test_consensus_gaps_no_venue_metadata_flags_nothing():
    rows = [
        (isin, d, 100.0)
        for isin in ["E1", "E2", "E3", "E4", "E5"]
        for d in ["2024-01-01", "2024-01-02", "2024-01-03"]
        if not (isin == "E1" and d == "2024-01-02")
    ]
    assert validate_cmd.consensus_gaps(_long_prices(rows), {}) == {}  # no venues → skip


def test_consensus_gaps_empty_frame():
    assert validate_cmd.consensus_gaps(
        pd.DataFrame(columns=["isin", "date", "close"]), {}
    ) == {}


def test_validate_reports_interior_gap_warning(paths, capsys, monkeypatch):
    isins = [f"II000000000{i}" for i in range(5)]
    write_config(paths["config"], isins)
    write_meta(paths["meta"], isins)  # all pinned on LSE (T:LSE:USD)
    monkeypatch.setattr(validate_cmd, "DEFAULT_CURRENCY_META", paths["meta"])
    days = pd.bdate_range(end="2026-08-12", periods=8).strftime("%Y-%m-%d").tolist()
    with closing(sqlite3.connect(paths["db"])) as conn:
        conn.execute(
            "CREATE TABLE prices (isin TEXT, date TEXT, close REAL, PRIMARY KEY (isin, date))"
        )
        for isin in isins:
            for j, d in enumerate(days):
                if isin == isins[0] and j == 4:
                    continue  # the interior hole
                conn.execute("INSERT INTO prices VALUES (?, ?, ?)", (isin, d, 100.0 + j))
        conn.commit()

    rc = validate_cmd.main(
        ["--config", paths["config"], "--db", paths["db"], "--currency-meta", paths["meta"]]
    )
    out = capsys.readouterr().out
    assert rc == 0  # a warning, never fatal
    assert "Interior gaps" in out
    assert isins[0] in out and days[4] in out
