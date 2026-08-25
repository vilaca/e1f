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
        ({"scenarios": ["oops"]}, "must be a mapping of name"),
        ({"scenarios": {"core": []}}, "must be a mapping"),
        ({"scenarios": {"core": {}}}, "non-empty 'targets'"),
        ({"scenarios": {"core": {"targets": {A: "x"}}}}, "non-numeric percent"),
        ({"scenarios": {"core": {"targets": {A: 5}, "months": "x"}}}, "must be an integer"),
    ],
)
def test_malformed_file_raises(tmp_path, body, match):
    path = tmp_path / "s.yaml"
    path.write_text(yaml.dump(body))
    with pytest.raises(ScenarioError, match=match):
        load_scenarios(str(path))


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
