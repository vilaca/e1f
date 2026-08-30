"""Configured-universe candidate table (ADR-0042)."""

import math
import sqlite3
from contextlib import closing
from pathlib import Path

import pandas as pd
import pytest
import yaml

from e1f import funds
from e1f.common import Status, clip_price_frame, consensus_gaps, load_price_frame
from e1f.funds import (
    _dist_matches,
    _distribution_label,
    build_row,
    clip_closes,
    risk_from_returns,
    sort_rows,
)


A = "IE00FUNDA0000"
B = "IE00FUNDB0000"
C = "IE00FUNDC0000"
D = "IE00FUNDD0000"
E = "IE00FUNDE0000"
YOUNG = "IE00YOUNG0000"


def test_clip_closes_respects_from_and_does_not_bridge_before():
    closes = [("2024-01-01", 100.0), ("2024-01-02", 110.0), ("2024-01-03", 99.0)]
    window = clip_closes(closes, start="2024-01-02", as_of="2024-01-03")
    assert window == [("2024-01-02", 110.0), ("2024-01-03", 99.0)]


def test_risk_from_returns_matches_hand_computed_path():
    # closes 100 → 110 → 99  ⇒  r = +10%, −10%
    twr, vol, maxdd = risk_from_returns([0.10, -0.10])
    assert twr == pytest.approx(1.10 * 0.90 - 1.0)
    assert maxdd == pytest.approx(0.99 / 1.10 - 1.0)
    assert vol == pytest.approx(math.sqrt(2) * 0.10 * math.sqrt(252))


def test_risk_from_returns_needs_two_for_vol():
    twr, vol, maxdd = risk_from_returns([0.05])
    assert twr == pytest.approx(0.05)
    assert vol is None
    assert maxdd == pytest.approx(0.0)


def test_build_row_younger_than_from_is_short_not_gappy():
    # First close after --from: From is later, pre-listing days are not Gap.
    row = build_row(
        YOUNG,
        {"name": "Young", "asset_class": "Equity", "distribution": "Accumulating", "ter": 0.07},
        held=False,
        closes=[("2024-06-01", 50.0), ("2024-06-02", 55.0)],
        gap_dates=[],
        start="2020-01-01",
        as_of="2024-06-02",
        currency="EUR",
    )
    assert row.start == "2024-06-01"
    assert row.n == 1
    assert row.gap == 0
    assert row.twr == pytest.approx(0.10)
    assert row.status is Status.CALCULATED


def test_build_row_no_closes_is_unavailable():
    row = build_row(
        A,
        {"name": "Empty"},
        held=False,
        closes=[],
        gap_dates=[],
        start="2020-01-01",
        as_of="2024-01-01",
        currency="EUR",
    )
    assert row.status is Status.UNAVAILABLE
    assert row.start is None and row.n == 0
    assert row.twr is None


def test_clip_price_frame_drops_days_outside_window():
    frame = pd.DataFrame(
        [
            (A, "2019-12-31", 1.0),
            (A, "2020-01-02", 2.0),
            (A, "2021-01-01", 3.0),
        ],
        columns=["isin", "date", "close"],
    )
    clipped = clip_price_frame(frame, start="2020-01-01", as_of="2020-12-31")
    assert list(clipped["date"]) == ["2020-01-02"]


def test_consensus_gaps_younger_fund_pre_listing_days_are_not_gaps():
    # Five LSE funds; YOUNG lists only on the last two days. Peers trade all five.
    days = ["2024-01-01", "2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05"]
    rows = [(isin, d, 100.0) for isin in [A, B, C, D] for d in days]
    rows += [(YOUNG, "2024-01-04", 100.0), (YOUNG, "2024-01-05", 100.0)]
    venues = {isin: "LSE" for isin in [A, B, C, D, YOUNG]}
    frame = pd.DataFrame(rows, columns=["isin", "date", "close"])
    assert YOUNG not in consensus_gaps(frame, venues)


def test_consensus_gaps_interior_hole_after_listing_counts():
    days = ["2024-01-01", "2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05"]
    rows = [
        (isin, d, 100.0)
        for isin in [A, B, C, D, E]
        for d in days
        if not (isin == A and d == "2024-01-03")
    ]
    venues = {isin: "LSE" for isin in [A, B, C, D, E]}
    assert consensus_gaps(
        pd.DataFrame(rows, columns=["isin", "date", "close"]), venues
    ) == {A: ["2024-01-03"]}


# ---------------------------------------------------------------------------
# Command
# ---------------------------------------------------------------------------


def _seed(tmp_path, *, transactions=(), prices, currencies, etfs):
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
        if transactions:
            conn.executemany(
                "INSERT INTO transactions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", transactions
            )
        conn.executemany("INSERT INTO prices VALUES (?, ?, ?)", prices)
        conn.commit()
    config = tmp_path / "config.yaml"
    config.write_text(yaml.dump({"etfs": etfs}))
    meta = tmp_path / "meta.yaml"
    meta.write_text(
        yaml.dump({
            i: {"currency": c, "symbol": f"{i}:LSE:{c}", "xid": "1"}
            for i, c in currencies.items()
        })
    )
    return str(db), str(config), str(meta)


def _args(db, config, meta, *extra):
    return ["--db", db, "--config", config, "--currency-meta", meta, *extra]


def test_cmd_empty_config(tmp_path, capsys):
    db, config, meta = _seed(tmp_path, prices=[], currencies={}, etfs={})
    assert funds.main(_args(db, config, meta)) == 0
    assert "No ETFs in configuration" in capsys.readouterr().out


def test_cmd_from_window_younger_fund_stays_listed(tmp_path, capsys):
    # Old fund: 1 Jan-3 Jan. Young fund: only 3 Jan onward (one return 3->4).
    prices = [
        (A, "2024-01-01", 100.0),
        (A, "2024-01-02", 110.0),
        (A, "2024-01-03", 99.0),
        (A, "2024-01-04", 99.0),
        (YOUNG, "2024-01-03", 50.0),
        (YOUNG, "2024-01-04", 55.0),
    ]
    db, config, meta = _seed(
        tmp_path,
        prices=prices,
        currencies={A: "EUR", YOUNG: "EUR"},
        etfs={
            A: {
                "name": "Old Fund",
                "asset_class": "Equity",
                "distribution": "Accumulating",
                "ter": 0.14,
            },
            YOUNG: {
                "name": "Young Fund",
                "asset_class": "Equity",
                "distribution": "Accumulating",
                "ter": 0.07,
            },
        },
    )
    assert funds.main(_args(db, config, meta, "--from", "2024-01-01", "--as-of", "2024-01-04")) == 0
    out = capsys.readouterr().out
    assert "2024-01-01 → 2024-01-04" in out
    assert "Old Fund" in out and "Young Fund" in out
    old = next(line for line in out.splitlines() if line.startswith(A))
    young = next(line for line in out.splitlines() if line.startswith(YOUNG))
    assert "2024-01-01" in old
    assert "2024-01-03" in young
    assert "0.14%" in old and "0.07%" in young


def test_cmd_held_star_and_unheld_filter(tmp_path, capsys):
    prices = [(A, "2024-01-01", 100.0), (A, "2024-01-02", 101.0),
              (B, "2024-01-01", 50.0), (B, "2024-01-02", 51.0)]
    db, config, meta = _seed(
        tmp_path,
        transactions=[("tr", "t1", "2024-01-01", A, "BUY", 10.0, 100.0, 0.0, 0.0)],
        prices=prices,
        currencies={A: "EUR", B: "EUR"},
        etfs={A: {"name": "Held"}, B: {"name": "Candidate"}},
    )
    assert funds.main(_args(db, config, meta, "--as-of", "2024-01-02")) == 0
    out = capsys.readouterr().out
    assert "Held*" in out
    assert "Candidate" in out and "Candidate*" not in out
    assert "* also a current portfolio holding." in out

    assert funds.main(_args(db, config, meta, "--as-of", "2024-01-02", "--unheld")) == 0
    filtered = capsys.readouterr().out
    assert "Candidate" in filtered
    assert "Held" not in filtered


def test_cmd_from_after_as_of_is_error(tmp_path, capsys):
    db, config, meta = _seed(
        tmp_path, prices=[], currencies={A: "EUR"}, etfs={A: {"name": "X"}},
    )
    assert funds.main(_args(db, config, meta, "--from", "2024-06-01", "--as-of", "2024-01-01")) == 1
    assert "--from" in capsys.readouterr().out


def test_cmd_invalid_from(capsys):
    assert funds.main(["--from", "yesterday"]) == 1
    assert "YYYY-MM-DD" in capsys.readouterr().out


def test_cmd_interior_gap_count_and_explain(tmp_path, capsys):
    days = ["2024-01-01", "2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05"]
    prices = [
        (isin, d, 100.0 + i)
        for i, isin in enumerate([A, B, C, D, E])
        for d in days
        if not (isin == A and d == "2024-01-03")
    ]
    db, config, meta = _seed(
        tmp_path,
        prices=prices,
        currencies={isin: "EUR" for isin in [A, B, C, D, E]},
        etfs={isin: {"name": f"F{isin[-1]}"} for isin in [A, B, C, D, E]},
    )
    assert funds.main(_args(
        db, config, meta, "--from", "2024-01-01", "--as-of", "2024-01-05", "--explain",
    )) == 0
    out = capsys.readouterr().out
    hole = next(line for line in out.splitlines() if line.startswith(A))
    peer = next(line for line in out.splitlines() if line.startswith(B))
    # A bridged the 01-03 hole: 4 closes → 3 returns, Gap 1.
    parts_a = hole.split()
    parts_b = peer.split()
    assert parts_a[-5] == "3" and parts_a[-4] == "1"   # n, Gap
    assert parts_b[-5] == "4" and parts_b[-4] == "0"
    assert "2024-01-03" in out  # --explain lists the date


def test_cmd_class_and_dist_filters(tmp_path, capsys):
    prices = [
        (A, "2024-01-01", 100.0), (A, "2024-01-02", 101.0),
        (B, "2024-01-01", 50.0), (B, "2024-01-02", 51.0),
    ]
    db, config, meta = _seed(
        tmp_path,
        prices=prices,
        currencies={A: "EUR", B: "EUR"},
        etfs={
            A: {"name": "Stock", "asset_class": "Equity", "distribution": "Accumulating"},
            B: {"name": "Bond", "asset_class": "Bonds", "distribution": "Distributing"},
        },
    )
    assert funds.main(_args(db, config, meta, "--as-of", "2024-01-02", "--class", "Equity")) == 0
    out = capsys.readouterr().out
    assert "Stock" in out and "Bond" not in out

    assert funds.main(_args(db, config, meta, "--as-of", "2024-01-02", "--dist", "dist")) == 0
    out = capsys.readouterr().out
    assert "Bond" in out and "Stock" not in out


def test_cmd_sort_ter_and_one_close_unavailable(tmp_path, capsys):
    prices = [
        (A, "2024-01-01", 100.0), (A, "2024-01-02", 101.0),
        (B, "2024-01-02", 50.0),
    ]
    db, config, meta = _seed(
        tmp_path,
        prices=prices,
        currencies={A: "EUR", B: "EUR"},
        etfs={
            A: {"name": "Dear", "ter": 0.50},
            B: {"name": "Cheap", "ter": 0.05},
        },
    )
    assert funds.main(_args(
        db, config, meta, "--as-of", "2024-01-02", "--sort", "ter",
    )) == 0
    out = capsys.readouterr().out
    cheap = out.index("Cheap")
    dear = out.index("Dear")
    assert cheap < dear
    assert "only one EUR close" in out


def test_distribution_label_and_sort_keys():
    assert _distribution_label("Accumulating") == "ACC"
    assert _distribution_label("Distributing") == "Dist"
    assert _distribution_label("Other") == "Othe"
    assert _distribution_label("") == ""
    assert _dist_matches("Accumulating", "acc")
    assert _dist_matches("Distributing", "DIST")
    assert _dist_matches("Custom", "custom")
    assert not _dist_matches("Equity", "acc")
    rows = [
        build_row(
            A, {"name": "Zed", "asset_class": "Equity", "distribution": "Accumulating"},
            held=False, closes=[("2024-01-02", 1.0), ("2024-01-03", 1.1)],
            gap_dates=[], start=None, as_of="2024-01-03", currency="USD",
        ),
        build_row(
            B, {"name": "Aye", "asset_class": "Bonds", "distribution": "Distributing"},
            held=False, closes=[("2024-01-01", 1.0), ("2024-01-03", 1.2)],
            gap_dates=["2024-01-02"], start=None, as_of="2024-01-03", currency="EUR",
        ),
    ]
    assert [r.isin for r in sort_rows(rows, sort_by="name")] == [B, A]
    assert [r.isin for r in sort_rows(rows, sort_by="class")] == [B, A]
    assert [r.isin for r in sort_rows(rows, sort_by="dist")] == [A, B]
    assert [r.isin for r in sort_rows(rows, sort_by="ccy")] == [B, A]
    assert [r.isin for r in sort_rows(rows, sort_by="from")] == [B, A]


def test_load_price_frame_missing_db_and_empty_clip(tmp_path):
    missing = tmp_path / "no.db"
    frame = load_price_frame(str(missing))
    assert frame.empty
    assert clip_price_frame(frame, start="2020-01-01", as_of="2024-01-01").empty
    empty_db = tmp_path / "empty.db"
    with closing(sqlite3.connect(str(empty_db))):
        pass
    assert load_price_frame(str(empty_db)).empty


def test_cmd_invalid_as_of(capsys):
    assert funds.main(["--as-of", "nope"]) == 1
    assert "YYYY-MM-DD" in capsys.readouterr().out


def test_cmd_corrupt_config_is_error(tmp_path, capsys):
    db, config, meta = _seed(
        tmp_path,
        prices=[(A, "2024-01-01", 100.0)],
        currencies={A: "EUR"},
        etfs={A: {"name": "X"}},
    )
    Path(config).write_text("{not: [valid")
    assert funds.main(_args(db, config, meta, "--as-of", "2024-01-01")) == 1
    assert "Error" in capsys.readouterr().out


def test_cmd_explain_unavailable(tmp_path, capsys):
    db, config, meta = _seed(
        tmp_path,
        prices=[],
        currencies={A: "EUR"},
        etfs={A: {"name": "Ghost"}},
    )
    assert funds.main(_args(db, config, meta, "--as-of", "2024-01-01", "--explain")) == 0
    out = capsys.readouterr().out
    assert "UNAVAILABLE" in out
    assert "Fund window" in out


def test_cmd_no_filter_match(tmp_path, capsys):
    db, config, meta = _seed(
        tmp_path,
        prices=[(A, "2024-01-01", 100.0), (A, "2024-01-02", 101.0)],
        currencies={A: "EUR"},
        etfs={A: {"name": "Stock", "asset_class": "Equity"}},
    )
    assert funds.main(_args(db, config, meta, "--as-of", "2024-01-02", "--class", "Bonds")) == 0
    assert "No funds match the filters" in capsys.readouterr().out
