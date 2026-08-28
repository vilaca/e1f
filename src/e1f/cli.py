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
import sys
from collections.abc import Callable

from e1f import (
    autocomplete,
    benchmark,
    config,
    correlation,
    deposits,
    fetch,
    glossary,
    performance,
    portfolio,
    rebalance,
    scenario,
    transactions,
    validate,
)

# Experimental tier (ADR-0024): isolated behind a one-way import boundary — no
# stable module imports ``e1f.experimental``. The CLI router is the sole exception,
# so it can register these commands; nothing else may reach experimental code.
from e1f.experimental import backtest, concentration, lookthrough, overlap, seasonality

Command = Callable[[list[str]], int]

# Stable commands and the isolated experimental tier are registered separately so
# the split has one home (ADR-0024); ``COMMANDS`` merges them for dispatch.
STABLE_PARSER_FACTORIES = {
    "autocomplete": autocomplete._build_parser,
    "config": config._build_parser,
    "fetch": fetch._build_parser,
    "validate": validate._build_parser,
    "transactions": transactions._build_parser,
    "portfolio": portfolio._build_parser,
    "performance": performance._build_parser,
    "benchmark": benchmark._build_parser,
    "deposits": deposits._build_parser,
    "correlation": correlation._build_parser,
    "rebalance": rebalance._build_parser,
    "scenario": scenario._build_parser,
    "glossary": glossary._build_parser,
}
EXPERIMENTAL_PARSER_FACTORIES = {
    "concentration": concentration._build_parser,
    "overlap": overlap._build_parser,
    "backtest": backtest._build_parser,
    "lookthrough": lookthrough._build_parser,
    "seasonality": seasonality._build_parser,
}
PARSER_FACTORIES = {**STABLE_PARSER_FACTORIES, **EXPERIMENTAL_PARSER_FACTORIES}


def _autocomplete_main(argv: list[str]) -> int:
    return autocomplete.main(argv, PARSER_FACTORIES)

STABLE_COMMANDS: dict[str, Command] = {
    "autocomplete": _autocomplete_main,
    "config": config.main,
    "fetch": fetch.main,
    "validate": validate.main,
    "transactions": transactions.main,
    "portfolio": portfolio.main,
    "performance": performance.main,
    "benchmark": benchmark.main,
    "deposits": deposits.main,
    "correlation": correlation.main,
    "rebalance": rebalance.main,
    "scenario": scenario.main,
    "glossary": glossary.main,
}
EXPERIMENTAL_COMMANDS: dict[str, Command] = {
    "concentration": concentration.main,
    "overlap": overlap.main,
    "backtest": backtest.main,
    "lookthrough": lookthrough.main,
    "seasonality": seasonality.main,
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
  validate      Check config, metadata, and stored price data
  transactions  Ingest and list broker ETF trades in SQLite
  portfolio     Show ETF holdings and average cost from transactions
  performance   Report market value, P&L, and return metrics per holding
  benchmark     Compare the portfolio's returns against benchmark ETFs (beta, R², TE, IR)
  deposits      Organic-vs-reported value, ROIC, and per-deposit contribution impact
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
