"""Glossary command: Markdown parse, lookup, and CLI rendering (ADR-0034)."""

from pathlib import Path

from e1f import glossary
from e1f.common import DEFAULT_GLOSSARY

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


def test_shipped_glossary_column_aliases_resolve():
    """Screen labels in headings so name-match finds them (not stolen by a sibling)."""
    terms = glossary.parse_glossary(Path(DEFAULT_GLOSSARY).read_text(encoding="utf-8"))

    def names(query: str) -> list[str]:
        return [t.name for t in glossary.find_terms(terms, query)]

    maxdd = names("MaxDD")
    assert any(n.startswith("Max Drawdown") for n in maxdd)
    assert any(n.startswith("MaxDD Duration") for n in maxdd)

    assert any("Max Gain / Max Loss" in n for n in names("G/L"))
    assert any(n.startswith("Trailing") for n in names("1 Month"))
    assert any(n.startswith("Trailing") for n in names("3 Months"))
    assert any(n.startswith("Volatility") for n in names("Vol"))
    assert any(n.startswith("Recovery Factor") for n in names("RecFac"))
    assert any(n.startswith("Days Since High") for n in names("SinceHi"))
    assert any(n.startswith("MaxDD Duration") for n in names("DDdur"))
    assert any(n.startswith("Underwater") for n in names("Underwtr"))
    assert any("Best Day" in n for n in names("Best"))
    assert any("Worst Day" in n for n in names("Worst"))
    assert names("n") == ["n (observations)"]

    # R2 (ASCII, as the benchmark table prints its column) resolves to the R² term.
    assert names("R2") == names("R²")
    assert any(n == "R²" for n in names("R2"))
    # 'TER' hits the weighted-TER entry, not the buried 'ter' in 'Underwater'.
    ter = names("TER")
    assert any("WTER" in n for n in ter)
    assert not any("Underwater" in n for n in ter)


def test_special_char_single_queries_resolve():
    """€, %, Δ are not token-isolated in names so must fall back to plain substring."""
    terms = glossary.parse_glossary(Path(DEFAULT_GLOSSARY).read_text(encoding="utf-8"))

    def names(query: str) -> list[str]:
        return [t.name for t in glossary.find_terms(terms, query)]

    assert any("€" in n for n in names("€"))
    assert any("€" in n for n in names("e"))  # ASCII alias for €
    assert any("%" in n for n in names("%"))
    assert any("%" in n for n in names("pct"))  # ASCII alias for %


def test_main_on_shipped_glossary_lists_and_looks_up(capsys):
    assert glossary.main([]) == 0  # default --file is the shipped glossary
    assert "e1f metrics glossary" in capsys.readouterr().out

    assert glossary.main(["P&L"]) == 0
    out = capsys.readouterr().out
    # 'P&L' fans out to the whole family: P&L€, P&L%, P&Lctr, ΔP&L€, %P&L.
    for name in ("P&L€", "P&L%", "P&Lctr", "ΔP&L€", "%P&L"):
        assert name in out, name
