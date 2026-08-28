"""Focused mutation contract for glossary query semantics."""

from e1f.glossary import Term, find_terms


TERMS = [
    Term("TWR", "Return", ["mentions annualized"]),
    Term("CAGR", "Return", ["annualized TWR"]),
    Term("n (observations)", "Risk", ["count"]),
    Term("R²", "Benchmark", ["fit"]),
]


def test_find_terms_prefers_name_matches_over_body_matches() -> None:
    assert [term.name for term in find_terms(TERMS, "twr")] == ["TWR"]


def test_find_terms_falls_back_to_group_and_body() -> None:
    assert [term.name for term in find_terms(TERMS, "annualized")] == ["TWR", "CAGR"]
    assert [term.name for term in find_terms(TERMS, "risk")] == ["n (observations)"]


def test_find_terms_rejects_empty_and_single_letter_body_searches() -> None:
    assert find_terms(TERMS, "   ") == []
    assert [term.name for term in find_terms(TERMS, "n")] == ["n (observations)"]
    assert find_terms(TERMS, "x") == []


def test_find_terms_applies_ascii_aliases() -> None:
    assert [term.name for term in find_terms(TERMS, "r2")] == ["R²"]
