"""e1f glossary — look up what a metric means and what it's good for (ADR-0034).

Reads the human-readable ``data/glossary.md`` and either lists every term (grouped)
or prints the entries matching a query. The Markdown file is the single source: it
is meant to be read directly *and* queried here, so this module only parses and
renders it — the content lives in one place, not duplicated in code.
"""

import argparse
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

from e1f.common import DEFAULT_GLOSSARY


@dataclass
class Term:
    """One glossary entry: a metric name, its group, and its Markdown body lines."""

    name: str
    group: str
    body: list[str] = field(default_factory=list)


def parse_glossary(text: str) -> list[Term]:
    """Parse the glossary Markdown into ``Term`` entries.

    ``## `` headings are groups, ``### `` headings are terms; everything until the
    next heading is a term's body. Text before the first term (title + intro) is
    ignored. Trailing blank lines are trimmed from each body.
    """
    terms: list[Term] = []
    group = ""
    current: Term | None = None
    for line in text.splitlines():
        if line.startswith("### "):
            current = Term(name=line[4:].strip(), group=group)
            terms.append(current)
        elif line.startswith("## "):
            group = line[3:].strip()
            current = None
        elif line.startswith("# "):
            current = None
        elif current is not None:
            current.body.append(line)
    for term in terms:
        while term.body and not term.body[-1].strip():
            term.body.pop()
    return terms


def _name_matches(name: str, needle: str) -> bool:
    """Case-insensitive name hit anchored at a token start.

    A needle beginning with a letter/digit must sit at a left word boundary, so
    ``P&L`` still fans out to ``P&L€`` / ``P&L%`` / ``P&Lctr`` (all prefixes) while
    ``TER`` hits ``weighted TER`` without matching the buried ``ter`` in
    ``Underwater``. A single letter/digit additionally needs a right boundary
    (``n`` finds ``n (observations)``, not ``drawdown``). Needles starting with a
    non-alphanumeric char (``€``, ``%``, ``Δ``) use plain substring so ``€`` finds
    all EUR-suffixed metrics even though they are never token-isolated.
    """
    hay = name.lower()
    needle = needle.lower()
    if not re.match(r"[a-z0-9]", needle):
        return needle in hay
    right = r"(?![a-z0-9])" if len(needle) == 1 else ""
    return re.search(rf"(?<![a-z0-9]){re.escape(needle)}{right}", hay) is not None


# ASCII stand-ins accepted as aliases for hard-to-type chars.
_ALIASES: dict[str, str] = {"e": "€", "pct": "%", "r2": "r²"}


def find_terms(terms: list[Term], query: str) -> list[Term]:
    """Terms matching ``query`` (case-insensitive): by name first, else by group/body."""
    needle = query.strip().lower()
    if not needle:
        return []
    needle = _ALIASES.get(needle, needle)
    by_name = [t for t in terms if _name_matches(t.name, needle)]
    if by_name:
        return by_name
    if len(needle) == 1 and re.match(r"[a-z0-9]", needle):
        return []  # a letter/digit is not a topical search of every body
    return [
        t for t in terms if needle in t.group.lower() or needle in "\n".join(t.body).lower()
    ]


def _render_body(body: list[str]) -> list[str]:
    """Body lines for the terminal: drop bold markers, bullet ``- `` as ``  • ``."""
    rendered: list[str] = []
    for line in body:
        clean = line.replace("**", "")
        if clean.startswith("- "):
            clean = "  • " + clean[2:]
        rendered.append(clean)
    return rendered


def render_term(term: Term) -> list[str]:
    return [f"\n── {term.name} ──  [{term.group}]", *_render_body(term.body)]


def render_list(terms: list[Term]) -> list[str]:
    lines = [f"\ne1f metrics glossary — {len(terms)} terms"]
    group = None
    for term in terms:
        if term.group != group:
            group = term.group
            lines.append(f"\n{group}")
        lines.append(f"  {term.name}")
    lines.append("\nLook up one:  e1f glossary TWR   (case-insensitive, matches P&L → P&L€ …)")
    return lines


def _cmd_glossary(glossary_path: str, query: str | None) -> int:
    text = Path(glossary_path).read_text(encoding="utf-8")
    terms = parse_glossary(text)
    if not terms:
        print(f"No terms found in glossary file: {glossary_path}")
        return 0

    if query is None:
        for line in render_list(terms):
            print(line)
        return 0

    matches = find_terms(terms, query)
    if not matches:
        print(f"No glossary term matching {query!r}. Run 'e1f glossary' to list them all.")
        return 0
    for term in matches:
        for line in render_term(term):
            print(line)
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="e1f glossary",
        description="Look up what a metric means and what it's useful for",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Reads data/glossary.md (checked in) — the same file you can open and read. With no
term it lists every metric, grouped; with a term it prints the matching entries
(case-insensitive, matched at word starts, so 'P&L' fans out to the whole P&L
family). Quote terms with shell metacharacters, e.g. "P&L".

Examples:
  e1f glossary                # list every term, grouped
  e1f glossary TWR            # one term
  e1f glossary drawdown       # every term whose name contains 'drawdown'
  e1f glossary "P&L"          # P&L€, P&L%, P&Lctr, ΔP&L€, %P&L
        """,
    )
    parser.add_argument(
        "term",
        nargs="*",
        help="Metric to look up (omit to list all). Multiple words are joined.",
    )
    parser.add_argument(
        "--file",
        default=DEFAULT_GLOSSARY,
        help="Glossary Markdown file to read (default: data/glossary.md)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    query = " ".join(args.term).strip() if args.term else None
    try:
        return _cmd_glossary(args.file, query)
    except Exception as e:  # noqa: BLE001 — CLI top-level; all errors become exit code 1
        print(f"✗ Error: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
