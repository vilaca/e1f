"""Named ISIN→percent baskets persisted in one YAML file (ADR-0017, ADR-0061)."""

import math
import os
from dataclasses import dataclass
from typing import Any


from .defaults import DEFAULT_SCENARIOS
from .persistence import atomic_write_yaml, load_yaml


@dataclass(frozen=True)
class Category:
    """A named group of a sleeve scenario's funds (ADR-0061): ``pct`` percent of the
    sleeve, split by fixed ``funds`` shares (ISIN → percent of the category, Σ 100).
    A share of 0 lists a fund under the category without buying it (it is frozen)."""

    name: str
    pct: float
    funds: dict[str, float]


def category_targets(categories: tuple[Category, ...]) -> dict[str, float]:
    """The flat sleeve targets the categories imply: ``pct × share / 100`` per fund,
    leaving out the funds a category lists at share 0."""
    return {
        isin: category.pct * share / 100.0
        for category in categories
        for isin, share in category.funds.items()
        if share > 0.0
    }


def zero_share_funds(categories: tuple[Category, ...]) -> tuple[str, ...]:
    """The funds listed at share 0 in some category, in scenario order."""
    return tuple(
        isin for category in categories for isin, share in category.funds.items() if share == 0.0
    )


@dataclass(frozen=True)
class Scenario:
    """A named basket: ISIN → target percent, plus an optional default DCA horizon
    in months (consumed by ``rebalance``; ignored by ``correlation``).

    Without ``frozen`` the percents are of the whole book and Σ ≤ 100 (ADR-0017).
    With ``frozen`` (possibly empty) the scenario is a sleeve plan (ADR-0060): the
    frozen funds are never bought, the percents are of the non-frozen sleeve and
    Σ = 100, and ``monthly`` optionally sets the EUR budget per month.

    A sleeve scenario may group its funds into ``categories`` instead (ADR-0061);
    ``targets`` is then the flat mapping they imply, and every consumer reads it.
    """

    name: str
    targets: dict[str, float]
    months: int | None = None
    frozen: tuple[str, ...] | None = None
    monthly: float | None = None
    categories: tuple[Category, ...] | None = None

    @classmethod
    def from_categories(
        cls,
        name: str,
        categories: tuple[Category, ...],
        *,
        months: int | None = None,
        frozen: tuple[str, ...] | None = None,
        monthly: float | None = None,
    ) -> Scenario:
        """A scenario whose ``targets`` are derived from ``categories``; the funds at
        share 0 are added to ``frozen`` (the plan never buys them)."""
        if frozen is not None:
            frozen = (*frozen, *zero_share_funds(categories))
        return cls(name, category_targets(categories), months, frozen, monthly, categories)

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
    loaded = load_yaml(path)
    raw = {} if loaded is None else loaded
    if not isinstance(raw, dict):
        raise ScenarioError(f"{path}: root must be a mapping")
    entries = raw.get("scenarios") or {}
    if not isinstance(entries, dict):
        raise ScenarioError(f"{path}: 'scenarios' must be a mapping of name → definition")
    return {str(name): _parse_scenario(str(name), body, path) for name, body in entries.items()}


def _parse_scenario(name: str, body: Any, path: str) -> Scenario:
    prefix = f"{path}: scenario {name!r}"
    if not isinstance(body, dict):
        raise ScenarioError(f"{prefix} must be a mapping")
    frozen_raw = body.get("frozen")
    if frozen_raw is not None and not isinstance(frozen_raw, list):
        raise ScenarioError(f"{prefix} 'frozen' must be a list of ISINs")
    frozen = None if frozen_raw is None else tuple(str(isin) for isin in frozen_raw)
    if "categories" in body:
        if "targets" in body:
            raise ScenarioError(f"{prefix} has both 'targets' and 'categories' — use one")
        categories_raw = body["categories"]
        if not isinstance(categories_raw, dict) or not categories_raw:
            raise ScenarioError(
                f"{prefix} 'categories' must be a non-empty mapping of name → {{pct, funds}}"
            )
        scenario = Scenario.from_categories(
            name,
            tuple(_parse_category(str(cat), raw, prefix) for cat, raw in categories_raw.items()),
            months=body.get("months"),
            frozen=frozen,
            monthly=body.get("monthly"),
        )
    else:
        targets_raw = body.get("targets")
        if not isinstance(targets_raw, dict) or not targets_raw:
            raise ScenarioError(f"{prefix} needs a non-empty 'targets' mapping")
        scenario = Scenario(
            name=name,
            targets=_parse_percents(targets_raw, f"{prefix} target"),
            months=body.get("months"),
            frozen=frozen,
            monthly=body.get("monthly"),
        )
    _validate_scenario(scenario, path)
    return scenario


def _parse_percents(raw: dict[Any, Any], label: str) -> dict[str, float]:
    percents: dict[str, float] = {}
    for isin, pct in raw.items():
        try:
            percents[str(isin)] = float(pct)
        except (TypeError, ValueError):
            raise ScenarioError(f"{label} {isin!r} has non-numeric percent {pct!r}") from None
    return percents


def _parse_category(name: str, raw: Any, prefix: str) -> Category:
    label = f"{prefix} category {name!r}"
    if not isinstance(raw, dict) or not isinstance(raw.get("funds"), dict) or not raw["funds"]:
        raise ScenarioError(f"{label} needs 'pct' and a non-empty 'funds' mapping")
    try:
        pct = float(raw["pct"])
    except (KeyError, TypeError, ValueError):
        raise ScenarioError(f"{label} has non-numeric 'pct' {raw.get('pct')!r}") from None
    return Category(name, pct, _parse_percents(raw["funds"], f"{label} fund"))


def _is_percent(value: Any) -> bool:
    """True for a finite number in (0, 100]."""
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(float(value))
        and 0.0 < float(value) <= 100.0
    )


def _validate_categories(scenario: Scenario, prefix: str) -> None:
    """The ADR-0061 contract: a sleeve plan, Σ pct = 100, Σ shares = 100 per category,
    each fund in one category, and ``targets`` equal to what the categories imply."""
    categories = scenario.categories or ()
    if scenario.frozen is None:
        raise ScenarioError(f"{prefix} 'categories' needs 'frozen' (a sleeve plan)")
    if not categories:
        raise ScenarioError(f"{prefix} 'categories' must not be empty")
    names = [category.name for category in categories]
    if len(set(names)) != len(names):
        raise ScenarioError(f"{prefix} lists a category name twice")
    seen: set[str] = set()
    for category in categories:
        label = f"{prefix} category {category.name!r}"
        if not _is_percent(category.pct):
            raise ScenarioError(f"{label} 'pct' must be finite and in (0, 100]")
        if not category.funds:
            raise ScenarioError(f"{label} needs a non-empty 'funds' mapping")
        for isin, share in category.funds.items():
            if not (share == 0.0 or _is_percent(share)):
                raise ScenarioError(f"{label} fund {isin!r} share must be finite and in [0, 100]")
            if isin in seen:
                raise ScenarioError(f"{prefix} fund {isin!r} is in more than one category")
            seen.add(isin)
        shares = sum(float(share) for share in category.funds.values())
        if abs(shares - 100.0) > 1e-6:
            raise ScenarioError(
                f"{label} fund shares sum to {shares:.2f}% — must sum to 100%"
            )
    total = sum(float(category.pct) for category in categories)
    if abs(total - 100.0) > 1e-6:
        raise ScenarioError(
            f"{prefix} category percentages sum to {total:.2f}% — they are of the "
            f"non-frozen sleeve and must sum to 100%"
        )
    implied = category_targets(categories)
    if set(implied) != set(scenario.targets) or any(
        not math.isclose(scenario.targets[isin], pct, rel_tol=0.0, abs_tol=1e-9)
        for isin, pct in implied.items()
    ):
        raise ScenarioError(f"{prefix} targets do not match its categories")
    unfrozen = sorted(set(zero_share_funds(categories)) - set(scenario.frozen))
    if unfrozen:
        raise ScenarioError(f"{prefix} share-0 fund(s) must be frozen: {', '.join(unfrozen)}")


def _validate_scenario(scenario: Scenario, path: str) -> None:
    """Enforce the complete stored-basket contract at every persistence boundary."""
    prefix = f"{path}: scenario {scenario.name!r}"
    if scenario.categories is not None:
        _validate_categories(scenario, prefix)
    if not scenario.targets:
        raise ScenarioError(f"{prefix} needs a non-empty 'targets' mapping")
    for isin, pct in scenario.targets.items():
        if not _is_percent(pct):
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
        implied = set(zero_share_funds(scenario.categories or ()))
        entry["frozen"] = [isin for isin in scenario.frozen if isin not in implied]
    if scenario.categories is None:
        entry["targets"] = dict(scenario.targets)
    else:
        entry["categories"] = {
            category.name: {"pct": category.pct, "funds": dict(category.funds)}
            for category in scenario.categories
        }
    return entry
