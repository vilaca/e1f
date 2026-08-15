"""Generate and serve shell completion for the e1f CLI."""

import argparse
import os
from collections.abc import Callable

ParserFactory = Callable[[], argparse.ArgumentParser]
_FILE_MARKER = "__E1F_FILES__"
_PATH_DESTINATIONS = {"config", "currency_meta", "db"}

_BASH = r"""_e1f() {
    local candidate cur wants_files
    local -a candidates
    cur="${COMP_WORDS[COMP_CWORD]}"
    wants_files=0

    while IFS= read -r candidate; do
        if [[ "$candidate" == "__E1F_FILES__" ]]; then
            wants_files=1
        elif [[ -n "$candidate" ]]; then
            candidates+=("$candidate")
        fi
    done < <(command e1f autocomplete --complete "${COMP_WORDS[@]:1}")

    COMPREPLY=()
    if (( ${#candidates[@]} )); then
        while IFS= read -r candidate; do
            COMPREPLY+=("$candidate")
        done < <(compgen -W "${candidates[*]}" -- "$cur")
    fi
    if (( wants_files )); then
        while IFS= read -r candidate; do
            COMPREPLY+=("$candidate")
        done < <(compgen -f -- "$cur")
    fi
}
complete -F _e1f e1f
"""

_ZSH = r"""#compdef e1f
_e1f() {
    local -a candidates
    local wants_files=0
    candidates=("${(@f)$(command e1f autocomplete --complete "${words[@]:1}")}")

    if (( ${candidates[(I)__E1F_FILES__]} )); then
        wants_files=1
        candidates=(${candidates:#__E1F_FILES__})
    fi
    (( ${#candidates} )) && compadd -- "${candidates[@]}"
    (( wants_files )) && _files
}
compdef _e1f e1f
"""


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="e1f autocomplete",
        description="Print shell completion setup",
        epilog=(
            "Activate for the current shell:\n"
            '  source <(e1f autocomplete bash)\n'
            '  source <(e1f autocomplete zsh)'
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "shell",
        nargs="?",
        choices=("bash", "zsh"),
        help="Shell to generate for (default: infer from $SHELL)",
    )
    return parser


def _subparser_action(
    parser: argparse.ArgumentParser,
) -> argparse._SubParsersAction[argparse.ArgumentParser] | None:
    return next(
        (
            action
            for action in parser._actions
            if isinstance(action, argparse._SubParsersAction)
        ),
        None,
    )


def _active_parser(
    parser: argparse.ArgumentParser, tokens: list[str]
) -> argparse.ArgumentParser:
    action = _subparser_action(parser)
    if action is None:
        return parser
    for token in tokens:
        if token in action.choices:
            return action.choices[token]
    return parser


def _option_action(
    parser: argparse.ArgumentParser, option: str
) -> argparse.Action | None:
    return next(
        (action for action in parser._actions if option in action.option_strings),
        None,
    )


def _wants_files(
    parser: argparse.ArgumentParser, consumed: list[str], prefix: str
) -> bool:
    if prefix.startswith("-"):
        return False
    if consumed:
        previous = _option_action(parser, consumed[-1])
        if previous is not None:
            return previous.dest in _PATH_DESTINATIONS
    return any(
        not action.option_strings and action.dest.endswith("_path")
        for action in parser._actions
    )


def _candidates(
    words: list[str], factories: dict[str, ParserFactory]
) -> list[str]:
    prefix = words[-1] if words else ""
    consumed = words[:-1] if words else []

    if not consumed:
        return [command for command in factories if command.startswith(prefix)]

    command = consumed[0]
    if command not in factories:
        return []

    parser = _active_parser(factories[command](), consumed[1:])
    previous = _option_action(parser, consumed[-1])
    if previous is not None and previous.choices is not None:
        return [
            str(choice)
            for choice in previous.choices
            if str(choice).startswith(prefix)
        ]

    candidates = [
        option
        for action in parser._actions
        for option in action.option_strings
        if option.startswith(prefix)
    ]
    subparsers = _subparser_action(parser)
    if subparsers is not None:
        candidates.extend(
            choice for choice in subparsers.choices if choice.startswith(prefix)
        )
    if _wants_files(parser, consumed, prefix):
        candidates.append(_FILE_MARKER)
    return candidates


def main(argv: list[str], factories: dict[str, ParserFactory]) -> int:
    if argv and argv[0] == "--complete":
        print("\n".join(_candidates(argv[1:], factories)))
        return 0

    args = _build_parser().parse_args(argv)
    shell = args.shell or os.path.basename(os.environ.get("SHELL", ""))
    scripts = {"bash": _BASH, "zsh": _ZSH}
    if shell not in scripts:
        _build_parser().error("could not infer bash or zsh; specify the shell explicitly")
    print(scripts[shell], end="")
    return 0
