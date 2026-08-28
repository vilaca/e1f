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


def test_main_on_shipped_glossary_lists_and_looks_up(capsys):
    assert glossary.main([]) == 0  # default --file is the shipped glossary
    assert "e1f metrics glossary" in capsys.readouterr().out

    assert glossary.main(["P&L"]) == 0
    out = capsys.readouterr().out
    assert "P&L€" in out and "P&Lctr" in out  # substring 'P&L' fans out to the family
