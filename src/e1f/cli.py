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
    config,
    fetch,
    performance,
    portfolio,
    transactions,
    validate,
)

Command = Callable[[list[str]], int]

PARSER_FACTORIES = {
    "autocomplete": autocomplete._build_parser,
    "config": config._build_parser,
    "fetch": fetch._build_parser,
    "validate": validate._build_parser,
    "transactions": transactions._build_parser,
    "portfolio": portfolio._build_parser,
    "performance": performance._build_parser,
}


def _autocomplete_main(argv: list[str]) -> int:
    return autocomplete.main(argv, PARSER_FACTORIES)

COMMANDS: dict[str, Command] = {
    "autocomplete": _autocomplete_main,
    "config": config.main,
    "fetch": fetch.main,
    "validate": validate.main,
    "transactions": transactions.main,
    "portfolio": portfolio.main,
    "performance": performance.main,
}


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
