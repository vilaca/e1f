"""e1f command-line entry point.

Dispatches the top-level ``config`` / ``fetch`` / ``transactions`` / ``portfolio``
commands to the respective module's ``main``. Each of those keeps its own argparse definitions;
this layer only routes the first token and forwards the rest (so that, e.g.,
``e1f fetch --help`` reaches the fetch parser).

    e1f config add IE00BM67HK77
    e1f fetch
    e1f transactions trade-republic transactions.csv
"""

import argparse
import sys

from e1f import config, fetch, portfolio, transactions

COMMANDS = {
    "config": config.main,
    "fetch": fetch.main,
    "transactions": transactions.main,
    "portfolio": portfolio.main,
}


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="e1f",
        description="ETF universe config and price fetching.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Commands:
  config        Build/maintain the ETF universe YAML from ISINs
  fetch         Populate the SQLite price DB for the universe
  transactions  Ingest and list broker ETF trades in SQLite
  portfolio     Show ETF holdings and average cost from transactions

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
