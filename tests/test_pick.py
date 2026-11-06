"""pick: below-cost tilt of one month's rebalance buys (ADR-0062)."""

import dataclasses
import sqlite3
from contextlib import closing

import pytest
import yaml
from hypothesis import given
from hypothesis import strategies as st

from e1f.common import Scenario, save_scenario
from e1f.experimental import pick
from e1f.experimental.pick import Pick, PickError, PickRow, PickRun, tilt_buys

A = "IE00FUND000A0"
B = "IE00FUND000B0"
C = "IE00FUND000C0"
D = "IE00FUND000D0"
E = "IE00FUND000E0"
F = "IE00FUND000F0"
G = "IE00FUND000G0"


# ---------------------------------------------------------------------------
# Pure tilt math — hand-computed pins.
# ---------------------------------------------------------------------------

PLAN = {A: 100.0, B: 50.0, C: 50.0, D: 0.0, E: 20.0}
VS_COST = {A: 0.10, B: -0.10, C: -0.20, D: -0.30, E: None}


def test_tilt_splits_by_plan_times_depth():
    # offered = 0.5 × 100 (A) = 50; weights B 50×0.1 = 5, C 50×0.2 = 10.
    buys, outcome, offered, moved = tilt_buys(PLAN, VS_COST, 0.5, 1)
    assert offered == pytest.approx(50.0)
    assert moved == pytest.approx(50.0)
    assert buys == pytest.approx({A: 50.0, B: 50.0 + 50 / 3, C: 50.0 + 100 / 3, D: 0.0, E: 20.0})
    assert outcome == {A: Pick.DONOR, B: Pick.BELOW, C: Pick.BELOW, D: Pick.NO_BUY,
                       E: Pick.NO_COST}


def test_capped_fund_passes_the_rest_on():
    # offered 50; weights B 50×0.1 = 5, C 10×0.4 = 4 → C's 22.22 is capped at 1 × 10,
    # and B takes the other 40 (under its 50 headroom).
    buys, outcome, _offered, moved = tilt_buys(
        {A: 100.0, B: 50.0, C: 10.0}, {A: 0.1, B: -0.1, C: -0.4}, 0.5, 1
    )
    assert moved == pytest.approx(50.0)
    assert buys == pytest.approx({A: 50.0, B: 90.0, C: 20.0})
    assert outcome[C] is Pick.CAPPED
    assert outcome[B] is Pick.BELOW


def test_pull_limit_returns_untaken_cash_to_donors():
    # tilt 1 offers 100, but one month of pull is 10 + 10.
    buys, outcome, offered, moved = tilt_buys(
        {A: 100.0, B: 10.0, C: 10.0}, {A: 0.1, B: -0.1, C: -0.2}, 1.0, 1
    )
    assert (offered, moved) == pytest.approx((100.0, 20.0))
    assert buys == pytest.approx({A: 80.0, B: 20.0, C: 20.0})
    assert outcome[B] is outcome[C] is Pick.CAPPED


def test_more_pull_months_take_more():
    buys, _out, _offered, moved = tilt_buys(
        {A: 100.0, B: 10.0, C: 10.0}, {A: 0.1, B: -0.1, C: -0.2}, 1.0, 3
    )
    assert moved == pytest.approx(60.0)
    assert buys == pytest.approx({A: 40.0, B: 40.0, C: 40.0})


def test_zero_pull_moves_nothing():
    buys, outcome, _offered, moved = tilt_buys(PLAN, VS_COST, 1.0, 0)
    assert buys == PLAN
    assert moved == 0.0
    assert outcome[B] is outcome[C] is Pick.CAPPED


def test_min_depth_turns_shallow_dips_into_donors():
    # min depth 15%: B (−10%) donates; offered 0.5 × (100 + 50) = 75, C takes its 50
    # headroom, and A and B each give 50 / 150 of their buy.
    buys, outcome, offered, moved = tilt_buys(PLAN, VS_COST, 0.5, 1, 0.15)
    assert (offered, moved) == pytest.approx((75.0, 50.0))
    assert buys == pytest.approx({A: 200 / 3, B: 100 / 3, C: 100.0, D: 0.0, E: 20.0})
    assert outcome[B] is Pick.DONOR
    assert outcome[C] is Pick.CAPPED


def test_overweight_fund_below_cost_is_never_picked():
    buys, outcome, _o, _m = tilt_buys(PLAN, VS_COST, 1.0, 10)
    assert buys[D] == 0.0
    assert outcome[D] is Pick.NO_BUY


def test_nothing_moves_without_donors_or_recipients():
    buys, _out, offered, moved = tilt_buys({A: 10.0, B: 10.0}, {A: -0.1, B: -0.2}, 1.0, 1)
    assert buys == {A: 10.0, B: 10.0}
    assert (offered, moved) == (0.0, 0.0)
    buys, _out, offered, _m = tilt_buys({A: 10.0, B: 10.0}, {A: 0.1, B: 0.0}, 1.0, 1)
    assert buys == {A: 10.0, B: 10.0}
    assert offered == 0.0


def test_zero_tilt_keeps_the_plan():
    buys, _out, _o, moved = tilt_buys(PLAN, VS_COST, 0.0, 1)
    assert buys == PLAN
    assert moved == 0.0


@pytest.mark.parametrize(("tilt", "pull", "depth"),
                         [(-0.1, 1, 0.0), (1.1, 1, 0.0), (0.5, -1, 0.0), (0.5, 1, 1.0)])
def test_tilt_rejects_bad_parameters(tilt, pull, depth):
    with pytest.raises(ValueError):
        tilt_buys(PLAN, VS_COST, tilt, pull, depth)


_amount = st.floats(min_value=0.0, max_value=1e5)
_gap = st.one_of(st.none(), st.floats(min_value=-0.9, max_value=0.9))


@given(
    st.dictionaries(st.sampled_from([A, B, C, D, E, F]), st.tuples(_amount, _gap), min_size=1),
    st.floats(min_value=0.0, max_value=1.0),
    st.integers(min_value=0, max_value=6),
    st.floats(min_value=0.0, max_value=0.5),
)
def test_tilt_keeps_the_total_and_bounds(funds, tilt, pull, depth):
    plan = {isin: amount for isin, (amount, _g) in funds.items()}
    vs_cost = {isin: gap for isin, (_a, gap) in funds.items()}
    buys, outcome, offered, moved = tilt_buys(plan, vs_cost, tilt, pull, depth)
    assert sum(buys.values()) == pytest.approx(sum(plan.values()), rel=1e-9, abs=1e-6)
    assert moved <= offered + 1e-9
    for isin, buy in buys.items():
        assert -1e-9 <= buy <= (1 + pull) * plan[isin] + 1e-6
        if buy > plan[isin] + 1e-6:
            assert outcome[isin] in (Pick.BELOW, Pick.CAPPED)
            assert vs_cost[isin] is not None and vs_cost[isin] <= -depth


def test_plan_limit_is_the_close_plus_buffer():
    assert pick.plan_limit(10.0, 0.005) == pytest.approx(10.05)
    assert pick.plan_limit(None, 0.005) is None
    assert pick.plan_limit(0.0, 0.005) is None


def test_extra_limit_stays_below_cost_and_under_the_plan_limit():
    assert pick.extra_limit(10.0, 0.02, 10.5) == pytest.approx(9.8)   # cost bound
    assert pick.extra_limit(10.0, 0.02, 9.045) == pytest.approx(9.045)  # plan bound
    assert pick.extra_limit(None, 0.02, 9.0) is None
    assert pick.extra_limit(0.0, 0.02, 9.0) is None
    assert pick.extra_limit(10.0, 0.02, None) is None


def test_tilt_gain_nets_the_moved_euros():
    # +50 into a fund up 10%, −50 out of a flat one → +5.
    assert pick.tilt_gain([(50.0, 10.0, 11.0), (-50.0, 20.0, 20.0), (0.0, 0.0, None)]) == (
        pytest.approx(5.0))
    assert pick.tilt_gain([(50.0, 10.0, None)]) is None


# ---------------------------------------------------------------------------
# DB-backed evaluate / CLI.
# ---------------------------------------------------------------------------


def _seed(tmp_path, *, transactions, prices):
    db = tmp_path / "e1f.db"
    if db.exists():  # helpers share one book per test
        return str(db), str(tmp_path / "config.yaml"), str(tmp_path / "meta.yaml")
    with closing(sqlite3.connect(str(db))) as conn:
        conn.execute(
            "CREATE TABLE transactions (broker TEXT, transaction_id TEXT, datetime TEXT, "
            "symbol TEXT, side TEXT, shares REAL, price REAL, fee REAL, tax REAL, "
            "PRIMARY KEY (broker, transaction_id))"
        )
        conn.execute("CREATE TABLE prices (isin TEXT, date TEXT, close REAL, "
                     "PRIMARY KEY (isin, date))")
        conn.execute("CREATE TABLE fx_rates (base TEXT, quote TEXT, date TEXT, rate REAL, "
                     "PRIMARY KEY (base, quote, date))")
        conn.executemany("INSERT INTO transactions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                         transactions)
        conn.executemany("INSERT INTO prices VALUES (?, ?, ?)", prices)
        conn.commit()
    isins = sorted({p[0] for p in prices})
    config = tmp_path / "config.yaml"
    config.write_text(yaml.dump({"etfs": {i: {"name": f"Fund {i[-2]}"} for i in isins}}))
    meta = tmp_path / "meta.yaml"
    meta.write_text(yaml.dump({i: {"currency": "EUR", "symbol": f"{i}:X:EUR", "xid": "1"}
                               for i in isins}))
    return str(db), str(config), str(meta)


def _buy(txid, isin, shares, price, broker="tr"):
    return (broker, txid, "2024-01-01", isin, "BUY", shares, price, 0.0, 0.0)


def _book(tmp_path):
    # A: 100 @ 10 → 11 (+10%); B: 100 @ 10 → 9 (−10%), half at each broker;
    # F frozen: 10 @ 10 → 5 (−50%); C targeted, not held.  On 2024-01-03 B is 9.9.
    return _seed(
        tmp_path,
        transactions=[
            _buy("t1", A, 100.0, 10.0),
            _buy("t2", B, 50.0, 9.0, broker="xtb"),
            _buy("t3", B, 50.0, 11.0),
            _buy("t4", F, 10.0, 10.0),
        ],
        prices=[(A, "2024-01-02", 11.0), (B, "2024-01-02", 9.0), (F, "2024-01-02", 5.0),
                (A, "2024-01-03", 11.0), (B, "2024-01-03", 9.9), (F, "2024-01-03", 5.0)],
    )


def _scenario(tmp_path, **overrides):
    body = {"name": "core", "targets": {A: 40.0, B: 40.0, C: 20.0}, "months": 4,
            "frozen": (F,), "monthly": 250.0}
    body.update(overrides)
    path = str(tmp_path / "s.yaml")
    save_scenario(Scenario(**body), path)
    return path


def _run(tmp_path, *, tilt=0.5, pull=1, as_of="2024-01-02", **scenario):
    db, config, meta = _book(tmp_path)
    return pick.evaluate(db, config, meta, scenario_name="core",
                         scenarios_path=_scenario(tmp_path, **scenario),
                         as_of=as_of, tilt=tilt, pull=pull)


def _main(tmp_path, *extra, scenario=None):
    db, config, meta = _book(tmp_path)
    sf = _scenario(tmp_path, **(scenario or {}))
    return pick.main(["--scenarios-file", sf, "--db", db, "--config", config,
                      "--currency-meta", meta, *extra])


def test_evaluate_pins_one_month(tmp_path):
    # Sleeve S = 1100 + 900 + 0 = 2000; budget 4 × 250 ≥ C_min 750 → L = 3000:
    # plan A 0.4·3000 − 1100 = 100, B 1200 − 900 = 300, C 600, ÷ 4 months.
    # A donates 0.5 × 25.  Max unit prices: plan = close × 1.005 (A 11.055, B 9.045);
    # B's extra = min(10 × (1 − 2%), 9.045) = 9.045.  C has no price → no limit.
    run = _run(tmp_path)
    rows = {r.isin: r for r in run.rows}
    assert set(rows) == {A, B, C}  # the frozen fund is never a row
    assert {i: r.plan for i, r in rows.items()} == pytest.approx({A: 25.0, B: 75.0, C: 150.0})
    assert {i: r.pick for i, r in rows.items()} == pytest.approx({A: 12.5, B: 87.5, C: 150.0})
    assert {i: r.outcome for i, r in rows.items()} == {A: Pick.DONOR, B: Pick.BELOW,
                                                      C: Pick.NO_COST}
    assert rows[B].avg_cost == pytest.approx(10.0)  # netted across brokers
    assert rows[B].vs_cost == pytest.approx(-0.1)
    assert rows[A].plan_limit == pytest.approx(11.055)
    assert rows[B].plan_limit == pytest.approx(9.045)
    assert rows[B].limit == pytest.approx(9.045)
    assert rows[A].limit is None and rows[C].limit is None and rows[C].plan_limit is None
    assert rows[C].avg_cost is None and rows[C].vs_cost is None
    assert (run.offered, run.moved) == pytest.approx((12.5, 12.5))


def test_one_month_plan_has_nothing_to_pull(tmp_path):
    run = _run(tmp_path, months=1, monthly=1000.0)
    assert (run.pull, run.pull_requested, run.moved) == (0, 1, 0.0)
    lines = pick._summary(run)
    assert any("The pull limit is 0 months" in line for line in lines)
    assert "--pull 1 reduced to 0: the plan has 1 month(s)." in lines


def test_cli_output_discloses_limits_and_method(tmp_path, capsys):
    code = _main(tmp_path, "--scenario", "core", "--tilt", "50", "--as-of", "2024-01-02",
                 "--explain")
    out = capsys.readouterr().out
    assert code == 0
    assert "Moved €12.50 from 1 donor fund(s) to 1 at least 2% below cost" in out
    assert "      9.04       9.04  below cost" in out  # 9 × 1.005 = 9.0449…
    assert "Orders are buy-limits at a max unit price: Plan€ at MaxPlan€ (last close + 0.5%)" in out
    assert "If the Δ€ order does not fill" in out
    assert "run 'e1f fetch'" not in out
    assert "below_cost_tilt_v2" in out


def test_cli_output_discloses_stale_prices_and_shallow_dips(tmp_path, capsys):
    # As of 2024-01-04 the latest close is 2024-01-03's: B at 9.9 is only 1% below cost.
    code = _main(tmp_path, "--scenario", "core", "--tilt", "50", "--as-of", "2024-01-04")
    out = capsys.readouterr().out
    assert code == 0
    assert "9.90~" in out
    assert f"latest close before 2024-01-04: {B} (2024-01-03), {A} (2024-01-03)" in out
    assert "No fund the plan buys is at least 2% below its average cost" in out
    assert "run 'e1f fetch' before placing orders" in out
    assert "If the Δ€ order does not fill" not in out


def test_unavailable_plan_is_typed_and_disclosed(tmp_path):
    run = _run(tmp_path, frozen=())  # F is held but neither targeted nor frozen
    assert run.reason == "unclassified"
    assert not run.feasible and run.rows == []
    assert any("UNAVAILABLE — the rebalance plan is unclassified" in line
               for line in pick.render(run, explain=False))


def test_unknown_scenario_and_flat_monthly_are_errors(tmp_path, capsys):
    assert _main(tmp_path, "--scenario", "nope", "--tilt", "10") == 1
    assert "no scenario named 'nope'" in capsys.readouterr().err
    db, config, meta = _book(tmp_path)
    flat = str(tmp_path / "flat.yaml")
    save_scenario(Scenario("flat", {A: 50.0, B: 50.0}), flat)
    assert pick.main(["--scenario", "flat", "--tilt", "10", "--scenarios-file", flat,
                      "--db", db, "--config", config, "--currency-meta", meta,
                      "--monthly", "100"]) == 1
    assert "--monthly needs a sleeve scenario" in capsys.readouterr().err


@pytest.mark.parametrize("argv", [
    [],                                              # no scenario / tilt
    ["--scenario", "core"],                          # no tilt
    ["--review", "--scenario", "core"],
    ["--scenario", "core", "--tilt", "10", "--replace"],
])
def test_cli_rejects_bad_combinations(argv):
    with pytest.raises(SystemExit):
        pick.main(argv)


@pytest.mark.parametrize(("flag", "value"), [("--tilt", "101"), ("--tilt", "x"),
                                             ("--tilt", "nan"), ("--pull", "-1"),
                                             ("--pull", "x"), ("--min-depth", "100"),
                                             ("--buffer", "100"),
                                             ("--months", "0"), ("--monthly", "-1"),
                                             ("--as-of", "2024-13-01")])
def test_parser_rejects_bad_values(flag, value):
    argv = ["--scenario", "core", "--tilt", "10", flag, value]
    with pytest.raises(SystemExit):
        pick._build_parser().parse_args(argv)


# ---------------------------------------------------------------------------
# Record and review.
# ---------------------------------------------------------------------------


def _stored(db):
    with closing(sqlite3.connect(db)) as conn:
        return conn.execute("SELECT isin, as_of, plan, pick, price FROM picks "
                            "ORDER BY isin").fetchall()


def test_record_then_review_pins_the_gain(tmp_path, capsys):
    # Recorded 2024-01-02: B +12.5 at 9.0, A −12.5 at 11.0. By 2024-01-03 B is 9.9
    # (+10%) and A flat → gain 12.5 × 0.10 = 1.25, i.e. +10% on the moved €12.50.
    db, _config, meta = _book(tmp_path)
    run = _run(tmp_path)
    assert pick.record(db, run) == 3
    assert _stored(db)[1] == (B, "2024-01-02", 75.0, 87.5, 9.0)
    rev = pick.review(db, meta, as_of="2024-01-03")
    assert [(r.month, r.scenario, r.moved) for r in rev.rows] == [("2024-01", "core", 12.5)]
    assert rev.rows[0].gain == pytest.approx(1.25)
    assert pick.main(["--review", "--as-of", "2024-01-03", "--db", db,
                      "--currency-meta", meta, "--explain"]) == 0
    out = capsys.readouterr().out
    assert "2024-01  core                 2024-01-02      12.50       1.25   +10.0%" in out
    assert "1 priced month(s): fewer than 24 is an anecdote" in out
    assert "below_cost_tilt_review_v1" in out


def test_review_ignores_picks_after_its_date(tmp_path):
    db, _config, meta = _book(tmp_path)
    pick.record(db, _run(tmp_path, as_of="2024-01-03"))
    assert pick.review(db, meta, as_of="2024-01-02").rows == []


def test_record_refuses_a_recorded_month_unless_replaced(tmp_path, capsys):
    assert _main(tmp_path, "--scenario", "core", "--tilt", "50", "--as-of", "2024-01-02",
                 "--record") == 0
    assert "Recorded 3 fund(s) for core 2024-01." in capsys.readouterr().out
    assert _main(tmp_path, "--scenario", "core", "--tilt", "50", "--as-of", "2024-01-03",
                 "--record") == 1
    assert "already has a pick recorded for 2024-01 (computed 2024-01-02)" in (
        capsys.readouterr().err)
    db = str(tmp_path / "e1f.db")
    assert {row[1] for row in _stored(db)} == {"2024-01-02"}
    assert _main(tmp_path, "--scenario", "core", "--tilt", "50", "--as-of", "2024-01-03",
                 "--record", "--replace") == 0
    assert {row[1] for row in _stored(db)} == {"2024-01-03"}


def test_failed_replace_keeps_the_previous_record(tmp_path):
    db, _config, _meta = _book(tmp_path)
    run = _run(tmp_path)
    pick.record(db, run)
    before = _stored(db)
    broken = dataclasses.replace(
        run, as_of="2024-01-03",
        rows=[run.rows[0], dataclasses.replace(run.rows[1], plan=None)],
    )
    with pytest.raises(sqlite3.IntegrityError):
        pick.record(db, broken, replace=True)
    assert _stored(db) == before


def test_record_refuses_an_unavailable_plan(tmp_path):
    db, _config, _meta = _book(tmp_path)
    with pytest.raises(PickError, match="nothing to record: the rebalance plan is unclassified"):
        pick.record(db, _run(tmp_path, frozen=()))


def test_review_without_records(tmp_path, capsys):
    db, _config, meta = _book(tmp_path)
    assert pick.review(db, meta, as_of="2024-01-03").rows == []
    assert pick.main(["--review", "--db", db, "--currency-meta", meta]) == 0
    assert "No picks recorded" in capsys.readouterr().out


def test_review_discloses_an_unpriced_month(tmp_path, capsys):
    db, _config, meta = _book(tmp_path)
    pick.record(db, _run(tmp_path))
    with closing(sqlite3.connect(db)) as conn, conn:
        conn.execute("INSERT INTO picks VALUES ('2024-01', 'other', ?, '2024-01-02', "
                     "10, 15, 5.0, 0.5, 1, 0.02)", (G,))
    rev = pick.review(db, meta, as_of="2024-01-03")
    other = next(r for r in rev.rows if r.scenario == "other")
    assert other.gain is None and other.unpriced == (G,)
    assert len(rev.priced) == 1
    lines = pick.render_review(rev, explain=False)
    assert f"UNAVAILABLE (no price now, excluded from TOTAL): 2024-01 other ({G})" in lines


# ---------------------------------------------------------------------------
# Summary disclosures.
# ---------------------------------------------------------------------------


def _row(isin, plan, pick_, outcome, vs_cost):
    return PickRow(isin, "", 10.0, 10.0 * (1 + vs_cost), "2024-01-02", False, plan, pick_,
                   outcome)


def _summary_run(rows, offered=0.0, moved=0.0):
    return PickRun("core", "2024-01-02", 12, 0.5, 1, 0.02, rows, None, offered, moved)


def test_summary_when_nothing_is_below_cost():
    run = _summary_run([_row(A, 10.0, 10.0, Pick.DONOR, 0.1)])
    assert pick._summary(run) == ["No fund the plan buys is at least 2% below its average "
                                  "cost — Pick€ = Plan€."]


def test_summary_when_everything_is_below_cost():
    run = _summary_run([_row(A, 10.0, 10.0, Pick.BELOW, -0.1)])
    assert "Every fund the plan buys is at least 2% below cost" in pick._summary(run)[0]


def test_summary_names_pull_limits_and_overweight_funds_below_cost():
    run = _summary_run(
        [_row(A, 100.0, 90.0, Pick.DONOR, 0.1), _row(B, 10.0, 20.0, Pick.CAPPED, -0.1),
         _row(D, 0.0, 0.0, Pick.NO_BUY, -0.3)],
        offered=50.0, moved=10.0,
    )
    lines = pick._summary(run)
    assert f"Pull limit bound: {B} at 1 month(s) of plan; €40.00 stayed with the donors." in lines
    assert f"Below cost but not bought by the plan (overweight): {D}." in lines
