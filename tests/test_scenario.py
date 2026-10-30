"""Scenario file I/O (common) and the `scenario` CRUD command (ADR-0017)."""

import pytest
import yaml

from e1f import scenario as scn
from e1f.common import (
    RebalancePlan,
    Scenario,
    ScenarioError,
    delete_scenario,
    get_scenario,
    load_scenarios,
    post_rebalance_weights,
    save_scenario,
)

A = "IE00FUND000A0"
B = "IE00FUND000B0"
C = "IE00FUND000C0"


# ---------------------------------------------------------------------------
# common: load / get / save / delete round-trips + malformed-file errors
# ---------------------------------------------------------------------------


def test_load_missing_file_is_empty(tmp_path):
    assert load_scenarios(str(tmp_path / "nope.yaml")) == {}


def test_save_get_roundtrip_and_upsert_preserves_others(tmp_path):
    path = str(tmp_path / "s.yaml")
    save_scenario(Scenario("core", {A: 60.0, B: 40.0}, months=10), path)
    save_scenario(Scenario("bonds", {C: 100.0}), path)

    core = get_scenario("core", path)
    assert core.targets == {A: 60.0, B: 40.0}
    assert core.months == 10
    assert get_scenario("bonds", path).months is None

    # Upsert 'core' — 'bonds' must survive.
    existed = save_scenario(Scenario("core", {A: 100.0}), path)
    assert existed is True
    assert get_scenario("core", path).targets == {A: 100.0}
    assert "bonds" in load_scenarios(path)


def test_save_reports_new_vs_existing(tmp_path):
    path = str(tmp_path / "s.yaml")
    assert save_scenario(Scenario("core", {A: 50.0}), path) is False
    assert save_scenario(Scenario("core", {A: 50.0}), path) is True


def test_months_omitted_from_yaml_when_none(tmp_path):
    path = str(tmp_path / "s.yaml")
    save_scenario(Scenario("core", {A: 50.0}), path)
    raw = yaml.safe_load((tmp_path / "s.yaml").read_text())
    assert "months" not in raw["scenarios"]["core"]


def test_delete_removes_and_missing_raises(tmp_path):
    path = str(tmp_path / "s.yaml")
    save_scenario(Scenario("core", {A: 50.0}), path)
    delete_scenario("core", path)
    assert load_scenarios(path) == {}
    with pytest.raises(ScenarioError):
        delete_scenario("core", path)


def test_get_missing_raises(tmp_path):
    path = str(tmp_path / "s.yaml")
    save_scenario(Scenario("core", {A: 50.0}), path)
    with pytest.raises(ScenarioError, match="no scenario named 'ghost'"):
        get_scenario("ghost", path)


@pytest.mark.parametrize(
    "body, match",
    [
        ([], "root must be a mapping"),
        ({"scenarios": ["oops"]}, "must be a mapping of name"),
        ({"scenarios": {"core": []}}, "must be a mapping"),
        ({"scenarios": {"core": {}}}, "non-empty 'targets'"),
        ({"scenarios": {"core": {"targets": {A: "x"}}}}, "non-numeric percent"),
        ({"scenarios": {"core": {"targets": {A: 5}, "months": "x"}}}, "must be an integer"),
        ({"scenarios": {"core": {"targets": {A: 0}}}}, "finite and in"),
        ({"scenarios": {"core": {"targets": {A: float("nan")}}}}, "finite and in"),
        (
            {"scenarios": {"core": {"targets": {A: 60, B: 50}}}},
            "must not exceed 100%",
        ),
        ({"scenarios": {"core": {"targets": {A: 50}, "months": 0}}}, "integer >= 1"),
        ({"scenarios": {"core": {"targets": {A: 90}, "frozen": []}}}, "must sum to 100%"),
        ({"scenarios": {"core": {"targets": {A: 100}, "frozen": B}}}, "list of ISINs"),
        ({"scenarios": {"core": {"targets": {A: 100}, "frozen": [B, B]}}}, "frozen ISIN twice"),
        ({"scenarios": {"core": {"targets": {A: 100}, "frozen": [A]}}}, "targeted and frozen"),
        ({"scenarios": {"core": {"targets": {A: 50}, "monthly": 100}}}, "needs 'frozen'"),
        (
            {"scenarios": {"core": {"targets": {A: 100}, "frozen": [], "monthly": 0}}},
            "finite amount > 0",
        ),
    ],
)
def test_malformed_file_raises(tmp_path, body, match):
    path = tmp_path / "s.yaml"
    path.write_text(yaml.dump(body))
    with pytest.raises(ScenarioError, match=match):
        load_scenarios(str(path))


def test_sleeve_scenario_roundtrip_and_budget(tmp_path):
    path = str(tmp_path / "s.yaml")
    save_scenario(Scenario("s", {A: 60.0, B: 40.0}, months=12, frozen=(C,), monthly=250.0),
                  path)
    stored = yaml.safe_load((tmp_path / "s.yaml").read_text())["scenarios"]["s"]
    assert stored == {"months": 12, "monthly": 250.0, "frozen": [C],
                      "targets": {A: 60.0, B: 40.0}}
    loaded = get_scenario("s", path)
    assert loaded.frozen == (C,) and loaded.monthly == 250.0
    assert loaded.budget == 3000.0
    assert Scenario("x", {A: 100.0}, frozen=(), monthly=250.0).budget == 250.0
    assert Scenario("x", {A: 50.0}).budget is None


def test_save_rejects_invalid_scenario(tmp_path):
    with pytest.raises(ScenarioError, match="must not exceed 100%"):
        save_scenario(Scenario("bad", {A: 60.0, B: 50.0}), str(tmp_path / "s.yaml"))


# ---------------------------------------------------------------------------
# common: post_rebalance_weights
# ---------------------------------------------------------------------------


def _plan(*, feasible, buys=None):
    return RebalancePlan(
        feasible=feasible, reason=None if feasible else "empty_portfolio",
        unvaluable_targets=[], v=0.0, v_prime=0.0, c_min=0.0,
        buys=buys or {}, binders=[], residual_bound_binds=False,
    )


def test_post_rebalance_weights_infeasible_is_empty():
    assert post_rebalance_weights(_plan(feasible=False), {A: 100.0}) == {}


def test_post_rebalance_weights_sums_current_plus_buy_and_drops_zero():
    plan = _plan(feasible=True, buys={A: 40.0, B: 100.0, C: 0.0})
    values = {A: 60.0, B: 0.0, C: None}
    # A: 60+40=100, B: 0+100=100, C: 0+0=0 → dropped
    assert post_rebalance_weights(plan, values) == {A: 100.0, B: 100.0}


# ---------------------------------------------------------------------------
# `scenario` command
# ---------------------------------------------------------------------------


def _cfg(tmp_path, names):
    config = tmp_path / "config.yaml"
    config.write_text(yaml.dump({"etfs": {isin: {"name": n} for isin, n in names.items()}}))
    return str(config)


def test_cmd_save_list_show_delete(tmp_path, capsys):
    path = str(tmp_path / "s.yaml")
    config = _cfg(tmp_path, {A: "Fund A", B: "Fund B"})

    assert scn.main(["save", "core", "--target", f"{A}:60", "--target", f"{B}:40",
                     "--months", "10", "--file", path]) == 0
    out = capsys.readouterr().out
    assert "Saved scenario 'core'" in out and "10 month" in out

    assert scn.main(["list", "--file", path]) == 0
    assert "core" in capsys.readouterr().out

    assert scn.main(["show", "core", "--file", path, "--config", config]) == 0
    out = capsys.readouterr().out
    assert "Fund A" in out and "60.0%" in out and "TOTAL" in out

    assert scn.main(["delete", "core", "--file", path]) == 0
    assert "Deleted scenario 'core'" in capsys.readouterr().out


def test_cmd_save_update_message(tmp_path, capsys):
    path = str(tmp_path / "s.yaml")
    scn.main(["save", "core", "--target", f"{A}:50", "--file", path])
    capsys.readouterr()
    scn.main(["save", "core", "--target", f"{A}:70", "--file", path])
    assert "Updated scenario 'core'" in capsys.readouterr().out


def test_cmd_list_empty(tmp_path, capsys):
    assert scn.main(["list", "--file", str(tmp_path / "s.yaml")]) == 0
    assert "No scenarios saved" in capsys.readouterr().out


def test_cmd_save_requires_targets(tmp_path, capsys):
    assert scn.main(["save", "core", "--file", str(tmp_path / "s.yaml")]) == 1
    assert "at least one --target" in capsys.readouterr().out


def test_cmd_save_rejects_duplicate_isin(tmp_path, capsys):
    code = scn.main(["save", "core", "--target", f"{A}:10", "--target", f"{A}:20",
                     "--file", str(tmp_path / "s.yaml")])
    assert code == 1
    assert "duplicate ISIN" in capsys.readouterr().out


def test_cmd_save_rejects_sum_over_100(tmp_path, capsys):
    code = scn.main(["save", "core", "--target", f"{A}:80", "--target", f"{B}:40",
                     "--file", str(tmp_path / "s.yaml")])
    assert code == 1
    assert "must not exceed 100%" in capsys.readouterr().out


def test_cmd_delete_missing_returns_1(tmp_path, capsys):
    code = scn.main(["delete", "ghost", "--file", str(tmp_path / "s.yaml")])
    assert code == 1
    assert "no scenario named 'ghost'" in capsys.readouterr().out


def test_cmd_no_subcommand_prints_help(capsys):
    assert scn.main([]) == 1
    assert "scenario" in capsys.readouterr().out


def test_parse_target_rejections():
    with pytest.raises(scn.argparse.ArgumentTypeError):
        scn._parse_target("no-colon")
    with pytest.raises(scn.argparse.ArgumentTypeError):
        scn._parse_target(":30")
    with pytest.raises(scn.argparse.ArgumentTypeError):
        scn._parse_target(f"{A}:x")
    with pytest.raises(scn.argparse.ArgumentTypeError):
        scn._parse_target(f"{A}:0")


def test_cmd_save_show_list_sleeve_scenario(tmp_path, capsys):
    path = str(tmp_path / "s.yaml")
    config = _cfg(tmp_path, {A: "Fund A", C: "Fund C"})
    assert scn.main(["save", "s", "--target", f"{A}:100", "--frozen", C,
                     "--monthly", "250", "--months", "12", "--file", path]) == 0
    assert ("1 target(s) · Σ 100.0% of sleeve · 1 frozen · 12 month(s) · €250.00/month"
            in capsys.readouterr().out)

    assert scn.main(["show", "s", "--file", path, "--config", config]) == 0
    out = capsys.readouterr().out
    assert "Monthly budget: €250.00" in out and "of the non-frozen sleeve" in out
    assert "Frozen (never bought):" in out and "Fund C" in out

    assert scn.main(["list", "--file", path]) == 0
    assert "1 frozen" in capsys.readouterr().out


def test_cmd_save_sleeve_rejects_sum_not_100(tmp_path, capsys):
    code = scn.main(["save", "s", "--target", f"{A}:90", "--frozen", C,
                     "--file", str(tmp_path / "s.yaml")])
    assert code == 1
    assert "must sum to 100%" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Category scenarios (ADR-0061)
# ---------------------------------------------------------------------------


def _categorized(name="g", months=None, monthly=None):
    from e1f.common import Category
    return Scenario.from_categories(
        name,
        (Category("core", 60.0, {A: 100.0}), Category("rest", 40.0, {B: 75.0, C: 25.0})),
        months=months,
        frozen=(),
        monthly=monthly,
    )


def test_category_scenario_derives_targets_and_roundtrips(tmp_path):
    path = str(tmp_path / "s.yaml")
    scenario = _categorized(months=12, monthly=250.0)
    assert scenario.targets == {A: 60.0, B: 30.0, C: 10.0}
    save_scenario(scenario, path)
    stored = yaml.safe_load((tmp_path / "s.yaml").read_text())["scenarios"]["g"]
    assert stored == {
        "months": 12, "monthly": 250.0, "frozen": [],
        "categories": {"core": {"pct": 60.0, "funds": {A: 100.0}},
                       "rest": {"pct": 40.0, "funds": {B: 75.0, C: 25.0}}},
    }
    # Saving another scenario rewrites the file; the category tree must survive it.
    save_scenario(Scenario("other", {A: 100.0}), path)
    assert get_scenario("g", path) == scenario


def test_category_scenario_keeps_yaml_order(tmp_path):
    path = tmp_path / "s.yaml"
    path.write_text(
        "scenarios:\n  g:\n    frozen: []\n    categories:\n"
        f"      z: {{pct: 30, funds: {{{B}: 100}}}}\n"
        f"      a: {{pct: 70, funds: {{{C}: 40, {A}: 60}}}}\n"
    )
    loaded = get_scenario("g", str(path))
    assert [c.name for c in loaded.categories] == ["z", "a"]
    assert list(loaded.categories[1].funds) == [C, A]
    assert loaded.targets == pytest.approx({B: 30.0, C: 28.0, A: 42.0})


def _cat(pct, funds):
    return {"pct": pct, "funds": funds}


@pytest.mark.parametrize(
    "body, match",
    [
        ({"targets": {A: 100}, "frozen": [], "categories": {"c": _cat(100, {A: 100})}},
         "both 'targets' and 'categories'"),
        ({"categories": {"c": _cat(100, {A: 100})}}, "needs 'frozen'"),
        ({"frozen": [], "categories": {}}, "non-empty mapping"),
        ({"frozen": [], "categories": {"c": {"pct": 100}}}, "non-empty 'funds'"),
        ({"frozen": [], "categories": {"c": _cat("x", {A: 100})}}, "non-numeric 'pct'"),
        ({"frozen": [], "categories": {"c": _cat(100, {A: "x"})}}, "non-numeric percent"),
        ({"frozen": [], "categories": {"c": _cat(0, {A: 100})}}, "'pct' must be finite"),
        ({"frozen": [], "categories": {"c": _cat(100, {A: 0})}}, "share must be finite"),
        ({"frozen": [], "categories": {"c": _cat(90, {A: 100})}},
         "category percentages sum to 90.00%"),
        ({"frozen": [], "categories": {"c": _cat(100, {A: 60, B: 30})}},
         "fund shares sum to 90.00%"),
        ({"frozen": [], "categories": {"c": _cat(50, {A: 100}), "d": _cat(50, {A: 100})}},
         "more than one category"),
        ({"frozen": [A], "categories": {"c": _cat(100, {A: 100})}}, "targeted and frozen"),
    ],
)
def test_malformed_category_scenario_raises(tmp_path, body, match):
    path = tmp_path / "s.yaml"
    path.write_text(yaml.dump({"scenarios": {"g": body}}))
    with pytest.raises(ScenarioError, match=match):
        load_scenarios(str(path))


def test_save_rejects_targets_that_disagree_with_categories(tmp_path):
    from e1f.common import Category
    bad = Scenario("g", {A: 50.0, B: 50.0}, frozen=(),
                   categories=(Category("c", 100.0, {A: 100.0}),))
    with pytest.raises(ScenarioError, match="do not match its categories"):
        save_scenario(bad, str(tmp_path / "s.yaml"))
    dupe = Scenario("g", {A: 100.0}, frozen=(),
                    categories=(Category("c", 50.0, {A: 100.0}), Category("c", 50.0, {B: 100.0})))
    with pytest.raises(ScenarioError, match="category name twice"):
        save_scenario(dupe, str(tmp_path / "s.yaml"))


def test_cmd_show_and_list_category_scenario(tmp_path, capsys):
    path = str(tmp_path / "s.yaml")
    save_scenario(_categorized(), path)
    assert scn.main(["list", "--file", path]) == 0
    assert "3 target(s) in 2 categories · Σ 100.0% of sleeve · 0 frozen" in capsys.readouterr().out
    config = tmp_path / "cfg.yaml"
    config.write_text(yaml.dump({"etfs": {B: {"name": "Fund B"}}}))
    assert scn.main(["show", "g", "--file", path, "--config", str(config)]) == 0
    lines = capsys.readouterr().out.splitlines()
    header = next(i for i, ln in enumerate(lines) if "Share%" in ln)
    tree = [ln.split() for ln in lines[header + 2:header + 7]]
    assert tree == [
        ["core", "60.0%"],
        [A, "60.0%", "100.0%"],
        ["rest", "40.0%"],
        [B, "Fund", "B", "30.0%", "75.0%"],
        [C, "10.0%", "25.0%"],
    ]
    assert lines[header + 3].startswith(f"    {A}")
