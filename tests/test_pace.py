"""Pace: how far behind this month's deposits are (ADR-0063)."""

import sqlite3
from contextlib import closing

from e1f import pace
from e1f.common import Status

ISIN = "IE00EUR000001"


def _db(tmp_path, rows):
    db = tmp_path / "e1f.db"
    with closing(sqlite3.connect(str(db))) as conn:
        conn.execute(
            "CREATE TABLE transactions (broker TEXT, transaction_id TEXT, datetime TEXT, "
            "symbol TEXT, side TEXT, shares REAL, price REAL, fee REAL, tax REAL, "
            "PRIMARY KEY (broker, transaction_id))"
        )
        conn.executemany(
            "INSERT INTO transactions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [("tr", f"t{i}", day, ISIN, side, sh, px, fee, 0.0)
             for i, (day, side, sh, px, fee) in enumerate(rows)],
        )
        conn.commit()
    return str(db)


def test_monthly_deposits_sums_buys_with_fee_and_skips_sells_and_future(tmp_path):
    db = _db(tmp_path, [
        ("2026-09-03 10:00:00", "BUY", 10.0, 100.0, 1.0),  # 1001
        ("2026-09-20 10:00:00", "SAVINGS_PLAN", 5.0, 100.0, 0.0),  # 500
        ("2026-09-21 10:00:00", "SELL", 5.0, 100.0, 0.0),
        ("2026-11-20 10:00:00", "BUY", 1.0, 100.0, 0.0),  # after as-of
    ])
    assert pace.monthly_deposits(db, "2026-11-16") == {"2026-09": 1501.0}


def test_behind_is_mean_of_previous_months_minus_current_hand_computed():
    totals = {"2026-08": 1000.0, "2026-09": 2000.0, "2026-10": 1500.0, "2026-11": 400.0}
    result = pace.compute_pace(totals, "2026-11-16", months=3)
    assert result.status is Status.CALCULATED
    assert result.reference == 1500.0
    assert result.behind == 1100.0
    assert [k for k, _ in result.previous] == ["2026-08", "2026-09", "2026-10"]


def test_skipped_month_counts_as_zero():
    totals = {"2026-08": 1500.0, "2026-10": 1500.0}
    result = pace.compute_pace(totals, "2026-11-16", months=3)
    assert result.reference == 1000.0
    assert result.behind == 1000.0


def test_ahead_is_negative():
    result = pace.compute_pace({"2026-10": 1000.0, "2026-11": 1300.0}, "2026-11-16", months=1)
    assert result.behind == -300.0


def test_short_history_is_bounded_and_disclosed(capsys):
    result = pace.compute_pace({"2026-10": 1000.0}, "2026-11-16", months=6)
    assert result.status is Status.BOUNDED
    assert result.reason == pace.REASON_SHORT_HISTORY
    assert result.months_used == 1
    assert "only 1 previous month(s)" in "\n".join(pace.render(result, "2026-11-16"))


def test_no_previous_months_is_unavailable_and_discloses():
    result = pace.compute_pace({"2026-11": 500.0}, "2026-11-16")
    assert result.status is Status.UNAVAILABLE
    assert result.reason == pace.REASON_NO_HISTORY
    assert result.behind is None
    assert "Pace unavailable" in "\n".join(pace.render(result, "2026-11-16"))


def test_target_overrides_history():
    result = pace.compute_pace({"2026-11": 500.0}, "2026-11-16", target=1500.0)
    assert result.status is Status.CALCULATED
    assert result.behind == 1000.0
    assert "--target" in "\n".join(pace.render(result, "2026-11-16"))


def test_year_boundary_lookback():
    totals = {"2025-12": 1000.0, "2026-01": 2000.0}
    result = pace.compute_pace(totals, "2026-02-10", months=2)
    assert result.reference == 1500.0


def test_main_end_to_end(tmp_path, capsys):
    db = _db(tmp_path, [
        ("2026-10-05 10:00:00", "BUY", 10.0, 100.0, 0.0),
        ("2026-11-05 10:00:00", "BUY", 4.0, 100.0, 0.0),
    ])
    assert pace.main(["--db", db, "--as-of", "2026-11-16", "--months", "1"]) == 0
    out = capsys.readouterr().out
    assert "Behind by €600.00" in out


def test_main_rejects_bad_args(tmp_path, capsys):
    db = _db(tmp_path, [])
    assert pace.main(["--db", db, "--as-of", "nope"]) == 1
    assert pace.main(["--db", db, "--months", "0"]) == 1
    assert pace.main(["--db", db, "--target", "-5"]) == 1
    capsys.readouterr()


def test_year_total_and_deviation_rendered():
    totals = {"2025-12": 900.0, "2026-01": 1000.0, "2026-02": 2000.0, "2026-03": 500.0}
    result = pace.compute_pace(totals, "2026-03-10", months=2)
    assert result.year_total == 3500.0
    out = "\n".join(pace.render(result, "2026-03-10"))
    assert "Year to date (2026): €3,500.00" in out
    assert "-500.00" in out and "+500.00" in out
