"""e1f command-line entry point.

Dispatches top-level commands to their respective module's ``main``. Each module
keeps its own argparse definitions; this layer only routes the first token and
forwards the rest (so that, e.g., ``e1f fetch --help`` reaches the fetch parser).

    e1f config add IE00BM67HK77
    e1f fetch
    e1f validate
    e1f transactions trade-republic transactions.csv
"""

import argparse
import importlib
import sys
from collections.abc import Callable

Command = Callable[[list[str]], int]
ParserFactory = Callable[[], argparse.ArgumentParser]

# Each command's module is imported only when that command runs (or, for the
# parser factories, when completion needs its parser): the heavy dependencies
# (pandas, matplotlib, yfinance) otherwise cost ~1s of startup on every call.
STABLE_MODULES = {
    "autocomplete": "e1f.autocomplete",
    "config": "e1f.config",
    "fetch": "e1f.fetch",
    "funds": "e1f.funds",
    "validate": "e1f.validate",
    "transactions": "e1f.transactions",
    "portfolio": "e1f.portfolio",
    "performance": "e1f.performance",
    "benchmark": "e1f.benchmark",
    "deposits": "e1f.deposits",
    "correlation": "e1f.correlation",
    "rebalance": "e1f.rebalance",
    "scenario": "e1f.scenario",
    "glossary": "e1f.glossary",
}
# Experimental tier (ADR-0024): isolated behind a one-way import boundary — no
# stable module imports ``e1f.experimental``. The CLI router is the sole exception,
# so it can register these commands; nothing else may reach experimental code.
EXPERIMENTAL_MODULES = {
    "concentration": "e1f.experimental.concentration",
    "overlap": "e1f.experimental.overlap",
    "backtest": "e1f.experimental.backtest",
    "lookthrough": "e1f.experimental.lookthrough",
    "seasonality": "e1f.experimental.seasonality",
    "limitbuy": "e1f.experimental.limitbuy",
    "pick": "e1f.experimental.pick",
}
MODULES = {**STABLE_MODULES, **EXPERIMENTAL_MODULES}


def _factory(module: str) -> ParserFactory:
    def build() -> argparse.ArgumentParser:
        parser: argparse.ArgumentParser = importlib.import_module(module)._build_parser()
        return parser

    return build


def _command(module: str) -> Command:
    def run(argv: list[str]) -> int:
        code: int = importlib.import_module(module).main(argv)
        return code

    return run


# Stable commands and the isolated experimental tier are registered separately so
# the split has one home (ADR-0024); the merged dicts serve dispatch and completion.
STABLE_PARSER_FACTORIES = {name: _factory(mod) for name, mod in STABLE_MODULES.items()}
EXPERIMENTAL_PARSER_FACTORIES = {
    name: _factory(mod) for name, mod in EXPERIMENTAL_MODULES.items()
}
PARSER_FACTORIES = {**STABLE_PARSER_FACTORIES, **EXPERIMENTAL_PARSER_FACTORIES}


def _autocomplete_main(argv: list[str]) -> int:
    autocomplete = importlib.import_module("e1f.autocomplete")
    code: int = autocomplete.main(argv, PARSER_FACTORIES)
    return code


STABLE_COMMANDS: dict[str, Command] = {
    name: _command(mod) for name, mod in STABLE_MODULES.items()
}
STABLE_COMMANDS["autocomplete"] = _autocomplete_main
EXPERIMENTAL_COMMANDS: dict[str, Command] = {
    name: _command(mod) for name, mod in EXPERIMENTAL_MODULES.items()
}
COMMANDS: dict[str, Command] = {**STABLE_COMMANDS, **EXPERIMENTAL_COMMANDS}


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="e1f",
        description="ETF universe config and price fetching.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Commands:
  autocomplete  Print Bash or Zsh completion setup
  config        Build/maintain the ETF universe YAML from ISINs
  fetch         Populate the SQLite price DB for the universe
  funds         Configured ETFs with cost, windowed return/risk, and coverage
  validate      Check config, metadata, and stored price data
  transactions  Ingest and list broker ETF trades in SQLite
  portfolio     Show ETF holdings and average cost from transactions
  performance   Report market value, P&L, and return metrics per holding
  benchmark     Compare the portfolio's returns against benchmark ETFs (beta, R², TE, IR)
  deposits      Organic-vs-reported value, ROIC, per-deposit impact; --against replays buys
  correlation   Return co-movement redundancy: correlated-pair flags + clustering
  rebalance     Minimum-cash buy-only target rebalance & optional DCA schedule
  scenario      Save/list/show/delete named ISIN:pct baskets (used by rebalance & correlation)
  glossary      Look up what a metric means and what it's useful for

Experimental (ADR-0024 — isolated tier; may change or give wrong results):
  lookthrough   Refresh cached yfinance look-through snapshots for held funds
  concentration Within-fund concentration (security/sector/asset-class), coverage-aware
  overlap       Cross-fund single-name exposure floor via reviewed canonical identity
  backtest      Contribution-timing backtest: dip-reserve vs constant-DCA over one ETF's history
  seasonality   Calendar-month seasonality: --isin, --portfolio consensus, or --evaluate
  limitbuy      Worst-case bound on a buy-limit under the close vs buying at it
  pick          Tilt this month's rebalance buys toward funds below average cost

Run 'e1f <command> --help' for command-specific options.
        """,
    )
    parser.add_argument("command", choices=list(COMMANDS), help="Command to run")
    return parser


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = _build_parser()

    # Route only the first token; everything after it belongs to the
    # subcommand's own parser (including its --help).
    if not argv or argv[0] in ("-h", "--help"):
        parser.print_help()
        return 0 if argv else 1

    command, rest = argv[0], argv[1:]
    if command not in COMMANDS:
        parser.error(f"invalid choice: {command!r} (choose from {', '.join(COMMANDS)})")

    return COMMANDS[command](rest)


if __name__ == "__main__":
    sys.exit(main())
