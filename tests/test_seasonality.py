"""Calendar seasonality: month-end returns, tests, rules, and the command (ADR-0026+)."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import date

import pytest
import yaml
from scipy.stats import kruskal

from e1f.common import ConfigManager
from e1f.experimental import seasonality as sz

EUR_ISIN = "IE00EUR000001"
USD_ISIN = "IE00USD000001"
GBP_ISIN = "IE00GBP000001"


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


def _month_path(isin: str, start_year: int, end_year: int, r_fn):
    """Two closes per month: the 1st at the previous month-end, the 28th after ``r_fn``."""
    rows: list[tuple[str, str, float]] = []
    price = 100.0
    y, m = start_year - 1, 12
    rows.append((isin, f"{y}-{m:02d}-01", price))
    rows.append((isin, f"{y}-{m:02d}-28", price))
    y, m = start_year, 1
    while (y, m) <= (end_year, 12):
        day1 = price
        price = price * (1.0 + r_fn(y, m))
        rows.append((isin, f"{y}-{m:02d}-01", day1))
        rows.append((isin, f"{y}-{m:02d}-28", price))
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return rows


def _accum_db(tmp_path, start_year, end_year, r_fn, *, isin=EUR_ISIN, distribution="Accumulating"):
    return _seed(
        tmp_path,
        prices=_month_path(isin, start_year, end_year, r_fn),
        currencies={isin: "EUR"},
        funds={isin: {"name": "Core Acc", "distribution": distribution}},
    )


def _argv(db, config, meta, extra=(), *, isin=EUR_ISIN, to="2021-01-01"):
    return [
        "--isin", isin, "--db", db, "--config", config, "--currency-meta", meta,
        "--to", to, *extra,
    ]


# ---------------------------------------------------------------------------
# Month-end returns / partial-month exclusion.
# ---------------------------------------------------------------------------


def test_month_end_map_takes_last_close():
    ends = sz.month_end_map(
        ["2011-05-17", "2011-05-31", "2011-06-30"],
        [10.0, 11.0, 12.0],
    )
    assert ends[(2011, 5)] == ("2011-05-31", 11.0)
    assert ends[(2011, 6)] == ("2011-06-30", 12.0)


def test_complete_month_excludes_first_month_without_prior():
    dates = ["2011-05-17", "2011-05-31", "2011-06-15", "2011-06-30"]
    closes = [10.0, 11.0, 11.5, 12.0]
    returns, partials = sz.complete_month_returns(dates, closes, None, "2011-07-01")
    assert [(r.year, r.month) for r in returns] == [(2011, 6)]
    assert returns[0].ret == pytest.approx(12.0 / 11.0 - 1.0)
    assert any(p.month == 5 and "no prior" in p.reason for p in partials)


def test_complete_month_to_inside_month_drops_that_month():
    dates = ["2011-05-31", "2011-06-30", "2011-07-29"]
    closes = [11.0, 12.0, 13.0]
    returns, partials = sz.complete_month_returns(dates, closes, None, "2011-07-15")
    assert [(r.year, r.month) for r in returns] == [(2011, 6)]
    assert any(p.month == 7 and "has not elapsed" in p.reason for p in partials)


def test_complete_month_from_mid_month_excludes_that_month():
    dates = ["2011-05-31", "2011-06-30", "2011-07-29"]
    closes = [11.0, 12.0, 13.0]
    returns, partials = sz.complete_month_returns(dates, closes, "2011-06-15", "2011-08-01")
    assert [(r.year, r.month) for r in returns] == [(2011, 7)]
    assert any(p.month == 6 and "starts before" in p.reason for p in partials)


def test_missing_calendar_month_is_not_interpolated():
    dates = ["2011-05-31", "2011-07-29"]
    closes = [11.0, 13.0]
    returns, partials = sz.complete_month_returns(dates, closes, None, "2011-08-01")
    assert returns == []
    assert any(p.month == 7 and "no prior" in p.reason for p in partials)


def test_zero_prior_close_is_partial():
    dates = ["2011-05-31", "2011-06-30"]
    closes = [0.0, 12.0]
    returns, partials = sz.complete_month_returns(dates, closes, None, "2011-07-01")
    assert returns == []
    assert any("close is 0" in p.reason for p in partials)


# ---------------------------------------------------------------------------
# Descriptive statistics.
# ---------------------------------------------------------------------------


def _mr(year: int, month: int, ret: float) -> sz.MonthReturn:
    return sz.MonthReturn(year, month, f"{year}-{month:02d}-28", "x", ret)


def test_mean_median_excess_and_zero_frequency():
    returns = [
        _mr(2011, 1, 0.10),
        _mr(2012, 1, -0.10),
        _mr(2013, 1, 0.0),
        _mr(2011, 2, 0.00),
        _mr(2012, 2, 0.00),
        _mr(2013, 2, 0.00),
    ]
    stats = {s.month: s for s in sz.month_stats(returns)}
    jan, feb = stats[1], stats[2]
    assert jan.n == 3 and jan.mean == pytest.approx(0.0)
    assert jan.median == pytest.approx(0.0)
    assert jan.pct_positive == pytest.approx(1 / 3)
    assert jan.pct_negative == pytest.approx(1 / 3)
    assert jan.mean_excess == pytest.approx(0.0)
    assert feb.stdev == pytest.approx(0.0)
    assert stats[3].n == 0 and stats[3].mean is None


def test_weakest_mean_month_breaks_ties_toward_earlier_month():
    returns = [_mr(2011, m, 0.01) for m in range(1, 13)]
    assert sz.weakest_mean_month(sz.month_stats(returns)) == 1


def test_complete_year_count():
    returns = [_mr(2011, m, 0.0) for m in range(1, 13)] + [_mr(2012, 1, 0.0)]
    assert sz.complete_year_count(returns) == 1


# ---------------------------------------------------------------------------
# Kruskal-Wallis, BH, permutation.
# ---------------------------------------------------------------------------


def test_kruskal_wallis_hand_computed_and_matches_scipy():
    groups = [[1.0, 2.0], [3.0, 4.0], [5.0, 6.0]]
    # Ranks 1..6; R=(3,7,11); H = 12/(6*7)*(9/2+49/2+121/2) - 21 = 4.5714...
    assert sz.kruskal_wallis(groups) == pytest.approx(4.57142857142857)
    scipy_h, _ = kruskal(*groups)
    assert sz.kruskal_wallis(groups) == pytest.approx(float(scipy_h))


def test_kruskal_wallis_degenerate():
    assert sz.kruskal_wallis([[1.0, 1.0], [1.0, 1.0]]) == 0.0
    assert sz.kruskal_wallis([[1.0]]) == 0.0
    assert sz.kruskal_wallis([]) == 0.0


def test_benjamini_hochberg_hand_computed():
    raw = [0.01, 0.04, 0.03, 0.20]
    adj = sz.benjamini_hochberg(raw)
    assert adj[3] == pytest.approx(0.20)
    assert adj[0] == pytest.approx(0.04)
    assert adj[1] == pytest.approx(0.04 * 4 / 3)
    assert adj[2] == pytest.approx(0.04 * 4 / 3)
    assert sz.benjamini_hochberg([]) == []


def test_permutation_reproducible_and_bounded():
    returns = [_mr(2010 + i, (i % 12) + 1, 0.01 * ((i % 5) - 2)) for i in range(96)]
    a = sz.permutation_test(returns, permutations=80, seed=0)
    b = sz.permutation_test(returns, permutations=80, seed=0)
    c = sz.permutation_test(returns, permutations=80, seed=1)
    assert a == b
    assert a.month_excess_p_raw != c.month_excess_p_raw
    lo, hi = 1.0 / 81.0, 1.0
    assert lo <= a.p_omnibus <= hi
    assert lo <= a.p_worst <= hi
    assert lo <= a.p_best <= hi


def test_permutation_preserves_month_counts():
    labels = [r.month for r in [_mr(2011, m, 0.0) for m in range(1, 13) for _ in range(4)]]
    values = [float(i) for i in range(len(labels))]
    work = labels[:]
    sz.random.Random(0).shuffle(work)
    assert sorted(work) == sorted(labels)
    groups = sz._group_values(work, values)
    assert [len(g) for g in groups] == [4] * 12


def test_permutation_no_effect_does_not_invent_a_season():
    returns = [_mr(2011 + y, m, 0.01) for y in range(10) for m in range(1, 13)]
    perm = sz.permutation_test(returns, permutations=50, seed=0)
    assert perm.h_obs == pytest.approx(0.0)
    assert perm.p_omnibus == pytest.approx(1.0)
    assert perm.p_worst == pytest.approx(1.0)


def test_permutation_detects_january_premium():
    returns = [
        _mr(2011 + y, m, 0.10 if m == 1 else 0.0)
        for y in range(10)
        for m in range(1, 13)
    ]
    perm = sz.permutation_test(returns, permutations=200, seed=0)
    stats = sz.month_stats(returns)
    assert sz._rank_month(stats, lambda s: s.mean, reverse=True).month == 1
    assert perm.p_omnibus < 0.05
    assert perm.p_best < 0.05


# ---------------------------------------------------------------------------
# Strategy simulator.
# ---------------------------------------------------------------------------


def _path_for_sim(start_year=2011, end_year=2020, r_fn=None):
    fn = r_fn or (lambda _y, _m: 0.0)
    rows = _month_path("X", start_year, end_year, fn)
    dates = [r[1] for r in rows]
    closes = [r[2] for r in rows]
    returns, _ = sz.complete_month_returns(
        dates, closes, f"{start_year}-01-01", f"{end_year + 1}-01-01",
    )
    fills = sz.strategy_fills(dates, {(r.year, r.month) for r in returns})
    return dates, closes, returns, fills


def test_avoid_month_invariance_and_dca_identity():
    dates, closes, _returns, fills = _path_for_sim()
    dca = sz.simulate_seasonal(
        dates, closes, 100.0, 0.0, sz.DeployKind.DCA, 9, fills, "A",
    )
    avoid = sz.simulate_seasonal(
        dates, closes, 100.0, 0.0, sz.DeployKind.AVOID, 9, fills, "B",
    )
    drag = sz.simulate_seasonal(
        dates, closes, 100.0, 0.0, sz.DeployKind.AVOID_DRAG, 9, fills, "C",
    )
    assert sz.invariance_holds(dca, cash_rate=0.0)
    assert sz.invariance_holds(avoid, cash_rate=0.0)
    assert sz.invariance_holds(drag, cash_rate=0.0)
    n = len(fills)
    assert dca.equity_cost == pytest.approx(n * 100.0)
    assert dca.cash == pytest.approx(0.0)
    # Last fill is December, so every skipped September is redeployed in October.
    assert avoid.cash == pytest.approx(0.0)
    assert avoid.equity_cost == pytest.approx(n * 100.0)
    n_skip = sum(1 for i in fills if date.fromisoformat(dates[i]).month == 9)
    assert drag.cash == pytest.approx(n_skip * 100.0)
    assert drag.equity_cost == pytest.approx((n - n_skip) * 100.0)


def test_avoid_december_leaves_last_contribution_in_cash():
    dates, closes, _returns, fills = _path_for_sim()
    avoid = sz.simulate_seasonal(
        dates, closes, 100.0, 0.0, sz.DeployKind.AVOID, 12, fills, "B",
    )
    assert sz.invariance_holds(avoid, cash_rate=0.0)
    assert avoid.cash == pytest.approx(100.0)


def test_sit_out_skips_a_falling_month():
    dates, closes, _returns, fills = _path_for_sim(
        r_fn=lambda _y, m: -0.20 if m == 9 else 0.0,
    )
    dca = sz.simulate_seasonal(
        dates, closes, 100.0, 0.0, sz.DeployKind.DCA, 9, fills, "A",
    )
    sit = sz.simulate_seasonal(
        dates, closes, 100.0, 0.0, sz.DeployKind.SIT_OUT, 9, fills, "B",
    )
    assert sit.terminal > dca.terminal
    assert sz.invariance_holds(sit, cash_rate=0.0)  # sit-out is exempt


def test_sit_out_sells_at_selected_fill_and_reenters_at_next_fill():
    dates = ["2024-01-02", "2024-02-01", "2024-03-01"]
    closes = [10.0, 20.0, 40.0]
    result = sz.simulate_seasonal(
        dates,
        closes,
        contribution=100.0,
        cash_rate=0.0,
        kind=sz.DeployKind.SIT_OUT,
        selected_month=2,
        fills=[0, 1, 2],
        label="sit-out February",
    )

    # Jan: 10 shares. Feb fill: sell at 20 + add 100 => 300 cash.
    # Mar fill: add 100 and re-enter at 40 => 10 shares, terminal 400.
    assert result.cash == 0.0
    assert result.equity == pytest.approx(400.0)
    assert result.terminal == pytest.approx(400.0)


def test_cash_rate_grows_idle_cash():
    dates, closes, _returns, fills = _path_for_sim()
    flat = sz.simulate_seasonal(
        dates, closes, 100.0, 0.0, sz.DeployKind.AVOID_DRAG, 9, fills, "C0",
    )
    grown = sz.simulate_seasonal(
        dates, closes, 100.0, 0.03, sz.DeployKind.AVOID_DRAG, 9, fills, "C3",
    )
    assert grown.cash > flat.cash
    assert grown.cash_income > 0.0


# ---------------------------------------------------------------------------
# OOS selection — no leakage.
# ---------------------------------------------------------------------------


def test_oos_freezes_training_month_and_ignores_test():
    # Training Jan -10%; test Jan +50%. Full-sample Jan is *not* the weakest.
    def r_fn(year, month):
        if month != 1:
            return 0.0
        return -0.10 if year <= 2018 else 0.50

    rows = _month_path("X", 2011, 2021, r_fn)
    dates = [r[1] for r in rows]
    closes = [r[2] for r in rows]
    train, _ = sz.complete_month_returns(dates, closes, "2011-01-01", "2019-01-01")
    test, _ = sz.complete_month_returns(dates, closes, "2019-01-01", "2022-01-01")
    full, _ = sz.complete_month_returns(dates, closes, "2011-01-01", "2022-01-01")
    assert sz.weakest_mean_month(sz.month_stats(train)) == 1
    assert sz.weakest_mean_month(sz.month_stats(full)) != 1
    assert not sz.windows_overlap(train, test)
    assert sz.inferential_floor_met(sz.month_stats(train), sz.SEASONALITY_MIN_N_INFER)
    assert sz.inferential_floor_met(sz.month_stats(test), sz.SEASONALITY_MIN_N_OOS)


def test_windows_overlap_detects_shared_complete_month():
    a = [_mr(2018, 12, 0.0), _mr(2019, 1, 0.0)]
    b = [_mr(2019, 1, 0.1)]
    assert sz.windows_overlap(a, b)
    assert not sz.windows_overlap(a, [_mr(2020, 1, 0.0)])


# ---------------------------------------------------------------------------
# EUR series / catalog.
# ---------------------------------------------------------------------------


def test_eur_series_passthrough_and_conversion(tmp_path):
    db, _config, meta = _seed(
        tmp_path,
        prices=[
            (EUR_ISIN, "2024-01-01", 10.0),
            (USD_ISIN, "2024-01-01", 120.0),
            (USD_ISIN, "2024-01-02", 120.0),
        ],
        fx=[("EUR", "USD", "2024-01-02", 1.2)],
        currencies={EUR_ISIN: "EUR", USD_ISIN: "USD"},
    )
    _d, closes, ccy = sz.eur_series(db, EUR_ISIN, "2024-12-31", meta)
    assert ccy == "EUR" and closes == [10.0]
    usd_dates, usd_closes, ccy = sz.eur_series(db, USD_ISIN, "2024-12-31", meta)
    assert ccy == "USD" and usd_dates == ["2024-01-02"]
    assert usd_closes == [pytest.approx(100.0)]


def test_price_catalog_and_listing(tmp_path):
    db, config, _meta = _seed(
        tmp_path,
        prices=[(EUR_ISIN, "2024-01-01", 10.0)],
        funds={EUR_ISIN: {"name": "Euro Fund", "distribution": "Accumulating"}},
    )
    assert sz.price_catalog(db) == [(EUR_ISIN, 1, "2024-01-01", "2024-01-01")]
    listing = sz._candidate_listing(db, ConfigManager(config))
    assert EUR_ISIN in listing and "Euro Fund" in listing
    empty = tmp_path / "empty.db"
    with closing(sqlite3.connect(str(empty))) as conn:
        conn.execute("CREATE TABLE unrelated (x INTEGER)")
    assert sz.price_catalog(str(empty)) == []
    assert "no price series" in sz._candidate_listing(str(empty), ConfigManager(config))


# ---------------------------------------------------------------------------
# CLI.
# ---------------------------------------------------------------------------


def test_main_missing_isin_is_argparse_error():
    with pytest.raises(SystemExit) as exc:
        sz.main([])
    assert exc.value.code == 2


def test_month_without_rule_is_error():
    with pytest.raises(SystemExit) as exc:
        sz.main(["--isin", EUR_ISIN, "--month", "9"])
    assert exc.value.code == 2


def test_avoid_without_month_is_error():
    with pytest.raises(SystemExit) as exc:
        sz.main(["--isin", EUR_ISIN, "--rule", "avoid-month"])
    assert exc.value.code == 2


def test_historical_weakest_without_split_is_error():
    with pytest.raises(SystemExit) as exc:
        sz.main(["--isin", EUR_ISIN, "--rule", "historical-weakest"])
    assert exc.value.code == 2


def test_historical_weakest_rejects_month_flag():
    with pytest.raises(SystemExit) as exc:
        sz.main([
            "--isin", EUR_ISIN, "--rule", "historical-weakest", "--month", "9",
            "--training-from", "2011-01-01", "--training-to", "2019-01-01",
            "--test-from", "2019-01-01", "--test-to", "2022-01-01",
        ])
    assert exc.value.code == 2


def test_main_no_prices(tmp_path, capsys):
    db, config, meta = _seed(tmp_path, prices=[])
    code = sz.main(_argv(db, config, meta, to="2021-01-01"))
    assert code == 1
    assert "run 'e1f fetch'" in capsys.readouterr().err


def test_main_unknown_isin_lists_candidates(tmp_path, capsys):
    db, config, meta = _accum_db(tmp_path, 2019, 2020, lambda _y, _m: 0.0)
    code = sz.main(_argv(db, config, meta, to="2021-01-01", isin="IE00MISSING01"))
    assert code == 1
    assert EUR_ISIN in capsys.readouterr().err


def test_main_distributing_total_return_fails_without_table(tmp_path, capsys):
    db, config, meta = _accum_db(
        tmp_path, 2011, 2020, lambda _y, _m: 0.0, distribution="Distributing",
    )
    code = sz.main(_argv(db, config, meta, ("--permutations", "20")))
    err = capsys.readouterr()
    assert code == 1
    assert "total-return" in err.err
    assert "Calendar seasonality" not in err.out


def test_main_unknown_distribution_total_return_fails(tmp_path, capsys):
    db, _config, meta = _accum_db(
        tmp_path, 2011, 2020, lambda _y, _m: 0.0, distribution="",
    )
    # empty distribution string is stored as missing
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.dump({"etfs": {EUR_ISIN: {"name": "X"}}}))
    code = sz.main(_argv(db, str(config_path), meta, to="2021-01-01"))
    assert code == 1
    assert "unknown" in capsys.readouterr().err


def test_main_price_mode_allows_distributing(tmp_path, capsys):
    db, config, meta = _accum_db(
        tmp_path, 2011, 2020, lambda _y, _m: 0.0, distribution="Distributing",
    )
    code = sz.main(_argv(db, config, meta, ("--price-mode", "price", "--permutations", "20")))
    out = capsys.readouterr().out
    assert code == 0
    assert "price return" in out
    assert "Calendar seasonality" in out


def test_main_gbp_without_fx(tmp_path, capsys):
    db, config, meta = _seed(
        tmp_path,
        prices=[(GBP_ISIN, "2024-01-01", 10.0)],
        currencies={GBP_ISIN: "GBP"},
        funds={GBP_ISIN: {"name": "Pound", "distribution": "Accumulating"}},
    )
    code = sz.main(_argv(db, config, meta, to="2024-12-31", isin=GBP_ISIN))
    assert code == 1
    assert "FX" in capsys.readouterr().err


def test_main_no_complete_months(tmp_path, capsys):
    db, config, meta = _seed(
        tmp_path,
        prices=[(EUR_ISIN, "2024-06-15", 10.0)],
        currencies={EUR_ISIN: "EUR"},
        funds={EUR_ISIN: {"name": "X", "distribution": "Accumulating"}},
    )
    code = sz.main(_argv(db, config, meta, to="2024-06-20"))
    assert code == 1
    assert "no complete calendar months" in capsys.readouterr().err


def test_main_thin_sample_table_without_p_values(tmp_path, capsys):
    db, config, meta = _accum_db(tmp_path, 2019, 2020, lambda _y, _m: 0.01)
    code = sz.main(_argv(db, config, meta, ("--permutations", "20")))
    out = capsys.readouterr().out
    assert code == 0
    assert "Jan" in out and "Dec" in out
    assert "DESCRIPTIVE - insufficient history" in out
    assert "UNAVAILABLE - DESCRIPTIVE only" in out
    assert "Permutation p-value:" not in out
    for line in sz.INTERPRETATION_FOOTER:
        assert line in out
    assert "winner" not in out.lower()
    assert "loser" not in out.lower()


def test_main_known_january_effect(tmp_path, capsys):
    db, config, meta = _accum_db(
        tmp_path, 2011, 2020, lambda _y, m: 0.10 if m == 1 else 0.0,
    )
    code = sz.main(_argv(db, config, meta, ("--permutations", "80", "--seed", "0")))
    out = capsys.readouterr().out
    assert code == 0
    assert "strongest mean" in out and "Jan" in out
    assert "Kruskal-Wallis H:" in out
    assert "Permutation p-value:" in out
    h_line = next(ln for ln in out.splitlines() if ln.startswith("Kruskal-Wallis H:"))
    p_line = next(ln for ln in out.splitlines() if ln.startswith("Permutation p-value:"))
    assert h_line != p_line
    assert "UNAVAILABLE" not in h_line


def test_main_no_effect_footer_is_non_prescriptive(tmp_path, capsys):
    db, config, meta = _accum_db(tmp_path, 2011, 2020, lambda _y, _m: 0.01)
    code = sz.main(_argv(db, config, meta, ("--permutations", "40")))
    out = capsys.readouterr().out
    assert code == 0
    assert "no statistically significant calendar-month effect" in out
    assert "does not establish that the variation is random" in out
    assert "consistent with random" not in out
    assert "no actionable seasonal strategy is established" in out
    assert "therefore" not in out.lower()
    for line in sz.INTERPRETATION_FOOTER:
        assert line in out


def test_main_seed_reproducible(tmp_path, capsys):
    db, config, meta = _accum_db(
        tmp_path, 2011, 2020, lambda y, m: 0.02 if (y + m) % 3 == 0 else -0.01,
    )
    args = _argv(db, config, meta, ("--permutations", "60", "--seed", "0"))
    assert sz.main(args) == 0
    first = capsys.readouterr().out
    assert sz.main(args) == 0
    second = capsys.readouterr().out
    assert first == second
    args_other = _argv(db, config, meta, ("--permutations", "60", "--seed", "1"))
    assert sz.main(args_other) == 0
    assert capsys.readouterr().out != first


def test_main_explain_records_provenance(tmp_path, capsys):
    db, config, meta = _accum_db(tmp_path, 2011, 2020, lambda _y, _m: 0.01)
    code = sz.main(_argv(db, config, meta, ("--permutations", "20", "--seed", "0", "--explain")))
    out = capsys.readouterr().out
    assert code == 0
    assert "calendar_seasonality_v1" in out
    assert "seed 0" in out
    assert "20 permutations" in out or "Permutations:       20" in out
    assert "total-return" in out
    assert "Partial handling" in out
    assert "Benjamini-Hochberg" in out


def test_main_cash_rate_without_rule_is_sketch_only(tmp_path, capsys):
    db, config, meta = _accum_db(tmp_path, 2011, 2020, lambda _y, _m: 0.01)
    code = sz.main(_argv(db, config, meta, ("--permutations", "20", "--cash-rate", "0.03")))
    out = capsys.readouterr().out
    assert code == 0
    assert "Opportunity-cost sketch" in out
    assert "constant-DCA" not in out


def test_main_avoid_month_prints_controls(tmp_path, capsys):
    db, config, meta = _accum_db(
        tmp_path, 2011, 2020, lambda _y, m: -0.05 if m == 9 else 0.01,
    )
    code = sz.main(_argv(
        db, config, meta, ("--permutations", "20", "--rule", "avoid-month", "--month", "9"),
    ))
    out = capsys.readouterr().out
    assert code == 0
    assert "A  constant-DCA" in out
    assert "B  avoid Sep" in out
    assert "C  cash-drag Sep" in out
    assert "no actionable seasonal strategy is established" in out or "Economic:" in out


def test_main_sit_out_omits_cash_drag_control(tmp_path, capsys):
    db, config, meta = _accum_db(tmp_path, 2011, 2020, lambda _y, _m: 0.01)
    code = sz.main(_argv(
        db, config, meta, ("--permutations", "20", "--rule", "sit-out-month", "--month", "3"),
    ))
    out = capsys.readouterr().out
    assert code == 0
    assert "sit-out Mar" in out
    assert "cash-drag" not in out


def test_main_historical_weakest_no_leakage(tmp_path, capsys):
    def r_fn(year, month):
        if month != 1:
            return 0.0
        return -0.10 if year <= 2018 else 0.50

    db, config, meta = _accum_db(tmp_path, 2011, 2021, r_fn)
    code = sz.main(_argv(
        db, config, meta,
        (
            "--from", "2011-01-01",
            "--permutations", "20",
            "--rule", "historical-weakest",
            "--training-from", "2011-01-01", "--training-to", "2019-01-01",
            "--test-from", "2019-01-01", "--test-to", "2022-01-01",
            "--explain",
        ),
        to="2022-01-01",
    ))
    out = capsys.readouterr().out
    assert code == 0
    assert "frozen Jan" in out
    assert "selected on training only" in out
    assert "no test leakage" in out


def test_main_oos_overlap_refused(tmp_path, capsys):
    db, config, meta = _accum_db(tmp_path, 2011, 2020, lambda _y, _m: 0.01)
    code = sz.main(_argv(
        db, config, meta,
        (
            "--permutations", "20",
            "--rule", "historical-weakest",
            "--training-from", "2011-01-01", "--training-to", "2019-01-01",
            "--test-from", "2018-06-01", "--test-to", "2021-01-01",
        ),
    ))
    out = capsys.readouterr().out
    assert code == 0
    assert "overlap" in out
    assert "UNAVAILABLE" in out
    assert "frozen" not in out.lower() or "Out-of-sample rule: UNAVAILABLE" in out


def test_main_oos_short_test_refused(tmp_path, capsys):
    db, config, meta = _accum_db(tmp_path, 2011, 2020, lambda _y, _m: 0.01)
    code = sz.main(_argv(
        db, config, meta,
        (
            "--permutations", "20",
            "--rule", "historical-weakest",
            "--training-from", "2011-01-01", "--training-to", "2019-01-01",
            "--test-from", "2019-01-01", "--test-to", "2020-01-01",
        ),
    ))
    out = capsys.readouterr().out
    assert code == 0
    assert f"N<{sz.SEASONALITY_MIN_N_OOS}" in out


def test_main_footer_wording_is_frozen(tmp_path, capsys):
    db, config, meta = _accum_db(tmp_path, 2011, 2020, lambda _y, _m: 0.01)
    sz.main(_argv(db, config, meta, ("--permutations", "10")))
    out = capsys.readouterr().out
    assert sz.INTERPRETATION_FOOTER == (
        "Descriptive only: monthly rankings describe this sample; they do not "
        "establish a tradable effect.",
        "Inference: statistical inference is unavailable below the minimum "
        "per-month sample floor.",
        "Selection: no month is automatically selected for trading.",
        "Actionability: a seasonal rule requires an explicitly pre-specified "
        "rule and, where applicable, a non-overlapping test period.",
    )
    for line in sz.INTERPRETATION_FOOTER:
        assert line in out


# ---------------------------------------------------------------------------
# Portfolio consensus (ADR-0027).
# ---------------------------------------------------------------------------


def _fund(
    isin: str, returns: list[sz.MonthReturn], *, skip: str | None = None,
) -> sz.FundSeasonality:
    return sz._fund_from_returns(isin, isin, "Accumulating", "EUR", returns, skip)


def test_consensus_counts_strongest_and_excludes_short_history():
    long_jan = [_mr(2011 + y, m, 0.10 if m == 1 else 0.0) for y in range(10) for m in range(1, 13)]
    long_aug = [_mr(2011 + y, m, 0.10 if m == 8 else 0.0) for y in range(10) for m in range(1, 13)]
    short = [_mr(2020, m, 0.50 if m == 5 else -0.50 if m == 3 else 0.0) for m in range(1, 13)]
    funds = [
        _fund("LONGJAN", long_jan),
        _fund("LONGAUG", long_aug),
        _fund("SHORT", short),
    ]
    assert funds[0].infer_ok and funds[1].infer_ok
    assert not funds[2].infer_ok
    rows = {r.month: r for r in sz.consensus_rows(funds)}
    assert rows[1].n_strongest == 1
    assert rows[8].n_strongest == 1
    assert rows[5].n_strongest == 0  # short fund does not vote
    assert rows[1].n_funds == 2


def test_cross_section_detects_shared_january_and_is_reproducible():
    funds = [
        _fund(
            f"F{i}",
            [_mr(2011 + y, m, 0.10 if m == 1 else 0.0) for y in range(10) for m in range(1, 13)],
        )
        for i in range(4)
    ]
    a = sz.cross_sectional_permutation(funds, permutations=80, seed=0)
    b = sz.cross_sectional_permutation(funds, permutations=80, seed=0)
    c = sz.cross_sectional_permutation(funds, permutations=80, seed=1)
    assert a is not None and a == b
    assert a.top_strongest_month == 1
    assert a.top_strongest_count == 4
    assert a.p_max_strongest < 0.05
    assert c is not None and a != c


def test_cross_section_none_without_inferential_funds():
    short = [_mr(2020, m, 0.0) for m in range(1, 13)]
    assert sz.cross_sectional_permutation([_fund("S", short)], 20, 0) is None


def test_main_requires_isin_or_portfolio():
    with pytest.raises(SystemExit) as exc:
        sz.main([])
    assert exc.value.code == 2
    with pytest.raises(SystemExit) as exc:
        sz.main(["--isin", EUR_ISIN, "--portfolio"])
    assert exc.value.code == 2


def test_portfolio_rejects_rule():
    with pytest.raises(SystemExit) as exc:
        sz.main(["--portfolio", "--rule", "avoid-month", "--month", "9"])
    assert exc.value.code == 2


def test_main_portfolio_splits_cohorts_and_prints_consensus(tmp_path, capsys):
    prices = []
    funds = {}
    currencies = {}
    for isin, r_fn, start, end in (
        ("IE00LONGJAN01", lambda _y, m: 0.10 if m == 1 else 0.0, 2011, 2020),
        ("IE00LONGJAN02", lambda _y, m: 0.10 if m == 1 else 0.0, 2011, 2020),
        ("IE00SHORT0001", lambda _y, m: 0.40 if m == 5 else 0.0, 2019, 2020),
    ):
        prices.extend(_month_path(isin, start, end, r_fn))
        funds[isin] = {"name": isin, "distribution": "Accumulating"}
        currencies[isin] = "EUR"
    db, config, meta = _seed(tmp_path, prices=prices, currencies=currencies, funds=funds)
    code = sz.main([
        "--portfolio", "--db", db, "--config", config, "--currency-meta", meta,
        "--to", "2021-01-01", "--permutations", "40", "--seed", "0", "--explain",
    ])
    out = capsys.readouterr().out
    assert code == 0
    assert "Portfolio seasonality consensus" in out
    assert "Inferential cohort" in out
    assert "DESCRIPTIVE - insufficient history" in out
    assert "IE00LONGJAN01" in out and "IE00SHORT0001" in out
    assert "Cross-sectional test" in out
    assert "Placebo probability (max concentration)" in out
    assert (
        "no statistically significant calendar-month effect" in out
        or "detected at the 5% level" in out
    )
    assert "no actionable seasonal strategy is established" in out
    assert "calendar_seasonality_v1" in out
    for line in sz.INTERPRETATION_FOOTER:
        assert line in out


def test_main_portfolio_skips_distributing_under_total_return(tmp_path, capsys):
    prices = _month_path("IE00DIST00001", 2011, 2020, lambda _y, _m: 0.01)
    db, config, meta = _seed(
        tmp_path,
        prices=prices,
        currencies={"IE00DIST00001": "EUR"},
        funds={"IE00DIST00001": {"name": "Dist", "distribution": "Distributing"}},
    )
    code = sz.main([
        "--portfolio", "--db", db, "--config", config, "--currency-meta", meta,
        "--to", "2021-01-01", "--permutations", "10",
    ])
    out = capsys.readouterr().out
    assert code == 0
    assert "Excluded" in out
    assert "Distributing" in out
    assert "UNAVAILABLE - no fund met the inference floor" in out


def test_equal_weight_balanced_panel_drops_incomplete_month_years():
    long = [_mr(2011 + y, m, 0.10 if m == 1 else 0.0) for y in range(10) for m in range(1, 13)]
    # Same span but missing January 2011.
    other = [r for r in long if not (r.year == 2011 and r.month == 1)]
    other += [_mr(2021, 1, 0.0)]  # extra year-month the first fund lacks
    funds = [_fund("A", long), _fund("B", other)]
    panel = sz.equal_weight_returns(funds)
    keys = {(r.year, r.month) for r in panel}
    assert (2011, 1) not in keys
    assert (2021, 1) not in keys
    assert (2012, 1) in keys
    jan = [r.ret for r in panel if r.month == 1]
    assert jan and all(v == pytest.approx(0.10) for v in jan)


def test_equal_weight_excludes_descriptive_and_detects_january():
    long = [_mr(2011 + y, m, 0.10 if m == 1 else 0.0) for y in range(10) for m in range(1, 13)]
    short = [_mr(2020, m, 0.50 if m == 5 else 0.0) for m in range(1, 13)]
    funds = [_fund("A", long), _fund("B", long), _fund("S", short)]
    panel = sz.equal_weight_returns(funds)
    assert all(r.month != 5 or r.ret == pytest.approx(0.0) for r in panel)
    stats = sz.month_stats(panel)
    assert sz.strongest_mean_month(stats) == 1
    perm = sz.permutation_test(panel, permutations=80, seed=0)
    assert perm.p_omnibus < 0.05


def test_cross_section_caveat_names_cohort_size():
    text = sz.cross_section_caveat(14)
    assert "not 14 independent replications" in text
    assert "not independent observations" in text


def test_main_portfolio_prints_equal_weight_and_caveat(tmp_path, capsys):
    prices = []
    funds = {}
    currencies = {}
    for isin in ("IE00LONGJAN01", "IE00LONGJAN02"):
        prices.extend(_month_path(isin, 2011, 2020, lambda _y, m: 0.10 if m == 1 else 0.0))
        funds[isin] = {"name": isin, "distribution": "Accumulating"}
        currencies[isin] = "EUR"
    db, config, meta = _seed(tmp_path, prices=prices, currencies=currencies, funds=funds)
    code = sz.main([
        "--portfolio", "--db", db, "--config", config, "--currency-meta", meta,
        "--to", "2021-01-01", "--permutations", "40", "--seed", "0",
    ])
    out = capsys.readouterr().out
    assert code == 0
    assert "Equal-weight book (balanced panel)" in out
    assert "not 2 independent replications" in out
    assert "Equal-weight seasonality test" in out
    assert "--evaluate" in out


# ---------------------------------------------------------------------------
# Frozen evaluation (ADR-0028).
# ---------------------------------------------------------------------------


def test_frozen_months_are_august_and_november():
    assert sz.FROZEN_WEAK_MONTH == 8
    assert sz.FROZEN_STRONG_MONTH == 11
    assert sz.FROZEN_SHIFT_MONTHS == (10, 11)


def test_shift_september_matches_avoid_august():
    dates, closes, _returns, fills = _path_for_sim()
    avoid = sz.simulate_seasonal(
        dates, closes, 100.0, 0.0, sz.DeployKind.AVOID, 8, fills, "skip",
    )
    shift = sz.simulate_seasonal(
        dates, closes, 100.0, 0.0, sz.DeployKind.SHIFT, 8, fills, "sep", 9,
    )
    assert avoid.terminal == pytest.approx(shift.terminal)
    assert sz.invariance_holds(shift, cash_rate=0.0)


def test_shift_october_holds_through_september_gain():
    dates, closes, _returns, fills = _path_for_sim(
        r_fn=lambda _y, m: 0.50 if m == 9 else 0.0,
    )
    skip = sz.simulate_seasonal(
        dates, closes, 100.0, 0.0, sz.DeployKind.AVOID, 8, fills, "sep",
    )
    later = sz.simulate_seasonal(
        dates, closes, 100.0, 0.0, sz.DeployKind.SHIFT, 8, fills, "oct", 10,
    )
    assert sz.invariance_holds(later, cash_rate=0.0)
    assert later.terminal < skip.terminal


def test_evaluate_battery_august_crash_beats_dca():
    dates, closes, returns, _fills = _path_for_sim(
        r_fn=lambda _y, m: -0.20 if m == 8 else 0.0,
    )
    results, scores = sz.evaluate_battery(dates, closes, returns, 100.0, 0.0)
    by_label = {r.label: r for r in results}
    assert by_label["August-skip"].terminal > by_label["A  constant-DCA"].terminal
    skip_score = dict(scores)["August-skip"]
    assert skip_score.better > 0
    assert skip_score.worse == 0


def test_evaluate_rejects_portfolio_rule_and_month():
    with pytest.raises(SystemExit) as exc:
        sz.main(["--evaluate", "--portfolio"])
    assert exc.value.code == 2
    with pytest.raises(SystemExit) as exc:
        sz.main(["--isin", EUR_ISIN, "--evaluate", "--rule", "avoid-month"])
    assert exc.value.code == 2
    with pytest.raises(SystemExit) as exc:
        sz.main(["--isin", EUR_ISIN, "--evaluate", "--month", "8"])
    assert exc.value.code == 2
    with pytest.raises(SystemExit) as exc:
        sz.main(["--evaluate"])
    assert exc.value.code == 2


def test_main_evaluate_prints_battery_not_discovery(tmp_path, capsys):
    db, config, meta = _accum_db(
        tmp_path, 2011, 2020, lambda _y, m: -0.20 if m == 8 else 0.0,
    )
    code = sz.main(_argv(
        db, config, meta,
        ("--evaluate", "--permutations", "10", "--explain"),
        to="2021-01-01",
    ))
    out = capsys.readouterr().out
    assert code == 0
    assert "Seasonal strategy evaluation" in out
    assert "Test A - contribution skip (primary)" in out
    assert "Test B - contribution shift" in out
    assert "Test C - full-portfolio sit-out (secondary)" in out
    assert "August-skip" in out
    assert "November-skip" in out
    assert "In-sample descriptive rankings" not in out
    assert "Kruskal-Wallis H:" not in out
    assert "no actionable seasonal strategy is established" in out
    for line in sz.INTERPRETATION_FOOTER:
        assert line in out
    assert "calendar_seasonality_v1" in out


def test_main_evaluate_split_labels_reverse_era(tmp_path, capsys):
    db, config, meta = _accum_db(
        tmp_path, 2011, 2020, lambda _y, m: -0.10 if m == 8 else 0.0,
    )
    code = sz.main(_argv(
        db, config, meta,
        (
            "--evaluate",
            "--training-from", "2016-01-01", "--training-to", "2021-01-01",
            "--test-from", "2011-01-01", "--test-to", "2016-01-01",
        ),
        to="2021-01-01",
    ))
    out = capsys.readouterr().out
    assert code == 0
    assert "discovery-era (not a test)" in out
    assert "reverse-era evaluation (not prospective)" in out


def test_main_evaluate_overlap_is_refused(tmp_path, capsys):
    db, config, meta = _accum_db(tmp_path, 2011, 2020, lambda _y, _m: 0.0)
    code = sz.main(_argv(
        db, config, meta,
        (
            "--evaluate",
            "--training-from", "2011-01-01", "--training-to", "2018-01-01",
            "--test-from", "2017-01-01", "--test-to", "2021-01-01",
        ),
        to="2021-01-01",
    ))
    err = capsys.readouterr()
    assert code == 1
    assert "overlap" in err.err
    assert "Seasonal strategy evaluation" not in err.out
