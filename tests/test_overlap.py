"""Overlap: pure floor math, the alias round-trip, and the command (ADR-0013 v1b)."""

import sqlite3
from contextlib import closing

import pytest
import yaml

from e1f.experimental import overlap
from e1f.experimental.common import (
    DIMENSION_SECURITY,
    HoldingRow,
    insert_lookthrough_snapshot,
    load_security_aliases,
    normalize_security_name,
    upsert_security_alias,
)
from e1f.experimental.overlap import (
    Observation,
    ValuationView,
    compute_floors,
    unresolved_worklist,
)

F1 = "IE00FUND00001"
F2 = "IE00FUND00002"
F3 = "IE00FUND00003"


def _obs(fund, raw, weight=0.05, as_of="2026-07-01"):
    return Observation(fund, raw, normalize_security_name(raw), weight, as_of)


# ---------------------------------------------------------------------------
# Pure floor math — resolve → group_by(canonical_key) → filter → Σ Vf·w → gate
# ---------------------------------------------------------------------------


def test_four_string_identity_fixture_novartis_one_group_roche_two():
    """The load-bearing regression: canonical_key is the sole join key (decision 2).

    Two Novartis source strings collapse to ONE exposure group; the two Roche
    securities (same issuer) stay as TWO — a naïve issuer-name fold is wrong both
    ways. The gate needs each spanning ≥2 funds, so both funds carry every name.
    """
    aliases = {
        "Novartis AG": ("novartis-ord", "Novartis AG"),
        "Novartis AG Registered Shares": ("novartis-ord", "Novartis AG"),
        "Roche Holding AG Ordinary Shares new": ("roche-ord", "Roche Ord"),
        "Roche Holding AG Dividend Right Cert.": ("roche-drc", "Roche DRC"),
    }
    observations = [
        _obs(F1, "Novartis AG", 0.05),
        _obs(F1, "Roche Holding AG Ordinary Shares new", 0.04),
        _obs(F1, "Roche Holding AG Dividend Right Cert.", 0.02),
        _obs(F2, "Novartis AG Registered Shares", 0.03),
        _obs(F2, "Roche Holding AG Ordinary Shares new", 0.03),
        _obs(F2, "Roche Holding AG Dividend Right Cert.", 0.01),
    ]
    floors = compute_floors(observations, {F1: 1000.0, F2: 2000.0}, aliases)

    assert {f.canonical_key for f in floors} == {"novartis-ord", "roche-ord", "roche-drc"}
    novartis = next(f for f in floors if f.canonical_key == "novartis-ord")
    assert novartis.resolved_in == 2  # merged across two source strings
    assert novartis.floor_eur == pytest.approx(1000 * 0.05 + 2000 * 0.03)  # 110


def test_single_fund_resolved_security_is_absent_gate_after_resolution():
    aliases = {"Apple Inc.": ("apple-ord", "Apple Inc.")}
    observations = [_obs(F1, "Apple Inc.", 0.05)]  # one fund only
    assert compute_floors(observations, {F1: 1000.0}, aliases) == []


def test_negative_weight_excluded_individually_never_clamped():
    aliases = {"Apple Inc.": ("apple-ord", "Apple Inc.")}
    observations = [_obs(F1, "Apple Inc.", 0.05), _obs(F2, "Apple Inc.", -0.10)]
    floors = compute_floors(observations, {F1: 1000.0, F2: 1000.0}, aliases)
    assert len(floors) == 1
    floor = floors[0]
    assert floor.resolved_in == 2   # identity is resolved in both funds
    assert floor.floor_from == 1    # only the valid weight contributes
    assert floor.excluded == 1
    assert floor.floor_eur == pytest.approx(50.0)  # the short leg never inflates it


def test_over_100pct_weight_excluded():
    aliases = {"Apple Inc.": ("apple-ord", "Apple Inc.")}
    observations = [_obs(F1, "Apple Inc.", 0.05), _obs(F2, "Apple Inc.", 1.5)]
    floors = compute_floors(observations, {F1: 1000.0, F2: 1000.0}, aliases)
    assert floors[0].excluded == 1 and floors[0].floor_eur == pytest.approx(50.0)


def test_unvalued_fund_observation_dropped_from_gate_and_floor():
    # F2 is not in fund_values (unvaluable) -> its resolved obs never counts, so the
    # security is resolved in only one *valued* fund and fails the ≥2 gate.
    aliases = {"Apple Inc.": ("apple-ord", "Apple Inc.")}
    observations = [_obs(F1, "Apple Inc.", 0.05), _obs(F2, "Apple Inc.", 0.05)]
    assert compute_floors(observations, {F1: 1000.0}, aliases) == []


def test_denominator_two_filter_valued_fund_with_invalid_obs_stays_in_denominator():
    # F3 is valued (in fund_values) but its only observation is invalid: it stays
    # in the denominator (numerator excludes it) — the load-bearing sentence.
    aliases = {"Apple Inc.": ("apple-ord", "Apple Inc.")}
    observations = [
        _obs(F1, "Apple Inc.", 0.10),
        _obs(F2, "Apple Inc.", 0.10),
        _obs(F3, "Apple Inc.", -0.10),  # invalid -> excluded from numerator
    ]
    fund_values = {F1: 1000.0, F2: 1000.0, F3: 1000.0}
    floor = compute_floors(observations, fund_values, aliases)[0]
    assert floor.resolved_in == 3 and floor.floor_from == 2 and floor.excluded == 1
    assert floor.floor_eur == pytest.approx(200.0)
    view = ValuationView("2026-08-24", fund_values, [])
    # 200 / 3000 — F3's €1,000 remains in the denominator though its obs was excluded.
    assert floor.pct_of(view.valued_total) == pytest.approx(200.0 / 3000.0 * 100.0)


def test_unresolved_name_never_produces_a_floor():
    observations = [_obs(F1, "Apple Inc.", 0.05), _obs(F2, "Apple Inc.", 0.05)]
    assert compute_floors(observations, {F1: 1000.0, F2: 1000.0}, {}) == []


def test_floors_sorted_by_euro_descending():
    aliases = {
        "Apple Inc.": ("apple-ord", "Apple Inc."),
        "ASML Holding NV": ("asml-ord", "ASML Holding NV"),
    }
    observations = [
        _obs(F1, "Apple Inc.", 0.02), _obs(F2, "Apple Inc.", 0.02),
        _obs(F1, "ASML Holding NV", 0.05), _obs(F2, "ASML Holding NV", 0.05),
    ]
    floors = compute_floors(observations, {F1: 1000.0, F2: 1000.0}, aliases)
    assert [f.canonical_key for f in floors] == ["asml-ord", "apple-ord"]


# ---------------------------------------------------------------------------
# Unresolved worklist + eligibility
# ---------------------------------------------------------------------------


def test_unresolved_worklist_lists_cooccurring_unresolved_names_only():
    # The worklist groups by normalized name (Tier-1 co-occurrence): "Apple Inc."
    # and "APPLE INC" fold to the same key; a single-fund name never co-occurs.
    observations = [
        _obs(F1, "Apple Inc."),  # weight defaulted below
        _obs(F2, "APPLE INC"),   # same normalized key, different source string
        _obs(F1, "Tesla Inc"),   # single fund -> not co-occurring
    ]
    worklist = unresolved_worklist(observations, {})
    assert len(worklist) == 1
    assert worklist[0].raw_names == ["APPLE INC", "Apple Inc."]
    assert worklist[0].fund_count == 2


def test_worklist_drops_group_once_every_name_is_resolved():
    observations = [_obs(F1, "Apple Inc.", 0.05), _obs(F2, "Apple Inc.", 0.05)]
    resolved = {"Apple Inc.": ("apple-ord", "Apple Inc.")}
    assert unresolved_worklist(observations, resolved) == []


def test_weight_issue_flags_negative_and_over_100_but_not_valid():
    assert overlap._weight_issue(-0.01) is not None
    assert overlap._weight_issue(1.5) is not None
    assert overlap._weight_issue(0.5) is None
    assert overlap._weight_issue(1.0) is None  # a 100% weight is a valid long weight


# ---------------------------------------------------------------------------
# security_alias round-trip (common primitives overlap writes/reads)
# ---------------------------------------------------------------------------


def test_resolve_upsert_is_idempotent_and_stamps_reviewed_at(tmp_path):
    db = str(tmp_path / "e1f.db")
    stamp = upsert_security_alias(db, "Apple Inc.", "apple-ord", canonical_name="Apple")
    assert stamp  # reviewed_at auto-stamped by the write act
    assert load_security_aliases(db) == {"Apple Inc.": ("apple-ord", "Apple")}

    # Re-resolving updates the key and bumps reviewed_at, never duplicates the row.
    upsert_security_alias(db, "Apple Inc.", "apple-us", reviewed_at="2030-01-01T00:00:00")
    aliases = load_security_aliases(db)
    assert aliases == {"Apple Inc.": ("apple-us", "Apple Inc.")}  # name defaults to raw
    with closing(sqlite3.connect(db)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM security_alias").fetchone()[0] == 1


def test_load_security_aliases_empty_when_no_table(tmp_path):
    assert load_security_aliases(str(tmp_path / "missing.db")) == {}


def test_db_flag_accepted_after_the_subcommand(tmp_path):
    # `--db` must work placed after `resolve` (natural CLI order), not only before
    # the subcommand token — the argparse subparser-default trap.
    db = str(tmp_path / "e1f.db")
    assert overlap.main(["resolve", "Apple Inc.", "apple-ord", "--db", db]) == 0
    assert load_security_aliases(db) == {"Apple Inc.": ("apple-ord", "Apple Inc.")}


# ---------------------------------------------------------------------------
# Command end to end
# ---------------------------------------------------------------------------


def _seed(tmp_path, *, snapshots=None):
    """Two EUR funds, each bought and priced, with look-through snapshots."""
    db = tmp_path / "e1f.db"
    with closing(sqlite3.connect(str(db))) as conn:
        conn.execute(
            "CREATE TABLE transactions (broker TEXT, transaction_id TEXT, datetime TEXT, "
            "symbol TEXT, side TEXT, shares REAL, price REAL, fee REAL, tax REAL, "
            "PRIMARY KEY (broker, transaction_id))"
        )
        conn.execute(
            "CREATE TABLE prices (isin TEXT, date TEXT, close REAL, PRIMARY KEY (isin, date))"
        )
        conn.executemany(
            "INSERT INTO transactions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                ("tr", "t1", "2024-01-01", F1, "BUY", 100.0, 10.0, 0.0, 0.0),
                ("tr", "t2", "2024-01-01", F2, "BUY", 100.0, 10.0, 0.0, 0.0),
            ],
        )
        conn.executemany(
            "INSERT INTO prices VALUES (?, ?, ?)",
            [
                (F1, "2024-01-01", 10.0), (F1, "2024-12-31", 12.0),
                (F2, "2024-01-01", 10.0), (F2, "2024-12-31", 12.0),
            ],
        )
        conn.commit()
    meta = tmp_path / "meta.yaml"
    meta.write_text(yaml.dump({F1: {"currency": "EUR"}, F2: {"currency": "EUR"}}))

    for isin, rows in (snapshots or {}).items():
        insert_lookthrough_snapshot(
            str(db), fund_id=isin, as_of="2024-11-01", source="yfinance", tier="provider",
            retrieved_at="2024-11-01T00:00:00", reported_holding_count=None, holdings=rows,
        )
    return str(db), str(meta)


def _security(name, weight, rank):
    return HoldingRow(DIMENSION_SECURITY, name, normalize_security_name(name), weight, rank)


def test_report_shows_floor_after_resolution(tmp_path, capsys):
    db, meta = _seed(
        tmp_path,
        snapshots={
            F1: [_security("Apple Inc.", 0.05, 1)],
            F2: [_security("Apple Inc.", 0.04, 1)],
        },
    )
    # Before resolution: unresolved worklist, no floor.
    assert overlap.main(["--db", db, "--currency-meta", meta, "--as-of", "2024-12-31"]) == 0
    before = capsys.readouterr().out
    assert "No cross-fund overlap established yet" in before
    assert "Apple Inc." in before  # in the worklist

    assert overlap.main(["--db", db, "resolve", "Apple Inc.", "apple-ord"]) == 0
    capsys.readouterr()

    assert overlap.main(["--db", db, "--currency-meta", meta, "--as-of", "2024-12-31"]) == 0
    after = capsys.readouterr().out
    # Each fund worth 100*12 = 1200; floor = 1200*.05 + 1200*.04 = 108; denom 2400.
    assert "≥ €108" in after
    assert "of portfolio" in after  # 100% valuation coverage collapses the label
    assert "resolved in 2 funds" in after


def test_report_explain_reconstructs_chain_and_both_dates(tmp_path, capsys):
    db, meta = _seed(
        tmp_path,
        snapshots={
            F1: [_security("Apple Inc.", 0.05, 1)],
            F2: [_security("Apple Inc.", 0.04, 1)],
        },
    )
    upsert_security_alias(db, "Apple Inc.", "apple-ord")
    assert overlap.main(
        ["--db", db, "--currency-meta", meta, "--as-of", "2024-12-31", "--explain"]
    ) == 0
    out = capsys.readouterr().out
    assert "apple-ord" in out
    assert "snapshot as_of 2024-11-01" in out    # weights date
    assert "valuation as_of 2024-12-31" in out   # € value date
    assert "E_floor = Σ Vf·w" in out


def test_candidates_lists_both_tiers(tmp_path, capsys):
    db, _meta = _seed(
        tmp_path,
        snapshots={
            F1: [_security("Apple Inc.", 0.05, 1), _security("Tesla Inc", 0.03, 2)],
            F2: [_security("Apple Inc.", 0.04, 1)],
        },
    )
    upsert_security_alias(db, "Apple Inc.", "apple-ord")
    assert overlap.main(["--db", db, "candidates"]) == 0
    out = capsys.readouterr().out
    assert "Tier 1 — co-occurrence seed" in out
    assert "Tier 2 — complete observed-name roster" in out
    assert "apple-ord" in out   # resolved group collapsed to its key
    assert "Tesla Inc" in out   # unresolved, single-fund, still in the roster


def test_report_no_holdings(tmp_path, capsys):
    db = tmp_path / "empty.db"
    with closing(sqlite3.connect(str(db))) as conn:
        conn.execute(
            "CREATE TABLE transactions (broker TEXT, transaction_id TEXT, datetime TEXT, "
            "symbol TEXT, side TEXT, shares REAL, price REAL, fee REAL, tax REAL, "
            "PRIMARY KEY (broker, transaction_id))"
        )
        conn.commit()
    assert overlap.main(["--db", str(db)]) == 0
    assert "No ETF holdings in database" in capsys.readouterr().out


def test_report_discloses_unvaluable_fund(tmp_path, capsys):
    # F1 priced (valued), F2 has no price -> unvaluable, disclosed and excluded.
    db, meta = _seed(
        tmp_path,
        snapshots={F1: [_security("Apple Inc.", 0.05, 1)]},
    )
    with closing(sqlite3.connect(db)) as conn:
        conn.execute("DELETE FROM prices WHERE isin = ?", (F2,))
        conn.commit()
    assert overlap.main(["--db", db, "--currency-meta", meta, "--as-of", "2024-12-31"]) == 0
    out = capsys.readouterr().out
    assert "excluded from overlap" in out and F2 in out


def test_bad_as_of_is_exit_1(tmp_path, capsys):
    db, meta = _seed(tmp_path)
    assert overlap.main(["--db", db, "--currency-meta", meta, "--as-of", "nope"]) == 1
    assert "✗ Error" in capsys.readouterr().out
