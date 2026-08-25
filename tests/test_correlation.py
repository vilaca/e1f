"""Correlation: pure pair/return math, universe & clustering, and the command (ADR-0015).

The numerical core (alignment → variance guard → pairwise ρ over its window →
distance → all-peer-valid subset → linkage → flag thresholds) is tested in
isolation, and the ``PairwiseOverlap`` invariants and the sparse/young/gap
fixtures are regression tests, mirroring the ADR's Consequences.
"""

import math
import sqlite3
from contextlib import closing
from datetime import date, timedelta

import pytest
import yaml

from e1f import correlation
from e1f.common import Status
from e1f.correlation import (
    MIN_OVERLAP,
    Cluster,
    FlaggedPair,
    PairwiseOverlap,
    all_peer_valid_subset,
    analyze,
    build_clusters,
    clustering_eligible,
    eur_return_series,
    pairwise_overlap,
    pearson_correlation,
)

A = "IE00FUND000A0"
B = "IE00FUND000B0"
C = "IE00FUND000C0"
D = "IE00FUND000D0"


_EPOCH = date(2024, 1, 1)


def _day(offset):
    return (_EPOCH + timedelta(days=offset)).isoformat()


def _series(returns, start_day=0):
    """``(YYYY-MM-DD, return)`` on consecutive days from ``start_day`` days after epoch."""
    return [(_day(start_day + i), r) for i, r in enumerate(returns)]


# ---------------------------------------------------------------------------
# pearson_correlation — pure NumPy, deterministic (decision 7).
# ---------------------------------------------------------------------------


def test_pearson_perfect_positive_negative_and_zero():
    assert pearson_correlation([1, 2, 3, 4], [2, 4, 6, 8]) == pytest.approx(1.0)
    assert pearson_correlation([1, 2, 3, 4], [8, 6, 4, 2]) == pytest.approx(-1.0)
    # Orthogonal by construction: a symmetric zig-zag against a linear ramp.
    assert pearson_correlation([1, -1, 1, -1], [1, 1, -1, -1]) == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# pairwise_overlap — the frozen 8-step pipeline, one test per branch, plus the
# len(returns_a) == len(returns_b) == n invariant on every path (decision 3).
# ---------------------------------------------------------------------------


def _assert_lengths(overlap: PairwiseOverlap) -> None:
    assert len(overlap.returns_a) == len(overlap.returns_b) == overlap.n


def test_insufficient_overlap_is_unavailable_with_empty_vectors():
    short = _series([0.01, 0.02, -0.01])
    overlap = pairwise_overlap(short, short, min_overlap=60)
    assert overlap.status is Status.UNAVAILABLE
    assert overlap.reason == "insufficient_overlap"
    assert overlap.rho is None
    assert overlap.start is None and overlap.end is None
    assert overlap.returns_a == [] and overlap.returns_b == []
    assert overlap.n == 0  # vectors discarded → n == len(returns) == 0 (invariant holds)
    _assert_lengths(overlap)


def test_exact_date_inner_join_counts_only_shared_days():
    # A spans Jan 1..70; B spans Jan 20..89 → overlap is Jan 20..70 = 51 < 60.
    a = _series([0.01 if i % 2 else -0.01 for i in range(70)], start_day=1)
    b = _series([0.02 if i % 3 else -0.01 for i in range(70)], start_day=20)
    overlap = pairwise_overlap(a, b, min_overlap=2)
    assert overlap.n == 51
    _assert_lengths(overlap)


def test_zero_variance_over_window_retains_vectors_even_with_global_variance():
    # A is constant over the shared window (a pinned/stale stretch) yet varies
    # globally elsewhere; the guard is on the aligned sample, so THIS pair is
    # zero_variance — and it must RETAIN the aligned vectors (the subtle trap).
    flat = _series([0.0] * 65)
    moving = _series([0.01 if i % 2 else -0.01 for i in range(65)])
    overlap = pairwise_overlap(flat, moving, min_overlap=60)
    assert overlap.status is Status.UNAVAILABLE
    assert overlap.reason == "zero_variance"
    assert overlap.rho is None
    assert overlap.n == 65
    assert overlap.returns_a and overlap.returns_b  # retained, NOT []
    _assert_lengths(overlap)
    assert overlap.start is not None and overlap.end is not None


def test_calculated_pair_sets_rho_window_and_reason_none():
    moving = _series([0.01 if i % 2 else -0.02 for i in range(65)])
    overlap = pairwise_overlap(moving, moving, min_overlap=60)
    assert overlap.status is Status.CALCULATED
    assert overlap.reason is None
    assert overlap.rho == pytest.approx(1.0)
    assert overlap.start is not None and overlap.end is not None
    assert overlap.n == 65
    _assert_lengths(overlap)


def test_non_finite_rho_is_numerical_error_before_range_test(monkeypatch):
    # A NaN ρ must be caught by the finite check FIRST: nan < -1 and nan > 1 are
    # both false, so the range test would let it slip through as CALCULATED.
    monkeypatch.setattr(correlation, "pearson_correlation", lambda a, b: float("nan"))
    moving = _series([0.01 if i % 2 else -0.02 for i in range(65)])
    overlap = pairwise_overlap(moving, moving, min_overlap=60)
    assert overlap.status is Status.UNAVAILABLE
    assert overlap.reason == "numerical_error"
    assert overlap.rho is None
    assert overlap.returns_a and overlap.returns_b  # numerical_error retains vectors too
    _assert_lengths(overlap)


def test_infinite_rho_is_numerical_error(monkeypatch):
    # Not just NaN: an extreme-magnitude input can overflow to ±inf. The finite
    # check must turn that into numerical_error, never a spurious coefficient.
    monkeypatch.setattr(correlation, "pearson_correlation", lambda a, b: float("inf"))
    moving = _series([0.01 if i % 2 else -0.02 for i in range(65)])
    overlap = pairwise_overlap(moving, moving, min_overlap=60)
    assert overlap.status is Status.UNAVAILABLE
    assert overlap.reason == "numerical_error"
    assert overlap.rho is None
    assert overlap.returns_a and overlap.returns_b  # retained
    _assert_lengths(overlap)


def test_float_noise_within_tolerance_is_clamped_to_one(monkeypatch):
    monkeypatch.setattr(correlation, "pearson_correlation", lambda a, b: 1.0 + 5e-13)
    moving = _series([0.01 if i % 2 else -0.02 for i in range(65)])
    overlap = pairwise_overlap(moving, moving, min_overlap=60)
    assert overlap.status is Status.CALCULATED
    assert overlap.rho == 1.0  # clamped exactly, not left as 1.0000000000005
    _assert_lengths(overlap)


def test_negative_one_clamp_within_tolerance(monkeypatch):
    monkeypatch.setattr(correlation, "pearson_correlation", lambda a, b: -1.0 - 5e-13)
    moving = _series([0.01 if i % 2 else -0.02 for i in range(65)])
    overlap = pairwise_overlap(moving, moving, min_overlap=60)
    assert overlap.status is Status.CALCULATED
    assert overlap.rho == -1.0
    _assert_lengths(overlap)


def test_out_of_range_rho_beyond_tolerance_is_numerical_error(monkeypatch):
    monkeypatch.setattr(correlation, "pearson_correlation", lambda a, b: 1.5)
    moving = _series([0.01 if i % 2 else -0.02 for i in range(65)])
    overlap = pairwise_overlap(moving, moving, min_overlap=60)
    assert overlap.status is Status.UNAVAILABLE
    assert overlap.reason == "numerical_error"
    assert overlap.rho is None
    _assert_lengths(overlap)


# ---------------------------------------------------------------------------
# eur_return_series — gap bridging, FX-missing bridging, no pinned currency.
# ---------------------------------------------------------------------------


def _price_db(tmp_path, prices, *, fx=None, meta=None):
    db = tmp_path / "e1f.db"
    with closing(sqlite3.connect(str(db))) as conn:
        conn.execute(
            "CREATE TABLE prices (isin TEXT, date TEXT, close REAL, PRIMARY KEY (isin, date))"
        )
        conn.execute(
            "CREATE TABLE fx_rates (base TEXT, quote TEXT, date TEXT, rate REAL, "
            "PRIMARY KEY (base, quote, date))"
        )
        conn.executemany("INSERT INTO prices VALUES (?, ?, ?)", prices)
        if fx:
            conn.executemany("INSERT INTO fx_rates VALUES (?, ?, ?, ?)", fx)
        conn.commit()
    meta_path = tmp_path / "meta.yaml"
    meta_path.write_text(yaml.dump(meta or {}))
    return str(db), str(meta_path)


def test_gap_bridging_one_return_dated_at_later_date():
    # EUR fund: Jan-1=100, Jan-2 MISSING, Jan-3=102 → exactly ONE return dated
    # Jan-3 = 2%. n counts returns, not calendar/trading dates; the gap is bridged.
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        db, meta = _price_db(
            tmp_path,
            [(A, "2024-01-01", 100.0), (A, "2024-01-03", 102.0)],
            meta={A: {"currency": "EUR"}},
        )
        series = eur_return_series(db, A, "2024-12-31", meta)
    assert len(series) == 1
    assert series[0][0] == "2024-01-03"
    assert series[0][1] == pytest.approx(0.02)


def test_day_before_fx_series_has_no_eur_close_and_is_skipped():
    # USD fund priced Jan-1/2/3, but FX only begins Jan-2 (nearest-prior FX carries
    # forward, so a day is skipped only when NO rate exists on or before it). Jan-1
    # has no EUR close and is dropped; the surviving return spans Jan-2→Jan-3.
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        db, meta = _price_db(
            tmp_path,
            [
                (A, "2024-01-01", 100.0),
                (A, "2024-01-02", 110.0),
                (A, "2024-01-03", 102.0),
            ],
            fx=[
                ("EUR", "USD", "2024-01-02", 1.0),
                ("EUR", "USD", "2024-01-03", 1.0),
            ],
            meta={A: {"currency": "USD"}},
        )
        series = eur_return_series(db, A, "2024-12-31", meta)
    assert [d for d, _ in series] == ["2024-01-03"]  # Jan-1 has no EUR close, dropped
    assert series[0][1] == pytest.approx(102.0 / 110.0 - 1.0)


def test_no_pinned_currency_yields_empty_series(tmp_path):
    db, meta = _price_db(
        tmp_path, [(A, "2024-01-01", 100.0), (A, "2024-01-02", 101.0)], meta={}
    )
    assert eur_return_series(db, A, "2024-12-31", meta) == []


def test_unsupported_currency_yields_empty_series(tmp_path):
    # GBX (pence) has no EUR FX rule — convert_to_eur refuses it permanently, so no
    # EUR close ever exists. The guard returns [] up front (not via the except).
    db, meta = _price_db(
        tmp_path,
        [(A, "2024-01-01", 100.0), (A, "2024-01-02", 101.0)],
        meta={A: {"currency": "GBX"}},
    )
    assert eur_return_series(db, A, "2024-12-31", meta) == []


def test_long_gap_yields_one_observation_spanning_it(tmp_path):
    # Only day 0 and day 40 priced → ONE return spanning the 40-day gap. n counts
    # observations, not temporal coverage, so --min-overlap is a count threshold and
    # never guarantees a minimum calendar span (a documented ADR-0015 consequence).
    d0 = date(2024, 1, 1)
    d40 = d0 + timedelta(days=40)
    db, meta = _price_db(
        tmp_path,
        [(A, d0.isoformat(), 100.0), (A, d40.isoformat(), 140.0)],
        meta={A: {"currency": "EUR"}},
    )
    series = eur_return_series(db, A, "2024-12-31", meta)
    assert len(series) == 1
    assert series[0] == (d40.isoformat(), pytest.approx(0.40))


# ---------------------------------------------------------------------------
# Clustering-eligible + all-peer-valid subset — the sparse-fund fixture.
# ---------------------------------------------------------------------------


def _calc(rho=0.95):
    return PairwiseOverlap(Status.CALCULATED, [0.0], [0.0], None, None, MIN_OVERLAP, rho, None)


def _uncalc():
    return PairwiseOverlap(Status.UNAVAILABLE, [], [], None, None, 0, None, "insufficient_overlap")


def test_sparse_fund_destroys_the_triangle_subset_is_singleton():
    # eligible = A,B,C,D; valid A-B, A-C, A-D, B-C; missing B-D, C-D.
    # A → all peers valid → included; B,C,D each miss an edge → excluded.
    pairs = {
        ("IE00FUND000A0", "IE00FUND000B0"): _calc(),
        ("IE00FUND000A0", "IE00FUND000C0"): _calc(),
        ("IE00FUND000A0", "IE00FUND000D0"): _calc(),
        ("IE00FUND000B0", "IE00FUND000C0"): _calc(),
        ("IE00FUND000B0", "IE00FUND000D0"): _uncalc(),
        ("IE00FUND000C0", "IE00FUND000D0"): _uncalc(),
    }
    eligible = clustering_eligible([A, B, C, D], pairs)
    assert eligible == [A, B, C, D]  # each has ≥1 CALCULATED pair
    subset = all_peer_valid_subset(eligible, pairs)
    assert subset == [A]  # one sparse peer collapses an otherwise-valid triangle
    assert build_clusters(subset, lambda a, b: 1.0, 0.8) == []  # size 1 → no cluster


def test_fund_with_no_calculated_pair_is_not_eligible():
    pairs = {
        ("IE00FUND000A0", "IE00FUND000B0"): _calc(),
        ("IE00FUND000A0", "IE00FUND000C0"): _uncalc(),
        ("IE00FUND000B0", "IE00FUND000C0"): _uncalc(),
    }
    # C connects to nobody → not eligible; it can't cause A/B's exclusion either.
    eligible = clustering_eligible([A, B, C], pairs)
    assert eligible == [A, B]
    assert all_peer_valid_subset(eligible, pairs) == [A, B]


# ---------------------------------------------------------------------------
# build_clusters — average linkage cut at the ρ height (decision 6).
# ---------------------------------------------------------------------------


def test_build_clusters_groups_correlated_and_omits_singletons():
    rho = {(A, B): 0.98, (A, C): 0.1, (B, C): 0.1}
    clusters = build_clusters([A, B, C], lambda a, b: rho[(a, b) if (a, b) in rho else (b, a)], 0.8)
    assert clusters == [[A, B]]  # A,B merge below the cut; C is a singleton, omitted


def test_build_clusters_low_correlation_yields_no_cluster():
    assert build_clusters([A, B], lambda a, b: 0.1, 0.8) == []


def test_cluster_rho_boundary_one_merges_only_zero_distance():
    # cluster_rho = 1 → cut height 0: only ρ = 1 (distance 0) pairs merge.
    assert build_clusters([A, B], lambda a, b: 1.0, 1.0) == [[A, B]]
    assert build_clusters([A, B], lambda a, b: 0.98, 1.0) == []  # distance 0.1 > 0


def test_cluster_rho_boundary_minus_one_merges_everything():
    # cluster_rho = -1 → cut height 1 (the max correlation distance): even a weakly
    # correlated pair merges. The full [-1, 1] range is frozen by the ADR, so pin it.
    assert build_clusters([A, B], lambda a, b: 0.1, -1.0) == [[A, B]]


def test_cluster_may_contain_pair_below_cluster_rho_average_linkage():
    # Average linkage cuts at a dendrogram HEIGHT, not a per-pair predicate. A-B and
    # B-C are tight, but A-C (ρ=0.70) is below cluster_rho=0.80. A-B merges first,
    # then C joins because the AVERAGE distance {A,B}→C clears the cut — so the
    # resulting cluster contains the sub-threshold A-C pair. This is the ADR-0015
    # decision-6 semantics, NOT a bug: hard per-pair guarantees are the flags' job.
    # Pinned so a future "fix" can't silently turn it into a clique/all-pairs rule.
    rho = {(A, B): 0.96, (A, C): 0.70, (B, C): 0.95}
    clusters = build_clusters([A, B, C], lambda x, y: rho[(x, y)], 0.80)
    assert clusters == [[A, B, C]]
    assert rho[(A, C)] < 0.80  # the retained cluster contains a pair below the cut ρ


def test_distance_is_zero_at_rho_one_and_clamps_negative_noise():
    assert correlation._distance(1.0) == 0.0
    assert correlation._distance(1.0 + 1e-9) == 0.0  # float noise clamped ≥0
    assert correlation._distance(-1.0) == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# Command end-to-end.
# ---------------------------------------------------------------------------


def _cumulative_prices(isin, returns, start_day=0):
    price = 100.0
    rows = [(isin, _day(start_day), price)]
    for i, r in enumerate(returns, start=1):
        price *= 1.0 + r
        rows.append((isin, _day(start_day + i), round(price, 6)))
    return rows


def _pattern(length, seed):
    base = [0.012, -0.008, 0.02, -0.015, 0.006, -0.011, 0.017]
    return [base[(i + seed) % len(base)] for i in range(length)]


def _seed_funds(tmp_path, funds, *, extra_prices=None):
    """Seed transactions + prices for EUR ``funds`` = {isin: [returns]} (day-1 anchored)."""
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
        meta = {}
        for index, (isin, returns) in enumerate(funds.items()):
            rows = _cumulative_prices(isin, returns)
            conn.execute(
                "INSERT INTO transactions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ("tr", f"t{index}", "2024-01-01", isin, "BUY", 100.0, 100.0, 0.0, 0.0),
            )
            conn.executemany("INSERT INTO prices VALUES (?, ?, ?)", rows)
            meta[isin] = {"currency": "EUR"}
        for isin, _returns in (extra_prices or {}).items():
            meta.setdefault(isin, {"currency": "EUR"})
        conn.commit()
    meta_path = tmp_path / "meta.yaml"
    meta_path.write_text(yaml.dump(meta))
    return str(db), str(meta_path)


def test_command_reports_flagged_pair_and_cluster(tmp_path, capsys):
    # Three EUR funds with identical returns → ρ=1 pairwise → all flagged, one
    # cluster of three, each carrying a third of the universe weight.
    returns = _pattern(70, 0)
    db, meta = _seed_funds(tmp_path, {A: returns, B: returns, C: returns})
    assert correlation.main(["--db", db, "--currency-meta", meta, "--as-of", "2024-12-31"]) == 0
    out = capsys.readouterr().out
    assert "Return co-movement — ADR-0015" in out
    assert "Correlation universe: 3 funds" in out
    assert "Redundant pairs" in out and "ρ 1.00" in out
    assert "Cluster 1" in out
    assert A in out and B in out and C in out


def test_report_shows_fund_names_beside_isins(tmp_path, capsys):
    returns = _pattern(70, 0)
    db, meta = _seed_funds(tmp_path, {A: returns, B: returns})
    config = tmp_path / "universe.yaml"
    config.write_text(
        yaml.dump({"etfs": {A: {"name": "Amundi Prime ACWI"}, B: {"name": "iShares Core S&P 500"}}})
    )
    assert correlation.main(
        ["--db", db, "--currency-meta", meta, "--config", str(config), "--as-of", "2024-12-31"]
    ) == 0
    out = capsys.readouterr().out
    # Names appear beside their ISINs in the cluster block (one member per line).
    assert "Amundi Prime ACWI" in out
    assert "iShares Core S&P 500" in out
    assert A in out and B in out


def test_command_explain_reconstructs_flagged_pairs(tmp_path, capsys):
    returns = _pattern(70, 0)
    db, meta = _seed_funds(tmp_path, {A: returns, B: returns})
    assert correlation.main(
        ["--db", db, "--currency-meta", meta, "--as-of", "2024-12-31", "--explain"]
    ) == 0
    out = capsys.readouterr().out
    assert "--explain" in out
    assert "Reconstructed redundant pairs" in out
    assert "digest" in out
    assert "returns [" in out  # aligned-vector preview
    assert f"{A} × {B}" in out


def test_taxonomy_excluded_clustering_unavailable_correlation_and_universe(tmp_path, capsys):
    # A spans days 1..130; B only 1..65 and C only 66..130 → A-B and A-C are
    # CALCULATED but B-C share no dates → subset={A}, B & C excluded from
    # clustering. Y (days 200..245, ~45 returns) overlaps nobody → unavailable for
    # correlation. U has no price → unvaluable. H has a single price → no history.
    long_returns = _pattern(129, 0)
    b_returns = _pattern(64, 2)
    c_returns = _pattern(64, 4)
    y_returns = _pattern(44, 1)

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

        def _rows(isin, returns, first_ordinal):
            from datetime import date, timedelta

            price = 100.0
            start = date(2024, 1, 1) + timedelta(days=first_ordinal)
            out = [(isin, start.isoformat(), price)]
            for i, r in enumerate(returns, start=1):
                price *= 1.0 + r
                out.append((isin, (start + timedelta(days=i)).isoformat(), round(price, 6)))
            return out

        U = "IE00FUND000U0"
        H = "IE00FUND000H0"
        Y = "IE00FUND000Y0"
        for isin in (A, B, C, Y, U, H):
            conn.execute(
                "INSERT INTO transactions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ("tr", f"t-{isin}", "2024-01-01", isin, "BUY", 100.0, 100.0, 0.0, 0.0),
            )
        conn.executemany("INSERT INTO prices VALUES (?, ?, ?)", _rows(A, long_returns, 0))
        conn.executemany("INSERT INTO prices VALUES (?, ?, ?)", _rows(B, b_returns, 0))
        conn.executemany("INSERT INTO prices VALUES (?, ?, ?)", _rows(C, c_returns, 65))
        conn.executemany("INSERT INTO prices VALUES (?, ?, ?)", _rows(Y, y_returns, 200))
        conn.execute("INSERT INTO prices VALUES (?, ?, ?)", (H, "2024-02-01", 100.0))
        # U: no price row at all → unvaluable.
        conn.commit()

    meta_path = tmp_path / "meta.yaml"
    meta_path.write_text(
        yaml.dump({isin: {"currency": "EUR"} for isin in (A, B, C, Y, U, H)})
    )

    assert correlation.main(
        ["--db", str(db), "--currency-meta", str(meta_path), "--as-of", "2024-12-31"]
    ) == 0
    out = capsys.readouterr().out
    assert "Excluded from clustering" in out
    assert "IE00FUND000B0" in out and "IE00FUND000C0" in out
    assert "Unavailable for correlation" in out
    assert "IE00FUND000Y0" in out
    assert "Excluded from the universe" in out
    assert "IE00FUND000U0" in out  # unvaluable
    assert "IE00FUND000H0" in out  # positive value, no usable return series


def test_named_pair_explain_reconstructs_one_calculated_pair(tmp_path, capsys):
    returns = _pattern(70, 0)
    db, meta = _seed_funds(tmp_path, {A: returns, B: returns, C: returns})
    # A named pair is reconstructed even when the default flag thresholds would
    # exclude it (here combined weight is only 2/3, but the pair is still shown).
    assert correlation.main(
        ["--db", db, "--currency-meta", meta, "--as-of", "2024-12-31", "--explain", A, B]
    ) == 0
    out = capsys.readouterr().out
    assert f"--explain {A} {B}" in out
    assert "Reconstructed pair" in out
    assert f"{A} × {B}" in out
    assert "CALCULATED" in out and "digest" in out


def test_named_pair_explain_reports_unavailable_reason(tmp_path, capsys):
    # A ~40-observation fund paired with a full one: n below the floor → the named
    # reconstruction still renders, as UNAVAILABLE with the reason (no point estimate).
    long_returns = _pattern(70, 0)
    short_returns = _pattern(40, 0)
    db, meta = _seed_funds(tmp_path, {A: long_returns, B: short_returns})
    assert correlation.main(
        ["--db", db, "--currency-meta", meta, "--as-of", "2024-12-31", "--explain", A, B]
    ) == 0
    out = capsys.readouterr().out
    assert "UNAVAILABLE (insufficient_overlap)" in out
    assert "no aligned sample retained" in out


def test_named_pair_explain_blocks_when_fund_outside_universe(tmp_path, capsys):
    # B has a single price row → valued but no usable return series → not in the
    # universe, so the pair cannot be reconstructed; the blocker is disclosed.
    returns = _pattern(70, 0)
    db, meta = _seed_funds(tmp_path, {A: returns}, extra_prices={B: []})  # B pinned EUR
    with closing(sqlite3.connect(db)) as conn:
        conn.execute(
            "INSERT INTO transactions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("tr", "tb", "2024-01-01", B, "BUY", 100.0, 100.0, 0.0, 0.0),
        )
        conn.execute("INSERT INTO prices VALUES (?, ?, ?)", (B, "2024-01-01", 100.0))
        conn.commit()
    assert correlation.main(
        ["--db", db, "--currency-meta", meta, "--as-of", "2024-12-31", "--explain", A, B]
    ) == 0
    out = capsys.readouterr().out
    assert "Cannot reconstruct this pair" in out
    assert "no usable return series" in out


def test_named_pair_explain_zero_variance_retains_vectors(tmp_path, capsys):
    # B has a constant (flat) return series → the aligned pair is zero_variance;
    # the named reconstruction still renders, with its window and retained vectors.
    moving = _pattern(70, 0)
    flat = [0.0] * 70
    db, meta = _seed_funds(tmp_path, {A: moving, B: flat})
    assert correlation.main(
        ["--db", db, "--currency-meta", meta, "--as-of", "2024-12-31", "--explain", A, B]
    ) == 0
    out = capsys.readouterr().out
    assert "UNAVAILABLE (zero_variance)" in out
    assert "digest" in out  # retained vectors → preview + digest (window path)


def test_named_pair_explain_blocks_unvaluable_and_not_held(tmp_path, capsys):
    returns = _pattern(70, 0)
    db, meta = _seed_funds(tmp_path, {A: returns})
    with closing(sqlite3.connect(db)) as conn:
        # B is held (a BUY) but has no price and no pinned currency → unvaluable.
        conn.execute(
            "INSERT INTO transactions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("tr", "tb", "2024-01-01", B, "BUY", 100.0, 100.0, 0.0, 0.0),
        )
        conn.commit()
    assert correlation.main(
        ["--db", db, "--currency-meta", meta, "--as-of", "2024-12-31", "--explain", A, B]
    ) == 0
    assert "no positive EUR value" in capsys.readouterr().out

    # A never-held ISIN is disclosed as "not a held fund".
    assert correlation.main(
        ["--db", db, "--currency-meta", meta, "--as-of", "2024-12-31",
         "--explain", A, "IE00NOTHELD00"]
    ) == 0
    assert "not a held fund" in capsys.readouterr().out


def test_named_pair_explain_rejects_identical_isins(tmp_path, capsys):
    returns = _pattern(70, 0)
    db, meta = _seed_funds(tmp_path, {A: returns, B: returns})
    assert correlation.main(
        ["--db", db, "--currency-meta", meta, "--as-of", "2024-12-31", "--explain", A, A]
    ) == 0
    assert "give two distinct ISINs" in capsys.readouterr().out


def test_named_pair_wrong_argument_count_fails_fast(tmp_path):
    returns = _pattern(70, 0)
    db, meta = _seed_funds(tmp_path, {A: returns, B: returns})
    with pytest.raises(SystemExit):
        correlation.main(["--db", db, "--currency-meta", meta, "--explain", A])


def test_command_no_holdings(tmp_path, capsys):
    db = tmp_path / "empty.db"
    with closing(sqlite3.connect(str(db))) as conn:
        conn.execute(
            "CREATE TABLE transactions (broker TEXT, transaction_id TEXT, datetime TEXT, "
            "symbol TEXT, side TEXT, shares REAL, price REAL, fee REAL, tax REAL, "
            "PRIMARY KEY (broker, transaction_id))"
        )
        conn.commit()
    assert correlation.main(["--db", str(db)]) == 0
    assert "No ETF holdings in database" in capsys.readouterr().out


def test_command_universe_empty_when_held_but_no_usable_series(tmp_path, capsys):
    # A single held fund with one price row: valued, but 0 returns → no usable
    # series → empty universe, disclosed as excluded (no history).
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
        conn.execute(
            "INSERT INTO transactions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("tr", "t1", "2024-01-01", A, "BUY", 100.0, 100.0, 0.0, 0.0),
        )
        conn.execute("INSERT INTO prices VALUES (?, ?, ?)", (A, "2024-01-01", 100.0))
        conn.commit()
    meta = tmp_path / "meta.yaml"
    meta.write_text(yaml.dump({A: {"currency": "EUR"}}))
    assert correlation.main(
        ["--db", str(db), "--currency-meta", str(meta), "--as-of", "2024-12-31"]
    ) == 0
    out = capsys.readouterr().out
    assert "nothing to correlate" in out
    assert "no usable return series yet" in out


def test_no_flagged_pairs_shows_none_and_explain_shows_nothing(tmp_path, capsys):
    # Three equal-weight funds → each pair's combined weight is 2/3; a weight-flag
    # of 1.0 is impossible to cross, so nothing is flagged.
    returns = _pattern(70, 0)
    db, meta = _seed_funds(tmp_path, {A: returns, B: returns, C: returns})
    assert correlation.main(
        ["--db", db, "--currency-meta", meta, "--as-of", "2024-12-31",
         "--weight-flag", "1.0"]
    ) == 0
    assert "(none)" in capsys.readouterr().out

    assert correlation.main(
        ["--db", db, "--currency-meta", meta, "--as-of", "2024-12-31", "--explain",
         "--weight-flag", "1.0"]
    ) == 0
    assert "No redundant pair to reconstruct" in capsys.readouterr().out


def test_flag_thresholds_are_inclusive_at_the_boundary(tmp_path, capsys):
    # Two identical EUR funds → ρ = 1.00 exactly, combined weight = 1.00 exactly.
    # The flag predicate is `ρ >= rho_flag AND combined >= weight_flag`, so setting
    # BOTH thresholds to their achieved boundary value must still flag the pair
    # (>= is inclusive, not >). A weight strictly above the boundary excludes it.
    returns = _pattern(70, 0)
    db, meta = _seed_funds(tmp_path, {A: returns, B: returns})
    assert correlation.main(
        ["--db", db, "--currency-meta", meta, "--as-of", "2024-12-31",
         "--rho-flag", "1.0", "--weight-flag", "1.0"]
    ) == 0
    out = capsys.readouterr().out
    assert "(none)" not in out.split("Clusters")[0]  # a pair WAS flagged at ρ==1, w==1
    assert "ρ 1.00" in out and "combined 100.0%" in out


# ---------------------------------------------------------------------------
# CLI validation (decision 7) — fail fast at argparse, and the as-of guard.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "args",
    [
        ["--rho-flag", "2"],
        ["--rho-flag", "-1.5"],
        ["--cluster-rho", "9"],
        ["--cluster-rho", "-2"],
        ["--weight-flag", "1.5"],
        ["--weight-flag", "-0.1"],
        ["--min-overlap", "1"],
        ["--rho-flag", "notanumber"],
        ["--min-overlap", "notanint"],
    ],
)
def test_out_of_range_cli_values_fail_fast(args):
    with pytest.raises(SystemExit):
        correlation.main([*args])


def test_bad_as_of_is_exit_1(tmp_path, capsys):
    returns = _pattern(70, 0)
    db, meta = _seed_funds(tmp_path, {A: returns, B: returns})
    assert correlation.main(["--db", db, "--currency-meta", meta, "--as-of", "nope"]) == 1
    assert "✗ Error" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# analyze() weight normalization — weights sum to 1 over the universe (decision 8).
# ---------------------------------------------------------------------------


def test_weights_normalize_over_the_universe(tmp_path):
    returns = _pattern(70, 0)
    db, meta = _seed_funds(tmp_path, {A: returns, B: returns, C: returns})
    report = analyze(
        db,
        as_of="2024-12-31",
        currency_meta_path=meta,
        config_path=str(tmp_path / "missing-config.yaml"),
        rho_flag=0.90,
        cluster_rho=0.80,
        weight_flag=0.20,
        min_overlap=60,
    )
    assert len(report.universe) == 3
    assert sum(fund.weight for fund in report.universe) == pytest.approx(1.0)
    assert math.isclose(report.universe[0].weight, 1 / 3, rel_tol=1e-6)


def test_flagged_pair_sort_key_is_rho_times_weight():
    overlap = PairwiseOverlap(Status.CALCULATED, [0.0], [0.0], None, None, 60, 0.9, None)
    pair = FlaggedPair(A, B, overlap, 0.5)
    assert pair.sort_key == pytest.approx(0.45)  # ordering only, not a published metric


def test_cluster_dataclass_carries_members_and_weight():
    cluster = Cluster(members=[A, B], weight=0.4)
    assert cluster.members == [A, B] and cluster.weight == pytest.approx(0.4)


def test_preview_short_vectors_shown_whole_long_vectors_elided():
    assert correlation._preview([0.1, 0.2, 0.3]) == "[0.1000, 0.2000, 0.3000]"
    long = correlation._preview([0.01 * i for i in range(10)])
    assert "…" in long  # head, …, tail form beyond six observations


def test_cli_type_parsers_accept_in_range_values():
    assert correlation._int_at_least(2)("60") == 60
    assert correlation._bounded_float(-1.0, 1.0)("0.5") == pytest.approx(0.5)
