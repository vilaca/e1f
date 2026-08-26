"""backtest: contribution-timing simulation core + the command (ADR-0019).

The pure core (``simulate_strategy`` and friends) lives in ``common`` so it is
tested here without IO; the invariance property test is the centerpiece.
"""

import random
import sqlite3
from contextlib import closing
from datetime import date, timedelta

import pytest
import yaml

from e1f.experimental import backtest as bt
from e1f.experimental.common import (
    BACKTEST_MIN_CONTRIBUTIONS,
    DeployMode,
    SignalSpec,
    StrategyParams,
    _max_drawdown,
    blind_schedule,
    deployment_fraction,
    monthly_fill_indices,
    running_high,
    simulate_strategy,
)

ATH = SignalSpec(lookback=None)


# ---------------------------------------------------------------------------
# Signal + deployment primitives.
# ---------------------------------------------------------------------------


def test_running_high_rolling_vs_ath():
    closes = [10.0, 12.0, 8.0, 9.0, 7.0]
    assert running_high(closes, 4, None) == 12.0            # all-time high
    assert running_high(closes, 4, 2) == 9.0                # trailing 2 days: max(9,7)
    assert running_high(closes, 0, 3) == 10.0               # window clipped at series start


def test_deployment_fraction_deadzone_clamp_curvature():
    dca = StrategyParams("dip", 0.75, 5.0, 2.0, 0.0)
    assert deployment_fraction(0.0, dca) == 0.0             # no drawdown, nothing deployed
    assert deployment_fraction(0.10, dca) == pytest.approx(0.05)   # 5 * 0.10^2
    assert deployment_fraction(0.80, dca) == 1.0            # clamped to the full reserve
    deadzoned = StrategyParams("dz", 0.75, 5.0, 2.0, 0.05)
    assert deployment_fraction(0.04, deadzoned) == 0.0      # inside the dead-zone
    assert deployment_fraction(0.15, deadzoned) == pytest.approx(5 * 0.10 ** 2)


def test_max_drawdown_peak_to_trough():
    assert _max_drawdown([100.0, 120.0, 60.0, 90.0]) == pytest.approx(0.5)  # 120 -> 60
    assert _max_drawdown([100.0, 110.0, 130.0]) == 0.0                       # monotone up


def test_blind_schedule_even_delayed_and_random():
    # even: an equal share of the remaining months, emptying at the horizon.
    assert blind_schedule(DeployMode.EVEN, 4, 0, None) == pytest.approx([0.25, 1 / 3, 0.5, 1.0])
    # delayed: nothing before L, even after, still emptying at the horizon.
    delayed = blind_schedule(DeployMode.DELAYED, 5, 3, None)
    assert delayed == pytest.approx([0.0, 0.0, 0.0, 0.5, 1.0])
    # random: every fraction in [0,1], last forced to 1.0 (empties), reproducible.
    sched = blind_schedule(DeployMode.RANDOM, 6, 0, random.Random(0))
    assert all(0.0 <= f <= 1.0 for f in sched) and sched[-1] == 1.0
    assert blind_schedule(DeployMode.RANDOM, 6, 0, random.Random(0)) == sched   # same seed
    assert blind_schedule(DeployMode.RANDOM, 6, 0, random.Random(1)) != sched   # diff seed


def test_monthly_fill_indices_one_per_month_on_or_after():
    # Daily series across three months; contribution fills on the first close >= the 1st.
    dates = ["2021-01-04", "2021-01-20", "2021-02-01", "2021-02-15", "2021-03-02"]
    fills = monthly_fill_indices(dates, 0, len(dates) - 1)
    assert [dates[i] for i in fills] == ["2021-01-04", "2021-02-01", "2021-03-02"]


def test_monthly_fill_indices_empty_on_bad_range():
    assert monthly_fill_indices([], 0, 0) == []
    assert monthly_fill_indices(["2021-01-01"], 1, 0) == []


# ---------------------------------------------------------------------------
# The simulator — degenerate equivalences and the golden.
# ---------------------------------------------------------------------------


def _monthly(closes: list[float]) -> tuple[list[str], list[float]]:
    """A price series with one point per month (dates all on the 1st)."""
    dates = []
    y, m = 2021, 1
    for _ in closes:
        dates.append(date(y, m, 1).isoformat())
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return dates, list(closes)


def test_beta_one_is_constant_dca():
    dates, closes = _monthly([10.0, 20.0, 25.0])
    fills = monthly_fill_indices(dates, 0, len(dates) - 1)
    r = simulate_strategy(
        dates, closes, fills, StrategyParams("dca", 1.0, 0.0, 1.0, 0.0), ATH, 100.0
    )
    assert r.reserve_contributed == 0.0
    assert r.reserve_cash == 0.0
    assert r.equity_cost == pytest.approx(r.total_invested)
    # shares = 100/10 + 100/20 + 100/25 = 19; terminal = 19 * 25
    assert r.shares == pytest.approx(19.0)
    assert r.terminal_wealth == pytest.approx(475.0)


def test_a_zero_is_cash_drag_all_reserve_leftover():
    dates, closes = _monthly([10.0, 9.0, 8.0, 12.0])
    fills = monthly_fill_indices(dates, 0, len(dates) - 1)
    r = simulate_strategy(
        dates, closes, fills, StrategyParams("cd", 0.75, 0.0, 1.0, 0.0), ATH, 100.0
    )
    assert r.reserve_deployed == 0.0
    assert r.reserve_cash == pytest.approx(r.reserve_contributed) == pytest.approx(100.0)  # 4 * 25


def test_monotone_rising_never_deploys():
    dates, closes = _monthly([10.0, 11.0, 12.0, 13.0, 14.0])
    fills = monthly_fill_indices(dates, 0, len(dates) - 1)
    dip = simulate_strategy(
        dates, closes, fills, StrategyParams("dip", 0.75, 9.0, 2.0, 0.0), ATH, 100.0
    )
    assert dip.reserve_deployed == 0.0                       # every day is a new high => D = 0
    assert dip.reserve_cash == pytest.approx(dip.reserve_contributed)


def test_lump_sum_invests_everything_at_t0():
    dates, closes = _monthly([10.0, 5.0, 20.0])
    fills = monthly_fill_indices(dates, 0, len(dates) - 1)
    r = simulate_strategy(
        dates, closes, fills,
        StrategyParams("lump", 1.0, 0.0, 1.0, 0.0, lump_sum=True), ATH, 100.0,
    )
    assert r.equity_cost == pytest.approx(300.0)             # whole budget at t0
    assert r.reserve_cash == 0.0
    assert r.shares == pytest.approx(30.0)                   # 300 / 10
    assert r.terminal_wealth == pytest.approx(600.0)         # 30 * 20


def test_golden_dip_deploys_into_the_dip():
    # Hand-computed: β=0.5, a=1, b=1, C=100, ATH. See ADR-0019 test notes.
    dates, closes = _monthly([10.0, 5.0, 20.0])
    fills = monthly_fill_indices(dates, 0, len(dates) - 1)
    r = simulate_strategy(dates, closes, fills, StrategyParams("g", 0.5, 1.0, 1.0, 0.0), ATH, 100.0)
    assert r.shares == pytest.approx(27.5)
    assert r.equity_cost == pytest.approx(200.0)
    assert r.reserve_cash == pytest.approx(100.0)
    assert r.terminal_wealth == pytest.approx(650.0)


def test_cash_rate_grows_idle_reserve():
    dates, closes = _monthly([10.0] * 13)                    # flat, so nothing ever deploys
    fills = monthly_fill_indices(dates, 0, len(dates) - 1)
    params = StrategyParams("cd", 0.75, 0.0, 1.0, 0.0)
    flat = simulate_strategy(dates, closes, fills, params, ATH, 100.0, cash_rate=0.0)
    grown = simulate_strategy(dates, closes, fills, params, ATH, 100.0, cash_rate=0.05)
    assert grown.reserve_cash > flat.reserve_cash            # 5% grows the idle pile
    assert flat.reserve_cash == pytest.approx(flat.reserve_contributed)


# ---------------------------------------------------------------------------
# Centerpiece: invariance by construction (ADR-0019 decision 2).
# ---------------------------------------------------------------------------


def test_invariance_total_invested_equals_equity_plus_cash():
    rng = random.Random(20260826)
    for _ in range(50):
        n = rng.randint(15, 40)
        price = 100.0
        closes = []
        for _ in range(n):
            price *= 1.0 + rng.uniform(-0.15, 0.15)
            closes.append(price)
        dates, closes = _monthly(closes)
        fills = monthly_fill_indices(dates, 0, len(dates) - 1)
        params = StrategyParams(
            "rand",
            base_fraction=rng.uniform(0.3, 1.0),
            aggressiveness=rng.uniform(0.0, 20.0),
            curvature=rng.uniform(0.5, 3.0),
            deadzone=rng.uniform(0.0, 0.1),
        )
        r = simulate_strategy(
            dates, closes, fills, params, SignalSpec(rng.choice([None, 3, 6])), 1000.0
        )
        # ∑ contributions is exactly N·C ...
        assert r.total_invested == pytest.approx(1000.0 * len(fills))
        # ... and it is conserved: every euro is either in shares or in the reserve.
        assert r.total_invested == pytest.approx(r.equity_cost + r.reserve_cash, abs=1e-6)
        assert r.reserve_cash >= -1e-9
        assert r.reserve_deployed <= r.reserve_contributed + 1e-9   # cash-rate 0 => deploy ≤ inflow


# ---------------------------------------------------------------------------
# Blind-deployment controls (ADR-0020).
# ---------------------------------------------------------------------------


def test_blind_even_empties_the_reserve_by_the_horizon():
    dates, closes = _monthly([10.0, 9.0, 8.0, 12.0, 11.0, 15.0])
    fills = monthly_fill_indices(dates, 0, len(dates) - 1)
    r = simulate_strategy(
        dates, closes, fills,
        StrategyParams("be", 0.75, 0.0, 1.0, 0.0, deploy=DeployMode.EVEN), ATH, 100.0,
    )
    assert r.reserve_cash == pytest.approx(0.0, abs=1e-9)              # fully reinvested
    assert r.reserve_deployed == pytest.approx(r.reserve_contributed)  # every euro deployed


def test_blind_delayed_defers_then_empties():
    dates, closes = _monthly([10.0, 11.0, 12.0, 13.0, 14.0, 15.0])    # rising
    fills = monthly_fill_indices(dates, 0, len(dates) - 1)
    n = len(fills)
    even = simulate_strategy(
        dates, closes, fills,
        StrategyParams("be", 0.75, 0.0, 1.0, 0.0, deploy=DeployMode.EVEN), ATH, 100.0,
    )
    late = simulate_strategy(
        dates, closes, fills,
        StrategyParams("bd", 0.75, 0.0, 1.0, 0.0, deploy=DeployMode.DELAYED, delay_months=n),
        ATH, 100.0,
    )
    assert late.reserve_cash == pytest.approx(0.0, abs=1e-9)          # still empties at horizon
    # deferring all deployment to the horizon on a rising series buys fewer shares
    assert even.shares > late.shares


def test_blind_random_is_reproducible_and_empties():
    dates, closes = _monthly([10.0, 12.0, 9.0, 11.0, 13.0, 8.0, 14.0])
    fills = monthly_fill_indices(dates, 0, len(dates) - 1)

    def run(seed):
        return simulate_strategy(
            dates, closes, fills,
            StrategyParams("br", 0.75, 0.0, 1.0, 0.0, deploy=DeployMode.RANDOM, seed=seed),
            ATH, 100.0,
        )

    a, a2, b = run(7), run(7), run(8)
    assert a.shares == pytest.approx(a2.shares)                       # same seed → identical
    assert a.terminal_wealth == pytest.approx(a2.terminal_wealth)
    assert a.shares != pytest.approx(b.shares)                        # a different seed diverges
    assert a.reserve_cash == pytest.approx(0.0, abs=1e-9)             # empties by the horizon


@pytest.mark.parametrize(
    "mode", [DeployMode.SIGNAL, DeployMode.EVEN, DeployMode.DELAYED, DeployMode.RANDOM]
)
def test_invariance_holds_for_every_deploy_mode(mode):
    # ADR-0020: a bounded fraction of the current reserve preserves ADR-0019's
    # conservation law for every mode, and the blind modes empty the reserve.
    rng = random.Random(4)
    for _ in range(20):
        n = rng.randint(15, 40)
        price, closes = 100.0, []
        for _ in range(n):
            price *= 1.0 + rng.uniform(-0.15, 0.15)
            closes.append(price)
        dates, closes = _monthly(closes)
        fills = monthly_fill_indices(dates, 0, len(dates) - 1)
        params = StrategyParams(
            "m",
            base_fraction=rng.uniform(0.3, 1.0),
            aggressiveness=rng.uniform(0.0, 20.0),
            curvature=rng.uniform(0.5, 3.0),
            deadzone=rng.uniform(0.0, 0.1),
            deploy=mode, delay_months=rng.randint(0, 5), seed=rng.randint(0, 9999),
        )
        r = simulate_strategy(
            dates, closes, fills, params, SignalSpec(rng.choice([None, 3, 6])), 1000.0
        )
        assert r.total_invested == pytest.approx(1000.0 * len(fills))
        assert r.total_invested == pytest.approx(r.equity_cost + r.reserve_cash, abs=1e-6)
        assert r.reserve_cash >= -1e-9
        if mode != DeployMode.SIGNAL:
            assert r.reserve_cash == pytest.approx(0.0, abs=1e-6)     # blind ⇒ emptied


# ---------------------------------------------------------------------------
# EUR-series assembly + catalog.
# ---------------------------------------------------------------------------

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


def _daily_prices(isin, start, n, price_fn):
    """``n`` consecutive daily rows starting at ``start`` (YYYY-MM-DD)."""
    d0 = date.fromisoformat(start)
    return [(isin, (d0 + timedelta(days=i)).isoformat(), float(price_fn(i))) for i in range(n)]


def test_eur_series_passthrough_and_conversion_and_drop(tmp_path):
    db, _config, meta = _seed(
        tmp_path,
        prices=[
            (EUR_ISIN, "2024-01-01", 10.0), (EUR_ISIN, "2024-01-02", 11.0),
            (USD_ISIN, "2024-01-01", 100.0), (USD_ISIN, "2024-01-02", 120.0),
        ],
        fx=[("EUR", "USD", "2024-01-02", 1.2)],  # no rate on the 1st -> that USD day is dropped
        currencies={EUR_ISIN: "EUR", USD_ISIN: "USD"},
    )
    _eur_dates, eur_closes, ccy = bt.eur_series(db, EUR_ISIN, "2024-12-31", meta)
    assert ccy == "EUR" and eur_closes == [10.0, 11.0]                # passthrough
    usd_dates, usd_closes, ccy = bt.eur_series(db, USD_ISIN, "2024-12-31", meta)
    assert ccy == "USD" and usd_dates == ["2024-01-02"]              # 1st dropped (no FX)
    assert usd_closes == [pytest.approx(100.0)]                      # 120 / 1.2


def test_price_catalog_and_candidate_listing(tmp_path):
    db, config, _meta = _seed(
        tmp_path,
        prices=[(EUR_ISIN, "2024-01-01", 10.0), (EUR_ISIN, "2024-01-02", 11.0)],
        funds={EUR_ISIN: {"name": "Euro Fund", "distribution": "Accumulating"}},
    )
    catalog = bt.price_catalog(db)
    assert catalog == [(EUR_ISIN, 2, "2024-01-01", "2024-01-02")]
    listing = bt._candidate_listing(db, bt.ConfigManager(config))
    assert EUR_ISIN in listing and "Euro Fund" in listing and "acc" in listing


def test_price_catalog_no_table(tmp_path):
    empty = tmp_path / "empty.db"
    with closing(sqlite3.connect(str(empty))) as conn:
        conn.execute("CREATE TABLE unrelated (x INTEGER)")
    assert bt.price_catalog(str(empty)) == []


# ---------------------------------------------------------------------------
# Strategy parsing + construction.
# ---------------------------------------------------------------------------

_DEFAULTS = {"base_fraction": 0.75, "aggressiveness": 5.0, "curvature": 2.0, "deadzone": 0.0}


def test_parse_strategy_defaults_and_overrides():
    s = bt.parse_strategy("", _DEFAULTS)
    assert (s.base_fraction, s.aggressiveness, s.curvature, s.deadzone) == (0.75, 5.0, 2.0, 0.0)
    assert s.label == "dip(β=0.75,a=5,b=2)"
    s2 = bt.parse_strategy("beta=0.6,a=10,label=steep", _DEFAULTS)
    assert s2.base_fraction == 0.6 and s2.aggressiveness == 10.0 and s2.label == "steep"


def test_parse_strategy_errors():
    with pytest.raises(bt.BacktestError):
        bt.parse_strategy("nope", _DEFAULTS)              # missing '='
    with pytest.raises(bt.BacktestError):
        bt.parse_strategy("zzz=1", _DEFAULTS)             # unknown key
    with pytest.raises(bt.BacktestError):
        bt.parse_strategy("a=lots", _DEFAULTS)            # non-numeric


@pytest.mark.parametrize(
    "spec", ["beta=2", "base=-0.1", "a=-10", "b=0", "b=-1", "d0=1.5", "deadzone=-0.2"]
)
def test_parse_strategy_rejects_out_of_bounds(spec):
    # --strategy routes through the same validators as the top-level knobs, so a
    # value the CLI would reject (β∉[0,1], a<0, b≤0, D0∉[0,1]) is rejected here too.
    with pytest.raises(bt.BacktestError):
        bt.parse_strategy(spec, _DEFAULTS)


def test_build_strategies_benchmarks_then_dips():
    parser = bt._build_parser()
    args = parser.parse_args(
        ["--isin", EUR_ISIN, "--strategy", "a=5", "--strategy", "a=12,label=x"]
    )
    strategies = bt.build_strategies(args)
    labels = [s.label for s in strategies]
    # ADR-0020: a matched cash-drag + blind-even sits between the universal
    # benchmarks and the dips (one pair here — both dips share β=0.75).
    assert labels[:4] == ["lump-sum", "constant-DCA", "cash-drag(β=0.75)", "blind-even(β=0.75)"]
    assert labels[4:] == ["dip(β=0.75,a=5,b=2)", "x"]
    assert strategies[0].lump_sum is True


@pytest.mark.parametrize(
    ("specs", "match"),
    [
        (["a=5,label=constant-DCA"], "reserved"),   # shadows the DCA benchmark
        (["a=5,label=lump-sum"], "reserved"),        # shadows the lump-sum benchmark
        (["a=5,label=foo", "a=9,label=foo"], "duplicate"),   # two custom labels collide
        (["a=5", "a=5"], "duplicate"),               # identical params → identical auto-label
    ],
)
def test_build_strategies_rejects_label_collisions(specs, match):
    argv = ["--isin", EUR_ISIN]
    for spec in specs:
        argv += ["--strategy", spec]
    args = bt._build_parser().parse_args(argv)
    with pytest.raises(bt.BacktestError, match=match):
        bt.build_strategies(args)


# ---------------------------------------------------------------------------
# The command end-to-end.
# ---------------------------------------------------------------------------


def _seed_run_db(
    tmp_path, isin=EUR_ISIN, *, currency="EUR", distribution="Accumulating", months=40
):
    # ~months of daily EUR prices with a dip in the middle, so contributions >= 12.
    n = months * 31
    def price(i):
        return 100.0 * (1.0 + 0.4 * (i / n)) * (0.7 if n // 3 < i < n // 2 else 1.0)
    prices = _daily_prices(isin, "2020-01-01", n, price)
    return _seed(
        tmp_path,
        prices=prices,
        currencies={isin: currency},
        funds={isin: {"name": f"Fund {isin}", "distribution": distribution}},
    )


def test_main_missing_isin_is_argparse_error():
    with pytest.raises(SystemExit) as exc:
        bt.main([])
    assert exc.value.code == 2


def test_main_no_prices_at_all(tmp_path, capsys):
    db, config, meta = _seed(tmp_path, prices=[])
    code = bt.main(["--isin", EUR_ISIN, "--db", db, "--config", config, "--currency-meta", meta])
    assert code == 1
    assert "run 'e1f fetch'" in capsys.readouterr().err


def test_main_unknown_isin_lists_candidates(tmp_path, capsys):
    db, config, meta = _seed_run_db(tmp_path)
    code = bt.main(
        ["--isin", "IE00MISSING01", "--db", db, "--config", config, "--currency-meta", meta]
    )
    assert code == 1
    assert EUR_ISIN in capsys.readouterr().err            # candidate list names the real series


def test_main_gbp_without_fx_reports_currency_gap(tmp_path, capsys):
    db, config, meta = _seed(
        tmp_path,
        prices=[(GBP_ISIN, "2024-01-01", 10.0), (GBP_ISIN, "2024-01-02", 11.0)],
        currencies={GBP_ISIN: "GBP"},
        funds={GBP_ISIN: {"name": "Pound Fund"}},
    )
    code = bt.main(["--isin", GBP_ISIN, "--db", db, "--config", config, "--currency-meta", meta])
    assert code == 1
    assert "no EUR/GBP FX rate" in capsys.readouterr().err


def test_main_too_short_span(tmp_path, capsys):
    db, config, meta = _seed_run_db(tmp_path, months=6)   # 6 months < 12 contributions
    code = bt.main([
        "--isin", EUR_ISIN, "--db", db, "--config", config, "--currency-meta", meta,
        "--drawdown-ref", "all-time-high",
    ])
    assert code == 1
    assert str(BACKTEST_MIN_CONTRIBUTIONS) in capsys.readouterr().err


def test_main_single_run_outputs_table(tmp_path, capsys):
    db, config, meta = _seed_run_db(tmp_path)
    code = bt.main([
        "--isin", EUR_ISIN, "--db", db, "--config", config, "--currency-meta", meta,
        "--drawdown-ref", "all-time-high",
    ])
    assert code == 0
    out = capsys.readouterr().out
    assert "Contribution-timing backtest" in out
    for label in ("lump-sum", "constant-DCA", "cash-drag", "dip("):
        assert label in out
    assert "never fitted or ranked" in out              # anti-overfit note


def test_main_distributing_series_warns(tmp_path, capsys):
    db, config, meta = _seed_run_db(tmp_path, distribution="Distributing")
    bt.main([
        "--isin", EUR_ISIN, "--db", db, "--config", config, "--currency-meta", meta,
        "--drawdown-ref", "all-time-high",
    ])
    assert "Distributing" in capsys.readouterr().err


def test_main_explain_emits_provenance(tmp_path, capsys):
    db, config, meta = _seed_run_db(tmp_path)
    bt.main([
        "--isin", EUR_ISIN, "--db", db, "--config", config, "--currency-meta", meta,
        "--drawdown-ref", "all-time-high", "--explain",
    ])
    out = capsys.readouterr().out
    assert "Provenance" in out and "contribution_timing_backtest_v1" in out


def test_main_window_sweep_outputs_distribution(tmp_path, capsys):
    db, config, meta = _seed_run_db(tmp_path, months=40)
    code = bt.main([
        "--isin", EUR_ISIN, "--db", db, "--config", config, "--currency-meta", meta,
        "--drawdown-ref", "all-time-high", "--window", "12",
    ])
    assert code == 0
    out = capsys.readouterr().out
    assert "Rolling-window backtest" in out and "Win%" in out


def test_main_prints_decomposition_and_blind_even_control(tmp_path, capsys):
    db, config, meta = _seed_run_db(tmp_path)
    code = bt.main([
        "--isin", EUR_ISIN, "--db", db, "--config", config, "--currency-meta", meta,
        "--drawdown-ref", "all-time-high", "--blind-seeds", "8",
    ])
    assert code == 0
    out = capsys.readouterr().out
    assert "blind-even(β=0.75)" in out                    # matched control in the table
    assert "Decomposition (matched β" in out
    for col in ("ReserveCost€", "DeployBenefit€", "TimingBenefit€"):
        assert col in out
    assert "timing benefit = dip−blind-even" in out


def test_main_blind_random_block_default_on_and_disablable(tmp_path, capsys):
    db, config, meta = _seed_run_db(tmp_path)
    common = [
        "--isin", EUR_ISIN, "--db", db, "--config", config, "--currency-meta", meta,
        "--drawdown-ref", "all-time-high",
    ]
    bt.main([*common, "--blind-seeds", "16"])
    assert "Blind-random robustness (16 seeds" in capsys.readouterr().out
    bt.main([*common, "--blind-seeds", "0"])              # 0 disables the block
    assert "Blind-random robustness" not in capsys.readouterr().out


def test_main_matched_controls_per_distinct_beta(tmp_path, capsys):
    db, config, meta = _seed_run_db(tmp_path)
    bt.main([
        "--isin", EUR_ISIN, "--db", db, "--config", config, "--currency-meta", meta,
        "--drawdown-ref", "all-time-high", "--blind-seeds", "0",
        "--strategy", "beta=0.9,a=5,label=hi", "--strategy", "beta=0.5,a=5,label=lo",
    ])
    out = capsys.readouterr().out
    for label in ("cash-drag(β=0.9)", "blind-even(β=0.9)", "cash-drag(β=0.5)", "blind-even(β=0.5)"):
        assert label in out                              # every β gets its own matched pair


def test_main_explain_records_blind_seeds(tmp_path, capsys):
    db, config, meta = _seed_run_db(tmp_path)
    bt.main([
        "--isin", EUR_ISIN, "--db", db, "--config", config, "--currency-meta", meta,
        "--drawdown-ref", "all-time-high", "--blind-seeds", "32", "--explain",
    ])
    out = capsys.readouterr().out
    assert "blind-random over 32 fixed seeds 0…31" in out
    assert "timing benefit = dip − blind-even" in out


def test_parse_strategy_deploy_delay_seed():
    even = bt.parse_strategy("deploy=even", _DEFAULTS)
    assert even.deploy is DeployMode.EVEN and even.label == "blind-even(β=0.75)"
    late = bt.parse_strategy("deploy=delayed,delay=6,label=w6", _DEFAULTS)
    assert late.deploy is DeployMode.DELAYED and late.delay_months == 6 and late.label == "w6"
    rnd = bt.parse_strategy("deploy=random,seed=3", _DEFAULTS)
    assert rnd.deploy is DeployMode.RANDOM and rnd.seed == 3
    assert bt.parse_strategy("deploy=random", _DEFAULTS).seed == 0   # reproducible default
    with pytest.raises(bt.BacktestError, match="deploy must be one of"):
        bt.parse_strategy("deploy=nope", _DEFAULTS)
    with pytest.raises(bt.BacktestError, match="delay"):
        bt.parse_strategy("deploy=delayed,delay=-1", _DEFAULTS)


def test_main_blind_seeds_negative_is_argparse_error():
    with pytest.raises(SystemExit) as exc:
        bt.main(["--isin", EUR_ISIN, "--blind-seeds", "-1"])
    assert exc.value.code == 2


def test_main_window_below_minimum_is_argparse_error(tmp_path):
    db, config, meta = _seed_run_db(tmp_path)
    with pytest.raises(SystemExit) as exc:
        bt.main([
            "--isin", EUR_ISIN, "--db", db, "--config", config, "--currency-meta", meta,
            "--window", "5",
        ])
    assert exc.value.code == 2


def test_main_cash_rate_flag_runs(tmp_path, capsys):
    db, config, meta = _seed_run_db(tmp_path)
    code = bt.main([
        "--isin", EUR_ISIN, "--db", db, "--config", config, "--currency-meta", meta,
        "--drawdown-ref", "all-time-high", "--cash-rate", "0.03",
    ])
    assert code == 0 and "reserve cash-rate 3.0%" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Reporting-semantics regression tests (review Tier A + #3/#14). These pin the
# behaviours a refactor could silently break — chiefly the --from/crash-span bug.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad", ["0", "-5"])
def test_main_lookback_must_be_positive(bad):
    # Validated at parse time — no db needed; a clean argparse error, exit 2.
    with pytest.raises(SystemExit) as exc:
        bt.main(["--isin", EUR_ISIN, "--lookback", bad])
    assert exc.value.code == 2


def test_run_header_crash_span_is_the_test_span_not_the_data_span():
    # Data covers COVID 2020 (2020-02-19 → 2020-03-23); only the FIRST contribution
    # date decides crash inclusion, so a later --from must drop COVID (review #2).
    dates = ["2020-01-06", "2020-02-03", "2020-03-02", "2020-06-01",
             "2020-09-01", "2020-12-01", "2021-06-01"]

    def crash_line(fills):
        line = next(x for x in bt._run_header("X", "X", dates, fills, 1000.0, ATH, 0.0)
                    if x.startswith("Crashes:"))
        tested, _, absent = line.partition("absent:")
        return tested, absent

    # Contributions start 2020-06-01 (after COVID) → COVID is absent, never tested.
    tested, absent = crash_line([3, 4, 5, 6])
    assert "COVID 2020" in absent and "COVID 2020" not in tested
    # Contributions start 2020-01-06 (spanning COVID) → COVID is tested.
    tested, absent = crash_line([0, 1, 2, 3])
    assert "COVID 2020" in tested and "COVID 2020" not in absent


def test_main_single_run_reports_data_test_and_horizons(tmp_path, capsys):
    db, config, meta = _seed_run_db(tmp_path, months=40)   # ~3.3y → no 10y horizon
    bt.main([
        "--isin", EUR_ISIN, "--db", db, "--config", config, "--currency-meta", meta,
        "--drawdown-ref", "all-time-high",
    ])
    out = capsys.readouterr().out
    assert "Data:" in out and "Test:" in out
    assert "Horizons:" in out and "10y ✗" in out           # 3.3y span reaches no horizon


def test_main_explain_lists_terminals_and_never_ranks(tmp_path, capsys):
    db, config, meta = _seed_run_db(tmp_path)
    bt.main([
        "--isin", EUR_ISIN, "--db", db, "--config", config, "--currency-meta", meta,
        "--drawdown-ref", "all-time-high", "--explain",
    ])
    out = capsys.readouterr().out
    assert "descriptive, not ranked" in out and "highest terminal" not in out
    assert "native close × nearest-prior EUR/quote FX" in out   # provenance clause (#11/#13)


def test_main_window_reports_per_window_crash_coverage_and_horizon_counts(tmp_path, capsys):
    db, config, meta = _seed_run_db(tmp_path, months=40)
    bt.main([
        "--isin", EUR_ISIN, "--db", db, "--config", config, "--currency-meta", meta,
        "--drawdown-ref", "all-time-high", "--window", "12",
    ])
    out = capsys.readouterr().out
    assert "coverage across windows" in out
    for cname in ("dot-com 2000-2002", "GFC 2007-2009", "COVID 2020", "2022 bear"):
        assert cname in out          # every crash listed, incl. the 0/N absent ones
    assert "Feasible contribution windows:" in out and "120-month:" in out


# ---------------------------------------------------------------------------
# Daily dip-slice strategy (ADR-0021).
# ---------------------------------------------------------------------------

DAILY_DIP = SignalSpec(lookback=None)   # daily-dip ignores the signal; any spec works


def _daily(closes, start="2021-01-01"):
    """A price series with one point per consecutive calendar day."""
    d0 = date.fromisoformat(start)
    dates = [(d0 + timedelta(days=i)).isoformat() for i in range(len(closes))]
    return dates, list(closes)


def _daily_dip(slices):
    return StrategyParams("dd", 1.0, 0.0, 1.0, 0.0, deploy=DeployMode.DAILY_DIP, slices=slices)


def test_daily_dip_golden_buys_down_days_and_finishes_each_month():
    # Two months (a Jan window then a Feb window); N=2 slices, C=100 → 50 each.
    dates = [
        "2021-01-01", "2021-01-02", "2021-01-03", "2021-01-04", "2021-01-05",  # month A
        "2021-02-01", "2021-02-02", "2021-02-03",                               # month B
    ]
    closes = [10.0, 9.0, 11.0, 8.0, 12.0, 20.0, 15.0, 25.0]
    fills = monthly_fill_indices(dates, 0, len(dates) - 1)
    assert [dates[i] for i in fills] == ["2021-01-01", "2021-02-01"]
    r = simulate_strategy(dates, closes, fills, _daily_dip(2), DAILY_DIP, 100.0)
    # Month A: dip@9 (day2) + dip@8 (day4) → 50/9 + 50/8. Month B: dip@15 (day2)
    # + last-day dump of the remaining 50 @25 → 50/15 + 50/25.
    expected = 50 / 9 + 50 / 8 + 50 / 15 + 50 / 25
    assert r.shares == pytest.approx(expected)
    assert r.equity_cost == pytest.approx(200.0)             # both months fully deployed
    assert r.reserve_cash == 0.0                             # no cross-month reserve, ever


def test_daily_dip_monotone_up_still_fully_deploys():
    # No down day ever — catch-up + the last-day dump still deploy C every month.
    dates, closes = _daily([100.0 + i for i in range(70)])   # strictly rising, ~2.3 months
    fills = monthly_fill_indices(dates, 0, len(dates) - 1)
    r = simulate_strategy(dates, closes, fills, _daily_dip(20), DAILY_DIP, 1000.0)
    assert r.reserve_cash == 0.0
    assert r.equity_cost == pytest.approx(1000.0 * len(fills))


def test_daily_dip_more_slices_than_trading_days_dumps_remainder():
    # A month with only a few trading days but N=10 slices: it cannot spread them,
    # so the last day dumps the remainder — still exactly C per month.
    dates = ["2021-01-01", "2021-01-02", "2021-02-01", "2021-02-02", "2021-02-03"]
    closes = [10.0, 12.0, 11.0, 13.0, 9.0]
    fills = monthly_fill_indices(dates, 0, len(dates) - 1)
    r = simulate_strategy(dates, closes, fills, _daily_dip(10), DAILY_DIP, 100.0)
    assert r.equity_cost == pytest.approx(200.0)
    assert r.reserve_cash == 0.0


def test_daily_dip_invariance_reserve_cash_is_exactly_zero():
    rng = random.Random(20260826)
    for _ in range(40):
        n_days = rng.randint(60, 200)
        price, closes = 100.0, []
        for _ in range(n_days):
            price *= 1.0 + rng.uniform(-0.05, 0.05)
            closes.append(price)
        dates, closes = _daily(closes)
        fills = monthly_fill_indices(dates, 0, len(dates) - 1)
        r = simulate_strategy(
            dates, closes, fills, _daily_dip(rng.randint(1, 30)), DAILY_DIP, 1000.0,
        )
        assert r.total_invested == pytest.approx(1000.0 * len(fills))
        assert r.reserve_cash == 0.0                          # unconditional (ADR-0021 §2)
        assert r.equity_cost == pytest.approx(r.total_invested, abs=1e-6)
        assert r.reserve_contributed == 0.0 and r.reserve_deployed == 0.0


def test_daily_dip_ignores_cash_rate():
    dates, closes = _daily([100.0 + (i % 5) - 2 for i in range(90)])
    fills = monthly_fill_indices(dates, 0, len(dates) - 1)
    a = simulate_strategy(dates, closes, fills, _daily_dip(20), DAILY_DIP, 1000.0, cash_rate=0.0)
    b = simulate_strategy(dates, closes, fills, _daily_dip(20), DAILY_DIP, 1000.0, cash_rate=0.10)
    assert a.terminal_wealth == pytest.approx(b.terminal_wealth)   # no reserve to grow


def test_parse_strategy_daily_dip_slices_and_label():
    s = bt.parse_strategy("deploy=daily-dip,n=30", _DEFAULTS)
    assert s.deploy is DeployMode.DAILY_DIP and s.slices == 30
    assert s.label == "daily-dip(N=30)"
    d = bt.parse_strategy("deploy=daily-dip", {**_DEFAULTS, "slices": 20})
    assert d.slices == 20 and d.label == "daily-dip(N=20)"
    with pytest.raises(bt.BacktestError, match="slices must be"):
        bt.parse_strategy("deploy=daily-dip,n=0", _DEFAULTS)


def test_build_strategies_daily_dip_gets_no_beta_controls():
    args = bt._build_parser().parse_args(
        ["--isin", EUR_ISIN, "--strategy", "deploy=daily-dip,n=15"]
    )
    strategies = bt.build_strategies(args)
    labels = [s.label for s in strategies]
    # Only the universal benchmarks precede it — no cash-drag / blind-even, because
    # daily-dip holds no reserve to match a β against.
    assert labels == ["lump-sum", "constant-DCA", "daily-dip(N=15)"]


def test_main_daily_dip_row_and_no_signal_warmup(tmp_path, capsys):
    db, config, meta = _seed_run_db(tmp_path, months=40)
    code = bt.main([
        "--isin", EUR_ISIN, "--db", db, "--config", config, "--currency-meta", meta,
        "--strategy", "deploy=daily-dip,n=20", "--explain",
    ])
    assert code == 0
    out = capsys.readouterr().out
    assert "daily-dip(N=20)" in out
    assert "slices each month's C" in out                    # provenance clause (ADR-0021)


def test_main_daily_dip_in_window_sweep(tmp_path, capsys):
    db, config, meta = _seed_run_db(tmp_path, months=40)
    bt.main([
        "--isin", EUR_ISIN, "--db", db, "--config", config, "--currency-meta", meta,
        "--strategy", "deploy=daily-dip,n=20", "--window", "12",
    ])
    out = capsys.readouterr().out
    assert "daily-dip(N=20)" in out and "Win%" in out


# ---------------------------------------------------------------------------
# Carry-forward daily dip-slice strategy (ADR-0023).
# ---------------------------------------------------------------------------


def _daily_dip_carry(slices):
    return StrategyParams(
        "ddc", 1.0, 0.0, 1.0, 0.0, deploy=DeployMode.DAILY_DIP_CARRY, slices=slices,
    )


def test_daily_dip_carry_golden_flushes_accrued_pool_on_a_dip():
    # Month A (6 days, N=6 → slice 20): days 1-3 up (accrue), day 4 dips and spends
    # the whole accrued pool (4 slices = 80 @8), day 6 (last) flushes the rest (40 @9).
    # Month B (4 days): day 2 dips (40 @15), day 4 (last) flushes the remainder (80 @10).
    dates = [
        "2021-01-01", "2021-01-02", "2021-01-03", "2021-01-04", "2021-01-05", "2021-01-06",
        "2021-02-01", "2021-02-02", "2021-02-03", "2021-02-04",
    ]
    closes = [10.0, 11.0, 12.0, 8.0, 13.0, 9.0, 20.0, 15.0, 25.0, 10.0]
    fills = monthly_fill_indices(dates, 0, len(dates) - 1)
    assert [dates[i] for i in fills] == ["2021-01-01", "2021-02-01"]
    r = simulate_strategy(dates, closes, fills, _daily_dip_carry(6), DAILY_DIP, 120.0)
    expected = 80 / 8 + 40 / 9 + 40 / 15 + 80 / 10
    assert r.shares == pytest.approx(expected)
    assert r.equity_cost == pytest.approx(240.0)             # both months fully deployed
    assert r.reserve_cash == 0.0                             # no cross-month reserve


def test_daily_dip_carry_leans_harder_into_a_deep_dip_than_daily_dip():
    # One month, three flat days then a deep dip then a spike. daily-dip places one
    # slice per (catch-up) day and only one on the dip; carry holds everything back
    # and dumps the full accrued pool on the deep dip — so it buys strictly more.
    dates = ["2021-01-01", "2021-01-02", "2021-01-03", "2021-01-04", "2021-01-05"]
    closes = [10.0, 10.0, 10.0, 5.0, 20.0]
    fills = monthly_fill_indices(dates, 0, len(dates) - 1)
    dd = simulate_strategy(dates, closes, fills, _daily_dip(4), DAILY_DIP, 100.0)
    ddc = simulate_strategy(dates, closes, fills, _daily_dip_carry(4), DAILY_DIP, 100.0)
    assert ddc.shares == pytest.approx(100 / 5)              # whole C spent on the 5.0 dip
    assert ddc.shares > dd.shares
    assert ddc.equity_cost == pytest.approx(dd.equity_cost)  # same C, different days


def test_daily_dip_carry_invariance_reserve_cash_is_exactly_zero():
    rng = random.Random(20260826)
    for _ in range(40):
        n_days = rng.randint(60, 200)
        price, closes = 100.0, []
        for _ in range(n_days):
            price *= 1.0 + rng.uniform(-0.05, 0.05)
            closes.append(price)
        dates, closes = _daily(closes)
        fills = monthly_fill_indices(dates, 0, len(dates) - 1)
        r = simulate_strategy(
            dates, closes, fills, _daily_dip_carry(rng.randint(1, 30)), DAILY_DIP, 1000.0,
        )
        assert r.total_invested == pytest.approx(1000.0 * len(fills))
        assert r.reserve_cash == 0.0                          # unconditional (ADR-0023)
        assert r.equity_cost == pytest.approx(r.total_invested, abs=1e-6)
        assert r.reserve_contributed == 0.0 and r.reserve_deployed == 0.0


def test_parse_strategy_daily_dip_carry_slices_and_label():
    s = bt.parse_strategy("deploy=daily-dip-carry,n=30", _DEFAULTS)
    assert s.deploy is DeployMode.DAILY_DIP_CARRY and s.slices == 30
    assert s.label == "daily-dip-carry(N=30)"
    d = bt.parse_strategy("deploy=daily-dip-carry", {**_DEFAULTS, "slices": 20})
    assert d.slices == 20 and d.label == "daily-dip-carry(N=20)"


def test_build_strategies_daily_dip_carry_gets_no_beta_controls():
    args = bt._build_parser().parse_args(
        ["--isin", EUR_ISIN, "--strategy", "deploy=daily-dip-carry,n=15"]
    )
    labels = [s.label for s in bt.build_strategies(args)]
    assert labels == ["lump-sum", "constant-DCA", "daily-dip-carry(N=15)"]


def test_main_daily_dip_carry_row_and_provenance(tmp_path, capsys):
    db, config, meta = _seed_run_db(tmp_path, months=40)
    code = bt.main([
        "--isin", EUR_ISIN, "--db", db, "--config", config, "--currency-meta", meta,
        "--strategy", "deploy=daily-dip-carry,n=20", "--explain",
    ])
    assert code == 0
    out = capsys.readouterr().out
    assert "daily-dip-carry(N=20)" in out
    assert "spends every accrued-but-unspent slice" in out    # provenance clause (ADR-0023)


def test_main_daily_dip_carry_in_window_sweep(tmp_path, capsys):
    db, config, meta = _seed_run_db(tmp_path, months=40)
    bt.main([
        "--isin", EUR_ISIN, "--db", db, "--config", config, "--currency-meta", meta,
        "--strategy", "deploy=daily-dip-carry,n=20", "--window", "12",
    ])
    out = capsys.readouterr().out
    assert "daily-dip-carry(N=20)" in out and "Win%" in out


# ---------------------------------------------------------------------------
# Daily-sampled interim drawdown (ADR-0022).
# ---------------------------------------------------------------------------


def test_max_drawdown_is_daily_and_catches_intramonth_trough():
    # Fill-day prices rise monotonically (a monthly/fill-sampled curve would show
    # ZERO drawdown), but a sharp mid-month crash on day 45 fully recovers by the
    # next fill. Daily sampling (ADR-0022) must catch that trough.
    closes = [100.0 + 0.5 * i for i in range(90)]
    closes[45] = 40.0                                    # mid-Feb crash, recovers next day
    dates, closes = _daily(closes)
    fills = monthly_fill_indices(dates, 0, len(dates) - 1)
    # every fill lands on a rising day → a fill-sampled drawdown would be 0
    assert _max_drawdown([closes[i] for i in fills]) == 0.0
    dca = StrategyParams("dca", 1.0, 0.0, 1.0, 0.0)
    r = simulate_strategy(dates, closes, fills, dca, DAILY_DIP, 1000.0)
    assert r.max_drawdown > 0.5                          # daily curve sees the crash


def test_daily_drawdown_leaves_terminal_and_invariance_untouched():
    # ADR-0022 changes only the drawdown sampling — wealth/XIRR/accounting are the same.
    dates, closes = _monthly([10.0, 9.0, 8.0, 12.0, 11.0, 15.0, 14.0])
    fills = monthly_fill_indices(dates, 0, len(dates) - 1)
    r = simulate_strategy(
        dates, closes, fills, StrategyParams("cd", 0.6, 3.0, 2.0, 0.0), ATH, 1000.0,
    )
    assert r.total_invested == pytest.approx(r.equity_cost + r.reserve_cash, abs=1e-6)
    assert r.total_invested == pytest.approx(1000.0 * len(fills))
