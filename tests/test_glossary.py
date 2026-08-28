"""Glossary command: Markdown parse, lookup, and CLI rendering (ADR-0034)."""

import re
from pathlib import Path

import pytest

from e1f import benchmark, deposits, glossary, performance, portfolio
from e1f.common import DEFAULT_GLOSSARY
from e1f.cli import (
    EXPERIMENTAL_COMMANDS,
    STABLE_COMMANDS,
    STABLE_PARSER_FACTORIES,
)

_SAMPLE = """# e1f metrics glossary

Intro paragraph that should be ignored.

## Return

### TWR
- **Where:** performance
- **Definition:** time-weighted.


### CAGR
- **Definition:** annualized TWR.

## Risk

### Max Drawdown
- **Definition:** deepest decline.
"""


def test_parse_glossary_groups_terms_and_trims_bodies():
    terms = glossary.parse_glossary(_SAMPLE)
    assert [t.name for t in terms] == ["TWR", "CAGR", "Max Drawdown"]
    assert [t.group for t in terms] == ["Return", "Return", "Risk"]
    # Intro text before the first term is ignored; trailing blank lines trimmed.
    assert terms[0].body == ["- **Where:** performance", "- **Definition:** time-weighted."]


def test_parse_glossary_empty_when_no_terms():
    assert glossary.parse_glossary("# title\n\njust prose, no ### headings\n") == []


def test_find_terms_name_match_short_circuits_body():
    terms = glossary.parse_glossary(_SAMPLE)
    # 'twr' matches the TWR name; CAGR's *body* also says TWR but name match wins alone.
    assert [t.name for t in glossary.find_terms(terms, "twr")] == ["TWR"]


def test_find_terms_falls_back_to_group_and_body():
    terms = glossary.parse_glossary(_SAMPLE)
    assert [t.name for t in glossary.find_terms(terms, "annualized")] == ["CAGR"]  # body
    assert [t.name for t in glossary.find_terms(terms, "risk")] == ["Max Drawdown"]  # group
    assert glossary.find_terms(terms, "zzz") == []


def test_find_terms_rejects_stripped_empty_query():
    terms = glossary.parse_glossary(_SAMPLE)
    assert glossary.find_terms(terms, "   ") == []


def test_find_terms_single_char_is_a_token_not_a_letter():
    terms = glossary.parse_glossary(
        "## G\n### n (observations)\ncount\n### Max Drawdown\ndeepest n in body\n"
    )
    assert [t.name for t in glossary.find_terms(terms, "n")] == ["n (observations)"]
    assert glossary.find_terms(terms, "x") == []  # no name token; no body fallback


def test_render_term_and_list_shapes():
    terms = glossary.parse_glossary(_SAMPLE)
    term_lines = glossary.render_term(terms[0])
    assert term_lines[0] == "\n── TWR ──  [Return]"
    assert "  • Where: performance" in term_lines  # bold stripped, bullet rewritten

    list_lines = "\n".join(glossary.render_list(terms))
    assert "37 terms" not in list_lines  # sample has 3
    assert "Return" in list_lines and "  TWR" in list_lines and "Risk" in list_lines


def test_main_list_and_lookup_and_miss(tmp_path, capsys):
    path = tmp_path / "glossary.md"
    path.write_text(_SAMPLE, encoding="utf-8")

    assert glossary.main(["--file", str(path)]) == 0
    listed = capsys.readouterr().out
    assert "e1f metrics glossary — 3 terms" in listed and "Max Drawdown" in listed

    assert glossary.main(["--file", str(path), "drawdown"]) == 0
    hit = capsys.readouterr().out
    assert "── Max Drawdown ──" in hit and "deepest decline" in hit

    assert glossary.main(["--file", str(path), "zzz"]) == 0
    assert "No glossary term matching 'zzz'" in capsys.readouterr().out


def test_main_multiword_query_is_joined(tmp_path, capsys):
    path = tmp_path / "glossary.md"
    path.write_text(_SAMPLE, encoding="utf-8")
    assert glossary.main(["--file", str(path), "max", "drawdown"]) == 0
    assert "── Max Drawdown ──" in capsys.readouterr().out


def test_main_whitespace_query_is_not_treated_as_list_all(tmp_path, capsys):
    path = tmp_path / "glossary.md"
    path.write_text(_SAMPLE, encoding="utf-8")
    assert glossary.main(["--file", str(path), "   "]) == 0
    output = capsys.readouterr().out
    assert "No glossary term matching ''" in output
    assert "e1f metrics glossary" not in output


def test_main_no_terms_file(tmp_path, capsys):
    path = tmp_path / "empty.md"
    path.write_text("# title\n\nno terms here\n", encoding="utf-8")
    assert glossary.main(["--file", str(path)]) == 0
    assert "No terms found" in capsys.readouterr().out


def test_main_missing_file_is_error(capsys):
    assert glossary.main(["--file", "/no/such/glossary.md", "TWR"]) == 1
    assert "✗ Error" in capsys.readouterr().out


def test_shipped_glossary_parses_and_documents_core_metrics():
    terms = glossary.parse_glossary(Path(DEFAULT_GLOSSARY).read_text(encoding="utf-8"))
    names = {t.name for t in terms}
    assert {"TWR", "XIRR", "Ctr%", "ROIC", "Beta"} <= names
    assert all(t.body for t in terms)  # every shipped term actually says something
    assert all(t.group for t in terms)  # and belongs to a group


def test_shipped_glossary_entries_say_use_and_complements():
    """Each term teaches when to use it and what to read next to it."""
    terms = glossary.parse_glossary(Path(DEFAULT_GLOSSARY).read_text(encoding="utf-8"))
    for term in terms:
        body = "\n".join(term.body)
        assert "- **Useful for:**" in body, term.name
        assert "- **Read with:**" in body, term.name


def _parser_options(command: str) -> set[str]:
    pending = [STABLE_PARSER_FACTORIES[command]()]
    options: set[str] = set()
    while pending:
        parser = pending.pop()
        for action in parser._actions:
            options.update(action.option_strings)
            choices = getattr(action, "choices", None)
            if isinstance(choices, dict):
                pending.extend(choices.values())
    return options


def test_glossary_where_commands_and_flags_resolve_to_stable_parsers():
    terms = glossary.parse_glossary(Path(DEFAULT_GLOSSARY).read_text(encoding="utf-8"))
    for term in terms:
        where = [line for line in term.body if line.startswith("- **Where:**")]
        assert len(where) == 1, term.name
        current_command: str | None = None
        for fragment in re.findall(r"`([^`]+)`", where[0]):
            tokens = fragment.split()
            if not tokens:
                continue
            if tokens[0] in STABLE_COMMANDS:
                current_command = tokens[0]
                option_tokens = [token for token in tokens[1:] if token.startswith("-")]
            elif tokens[0] in EXPERIMENTAL_COMMANDS:
                pytest.fail(f"{term.name}: experimental command in stable glossary: {tokens[0]}")
            elif current_command is not None and tokens[0].startswith("-"):
                option_tokens = [token for token in tokens if token.startswith("-")]
            else:
                continue
            valid = _parser_options(current_command)
            assert set(option_tokens) <= valid, (
                term.name,
                current_command,
                option_tokens,
                sorted(valid),
            )


def test_stable_metric_headers_have_glossary_entries_with_matching_where():
    terms = glossary.parse_glossary(Path(DEFAULT_GLOSSARY).read_text(encoding="utf-8"))
    contracts = [
        (
            "performance",
            performance._HEADER,
            {
                "MktVal€": "MktVal€",
                "Cost€": "Cost€",
                "P&L€": "P&L€",
                "P&L%": "P&L%",
                "P&Lctr": "P&Lctr",
                "XIRR": "XIRR",
                "TWR": "TWR",
                "Vol": "Vol",
                "MaxDD": "MaxDD",
                "CAGR": "CAGR",
            },
        ),
        (
            "performance",
            performance._SERIES_HEADER,
            {"WTER": "WTER", "Fee€/yr": "Fee€/yr"},
        ),
        (
            "performance",
            performance._CONTRIB_HEADER,
            {"Weight": "Weight", "Ctr%": "Ctr%"},
        ),
        (
            "performance",
            performance._METRICS_SERIES_HEADER,
            {
                "DDdur": "DDdur",
                "SinceHi": "SinceHi",
                "Underwtr": "Underwtr",
                "RecFac": "RecFac",
                "Best": "Best",
                "Worst": "Worst",
                "G/L": "G/L",
            },
        ),
        (
            "portfolio",
            portfolio._table_header(
                show_broker=True,
                show_cost_basis=True,
                show_status=True,
            ),
            {
                "TER": "TER",
                "Fee/yr": "Fee€/yr",
                "Weight": "Weight",
                "Value€": "Value€",
            },
        ),
        (
            "deposits",
            deposits._HEADER,
            {
                "Amount€": "Amount€",
                "Value€": "Value€",
                "Gain€": "Gain€",
                "Ret%": "Ret%",
                "%P&L": "%P&L",
            },
        ),
        (
            "benchmark",
            benchmark._HEADER,
            {
                "n": "n",
                "Beta": "Beta",
                "R2": "R2",
                "TE": "TE",
                "IR": "IR",
                "RelStr": "RelStr",
                "Out%": "Out%",
            },
        ),
    ]
    for command, header, labels in contracts:
        for emitted, query in labels.items():
            assert emitted in header, (command, emitted)
            matches = glossary.find_terms(terms, query)
            assert matches, (command, emitted, query)
            assert any(
                f"`{command}" in line
                for term in matches
                for line in term.body
                if line.startswith("- **Where:**")
            ), (command, emitted, [term.name for term in matches])


def test_shipped_glossary_column_aliases_resolve():
    """Screen labels in headings so name-match finds them (not stolen by a sibling)."""
    terms = glossary.parse_glossary(Path(DEFAULT_GLOSSARY).read_text(encoding="utf-8"))

    def names(query: str) -> list[str]:
        return [t.name for t in glossary.find_terms(terms, query)]

    trailing = "Trailing 1M / 3M / 6M (1 Month / 3 Months / 6 Months)"
    assert names("MaxDD") == ["Max Drawdown (MaxDD)", "MaxDD Duration (DDdur)"]
    assert names("G/L") == ["Max Gain / Max Loss (G/L)"]
    assert names("1 Month") == [trailing]
    assert names("3 Months") == [trailing]
    assert names("Vol") == ["Volatility (Vol)"]
    assert names("RecFac") == ["Recovery Factor (RecFac)"]
    assert names("SinceHi") == ["Days Since High (SinceHi)"]
    assert names("DDdur") == ["MaxDD Duration (DDdur)"]
    assert names("Underwtr") == ["Underwater (Underwtr)"]
    best_worst = ["Best Day / Worst Day (Best / Worst)", "Best Month / Worst Month"]
    assert names("Best") == best_worst
    assert names("Worst") == best_worst
    assert names("n") == ["n (observations)"]

    # R2 (ASCII, as the benchmark table prints its column) resolves to the R² term.
    assert names("R2") == names("R²") == ["R²"]
    # 'TER' hits the weighted-TER entry, not the buried 'ter' in 'Underwater'.
    assert names("TER") == ["TER (per fund)", "WTER (weighted TER)"]


def test_special_char_single_queries_resolve():
    """€, %, Δ are not token-isolated in names so must fall back to plain substring."""
    terms = glossary.parse_glossary(Path(DEFAULT_GLOSSARY).read_text(encoding="utf-8"))

    def names(query: str) -> list[str]:
        return [t.name for t in glossary.find_terms(terms, query)]

    eur = [
        "MktVal€",
        "Cost€",
        "P&L€",
        "ΔMktVal€ / ΔCost€ / ΔP&L€",
        "Amount€",
        "Value€ (per deposit)",
        "Gain€ (per deposit)",
        "Fee€/yr",
    ]
    percent = ["P&L%", "Ctr%", "Ret% (per deposit)", "%P&L (per deposit)", "Out%"]
    assert names("€") == names("e") == eur
    assert names("%") == names("pct") == percent


def test_main_on_shipped_glossary_lists_and_looks_up(capsys):
    assert glossary.main([]) == 0  # default --file is the shipped glossary
    assert "e1f metrics glossary" in capsys.readouterr().out

    assert glossary.main(["P&L"]) == 0
    out = capsys.readouterr().out
    matched = [t.name for t in glossary.find_terms(
        glossary.parse_glossary(Path(DEFAULT_GLOSSARY).read_text(encoding="utf-8")),
        "P&L",
    )]
    assert matched == [
        "P&L€",
        "P&L%",
        "P&Lctr",
        "ΔMktVal€ / ΔCost€ / ΔP&L€",
        "%P&L (per deposit)",
    ]
    assert out.count("\n── ") == len(matched)
