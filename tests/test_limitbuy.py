"""limitbuy: close-only worst-case bound on a buy-limit order (ADR-0055)."""

import math
import sqlite3
from contextlib import closing

import pytest
import yaml
from hypothesis import given, settings
from hypothesis import strategies as st

from e1f.common import Status
from e1f.experimental import limitbuy as lb

EUR_ISIN = "IE00EUR000001"
USD_ISIN = "IE00USD000001"

# Pinned regression series (ADR-0055 §1-4); every expected value below is hand-computed.
PINNED = [
    ("2024-01-01", 100.0),
    ("2024-01-02", 101.0),
    ("2024-01-03", 99.0),
    ("2024-01-04", 98.0),
    ("2024-01-05", 102.0),
    ("2024-01-08", 104.0),
    ("2024-01-09", 103.0),
]
PINNED_CLOSES = [close for _, close in PINNED]


def _seed(tmp_path, *, prices, fx=(), currencies=None, funds=None):
    db = tmp_path / "e1f.db"
    with closing(sqlite3.connect(str(db))) as conn:
        conn.execute(
            "CREATE TABLE prices (isin TEXT, date TEXT, close REAL, PRIMARY KEY (isin, date))"
        )
        conn.execute(
            "CREATE TABLE fx_rates (base TEXT, quote TEXT, date TEXT, rate REAL, "
            "PRIMARY KEY (base, quote, date))"
        )
        conn.executemany("INSERT INTO prices VALUES (?, ?, ?)", prices)
        conn.executemany("INSERT INTO fx_rates VALUES (?, ?, ?, ?)", fx)
        conn.commit()
    config = tmp_path / "config.yaml"
    config.write_text(yaml.dump({"etfs": funds or {}}))
    meta = tmp_path / "meta.yaml"
    meta.write_text(yaml.dump({isin: {"currency": c} for isin, c in (currencies or {}).items()}))
    return str(db), str(config), str(meta)


def _seed_pinned(tmp_path, *, distribution="Accumulating"):
    return _seed(
        tmp_path,
        prices=[(EUR_ISIN, day, close) for day, close in PINNED],
        currencies={EUR_ISIN: "EUR"},
        funds={EUR_ISIN: {"name": "Pinned Fund", "distribution": distribution}},
    )


def _run(db, config, meta, *extra):
    return lb.main(
        ["--isin", EUR_ISIN, "--db", db, "--config", config, "--currency-meta", meta,
         "--to", "2024-12-31", *extra]
    )


# ---------------------------------------------------------------------------
# Pinned-date regression: Below 2%, Expiry 2 over PINNED.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("t", "filled", "delta"),
    [
        (0, False, 100 / 99 - 1),   # limit 98.00: 101, 99 stay above → buys at 99 (+1.01%)
        (1, True, 1 / 0.98 - 1),    # limit 98.98: 2024-01-04 closes 98 < 98.98 → fills
        (2, False, 99 / 102 - 1),   # limit 97.02: 98, 102 stay above → buys at 102
        (3, False, 98 / 104 - 1),   # limit 96.04: 102, 104 stay above → buys at 104
        (4, False, 102 / 103 - 1),  # limit 99.96: 104, 103 stay above → buys at 103
    ],
)
def test_pinned_limit_outcomes(t, filled, delta):
    got_filled, got_delta = lb.limit_outcome(PINNED_CLOSES, t, 0.02, 2)
    assert got_filled is filled
    assert got_delta == pytest.approx(delta)


def test_pinned_summary():
    row = lb.summarize(PINNED_CLOSES, 0, 0.02, 2)
    assert row.windows == 5                      # start days 2024-01-01 … 2024-01-05
    assert row.fill_rate == pytest.approx(1 / 5)  # only 2024-01-02's order fills
    assert row.win_rate == pytest.approx(2 / 5)   # 2024-01-01 (+1.01%) and the fill (+2.04%)
    assert row.mean == pytest.approx(
        (100 / 99 + 1 / 0.98 + 99 / 102 + 98 / 104 + 102 / 103 - 5) / 5
    )                                             # ≈ −1.326%
    assert row.median == pytest.approx(102 / 103 - 1)   # −0.97%, the middle of five
    assert row.p10 == pytest.approx(98 / 104 - 1)       # nearest rank ⌈0.5⌉ = 1st smallest
    assert row.worst == pytest.approx(98 / 104 - 1)     # −5.77%
    assert row.independent == 3                   # ⌈5 / 2⌉ non-overlapping windows
    assert row.illustrative                       # 3 < 24
    assert row.status is Status.BOUNDED


def test_pinned_end_to_end_row_and_disclosures(tmp_path, capsys):
    db, config, meta = _seed_pinned(tmp_path)
    assert _run(db, config, meta, "--below", "2", "--expiry", "2") == 0
    out = capsys.readouterr().out
    row = next(line for line in out.splitlines() if line.startswith("    2d"))
    assert row.split() == [
        "2d", "2.00%", "5", "3*", "20%", "40%", "-1.33%", "-0.97%", "-5.77%", "-5.77%"
    ]
    assert "Data:     2024-01-01 → 2024-01-09" in out
    assert "tested: —" in out and "absent: dot-com 2000-2002" in out
    assert "worst case for the limit order" in out
    assert f"* fewer than {lb.ILLUSTRATIVE_INDEPENDENT} non-overlapping windows" in out
    assert "Currency:" not in out                 # EUR series: nothing converted


def test_close_at_the_limit_does_not_fill_strictly_below_does():
    # 100 × (1 − 0.25) = 75.0 exactly; a close AT the limit is not a guaranteed fill.
    at_limit = lb.limit_outcome([100.0, 75.0, 90.0], 0, 0.25, 2)
    assert at_limit == (False, pytest.approx(100 / 90 - 1))    # bought at 90 on expiry
    through = lb.limit_outcome([100.0, 74.99, 90.0], 0, 0.25, 2)
    assert through == (True, pytest.approx(1 / 0.75 - 1))      # filled at 75


@given(
    closes=st.lists(st.floats(min_value=1.0, max_value=1_000.0), min_size=2, max_size=40),
    shallow=st.floats(min_value=0.0, max_value=0.2),
    extra_depth=st.floats(min_value=0.0, max_value=0.2),
    expiry=st.integers(min_value=1, max_value=8),
    extra_days=st.integers(min_value=0, max_value=8),
)
@settings(max_examples=200, deadline=None)
def test_fills_monotone_in_depth_and_expiry(closes, shallow, extra_depth, expiry, extra_days):
    deep = shallow + extra_depth
    longer = expiry + extra_days
    for t in range(len(closes) - longer):
        filled, delta = lb.limit_outcome(closes, t, shallow, expiry)
        # A deeper limit fills only if the shallower one does; a longer expiry keeps every fill.
        assert lb.limit_outcome(closes, t, deep, expiry)[0] <= filled
        assert lb.limit_outcome(closes, t, shallow, longer)[0] >= filled
        if filled:
            assert delta == pytest.approx(1 / (1 - shallow) - 1)
    row = lb.summarize(closes, 0, shallow, expiry)
    if row.windows:
        assert row.fill_rate is not None and row.win_rate is not None
        assert row.win_rate >= row.fill_rate      # every fill ends with more shares
        assert row.worst <= row.p10 <= row.median


# ---------------------------------------------------------------------------
# Typed outcomes + their disclosures.
# ---------------------------------------------------------------------------


def test_no_complete_window_is_unavailable_not_zero(tmp_path, capsys):
    row = lb.summarize(PINNED_CLOSES, 0, 0.01, 7)    # 7 closes cannot hold a 7-day window
    assert row.windows == 0 and row.status is Status.UNAVAILABLE
    assert row.fill_rate is None and row.mean is None and not row.illustrative

    db, config, meta = _seed_pinned(tmp_path)
    assert _run(db, config, meta, "--below", "1", "--expiry", "7", "--explain") == 0
    out = capsys.readouterr().out
    row_line = next(line for line in out.splitlines() if line.startswith("    7d"))
    assert "n/a" in row_line and "UNAVAILABLE" in row_line
    assert "n/a = UNAVAILABLE: no start day from 2024-01-01 has a full window for expiry 7d" in out
    assert "Status:         UNAVAILABLE" in out


def test_from_moves_the_first_start_day(tmp_path, capsys):
    row = lb.summarize(PINNED_CLOSES, 3, 0.02, 2)    # start days 2024-01-04, 2024-01-05
    assert row.windows == 2
    db, config, meta = _seed_pinned(tmp_path)
    assert _run(db, config, meta, "--below", "2", "--expiry", "2", "--from", "2024-01-04") == 0
    assert "Starts:   2024-01-04 onward" in capsys.readouterr().out


def test_from_after_last_close_is_refused(tmp_path, capsys):
    db, config, meta = _seed_pinned(tmp_path)
    assert _run(db, config, meta, "--from", "2024-02-01") == 1
    assert "after the last stored close (2024-01-09)" in capsys.readouterr().err


def test_default_grid_is_sorted_and_deduplicated():
    run = lb.evaluate(
        EUR_ISIN, "Fund", "EUR", False, [d for d, _ in PINNED], PINNED_CLOSES,
        from_date=None, below_pcts=[], limit_prices=[], expiries=[],
    )
    assert [(r.expiry, r.below) for r in run.rows] == [
        (expiry, pct / 100) for expiry in lb.DEFAULT_EXPIRY_DAYS for pct in lb.DEFAULT_BELOW_PCT
    ]
    run = lb.evaluate(
        EUR_ISIN, "Fund", "EUR", False, [d for d, _ in PINNED], PINNED_CLOSES,
        from_date=None, below_pcts=[2.0, 1.0, 2.0], limit_prices=[], expiries=[3, 1, 3],
    )
    assert [(r.expiry, r.below) for r in run.rows] == [
        (1, 0.01), (1, 0.02), (3, 0.01), (3, 0.02)
    ]


def test_limit_price_converts_against_the_last_close(tmp_path, capsys):
    db, config, meta = _seed_pinned(tmp_path)          # last close 103.00 on 2024-01-09
    assert _run(db, config, meta, "--limit", "100.94", "--expiry", "2") == 0
    out = capsys.readouterr().out
    assert "Limits:   vs last close €103.00 (2024-01-09): €100.94 → 2.00%" in out
    assert any(line.startswith("    2d   2.00%") for line in out.splitlines())
    assert lb.below_from_limit(103.0, 103.0) == 0.0    # at the close: 0% under it


def test_limit_above_the_last_close_is_refused(tmp_path, capsys):
    db, config, meta = _seed_pinned(tmp_path)
    assert _run(db, config, meta, "--limit", "104") == 1
    assert "above the last close €103.00" in capsys.readouterr().err


def test_converted_series_is_disclosed(tmp_path, capsys):
    db, config, meta = _seed(
        tmp_path,
        prices=[(USD_ISIN, day, close * 1.1) for day, close in PINNED],
        fx=[("EUR", "USD", "2024-01-01", 1.1)],
        currencies={USD_ISIN: "USD"},
    )
    code = lb.main(
        ["--isin", USD_ISIN, "--db", db, "--config", config, "--currency-meta", meta,
         "--to", "2024-12-31", "--below", "2", "--expiry", "2"]
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "Currency: USD closes → EUR at the nearest-prior FX rate" in out
    # 110 / 1.1 = 100 … the converted series is PINNED, so the pinned row reappears.
    row = next(line for line in out.splitlines() if line.startswith("    2d"))
    assert row.split()[4:7] == ["20%", "40%", "-1.33%"]
    run = lb.evaluate(
        USD_ISIN, "Fund", "USD", False, ["2024-01-01", "2024-01-02"], [1.0, 1.0],
        from_date=None, below_pcts=[1.0], limit_prices=[], expiries=[1],
    )
    assert run.converted


def test_distributing_fund_warns(tmp_path, capsys):
    db, config, meta = _seed_pinned(tmp_path, distribution="Distributing")
    assert _run(db, config, meta, "--below", "2", "--expiry", "2") == 0
    err = capsys.readouterr().err
    assert "Distributing" in err and "flatters the limit order" in err


def test_show_status_and_explain(tmp_path, capsys):
    db, config, meta = _seed_pinned(tmp_path)
    assert _run(db, config, meta, "--below", "2", "--expiry", "2", "--show-status") == 0
    out = capsys.readouterr().out
    assert "Status" in out and "BOUNDED" in out and "Provenance" not in out

    assert _run(db, config, meta, "--below", "2", "--expiry", "2", "--explain") == 0
    out = capsys.readouterr().out
    assert "Status:         BOUNDED   (method = limit_buy_close_bound_v1)" in out
    assert "lower bounds for 1 of 1 (Expiry, Below) rows" in out
    assert "Limited by:     intraday lows" in out


# ---------------------------------------------------------------------------
# Refusals.
# ---------------------------------------------------------------------------


def test_no_prices_at_all(tmp_path, capsys):
    db, config, meta = _seed(tmp_path, prices=[])
    assert _run(db, config, meta) == 1
    assert "run 'e1f fetch'" in capsys.readouterr().err


def test_unknown_isin_lists_candidates(tmp_path, capsys):
    db, config, meta = _seed_pinned(tmp_path)
    code = lb.main(["--isin", "IE00MISSING01", "--db", db, "--config", config,
                    "--currency-meta", meta])
    assert code == 1
    assert EUR_ISIN in capsys.readouterr().err


def test_no_close_before_to(tmp_path, capsys):
    db, config, meta = _seed_pinned(tmp_path)
    code = lb.main(["--isin", EUR_ISIN, "--db", db, "--config", config,
                    "--currency-meta", meta, "--to", "2023-12-31"])
    assert code == 1
    err = capsys.readouterr().err
    assert "no close of IE00EUR000001 on or before 2023-12-31" in err and "FX" not in err


def test_foreign_series_without_fx_hints_at_the_rate(tmp_path, capsys):
    db, config, meta = _seed(
        tmp_path, prices=[(USD_ISIN, "2024-01-01", 10.0)], currencies={USD_ISIN: "USD"}
    )
    code = lb.main(["--isin", USD_ISIN, "--db", db, "--config", config,
                    "--currency-meta", meta, "--to", "2024-12-31"])
    assert code == 1
    assert "is the EUR/USD FX rate stored?" in capsys.readouterr().err


@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["--isin", EUR_ISIN, "--below", "100"],
        ["--isin", EUR_ISIN, "--below", "-1"],
        ["--isin", EUR_ISIN, "--below", "nan"],
        ["--isin", EUR_ISIN, "--limit", "0"],
        ["--isin", EUR_ISIN, "--limit", "inf"],
        ["--isin", EUR_ISIN, "--expiry", "0"],
        ["--isin", EUR_ISIN, "--from", "2024-13-01"],
    ],
)
def test_argparse_rejections(argv):
    with pytest.raises(SystemExit) as exc:
        lb.main(argv)
    assert exc.value.code == 2


def test_independent_rounds_up():
    row = lb.summarize(list(map(float, range(1, 30))), 0, 0.01, 5)   # 24 windows
    assert row.windows == 24 and row.independent == math.ceil(24 / 5) == 5


def test_p10_is_nearest_rank():
    # 16 rising closes, expiry 6 → 10 windows, none filled: Δ_t = (t+1)/(t+7) − 1.
    # Nearest rank takes the ⌈0.1 × 10⌉ = 1st smallest (t = 0), not the 2nd.
    row = lb.summarize([float(i) for i in range(1, 17)], 0, 0.01, 6)
    assert row.windows == 10 and row.fill_rate == 0.0
    assert row.p10 == pytest.approx(1 / 7 - 1)
    assert row.worst == row.p10
