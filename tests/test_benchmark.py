"""Benchmark comparison: regression stats + the command (ADR-0033, Phase B)."""

import math
import sqlite3
from contextlib import closing

import pytest
import yaml

from e1f import benchmark as bench
from e1f.benchmark import BenchmarkStats, benchmark_stats
from e1f.common import Status

PORT_ISIN = "IE00PORT00001"
BENCH_ISIN = "IE00BENCH0001"


def _returns(values, start_day=1):
    """[(YYYY-01-DD, r)] over sequential January days from ``start_day``."""
    return [(f"2024-01-{start_day + i:02d}", r) for i, r in enumerate(values)]


# ---------------------------------------------------------------------------
# Alignment
# ---------------------------------------------------------------------------

def test_align_intersects_on_shared_dates():
    port = [("d1", 0.1), ("d2", 0.2), ("d3", 0.3)]
    benchmark = [("d2", 0.5), ("d3", 0.6), ("d4", 0.7)]
    a, b, dates = bench._align(port, benchmark)
    assert a == [0.2, 0.3]
    assert b == [0.5, 0.6]
    assert dates == ["d2", "d3"]


# ---------------------------------------------------------------------------
# benchmark_stats
# ---------------------------------------------------------------------------

def test_benchmark_stats_perfect_linear_beta_two():
    rb = [0.01, -0.02, 0.03, 0.00, 0.015, -0.01]
    port = _returns([2.0 * r for r in rb])   # rp = 2·rb exactly
    benchmark = _returns(rb)
    s = benchmark_stats(port, benchmark, BENCH_ISIN, "Bench", min_overlap=2)
    assert s.status is Status.CALCULATED
    assert s.n == len(rb)
    assert s.beta == pytest.approx(2.0)
    assert s.r_squared == pytest.approx(1.0)          # perfectly linear
    assert s.tracking_error is not None and s.tracking_error > 0.0
    assert s.information_ratio is not None


def test_benchmark_stats_identical_series_is_degenerate_active():
    rb = [0.01, -0.02, 0.03, 0.00, 0.015]
    same = _returns(rb)
    s = benchmark_stats(same, _returns(rb), BENCH_ISIN, "Bench", min_overlap=2)
    assert s.beta == pytest.approx(1.0)
    assert s.r_squared == pytest.approx(1.0)
    assert s.tracking_error == pytest.approx(0.0)     # rp − rb ≡ 0
    assert s.information_ratio is None                # 0/0 → undefined, not NaN
    assert s.relative_strength == pytest.approx(1.0)
    assert s.outperformance == pytest.approx(0.0)
    assert s.bench_vol is not None and s.bench_vol > 0.0
    assert s.bench_maxdd is not None


def test_benchmark_stats_insufficient_overlap_is_unavailable():
    # Default min_overlap is now 2 (the math floor), so demand more explicitly.
    s = benchmark_stats(_returns([0.1, 0.2]), _returns([0.1, 0.2]), BENCH_ISIN, "B", min_overlap=5)
    assert s.status is Status.UNAVAILABLE
    assert s.n == 2 and "insufficient overlap" in (s.reason or "")
    assert s.beta is None and s.tracking_error is None


def test_benchmark_stats_flat_benchmark_has_no_beta_but_keeps_te():
    # A benchmark with zero variance: beta/R² undefined, active-return stats still defined.
    port = _returns([0.01, -0.02, 0.03, 0.00, 0.015])
    flat = _returns([0.0, 0.0, 0.0, 0.0, 0.0])
    s = benchmark_stats(port, flat, BENCH_ISIN, "Flat", min_overlap=2)
    assert s.status is Status.CALCULATED
    assert s.beta is None and s.r_squared is None      # var(benchmark) == 0
    assert s.tracking_error is not None                # stdev(rp − 0) defined
    assert s.outperformance == pytest.approx(-s.port_twr)  # bench_twr == 0


def test_benchmark_stats_outperformance_and_relative_strength():
    port = _returns([0.10, 0.10])       # +21% compounded
    benchmark = _returns([0.05, 0.05])  # +10.25% compounded
    s = benchmark_stats(port, benchmark, BENCH_ISIN, "B", min_overlap=2)
    assert s.port_twr == pytest.approx(0.21)
    assert s.bench_twr == pytest.approx(0.1025)
    assert s.outperformance == pytest.approx(0.1025 - 0.21)
    assert s.relative_strength == pytest.approx(1.21 / 1.1025)
    line = bench._format_row(s)
    # Book TWR 21.0% is not a column; table TWR 10.3%; Out% = 10.25−21 → −10.8%
    assert "21.0%" not in line
    assert "10.3%" in line and "-10.8%" in line
    assert "Out%" in bench._HEADER
    assert "TWR" in bench._HEADER and "bTWR" not in bench._HEADER
    assert "Vol" in bench._HEADER and "bVol" not in bench._HEADER
    assert "MaxDD" in bench._HEADER and "bMaxDD" not in bench._HEADER
    # Constant daily returns → sample stdev is 0; never below peak.
    assert s.bench_vol == pytest.approx(0.0)
    assert s.bench_maxdd == pytest.approx(0.0)


def test_benchmark_stats_maxdd_pair_hand_computed():
    port = _returns([0.10, -0.10])
    benchmark = _returns([0.05, -0.05])
    s = benchmark_stats(port, benchmark, BENCH_ISIN, "B", min_overlap=2)
    assert s.bench_maxdd == pytest.approx(1.05 * 0.95 / 1.05 - 1.0)
    line = bench._format_row(s)
    assert "-5.0%" in line
    assert "-10.0%" not in line  # book MaxDD is the Book line, not a column


def test_ann_vol_and_max_dd_hand_computed():
    returns = [0.10, -0.10]
    assert bench._cumulative(returns) == pytest.approx(1.10 * 0.90 - 1.0)
    assert bench._max_drawdown(returns) == pytest.approx(0.99 / 1.10 - 1.0)
    assert bench._ann_vol(returns) == pytest.approx(math.sqrt(2) * 0.10 * math.sqrt(252))
    assert bench._ann_vol([0.05]) is None
    assert bench._max_drawdown([]) is None
    assert bench._max_drawdown([0.05]) == pytest.approx(0.0)


def test_book_summary_uses_full_series_not_an_overlap():
    port = [("2024-01-02", 0.10), ("2024-01-03", -0.10)]
    summary = bench.book_summary(port)
    assert summary.n == 2
    assert summary.start == "2024-01-02" and summary.end == "2024-01-03"
    assert summary.twr == pytest.approx(-0.01)
    assert summary.max_drawdown == pytest.approx(0.99 / 1.10 - 1.0)
    assert "TWR=-1.0%" in bench._format_book_summary(summary)
    assert "MaxDD=-10.0%" in bench._format_book_summary(summary)


# ---------------------------------------------------------------------------
# Command (DB-backed): portfolio_return_series + eur_return_series + render
# ---------------------------------------------------------------------------

def _seed(tmp_path, *, transactions, prices, currencies, names):
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
        conn.executemany(
            "INSERT INTO transactions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", transactions
        )
        conn.executemany("INSERT INTO prices VALUES (?, ?, ?)", prices)
        conn.commit()
    config = tmp_path / "config.yaml"
    config.write_text(yaml.dump({"etfs": {i: {"name": n} for i, n in names.items()}}))
    meta = tmp_path / "meta.yaml"
    meta.write_text(
        yaml.dump({i: {"currency": c, "symbol": f"{i}:X:{c}", "xid": "1"}
                   for i, c in currencies.items()})
    )
    return str(db), str(config), str(meta)


def _args(db, config, meta, *extra):
    return ["--db", db, "--config", config, "--currency-meta", meta, *extra]


def _daily(isin, base):
    # Six sequential daily closes → five returns for the shared window.
    closes = [base, base * 1.10, base * 0.99, base * 1.08, base * 1.20, base * 1.26]
    return [(isin, f"2024-01-0{i + 1}", c) for i, c in enumerate(closes)]


def test_cmd_benchmark_calculates_against_priced_benchmark(tmp_path, capsys):
    # One held EUR fund; benchmark tracks it exactly (bench = fund/2) → beta 1, R² 1.
    db, config, meta = _seed(
        tmp_path,
        transactions=[("tr", "t1", "2024-01-01", PORT_ISIN, "BUY", 10.0, 100.0, 0.0, 0.0)],
        prices=_daily(PORT_ISIN, 100.0) + _daily(BENCH_ISIN, 50.0),
        currencies={PORT_ISIN: "EUR", BENCH_ISIN: "EUR"},
        names={PORT_ISIN: "My Fund", BENCH_ISIN: "Bench Fund"},
    )
    code = bench.main(
        _args(db, config, meta, "--as-of", "2024-01-06", "--against", BENCH_ISIN,
              "--min-overlap", "3", "--explain")
    )
    out = capsys.readouterr().out
    assert code == 0
    assert "Bench Fund" in out
    assert "1.00" in out              # beta == 1 (and R² == 1)
    assert f"{BENCH_ISIN}" in out and "2024-01-" in out  # legend window
    assert "Provenance (--explain)" in out
    assert "Book 2024-01-01 → 2024-01-06" in out
    assert "TWR=26.0%" in out
    assert "n=6" in out
    assert out.index("Book ") < out.index("Benchmark")


def test_cmd_benchmark_unavailable_when_benchmark_unpriced(tmp_path, capsys):
    db, config, meta = _seed(
        tmp_path,
        transactions=[("tr", "t1", "2024-01-01", PORT_ISIN, "BUY", 10.0, 100.0, 0.0, 0.0)],
        prices=_daily(PORT_ISIN, 100.0),          # benchmark has no prices
        currencies={PORT_ISIN: "EUR"},
        names={PORT_ISIN: "My Fund"},
    )
    code = bench.main(_args(db, config, meta, "--as-of", "2024-01-06", "--against", BENCH_ISIN))
    out = capsys.readouterr().out
    assert code == 0
    assert "UNAVAILABLE" in out and "no return series" in out


def test_cmd_benchmark_no_portfolio_history(tmp_path, capsys):
    db, config, meta = _seed(
        tmp_path,
        transactions=[],
        prices=_daily(BENCH_ISIN, 50.0),
        currencies={BENCH_ISIN: "EUR"},
        names={BENCH_ISIN: "Bench Fund"},
    )
    code = bench.main(_args(db, config, meta, "--as-of", "2024-01-06"))
    out = capsys.readouterr().out
    assert code == 0
    assert "No priceable portfolio return history" in out


def test_cmd_benchmark_bad_as_of_and_min_overlap(tmp_path, capsys):
    db, config, meta = _seed(
        tmp_path,
        transactions=[("tr", "t1", "2024-01-01", PORT_ISIN, "BUY", 10.0, 100.0, 0.0, 0.0)],
        prices=_daily(PORT_ISIN, 100.0),
        currencies={PORT_ISIN: "EUR"},
        names={PORT_ISIN: "My Fund"},
    )
    assert bench.main(_args(db, config, meta, "--as-of", "nope")) == 1
    assert "must be YYYY-MM-DD" in capsys.readouterr().out
    assert bench.main(_args(db, config, meta, "--min-overlap", "1")) == 1
    assert ">= 2" in capsys.readouterr().out


def test_parse_benchmarks_splits_and_rejects_empty():
    assert bench._parse_benchmarks("A, B ,C") == ["A", "B", "C"]
    with pytest.raises(ValueError, match="at least one ISIN"):
        bench._parse_benchmarks(" , ")


def test_unavailable_helper_shape():
    s = bench._unavailable("X", "Name", "why")
    assert isinstance(s, BenchmarkStats)
    assert s.status is Status.UNAVAILABLE and s.outperformance is None


def test_bench_name_friendly_label_for_defaults_else_config(tmp_path):
    assert bench._bench_name("/no/config", "IE00B4L5Y983") == "iShares Core MSCI World (Acc)"
    assert bench._bench_name("/no/config", "IE00B3YLTY66") == "SPDR MSCI ACWI IMI (Acc)"
    config = tmp_path / "c.yaml"
    config.write_text(yaml.dump({"etfs": {"IE00XX": {"name": "Custom Fund"}}}))
    assert bench._bench_name(str(config), "IE00XX") == "Custom Fund"        # config fallback
    assert bench._bench_name("/no/config", "IE00ZZ") == "IE00ZZ"           # ISIN fallback


def test_cmd_benchmark_marks_held_benchmark_with_star(tmp_path, capsys):
    # Benchmark the book against a fund it holds → name gets '*' and the footnote appears.
    db, config, meta = _seed(
        tmp_path,
        transactions=[("tr", "t1", "2024-01-01", PORT_ISIN, "BUY", 10.0, 100.0, 0.0, 0.0)],
        prices=_daily(PORT_ISIN, 100.0),
        currencies={PORT_ISIN: "EUR"},
        names={PORT_ISIN: "My Fund"},
    )
    bench.main(
        _args(db, config, meta, "--as-of", "2024-01-06", "--against", PORT_ISIN,
              "--min-overlap", "3")
    )
    out = capsys.readouterr().out
    assert "My Fund*" in out
    assert "* also a current portfolio holding." in out


def test_sort_stats_by_name_and_n():
    alpha = bench._unavailable("IE00A", "Alpha", "no series", n=2)
    zeta = bench._unavailable("IE00B", "Zeta", "no series", n=9)
    by_name = bench.sort_stats([zeta, alpha], sort_by="name")
    assert [s.isin for s in by_name] == ["IE00A", "IE00B"]
    by_n = bench.sort_stats([alpha, zeta], sort_by="n", reverse=True)
    assert [s.isin for s in by_n] == ["IE00B", "IE00A"]


EXTRA_ISIN = "IE00EXTRA0001"


def test_priced_isins_sorted_distinct(tmp_path):
    db, _config, _meta = _seed(
        tmp_path,
        transactions=[],
        prices=_daily(BENCH_ISIN, 50.0) + _daily(PORT_ISIN, 100.0),
        currencies={PORT_ISIN: "EUR", BENCH_ISIN: "EUR"},
        names={PORT_ISIN: "My Fund", BENCH_ISIN: "Bench Fund"},
    )
    assert bench._priced_isins(db) == [BENCH_ISIN, PORT_ISIN]


def test_cmd_all_uses_every_priced_isin(tmp_path, capsys):
    db, config, meta = _seed(
        tmp_path,
        transactions=[("tr", "t1", "2024-01-01", PORT_ISIN, "BUY", 10.0, 100.0, 0.0, 0.0)],
        prices=_daily(PORT_ISIN, 100.0) + _daily(BENCH_ISIN, 50.0) + _daily(EXTRA_ISIN, 20.0),
        currencies={PORT_ISIN: "EUR", BENCH_ISIN: "EUR", EXTRA_ISIN: "EUR"},
        names={PORT_ISIN: "My Fund", BENCH_ISIN: "Bench Fund", EXTRA_ISIN: "Extra Fund"},
    )
    assert bench.main(_args(db, config, meta, "--as-of", "2024-01-06", "--min-overlap", "3")) == 0
    default_out = capsys.readouterr().out
    assert "Bench Fund" not in default_out and "Extra Fund" not in default_out

    assert bench.main(
        _args(db, config, meta, "--as-of", "2024-01-06", "--all", "--min-overlap", "3")
    ) == 0
    out = capsys.readouterr().out
    assert "Portfolio vs all priced ETFs" in out
    assert "My Fund*" in out and "Bench Fund" in out and "Extra Fund" in out
    assert "every ISIN in the prices table" in out


def test_cmd_all_and_against_are_exclusive(tmp_path, capsys):
    db, config, meta = _seed(
        tmp_path,
        transactions=[("tr", "t1", "2024-01-01", PORT_ISIN, "BUY", 10.0, 100.0, 0.0, 0.0)],
        prices=_daily(PORT_ISIN, 100.0),
        currencies={PORT_ISIN: "EUR"},
        names={PORT_ISIN: "My Fund"},
    )
    code = bench.main(
        _args(db, config, meta, "--all", "--against", BENCH_ISIN, "--as-of", "2024-01-06")
    )
    out = capsys.readouterr().out
    assert code == 1
    assert "mutually exclusive" in out


def test_cmd_all_empty_prices(tmp_path, capsys):
    db, config, meta = _seed(
        tmp_path,
        transactions=[("tr", "t1", "2024-01-01", PORT_ISIN, "BUY", 10.0, 100.0, 0.0, 0.0)],
        prices=[],
        currencies={PORT_ISIN: "EUR"},
        names={PORT_ISIN: "My Fund"},
    )
    code = bench.main(_args(db, config, meta, "--all"))
    out = capsys.readouterr().out
    assert code == 0
    assert "No price series in database" in out

