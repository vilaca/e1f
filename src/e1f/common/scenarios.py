"""Named ISIN→percent baskets persisted in one YAML file (ADR-0017)."""

import os
from dataclasses import dataclass
from typing import Any

import yaml

from .defaults import DEFAULT_SCENARIOS


@dataclass(frozen=True)
class Scenario:
    """A named basket: ISIN → target percent (of the whole book), plus an optional
    default DCA horizon in months (consumed by ``rebalance``; ignored by
    ``correlation``).  ``targets`` percents are the raw stored values in (0, 100];
    validation of the set (dupes, Σ ≤ 100) lives with the writers/consumers.
    """

    name: str
    targets: dict[str, float]
    months: int | None = None


class ScenarioError(Exception):
    """Raised for a missing scenario or a malformed scenarios file."""


def load_scenarios(path: str = DEFAULT_SCENARIOS) -> dict[str, Scenario]:
    """Load every scenario from ``path`` (missing file → empty dict)."""
    if not os.path.exists(path):
        return {}
    with open(path) as f:
        raw = yaml.safe_load(f) or {}
    entries = raw.get("scenarios") or {}
    if not isinstance(entries, dict):
        raise ScenarioError(f"{path}: 'scenarios' must be a mapping of name → definition")
    return {str(name): _parse_scenario(str(name), body, path) for name, body in entries.items()}


def _parse_scenario(name: str, body: Any, path: str) -> Scenario:
    if not isinstance(body, dict):
        raise ScenarioError(f"{path}: scenario {name!r} must be a mapping")
    targets_raw = body.get("targets")
    if not isinstance(targets_raw, dict) or not targets_raw:
        raise ScenarioError(f"{path}: scenario {name!r} needs a non-empty 'targets' mapping")
    targets: dict[str, float] = {}
    for isin, pct in targets_raw.items():
        try:
            targets[str(isin)] = float(pct)
        except (TypeError, ValueError):
            raise ScenarioError(
                f"{path}: scenario {name!r} target {isin!r} has non-numeric percent {pct!r}"
            ) from None
    months = body.get("months")
    if months is not None and not isinstance(months, int):
        raise ScenarioError(f"{path}: scenario {name!r} 'months' must be an integer")
    return Scenario(name=name, targets=targets, months=months)


def get_scenario(name: str, path: str = DEFAULT_SCENARIOS) -> Scenario:
    """Fetch one scenario by name, or raise ``ScenarioError`` listing what exists."""
    scenarios = load_scenarios(path)
    if name not in scenarios:
        known = ", ".join(sorted(scenarios)) or "(none saved)"
        raise ScenarioError(f"no scenario named {name!r} in {path} — saved: {known}")
    return scenarios[name]


def save_scenario(scenario: Scenario, path: str = DEFAULT_SCENARIOS) -> bool:
    """Upsert one scenario, preserving the others.  Returns True if it already existed."""
    scenarios = load_scenarios(path)
    existed = scenario.name in scenarios
    scenarios[scenario.name] = scenario
    _write_scenarios(scenarios, path)
    return existed


def delete_scenario(name: str, path: str = DEFAULT_SCENARIOS) -> None:
    """Remove one scenario, or raise ``ScenarioError`` if it is not present."""
    scenarios = load_scenarios(path)
    if name not in scenarios:
        known = ", ".join(sorted(scenarios)) or "(none saved)"
        raise ScenarioError(f"no scenario named {name!r} in {path} — saved: {known}")
    del scenarios[name]
    _write_scenarios(scenarios, path)


def _write_scenarios(scenarios: dict[str, Scenario], path: str) -> None:
    body = {"scenarios": {name: _scenario_to_yaml(scenarios[name]) for name in sorted(scenarios)}}
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w") as f:
        yaml.dump(body, f, default_flow_style=False, sort_keys=False)


def _scenario_to_yaml(scenario: Scenario) -> dict[str, Any]:
    entry: dict[str, Any] = {}
    if scenario.months is not None:
        entry["months"] = scenario.months
    entry["targets"] = dict(scenario.targets)
    return entry
