"""Named ISIN→percent baskets persisted in one YAML file (ADR-0017)."""

import math
import os
from dataclasses import dataclass
from typing import Any

import yaml

from .defaults import DEFAULT_SCENARIOS
from .persistence import atomic_write_yaml


@dataclass(frozen=True)
class Scenario:
    """A named basket: ISIN → target percent, plus an optional default DCA horizon
    in months (consumed by ``rebalance``; ignored by ``correlation``).

    Without ``frozen`` the percents are of the whole book and Σ ≤ 100 (ADR-0017).
    With ``frozen`` (possibly empty) the scenario is a sleeve plan (ADR-0060): the
    frozen funds are never bought, the percents are of the non-frozen sleeve and
    Σ = 100, and ``monthly`` optionally sets the EUR budget per month.
    """

    name: str
    targets: dict[str, float]
    months: int | None = None
    frozen: tuple[str, ...] | None = None
    monthly: float | None = None

    @property
    def budget(self) -> float | None:
        """Total EUR to invest: ``monthly`` × months (1 without months), or None."""
        return None if self.monthly is None else self.monthly * (self.months or 1)


class ScenarioError(Exception):
    """Raised for a missing scenario or a malformed scenarios file."""


def load_scenarios(path: str = DEFAULT_SCENARIOS) -> dict[str, Scenario]:
    """Load every scenario from ``path`` (missing file → empty dict)."""
    if not os.path.exists(path):
        return {}
    with open(path) as f:
        loaded = yaml.safe_load(f)
    raw = {} if loaded is None else loaded
    if not isinstance(raw, dict):
        raise ScenarioError(f"{path}: root must be a mapping")
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
    frozen_raw = body.get("frozen")
    if frozen_raw is not None and not isinstance(frozen_raw, list):
        raise ScenarioError(f"{path}: scenario {name!r} 'frozen' must be a list of ISINs")
    scenario = Scenario(
        name=name,
        targets=targets,
        months=body.get("months"),
        frozen=None if frozen_raw is None else tuple(str(isin) for isin in frozen_raw),
        monthly=body.get("monthly"),
    )
    _validate_scenario(scenario, path)
    return scenario


def _validate_scenario(scenario: Scenario, path: str) -> None:
    """Enforce the complete stored-basket contract at every persistence boundary."""
    prefix = f"{path}: scenario {scenario.name!r}"
    if not scenario.targets:
        raise ScenarioError(f"{prefix} needs a non-empty 'targets' mapping")
    for isin, pct in scenario.targets.items():
        if (
            isinstance(pct, bool)
            or not isinstance(pct, (int, float))
            or not math.isfinite(float(pct))
            or not 0.0 < float(pct) <= 100.0
        ):
            raise ScenarioError(
                f"{prefix} target {isin!r} percent must be finite and in (0, 100]"
            )
    total = sum(float(pct) for pct in scenario.targets.values())
    if scenario.frozen is None and total > 100.0 + 1e-9:
        raise ScenarioError(
            f"{prefix} target percentages sum to {total:.2f}% — must not exceed 100%"
        )
    if scenario.frozen is not None:
        if abs(total - 100.0) > 1e-6:
            raise ScenarioError(
                f"{prefix} target percentages sum to {total:.2f}% — with 'frozen' they "
                f"are of the non-frozen sleeve and must sum to 100%"
            )
        if len(set(scenario.frozen)) != len(scenario.frozen):
            raise ScenarioError(f"{prefix} lists a frozen ISIN twice")
        both = sorted(set(scenario.frozen) & set(scenario.targets))
        if both:
            raise ScenarioError(f"{prefix} ISIN(s) both targeted and frozen: {', '.join(both)}")
    if scenario.monthly is not None:
        if (
            isinstance(scenario.monthly, bool)
            or not isinstance(scenario.monthly, (int, float))
            or not math.isfinite(float(scenario.monthly))
            or scenario.monthly <= 0.0
        ):
            raise ScenarioError(f"{prefix} 'monthly' must be a finite amount > 0")
        if scenario.frozen is None:
            raise ScenarioError(f"{prefix} 'monthly' needs 'frozen' (a sleeve plan)")
    if scenario.months is not None and (
        isinstance(scenario.months, bool)
        or not isinstance(scenario.months, int)
        or scenario.months < 1
    ):
        raise ScenarioError(f"{prefix} 'months' must be an integer >= 1")


def get_scenario(name: str, path: str = DEFAULT_SCENARIOS) -> Scenario:
    """Fetch one scenario by name, or raise ``ScenarioError`` listing what exists."""
    scenarios = load_scenarios(path)
    if name not in scenarios:
        known = ", ".join(sorted(scenarios)) or "(none saved)"
        raise ScenarioError(f"no scenario named {name!r} in {path} — saved: {known}")
    return scenarios[name]


def save_scenario(scenario: Scenario, path: str = DEFAULT_SCENARIOS) -> bool:
    """Upsert one scenario, preserving the others.  Returns True if it already existed."""
    _validate_scenario(scenario, path)
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
    atomic_write_yaml(path, body, sort_keys=False)


def _scenario_to_yaml(scenario: Scenario) -> dict[str, Any]:
    entry: dict[str, Any] = {}
    if scenario.months is not None:
        entry["months"] = scenario.months
    if scenario.monthly is not None:
        entry["monthly"] = scenario.monthly
    if scenario.frozen is not None:
        entry["frozen"] = list(scenario.frozen)
    entry["targets"] = dict(scenario.targets)
    return entry
