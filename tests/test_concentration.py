"""Concentration: pure bounds math, snapshot storage, and the command (ADR-0012)."""

import sqlite3
from contextlib import closing

import pytest
import yaml

from e1f.common import _limited_by
from e1f.experimental import concentration as conc
from e1f.experimental.common import (
    DIMENSION_ASSET_CLASS,
    DIMENSION_SECTOR,
    DIMENSION_SECURITY,
    HoldingRow,
    init_lookthrough_schema,
    insert_lookthrough_snapshot,
    latest_lookthrough_snapshot,
    normalize_security_name,
)

VWCE = "IE00BK5BQT80"
CSPX = "IE00B5BMR087"


# ---------------------------------------------------------------------------
# Pure concentration math
# ---------------------------------------------------------------------------

def test_hhi_and_coverage_and_remainder():
    weights = [0.3, 0.2, 0.1]
    assert conc.hhi(weights) == pytest.approx(0.14)
    assert conc.coverage(weights) == pytest.approx(0.6)
    assert conc.remainder(weights) == pytest.approx(0.4)


def test_remainder_clamped_at_zero():
    # Weights that (with rounding) exceed 1 never produce a negative remainder.
    assert conc.remainder([0.6, 0.5]) == 0.0


def test_hhi_bounds_without_n_falls_back_to_observed():
    weights = [0.3, 0.2, 0.1]
    hhi_min, hhi_max = conc.hhi_bounds(weights, None)
    assert hhi_min == pytest.approx(0.14)              # infimum degrades to observed
    assert hhi_max == pytest.approx(0.14 + 0.4 * 0.1)  # rank cap R*w_k


def test_hhi_bounds_tightens_with_reported_count():
    weights = [0.3, 0.2, 0.1]  # k=3, R=0.4
    hhi_min, hhi_max = conc.hhi_bounds(weights, 13)
    assert hhi_min == pytest.approx(0.14 + (0.4 * 0.4) / (13 - 3))
    assert hhi_max == pytest.approx(0.18)


def test_hhi_bounds_ignores_count_not_exceeding_observed():
    weights = [0.3, 0.2, 0.1]
    # N <= k cannot tighten (no unobserved names) -> min stays at observed.
    assert conc.hhi_bounds(weights, 3)[0] == pytest.approx(0.14)


def test_hhi_bounds_empty_is_zero():
    assert conc.hhi_bounds([], 100) == (0.0, 0.0)


def test_effective_holdings_inverse_and_guard():
    assert conc.effective_holdings(0.1) == pytest.approx(10.0)
    assert conc.effective_holdings(0.0) is None


def test_cumulative_share_known_and_unknown():
    weights = [0.3, 0.2, 0.1]
    assert conc.cumulative_share(weights, 1) == pytest.approx(0.3)
    assert conc.cumulative_share(weights, 3) == pytest.approx(0.6)
    assert conc.cumulative_share(weights, 5) is None  # deeper than observed -> unknown


# ---------------------------------------------------------------------------
# Metric contracts (data-requirement declarations)
# ---------------------------------------------------------------------------

def test_dimension_issue_flags_unsound_weightings():
    assert conc.dimension_issue([("A", 0.6), ("B", 0.4)]) is None            # sound
    assert conc.dimension_issue([]) == "no weights"
    assert conc.dimension_issue([("A", -1e-12), ("B", 1.0)]) == "contains negative weights"
    assert conc.dimension_issue([("A", 1.145), ("B", -0.145)]) == "contains negative weights"
    assert conc.dimension_issue([("A", 1.2), ("B", 0.0)]) == "a weight exceeds 100%"
    assert conc.dimension_issue([("A", 0.30)]).startswith("weights sum to")


def test_security_issue_flags_invalid_partial_weights():
    assert conc.security_issue([0.6, 0.4]) is None
    assert conc.security_issue([float("nan")]) == "contains non-finite weights"
    assert conc.security_issue([0.6, 0.5]) == "observed weights sum to 110%"
    assert conc.security_issue([0.1, -0.001]) == "contains negative weights"
    assert conc.security_issue([1.01]) == "a weight exceeds 100%"


def test_dimension_weights_validity_properties():
    good = conc.DimensionWeights([("A", 0.7), ("B", 0.3)])
    bad = conc.DimensionWeights([("A", 1.145), ("B", -0.145)])
    assert good.is_valid and good.issue is None
    assert not bad.is_valid and bad.issue == "contains negative weights"


def test_contract_method_versions_are_declared():
    assert conc.SECURITY_CONTRACT.method_version == "hhi_rank_capped_tail_v1"
    assert conc.SECTOR_CONTRACT.method_version == "hhi_full_weights_v1"
    assert conc.REGION_CONTRACT.method_version == "region_unavailable_v1"


def test_limited_by_derives_from_requires_split():
    lines = _limited_by(conc.SECURITY_CONTRACT)
    limited = next(line for line in lines if "Limited by:" in line)
    not_limited = next(line for line in lines if "Not limited by:" in line)
    assert "reported holding count" in limited
    assert "canonical security identity" in not_limited


def test_limited_by_complete_metric_says_nothing():
    lines = _limited_by(conc.SECTOR_CONTRACT)
    assert any("nothing (complete)" in line for line in lines)


# ---------------------------------------------------------------------------
# Snapshot storage primitives (common)
# ---------------------------------------------------------------------------

def _security(name, weight, rank):
    return HoldingRow(DIMENSION_SECURITY, name, normalize_security_name(name), weight, rank)


def _sample_rows():
    return [
        _security("Apple Inc.", 0.05, 1),
        _security("Microsoft Corp", 0.04, 2),
        HoldingRow(DIMENSION_SECTOR, "Technology", None, 1.0, None),
        HoldingRow(DIMENSION_ASSET_CLASS, "stockPosition", None, 1.0, None),
    ]


def _insert(db, isin, rows, *, as_of="2026-08-24", source="yfinance", tier="provider",
            retrieved_at="2026-08-24T00:00:00", n=None):
    return insert_lookthrough_snapshot(
        db, fund_id=isin, as_of=as_of, source=source, tier=tier,
        retrieved_at=retrieved_at, reported_holding_count=n, holdings=rows,
    )


def test_insert_and_read_back_snapshot(tmp_path):
    db = str(tmp_path / "e1f.db")
    snapshot_id = _insert(db, VWCE, _sample_rows())
    assert snapshot_id is not None

    snap = latest_lookthrough_snapshot(db, VWCE)
    assert snap is not None
    assert snap.fund_id == VWCE and snap.source == "yfinance" and snap.tier == "provider"
    assert [h.raw_name for h in snap.by_dimension(DIMENSION_SECURITY)] == [
        "Apple Inc.", "Microsoft Corp"
    ]
    assert len(snap.by_dimension(DIMENSION_SECTOR)) == 1


def test_identical_reobservation_is_not_reinserted(tmp_path):
    db = str(tmp_path / "e1f.db")
    assert _insert(db, VWCE, _sample_rows()) is not None
    # Same composition, fetched on a later day -> not a new snapshot (no fetch log).
    assert _insert(db, VWCE, _sample_rows(), as_of="2026-09-01") is None


def test_correction_appends_a_new_snapshot(tmp_path):
    db = str(tmp_path / "e1f.db")
    first = _insert(db, VWCE, _sample_rows())
    changed = [_security("Apple Inc.", 0.06, 1), _security("Nvidia Corp", 0.05, 2)]
    second = _insert(db, VWCE, changed)
    assert second is not None and second != first

    with closing(sqlite3.connect(db)) as c:
        assert c.execute("SELECT COUNT(*) FROM holdings_snapshot").fetchone()[0] == 2
    # Analysis reads the latest; the original is retained as evidence.
    assert latest_lookthrough_snapshot(db, VWCE).id == second


def test_latest_prefers_higher_tier_even_when_older(tmp_path):
    db = str(tmp_path / "e1f.db")
    _insert(db, VWCE, _sample_rows(), as_of="2026-08-24", tier="provider")
    _insert(db, VWCE, [_security("Apple Inc.", 0.07, 1)],
            as_of="2020-01-01", tier="issuer")
    snap = latest_lookthrough_snapshot(db, VWCE)
    assert snap.tier == "issuer"  # tier rank beats the newer provider as_of


def test_latest_none_when_no_table(tmp_path):
    assert latest_lookthrough_snapshot(str(tmp_path / "empty.db"), VWCE) is None


def test_init_schema_is_idempotent(tmp_path):
    db = str(tmp_path / "e1f.db")
    with closing(sqlite3.connect(db)) as c:
        init_lookthrough_schema(c)
        init_lookthrough_schema(c)  # second call must not raise


def test_normalize_security_name_strips_suffixes():
    assert normalize_security_name("Apple Inc.") == "apple"
    assert normalize_security_name("MICROSOFT CORP") == "microsoft"
    assert normalize_security_name("Berkshire Hathaway") == "berkshire hathaway"


# ---------------------------------------------------------------------------
# Fund analysis assembly + resolution
# ---------------------------------------------------------------------------

def test_build_fund_concentration_statuses(tmp_path):
    db = str(tmp_path / "e1f.db")
    _insert(db, VWCE, _sample_rows())
    snap = latest_lookthrough_snapshot(db, VWCE)
    fund = conc.build_fund_concentration(VWCE, "Vanguard All-World", snap)
    assert fund.has_lookthrough
    assert fund.security_status == conc.Status.BOUNDED
    assert fund.sector_status == conc.Status.CALCULATED
    assert fund.asset_class_status == conc.Status.CALCULATED
    assert fund.security_weights == [0.05, 0.04]  # rank-descending


def test_build_fund_concentration_without_snapshot():
    fund = conc.build_fund_concentration(VWCE, "Vanguard All-World", None)
    assert not fund.has_lookthrough
    assert fund.security_status == conc.Status.UNAVAILABLE
    assert fund.sector_status == conc.Status.UNAVAILABLE


def test_resolve_fund_by_isin_ticker_and_name():
    entries = [(VWCE, {"name": "Vanguard FTSE All-World", "tickers": ["VWCE"]})]
    assert conc.resolve_fund(VWCE, entries) == VWCE
    assert conc.resolve_fund("vwce", entries) == VWCE
    assert conc.resolve_fund("all-world", entries) == VWCE


def test_resolve_fund_unknown_and_ambiguous():
    entries = [
        (VWCE, {"name": "Vanguard World A", "tickers": ["VWCE"]}),
        (CSPX, {"name": "Vanguard World B", "tickers": ["CSPX"]}),
    ]
    with pytest.raises(ValueError, match="no fund matches"):
        conc.resolve_fund("nope", entries)
    with pytest.raises(ValueError, match="ambiguous"):
        conc.resolve_fund("world", entries)


# ---------------------------------------------------------------------------
# Overlap candidates
# ---------------------------------------------------------------------------

def _fund_with(isin, names):
    """A ``(fund_id, snapshot)`` pair — the shape ``overlap_candidates`` consumes."""
    rows = [_security(n, 0.05, i + 1) for i, n in enumerate(names)]
    snap = type(
        "S", (), {"by_dimension": lambda self, d: rows if d == DIMENSION_SECURITY else []}
    )()
    return (isin, snap)


def test_overlap_candidates_lists_shared_names_only():
    funds = [
        _fund_with(VWCE, ["Apple Inc.", "Microsoft Corp", "Alphabet Inc"]),
        _fund_with(CSPX, ["Apple Inc.", "Microsoft Corp", "Tesla Inc"]),
    ]
    candidates = conc.overlap_candidates(funds)
    names = [name for name, _ in candidates]
    assert ("Apple Inc.", 2) in candidates
    assert ("Microsoft Corp", 2) in candidates
    assert "Tesla Inc" not in names  # appears in only one fund


def test_overlap_candidates_skips_funds_without_snapshot():
    assert conc.overlap_candidates([(VWCE, None)]) == []


# ---------------------------------------------------------------------------
# Command end to end
# ---------------------------------------------------------------------------

def _seed(tmp_path, *, held=(), snapshots=None, config=None):
    db = str(tmp_path / "e1f.db")
    with closing(sqlite3.connect(db)) as conn:
        conn.execute(
            "CREATE TABLE transactions (broker TEXT, transaction_id TEXT, datetime TEXT, "
            "symbol TEXT, side TEXT, shares REAL, price REAL, fee REAL, tax REAL, "
            "PRIMARY KEY (broker, transaction_id))"
        )
        conn.executemany(
            "INSERT INTO transactions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [("tr", f"t{i}", "2024-01-01", isin, "BUY", 1.0, 10.0, 0.0, 0.0)
             for i, isin in enumerate(held)],
        )
        conn.commit()
    for isin, rows in (snapshots or {}).items():
        _insert(db, isin, rows)
    cfg = tmp_path / "config.yaml"
    cfg.write_text(yaml.dump({"etfs": config or {}}))
    return db, str(cfg)


def _full_rows():
    return [
        _security("Apple Inc.", 0.071, 1),
        _security("Microsoft Corp", 0.065, 2),
        _security("Nvidia Corp", 0.05, 3),
        HoldingRow(DIMENSION_SECTOR, "Technology", None, 0.40, None),
        HoldingRow(DIMENSION_SECTOR, "Financials", None, 0.35, None),
        HoldingRow(DIMENSION_SECTOR, "Healthcare", None, 0.25, None),
        HoldingRow(DIMENSION_ASSET_CLASS, "stockPosition", None, 0.996, None),
        HoldingRow(DIMENSION_ASSET_CLASS, "cashPosition", None, 0.004, None),
    ]


def test_main_no_holdings_message(tmp_path, capsys):
    db, cfg = _seed(tmp_path)
    assert conc.main(["--db", db, "--config", cfg]) == 0
    assert "No ETF holdings in database" in capsys.readouterr().out


def test_main_single_fund_full_block(tmp_path, capsys):
    db, cfg = _seed(
        tmp_path,
        held=[VWCE],
        snapshots={VWCE: _full_rows()},
        config={VWCE: {"name": "Vanguard All-World", "tickers": ["VWCE"]}},
    )
    assert conc.main(["--db", db, "--config", cfg, "VWCE"]) == 0
    out = capsys.readouterr().out
    assert VWCE in out
    assert "Coverage" in out and "% NAV" in out
    assert "Top 1" in out and "7.1%" in out
    assert "Top 25" in out and "—" in out and "(unknown: top-10 source)" in out
    assert "Security HHI" in out and "observed" in out and "bounded" in out
    assert "[BOUNDED]" in out
    assert "Sector HHI" in out and "[CALCULATED]" in out and "Technology" in out
    # Sector HHI's denominator is the fund, cued inline so it is never read as
    # an observed-top-10 figure.
    assert "[CALCULATED] whole-fund" in out
    # Position-type buckets, labelled as such (not "Asset class"), shown verbatim.
    assert "Position types" in out and "stockPosition 99.6%" in out
    assert "Asset class" not in out
    # Effective holdings is a bound, tagged and without the estimate "~".
    assert "Eff. holdings" in out and "[BOUNDED]" in out
    assert "~" not in out
    assert "Region" in out and "unavailable" in out and "[UNAVAILABLE]" in out
    # Coverage caveat banner sits above the per-fund blocks (decision 6 / point 8).
    assert "Data status" in out and "whole-fund weightings" in out
    assert "Notes" in out
    # Single-fund view has no cross-fund overlap section.
    assert "overlap candidates" not in out


def test_main_all_funds_with_overlap(tmp_path, capsys):
    shared = [_security("Apple Inc.", 0.07, 1), _security("Microsoft Corp", 0.06, 2)]
    db, cfg = _seed(
        tmp_path,
        held=[VWCE, CSPX],
        snapshots={VWCE: shared, CSPX: shared},
        config={
            VWCE: {"name": "Vanguard All-World", "tickers": ["VWCE"]},
            CSPX: {"name": "iShares Core S&P 500", "tickers": ["CSPX"]},
        },
    )
    assert conc.main(["--db", db, "--config", cfg]) == 0
    out = capsys.readouterr().out
    assert VWCE in out and CSPX in out
    assert "Potential overlap candidates" in out and "[UNRESOLVED]" in out
    assert "Apple Inc." in out and "2 funds" in out


def test_main_held_fund_without_lookthrough(tmp_path, capsys):
    db, cfg = _seed(
        tmp_path,
        held=[VWCE],
        config={VWCE: {"name": "Vanguard All-World", "tickers": ["VWCE"]}},
    )
    assert conc.main(["--db", db, "--config", cfg]) == 0
    out = capsys.readouterr().out
    assert "look-through unavailable" in out
    assert "[UNAVAILABLE]" in out  # region still reported


def test_main_explain_shows_provenance_chain(tmp_path, capsys):
    db, cfg = _seed(
        tmp_path,
        held=[VWCE],
        snapshots={VWCE: _full_rows()},
        config={VWCE: {"name": "Vanguard All-World", "tickers": ["VWCE"]}},
    )
    assert conc.main(["--db", db, "--config", cfg, "VWCE", "--explain"]) == 0
    out = capsys.readouterr().out
    assert "method = hhi_rank_capped_tail_v1" in out
    assert "Result:" in out and "Inputs:" in out and "Method:" in out
    assert "Limited by:" in out and "Not limited by:" in out
    assert "snapshot #" in out
    assert "swap collateral available but refused" in out  # negative knowledge


def test_main_explain_unavailable_fund(tmp_path, capsys):
    db, cfg = _seed(
        tmp_path,
        held=[VWCE],
        config={VWCE: {"name": "Vanguard All-World", "tickers": ["VWCE"]}},
    )
    assert conc.main(["--db", db, "--config", cfg, "--explain"]) == 0
    assert "look-through unavailable" in capsys.readouterr().out


def test_main_unknown_fund_errors(tmp_path, capsys):
    db, cfg = _seed(tmp_path, held=[VWCE], config={VWCE: {"name": "X", "tickers": []}})
    assert conc.main(["--db", db, "--config", cfg, "NOPE"]) == 1
    assert "✗ Error" in capsys.readouterr().out


def test_main_help_exits_zero(capsys):
    with pytest.raises(SystemExit) as excinfo:
        conc.main(["--help"])
    assert excinfo.value.code == 0
    assert "concentration" in capsys.readouterr().out


def test_no_named_holdings_is_unavailable_not_fully_observed(tmp_path, capsys):
    # Snapshot has sector/asset-class but no security rows (bot-walled/swap fund):
    # coverage is 0%, so the security HHI must read unavailable — never a 0.0000
    # "fully observed", which would be the opposite of the truth.
    only_dims = [
        HoldingRow(DIMENSION_SECTOR, "Technology", None, 1.0, None),
        HoldingRow(DIMENSION_ASSET_CLASS, "stockPosition", None, 1.0, None),
    ]
    db, cfg = _seed(
        tmp_path,
        held=[VWCE],
        snapshots={VWCE: only_dims},
        config={VWCE: {"name": "Blocked Fund", "tickers": ["X"]}},
    )
    assert conc.main(["--db", db, "--config", cfg, VWCE]) == 0
    out = capsys.readouterr().out
    assert "no named holdings from source" in out
    assert "fully observed" not in out
    assert "[UNAVAILABLE]" in out
    assert "Sector HHI" in out and "[CALCULATED]" in out  # other dimensions still shown


def test_suspect_weighting_downgraded_and_flagged(tmp_path, capsys):
    # A swap/synthetic fund whose yfinance asset-class weights go negative /
    # over 100%: shown verbatim (flag, never suppress) but NOT stamped CALCULATED.
    junk = [
        _security("Apple Inc.", 0.1, 1),
        HoldingRow(DIMENSION_SECTOR, "basic_materials", None, 0.0, None),  # sum 0%
        HoldingRow(DIMENSION_ASSET_CLASS, "cashPosition", None, 1.145, None),
        HoldingRow(DIMENSION_ASSET_CLASS, "stockPosition", None, -0.145, None),
    ]
    db, cfg = _seed(
        tmp_path,
        held=[VWCE],
        snapshots={VWCE: junk},
        config={VWCE: {"name": "Invesco S&P 500", "tickers": ["X"]}},
    )
    assert conc.main(["--db", db, "--config", cfg, VWCE]) == 0
    out = capsys.readouterr().out
    assert "suspect" in out
    assert "contains negative weights" in out
    assert "cashPosition 114.5%" in out          # raw source data still shown
    assert "[CALCULATED]" not in out             # no dimension earns CALCULATED here


def test_suspect_weighting_in_explain(tmp_path, capsys):
    junk = [
        _security("Apple Inc.", 0.1, 1),
        HoldingRow(DIMENSION_ASSET_CLASS, "cashPosition", None, 1.145, None),
        HoldingRow(DIMENSION_ASSET_CLASS, "stockPosition", None, -0.145, None),
    ]
    db, cfg = _seed(
        tmp_path,
        held=[VWCE],
        snapshots={VWCE: junk},
        config={VWCE: {"name": "Invesco S&P 500", "tickers": ["X"]}},
    )
    assert conc.main(["--db", db, "--config", cfg, VWCE, "--explain"]) == 0
    out = capsys.readouterr().out
    assert "Position-type split" in out and "suspect: contains negative weights" in out


def test_suspect_security_weights_withhold_hhi_and_show_source(tmp_path, capsys):
    junk = [_security("Apple Inc.", 0.6, 1), _security("Microsoft Corp", 0.5, 2)]
    db, cfg = _seed(
        tmp_path,
        held=[VWCE],
        snapshots={VWCE: junk},
        config={VWCE: {"name": "Broken source", "tickers": ["X"]}},
    )

    assert conc.main(["--db", db, "--config", cfg, VWCE]) == 0

    out = capsys.readouterr().out
    assert "observed weights sum to 110%" in out
    assert "Apple Inc. 60.0%" in out and "Microsoft Corp 50.0%" in out
    assert "Security HHI" in out and "[UNAVAILABLE]" in out
    assert "0.6100" not in out
    assert "fully observed" not in out


def test_fully_observed_reports_no_bound(tmp_path, capsys):
    # Observed weights sum to 100% -> no unobserved tail, so both bounds collapse.
    db, cfg = _seed(
        tmp_path,
        held=[VWCE],
        snapshots={VWCE: [_security("Apple Inc.", 0.6, 1), _security("Msft", 0.4, 2)]},
        config={VWCE: {"name": "Concentrated", "tickers": ["X"]}},
    )
    assert conc.main(["--db", db, "--config", cfg, VWCE]) == 0
    assert "fully observed" in capsys.readouterr().out
