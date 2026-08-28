"""Tests for the experimental `lookthrough` command (ADR-0024).

Split out of `test_fetch.py` when the look-through refresh was extracted from
`fetch` into its own experimental command. The helpers are now module-level
functions (no `DataExtractor`), and `refresh_lookthrough` loads its universe from
a config file and its held funds from the transactions table.
"""
import sqlite3
from contextlib import closing

import pandas as pd
import pytest
import yaml

import e1f.experimental.lookthrough as lt
from e1f.experimental.common import (
    DIMENSION_ASSET_CLASS,
    DIMENSION_SECTOR,
    DIMENSION_SECURITY,
    latest_lookthrough_snapshot,
)

ISIN = "AA0000000001"
UNIVERSE = {ISIN: {"name": "Test ETF", "tickers": ["TST"], "exchange": "", "figi": ""}}


class _FakeFundsData:
    def __init__(self, top=None, sectors=None, assets=None):
        self._top = top
        self.sector_weightings = sectors if sectors is not None else {}
        self.asset_classes = assets if assets is not None else {}

    @property
    def top_holdings(self):
        return self._top


def _top_df(names_weights):
    return pd.DataFrame(
        {"Name": [n for n, _ in names_weights],
         "Holding Percent": [w for _, w in names_weights]}
    )


def _config(tmp_path, universe=None):
    cfg = tmp_path / "u.yaml"
    cfg.write_text(yaml.dump({"etfs": UNIVERSE if universe is None else universe}))
    return str(cfg)


def _hold_transaction(db, isin):
    with closing(sqlite3.connect(db)) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS transactions (broker TEXT, transaction_id TEXT, "
            "datetime TEXT, symbol TEXT, side TEXT, shares REAL, price REAL, fee REAL, "
            "tax REAL, PRIMARY KEY (broker, transaction_id))"
        )
        conn.execute(
            "INSERT INTO transactions "
            "(broker, transaction_id, datetime, symbol, side, shares, price, fee, tax) "
            "VALUES ('tr', ?, '2024-01-01', ?, 'BUY', 1.0, 10.0, 0.0, 0.0)",
            (f"t-{isin}", isin),
        )
        conn.commit()


def test_lookthrough_rows_parse_all_three_dimensions():
    fd = _FakeFundsData(
        top=_top_df([("Apple Inc.", 0.07), ("Microsoft Corp", 0.05)]),
        sectors={"Technology": 0.30, "Financials": 0.15},
        assets={"stockPosition": 0.99, "cashPosition": 0.01},
    )
    rows = lt._lookthrough_rows(fd)
    security = [r for r in rows if r.dimension == DIMENSION_SECURITY]
    assert [r.raw_name for r in security] == ["Apple Inc.", "Microsoft Corp"]
    assert security[0].rank == 1 and security[0].normalized_name == "apple"
    assert any(r.dimension == DIMENSION_SECTOR and r.raw_name == "Technology" for r in rows)
    assert any(r.dimension == DIMENSION_ASSET_CLASS for r in rows)


def test_lookthrough_rows_scale_percent_to_fraction():
    fd = _FakeFundsData(top=_top_df([("Apple Inc.", 7.0), ("Msft", 5.0)]))
    security = [r for r in lt._lookthrough_rows(fd) if r.dimension == DIMENSION_SECURITY]
    assert security[0].weight == pytest.approx(0.07)  # 7.0% -> 0.07 fraction


def test_lookthrough_rows_tolerate_missing_dimensions():
    class Broken:
        @property
        def top_holdings(self):
            raise RuntimeError("no holdings for this fund")
        sector_weightings = None
        asset_classes = "not a mapping"

    assert lt._lookthrough_rows(Broken()) == []


def test_fetch_lookthrough_tries_ticker_candidates(monkeypatch):
    fd = _FakeFundsData(top=_top_df([("Apple Inc.", 0.07)]))

    class FakeTicker:
        def __init__(self, symbol):
            # Only the .DE suffix candidate carries data.
            self.funds_data = fd if symbol == "TST.DE" else _FakeFundsData()

    monkeypatch.setattr(lt.yf, "Ticker", FakeTicker)
    rows = lt._fetch_lookthrough(["TST"])
    assert rows is not None and rows[0].raw_name == "Apple Inc."


def test_fetch_lookthrough_returns_none_when_all_empty(monkeypatch):
    monkeypatch.setattr(
        lt.yf, "Ticker", lambda s: type("T", (), {"funds_data": _FakeFundsData()})()
    )
    assert lt._fetch_lookthrough(["TST"]) is None


def test_refresh_lookthrough_stores_and_dedupes(tmp_path, monkeypatch):
    db = str(tmp_path / "t.db")
    config = _config(tmp_path)
    _hold_transaction(db, ISIN)
    rows = lt._lookthrough_rows(
        _FakeFundsData(top=_top_df([("Apple Inc.", 0.07)]), sectors={"Tech": 1.0})
    )
    monkeypatch.setattr(lt, "_fetch_lookthrough", lambda tickers: list(rows))

    first = lt.refresh_lookthrough(db, config)
    assert first.created_isins == (ISIN,)
    assert first.unchanged_isins == ()
    snap = latest_lookthrough_snapshot(db, ISIN)
    assert snap is not None and snap.source == "yfinance" and snap.tier == "provider"

    second = lt.refresh_lookthrough(db, config)  # identical re-observation -> no new snapshot
    assert second.created_isins == ()
    assert second.unchanged_isins == (ISIN,)
    with closing(sqlite3.connect(db)) as conn:
        count = conn.execute("SELECT COUNT(*) FROM holdings_snapshot").fetchone()[0]
    assert count == 1


def test_refresh_lookthrough_warns_when_no_data(tmp_path, monkeypatch, caplog):
    db = str(tmp_path / "t.db")
    config = _config(tmp_path)
    _hold_transaction(db, ISIN)
    monkeypatch.setattr(lt, "_fetch_lookthrough", lambda tickers: None)
    with caplog.at_level("WARNING"):
        summary = lt.refresh_lookthrough(db, config)
    assert summary.unavailable_isins == (ISIN,)
    assert any("no yfinance look-through" in r.message for r in caplog.records)
    assert latest_lookthrough_snapshot(db, ISIN) is None


def test_refresh_lookthrough_discloses_skipped_funds_and_summary(tmp_path, monkeypatch, caplog):
    db = str(tmp_path / "t.db")
    no_ticker = "AA0000000002"
    unconfigured = "AA0000000003"
    config = _config(
        tmp_path,
        {
            **UNIVERSE,
            no_ticker: {"name": "No Ticker ETF", "tickers": [], "exchange": "", "figi": ""},
        },
    )
    for isin in (ISIN, no_ticker, unconfigured):
        _hold_transaction(db, isin)
    rows = lt._lookthrough_rows(_FakeFundsData(top=_top_df([("Apple Inc.", 0.07)])))
    monkeypatch.setattr(lt, "_fetch_lookthrough", lambda tickers: rows)

    with caplog.at_level("INFO"):
        summary = lt.refresh_lookthrough(db, config)

    assert summary.held_isins == (ISIN, no_ticker, unconfigured)
    assert summary.created_isins == (ISIN,)
    assert summary.unchanged_isins == ()
    assert summary.unavailable_isins == ()
    assert summary.skipped_isins == (no_ticker, unconfigured)
    outcomes = (
        set(summary.created_isins)
        | set(summary.unchanged_isins)
        | set(summary.unavailable_isins)
        | set(summary.skipped_isins)
    )
    assert outcomes == set(summary.held_isins)
    messages = [record.message for record in caplog.records]
    assert any(f"{unconfigured} — held fund is absent from the ETF config" in m for m in messages)
    assert any(f"{no_ticker} No Ticker ETF — no configured ticker" in m for m in messages)
    assert (
        "Look-through refresh: held=3; new=1; unchanged=0; unavailable=0; skipped=2"
        in messages
    )


def test_refresh_lookthrough_no_holdings(tmp_path, capsys):
    db = str(tmp_path / "empty.db")
    config = _config(tmp_path)
    assert lt.refresh_lookthrough(db, config) == lt.LookthroughRefreshSummary(
        (), (), (), (), (),
    )
    assert "No ETF holdings" in capsys.readouterr().out


def test_main_dispatches_to_refresh(tmp_path, monkeypatch):
    db = str(tmp_path / "t.db")
    config = _config(tmp_path)
    _hold_transaction(db, ISIN)
    monkeypatch.setattr(
        lt, "_fetch_lookthrough",
        lambda tickers: lt._lookthrough_rows(_FakeFundsData(top=_top_df([("Apple Inc.", 0.07)]))),
    )
    assert lt.main(["--db", db, "--config", config]) == 0
    assert latest_lookthrough_snapshot(db, ISIN) is not None


def test_main_reports_error_as_exit_1(tmp_path, monkeypatch, capsys):
    def boom(db_path, config_path):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(lt, "refresh_lookthrough", boom)
    assert lt.main(["--db", str(tmp_path / "t.db")]) == 1
    assert "✗ Error: kaboom" in capsys.readouterr().out
