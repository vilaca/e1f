#!/usr/bin/env python
"""e1f scenario — manage named ISIN:pct baskets in one YAML file (ADR-0017).

This command only *creates and maintains* the scenarios file; it never runs an
analysis.  ``rebalance`` and ``correlation`` consume a saved scenario via their
own ``--scenario NAME`` flag.

Usage:
    e1f scenario save core --target IE00BM67HK77:12 --target IE0003XJA0J9:40 --months 10
    e1f scenario list
    e1f scenario show core
    e1f scenario delete core
"""

import argparse
import sys
from collections.abc import Callable

from e1f.common import (
    DEFAULT_CONFIG,
    DEFAULT_SCENARIOS,
    ConfigManager,
    Scenario,
    delete_scenario,
    get_scenario,
    load_scenarios,
    save_scenario,
)

_NAME_W = 32  # fund-name column width in `show`


def _parse_target(text: str) -> tuple[str, float]:
    """Validate ``ISIN:PCT`` — loose ISIN check, PCT in (0, 100]."""
    if ":" not in text:
        raise argparse.ArgumentTypeError(
            f"expected ISIN:PCT (e.g. IE00B4L5Y983:30), got {text!r} — missing colon"
        )
    isin, pct_str = text.split(":", 1)
    if not isin:
        raise argparse.ArgumentTypeError(f"ISIN part is empty in {text!r}")
    try:
        pct = float(pct_str)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"PCT part {pct_str!r} is not a number in {text!r}"
        ) from None
    if not (0.0 < pct <= 100.0):
        raise argparse.ArgumentTypeError(
            f"PCT must be in (0, 100] — got {pct} in {text!r}. "
            f"Targets are percentages of the whole valued book (e.g. 30 = 30%%)."
        )
    return isin, pct


def _int_at_least(minimum: int) -> Callable[[str], int]:
    def parse(text: str) -> int:
        try:
            value = int(text)
        except ValueError:
            raise argparse.ArgumentTypeError(f"{text!r} is not an integer") from None
        if value < minimum:
            raise argparse.ArgumentTypeError(f"must be ≥ {minimum} (got {value})")
        return value

    return parse


def _cmd_save(
    name: str,
    targets_raw: list[tuple[str, float]],
    months: int | None,
    scenarios_path: str,
) -> int:
    if not targets_raw:
        print("✗ Error: at least one --target ISIN:PCT is required")
        return 1
    isins = [isin for isin, _ in targets_raw]
    if len(isins) != len(set(isins)):
        print("✗ Error: duplicate ISIN in --target arguments")
        return 1
    total_pct = sum(pct for _, pct in targets_raw)
    if total_pct > 100.0 + 1e-9:
        print(f"✗ Error: target percentages sum to {total_pct:.2f}% — must not exceed 100%")
        return 1

    scenario = Scenario(name=name, targets=dict(targets_raw), months=months)
    existed = save_scenario(scenario, scenarios_path)
    verb = "Updated" if existed else "Saved"
    months_note = f", {months} month(s)" if months is not None else ""
    print(
        f"✓ {verb} scenario {name!r} "
        f"({len(targets_raw)} target(s), Σ {total_pct:.1f}%{months_note})"
    )
    print(f"  {scenarios_path}")
    return 0


def _cmd_list(scenarios_path: str) -> int:
    scenarios = load_scenarios(scenarios_path)
    if not scenarios:
        print(f"No scenarios saved in {scenarios_path}")
        print("Create one: e1f scenario save NAME --target ISIN:PCT ...")
        return 0
    print(f"Scenarios in {scenarios_path}:\n")
    for name in sorted(scenarios):
        s = scenarios[name]
        total = sum(s.targets.values())
        months_note = f" · {s.months} month(s)" if s.months is not None else ""
        print(f"  {name:<20} {len(s.targets)} target(s) · Σ {total:.1f}%{months_note}")
    return 0


def _cmd_show(name: str, scenarios_path: str, config_path: str) -> int:
    scenario = get_scenario(name, scenarios_path)
    config = ConfigManager(config_path)
    total = sum(scenario.targets.values())
    print(f"Scenario {name!r} (from {scenarios_path}):")
    if scenario.months is not None:
        print(f"  DCA months: {scenario.months}")
    print()
    print(f"  {'ISIN':<14} {'Name':<{_NAME_W}} {'Tgt%':>7}")
    print("  " + "-" * (14 + 1 + _NAME_W + 1 + 7))
    for isin, pct in sorted(scenario.targets.items(), key=lambda kv: (-kv[1], kv[0])):
        fund_name = str((config.get(isin) or {}).get("name", ""))[:_NAME_W]
        print(f"  {isin:<14} {fund_name:<{_NAME_W}} {pct:>6.1f}%")
    print("  " + "-" * (14 + 1 + _NAME_W + 1 + 7))
    print(f"  {'TOTAL':<14} {'':<{_NAME_W}} {total:>6.1f}%")
    return 0


def _cmd_delete(name: str, scenarios_path: str) -> int:
    delete_scenario(name, scenarios_path)
    print(f"✓ Deleted scenario {name!r} from {scenarios_path}")
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="e1f scenario",
        description="Create and maintain named ISIN:pct baskets in one YAML file. "
        "Consumed by 'e1f rebalance --scenario' and 'e1f correlation --scenario' (ADR-0017).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
A scenario is a named basket of target weights (percent of the whole book), with
an optional default DCA horizon. This command only writes the file; run the plan
with the consumers:

  e1f scenario save core --target IE00BM67HK77:12 --target IE0003XJA0J9:40 --months 10
  e1f rebalance --scenario core
  e1f correlation --scenario core

Examples:
  e1f scenario save core --target IE00B4L5Y983:60 --target IE00BK5BQT80:40
  e1f scenario list
  e1f scenario show core
  e1f scenario delete core
        """,
    )
    subparsers = parser.add_subparsers(dest="command")

    save_parser = subparsers.add_parser("save", help="Create or update a scenario")
    save_parser.add_argument("name", help="Scenario name")
    save_parser.add_argument(
        "--target",
        metavar="ISIN:PCT",
        action="append",
        dest="targets",
        type=_parse_target,
        help="Target weight — repeatable. PCT is a percent of the whole book in (0, 100].",
    )
    save_parser.add_argument(
        "--months",
        type=_int_at_least(1),
        default=None,
        metavar="N",
        help="Default DCA horizon stored with the scenario (rebalance uses it; ≥ 1).",
    )
    save_parser.add_argument(
        "--file", default=DEFAULT_SCENARIOS, help="Scenarios YAML file path"
    )

    list_parser = subparsers.add_parser("list", help="List saved scenarios")
    list_parser.add_argument(
        "--file", default=DEFAULT_SCENARIOS, help="Scenarios YAML file path"
    )

    show_parser = subparsers.add_parser("show", help="Show one scenario's targets")
    show_parser.add_argument("name", help="Scenario name")
    show_parser.add_argument(
        "--file", default=DEFAULT_SCENARIOS, help="Scenarios YAML file path"
    )
    show_parser.add_argument(
        "--config", "-c", default=DEFAULT_CONFIG, help="ETF universe config for fund names"
    )

    delete_parser = subparsers.add_parser("delete", help="Delete a scenario")
    delete_parser.add_argument("name", help="Scenario name")
    delete_parser.add_argument(
        "--file", default=DEFAULT_SCENARIOS, help="Scenarios YAML file path"
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if not args.command:
        parser.print_help()
        return 1

    try:
        if args.command == "save":
            return _cmd_save(args.name, args.targets or [], args.months, args.file)
        if args.command == "list":
            return _cmd_list(args.file)
        if args.command == "show":
            return _cmd_show(args.name, args.file, args.config)
        if args.command == "delete":
            return _cmd_delete(args.name, args.file)
        parser.error(f"unsupported command: {args.command!r}")
        return 1  # unreachable; keeps mypy happy
    except Exception as e:  # noqa: BLE001 — CLI top-level; all errors become exit code 1
        print(f"✗ Error: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
