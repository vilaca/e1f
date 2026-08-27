"""Experimental-only shared primitives for the e1f experimental tier (ADR-0024).

Holds the pieces used only by the experimental commands (``backtest``,
``concentration``, ``overlap``, ``lookthrough``, ``seasonality``): the look-through snapshot
model + ingest, the unresolved overlap-candidate signal, and the
contribution-timing backtest simulator. Stable commands never import from here
(enforced by the import-linter ``forbidden`` contract); this module may freely
consume shared ``e1f.common`` primitives (e.g. ``xirr``).
"""

import bisect
import random
import re
import sqlite3
from collections.abc import Iterable
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, date, datetime
from enum import StrEnum
from typing import Any

from e1f.common import xirr

# ---------------------------------------------------------------------------
# Look-through snapshots (ADR-0012): immutable, append-only observations of a
# fund's composition, split header (``holdings_snapshot``) / children
# (``holding``). Shared here so ``fetch`` can populate them and ``concentration``
# can read them without the two command modules importing each other (ADR-0003).
# ---------------------------------------------------------------------------

# The three look-through dimensions stored per snapshot. ``security`` rows are
# rank-ordered named holdings (top-10 from yfinance); ``sector`` / ``asset_class``
# rows are complete weightings and carry no rank.
DIMENSION_SECURITY = "security"
DIMENSION_SECTOR = "sector"
DIMENSION_ASSET_CLASS = "asset_class"

# Source tier priority: higher wins when several snapshots exist for one fund
# (ADR-0012 decision 5). ``provider`` is yfinance; the rest are v1b territory.
_TIER_RANK = {"inferred": 0, "provider": 1, "curated": 2, "issuer": 3}

_SECURITY_SUFFIXES = frozenset(
    {"inc", "corp", "co", "plc", "ltd", "ag", "sa", "nv", "se", "the", "class"}
)


@dataclass(frozen=True)
class HoldingRow:
    """One child row of a look-through snapshot (one dimension, one key)."""

    dimension: str
    raw_name: str
    normalized_name: str | None
    weight: float
    rank: int | None


@dataclass(frozen=True)
class LookthroughSnapshot:
    """One immutable observation of one fund's composition from one source."""

    id: int
    fund_id: str
    as_of: str
    source: str
    tier: str
    retrieved_at: str
    reported_holding_count: int | None
    holdings: list[HoldingRow]

    def by_dimension(self, dimension: str) -> list[HoldingRow]:
        return [h for h in self.holdings if h.dimension == dimension]

    @property
    def tier_rank(self) -> int:
        return _TIER_RANK.get(self.tier, _TIER_RANK["provider"])


def normalize_security_name(name: str) -> str:
    """Fold a holding name to a coarse match key — a *hint*, never identity.

    Lower-cases, drops punctuation and common corporate suffixes, and collapses
    whitespace so ``"Apple Inc."`` and ``"APPLE INC"`` co-occur in the unresolved
    overlap-candidate signal (ADR-0012 decision 2). It deliberately does not
    resolve share classes, dual listings, or ADRs — that is the reviewed
    ``security_alias`` work of v1b, not a string algorithm.
    """
    tokens = re.findall(r"[a-z0-9]+", (name or "").lower())
    kept = [t for t in tokens if t not in _SECURITY_SUFFIXES]
    return " ".join(kept or tokens)


def init_lookthrough_schema(conn: sqlite3.Connection) -> None:
    """Create the ADR-0012 look-through tables if absent (idempotent).

    ``holdings_snapshot`` is the immutable header (one observation of one fund
    from one source/tier); ``holding`` holds its children across all three
    dimensions; ``security_alias`` is the deliberately-empty v1a resolution table
    that v1b fills incrementally from the overlap-candidate report.
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS holdings_snapshot (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fund_id TEXT NOT NULL,
            as_of TEXT NOT NULL,
            source TEXT NOT NULL,
            tier TEXT NOT NULL,
            retrieved_at TEXT NOT NULL,
            reported_holding_count INTEGER
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS holding (
            snapshot_id INTEGER NOT NULL,
            dimension TEXT NOT NULL,
            raw_name TEXT NOT NULL,
            normalized_name TEXT,
            weight REAL NOT NULL,
            rank INTEGER,
            FOREIGN KEY (snapshot_id) REFERENCES holdings_snapshot(id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS security_alias (
            raw_name TEXT PRIMARY KEY,
            canonical_name TEXT,
            canonical_key TEXT,
            reviewed_at TEXT
        )
        """
    )
    conn.commit()


def _snapshot_signature(
    reported_holding_count: int | None, holdings: list[HoldingRow]
) -> tuple[Any, ...]:
    """Content fingerprint for identical-observation dedupe (as_of excluded).

    Two observations with the same composition are the *same* snapshot even if
    re-fetched on a later day, so the auto-refresh never becomes a fetch log
    (ADR-0012 decision 5). Weights are rounded to absorb float noise.
    """
    return (
        reported_holding_count,
        tuple(
            sorted(
                (h.dimension, h.raw_name, round(h.weight, 8), h.rank) for h in holdings
            )
        ),
    )


def _load_snapshot(conn: sqlite3.Connection, header: tuple[Any, ...]) -> LookthroughSnapshot:
    snapshot_id, fund_id, as_of, source, tier, retrieved_at, reported = header
    rows = conn.execute(
        "SELECT dimension, raw_name, normalized_name, weight, rank "
        "FROM holding WHERE snapshot_id = ? ORDER BY rank IS NULL, rank, raw_name",
        (snapshot_id,),
    ).fetchall()
    holdings = [
        HoldingRow(
            dimension=str(dim),
            raw_name=str(raw),
            normalized_name=None if norm is None else str(norm),
            weight=float(weight),
            rank=None if rank is None else int(rank),
        )
        for dim, raw, norm, weight, rank in rows
    ]
    return LookthroughSnapshot(
        id=int(snapshot_id),
        fund_id=str(fund_id),
        as_of=str(as_of),
        source=str(source),
        tier=str(tier),
        retrieved_at=str(retrieved_at),
        reported_holding_count=None if reported is None else int(reported),
        holdings=holdings,
    )


_SNAPSHOT_COLUMNS = "id, fund_id, as_of, source, tier, retrieved_at, reported_holding_count"


def _latest_for_source_tier(
    conn: sqlite3.Connection, fund_id: str, source: str, tier: str
) -> LookthroughSnapshot | None:
    header = conn.execute(
        f"SELECT {_SNAPSHOT_COLUMNS} FROM holdings_snapshot "
        "WHERE fund_id = ? AND source = ? AND tier = ? ORDER BY id DESC LIMIT 1",
        (fund_id, source, tier),
    ).fetchone()
    return None if header is None else _load_snapshot(conn, header)


def insert_lookthrough_snapshot(
    db_path: str,
    *,
    fund_id: str,
    as_of: str,
    source: str,
    tier: str,
    retrieved_at: str,
    reported_holding_count: int | None,
    holdings: list[HoldingRow],
) -> int | None:
    """Append one immutable snapshot, skipping an identical re-observation.

    Returns the new snapshot id, or ``None`` when the latest snapshot for the
    same ``(fund, source, tier)`` is content-identical (ADR-0012 decision 5:
    corrections append, identical re-observations do not). Never mutates an
    existing snapshot.
    """
    with closing(sqlite3.connect(db_path)) as conn:
        init_lookthrough_schema(conn)
        latest = _latest_for_source_tier(conn, fund_id, source, tier)
        if latest is not None and _snapshot_signature(
            latest.reported_holding_count, latest.holdings
        ) == _snapshot_signature(reported_holding_count, holdings):
            return None

        cursor = conn.execute(
            "INSERT INTO holdings_snapshot "
            "(fund_id, as_of, source, tier, retrieved_at, reported_holding_count) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (fund_id, as_of, source, tier, retrieved_at, reported_holding_count),
        )
        snapshot_id = int(cursor.lastrowid or 0)
        conn.executemany(
            "INSERT INTO holding "
            "(snapshot_id, dimension, raw_name, normalized_name, weight, rank) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [
                (snapshot_id, h.dimension, h.raw_name, h.normalized_name, h.weight, h.rank)
                for h in holdings
            ],
        )
        conn.commit()
        return snapshot_id


def latest_lookthrough_snapshot(db_path: str, fund_id: str) -> LookthroughSnapshot | None:
    """The analysis snapshot for a fund: highest tier, then latest as_of, then id.

    Prior snapshots are retained as evidence (immutable append-only); this picks
    the one analysis should read (ADR-0012 decision 5). ``None`` when the fund has
    no look-through observation yet.
    """
    with closing(sqlite3.connect(db_path)) as conn:
        if conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='holdings_snapshot'"
        ).fetchone() is None:
            return None
        headers = conn.execute(
            f"SELECT {_SNAPSHOT_COLUMNS} FROM holdings_snapshot WHERE fund_id = ?",
            (fund_id,),
        ).fetchall()
        if not headers:
            return None
        snapshots = [_load_snapshot(conn, header) for header in headers]

    return max(snapshots, key=lambda s: (s.tier_rank, s.as_of, s.id))


# ---------------------------------------------------------------------------
# Cross-fund overlap primitives (ADR-0013 decision 8), graduated down from
# ``concentration`` so both ``concentration`` (its unresolved signal) and
# ``overlap`` (its worklist + floor) consume one home. The Tier-1 co-occurrence
# scan is snapshot-only (no command-layer type dependency).
# ---------------------------------------------------------------------------


def overlap_candidates(
    funds: Iterable[tuple[str, LookthroughSnapshot | None]],
) -> list[tuple[str, int]]:
    """Raw security names co-occurring in ≥2 funds' top holdings — the *unresolved*
    signal (ADR-0012 Tier-1 seed / ADR-0013 decision 3).

    ``funds`` is ``(fund_id, snapshot)`` pairs. Grouped by normalized name (a
    hint), reported with a representative raw name and the fund count. Never
    summed into an exposure figure (ADR-0012 decision 2): its only job is to point
    at where v1b's reviewed canonical resolution would pay off.
    """
    by_norm: dict[str, tuple[str, set[str]]] = {}
    for fund_id, snapshot in funds:
        if snapshot is None:
            continue
        seen_here: set[str] = set()
        for row in snapshot.by_dimension(DIMENSION_SECURITY):
            norm = row.normalized_name or normalize_security_name(row.raw_name)
            if norm in seen_here:
                continue
            seen_here.add(norm)
            display, funds_seen = by_norm.get(norm, (row.raw_name, set()))
            funds_seen.add(fund_id)
            by_norm[norm] = (display, funds_seen)

    candidates = [
        (display, len(funds_seen))
        for display, funds_seen in by_norm.values()
        if len(funds_seen) >= 2
    ]
    candidates.sort(key=lambda c: (-c[1], c[0].lower()))
    return candidates


def load_security_aliases(db_path: str) -> dict[str, tuple[str, str]]:
    """``raw_name -> (canonical_key, canonical_name)`` from ``security_alias``.

    Only rows carrying a ``canonical_key`` (a resolved identity) are returned;
    ``canonical_name`` falls back to the ``raw_name`` when unset. Empty when the
    table is absent (``fetch`` never ran) or holds no resolutions yet.
    """
    with closing(sqlite3.connect(db_path)) as conn:
        if conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='security_alias'"
        ).fetchone() is None:
            return {}
        rows = conn.execute(
            "SELECT raw_name, canonical_key, canonical_name FROM security_alias "
            "WHERE canonical_key IS NOT NULL AND canonical_key != ''"
        ).fetchall()
    return {
        str(raw): (str(key), str(name) if name else str(raw))
        for raw, key, name in rows
    }


def upsert_security_alias(
    db_path: str,
    raw_name: str,
    canonical_key: str,
    *,
    canonical_name: str | None = None,
    reviewed_at: str | None = None,
) -> str:
    """Idempotent upsert of one reviewed identity into ``security_alias``.

    ``reviewed_at`` is stamped automatically (now, UTC) because running the write
    *is* the human review act (ADR-0012 decision 5 / ADR-0013 decision 3);
    re-resolving bumps it and updates the key. ``canonical_name`` defaults to the
    ``raw_name``. Returns the ``reviewed_at`` stamp actually written.
    """
    reviewed_at = reviewed_at or datetime.now(UTC).isoformat()
    canonical_name = canonical_name or raw_name
    with closing(sqlite3.connect(db_path)) as conn:
        init_lookthrough_schema(conn)
        conn.execute(
            "INSERT INTO security_alias (raw_name, canonical_name, canonical_key, reviewed_at) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(raw_name) DO UPDATE SET "
            "canonical_name = excluded.canonical_name, "
            "canonical_key = excluded.canonical_key, "
            "reviewed_at = excluded.reviewed_at",
            (raw_name, canonical_name, canonical_key, reviewed_at),
        )
        conn.commit()
    return reviewed_at


# ---------------------------------------------------------------------------
# Look-through provenance one-liner (ADR-0013/0014): rendered in the
# ``concentration`` --explain output for a snapshot's source/tier/as_of.
# ---------------------------------------------------------------------------


def _snapshot_provenance(snapshot: LookthroughSnapshot) -> str:
    return (
        f"snapshot #{snapshot.id}, source {snapshot.source}/{snapshot.tier}, "
        f"as_of {snapshot.as_of}, retrieved {snapshot.retrieved_at}"
    )


# ---------------------------------------------------------------------------
# Contribution-timing backtest core (ADR-0019): a pure simulator that runs one
# reserve-model contribution strategy over an EUR daily-close series. The
# ``backtest`` command owns EUR-series assembly, the CLI, and rendering; the
# arithmetic lives here so the invariance invariant (∑ contributions ==
# equity cost + leftover reserve cash) is unit-tested without IO.
# ---------------------------------------------------------------------------

BACKTEST_MIN_CONTRIBUTIONS = 12  # a run/window with fewer usable months is refused


class DeployMode(StrEnum):
    """How a reserve deploys (ADR-0020). ``SIGNAL`` is the ADR-0019 drawdown rule;
    the rest are drawdown-blind schedules that empty the reserve by the horizon."""

    SIGNAL = "signal"     # deploy on the drawdown signal — deployment_fraction()
    EVEN = "even"         # equal share of the remaining months
    DELAYED = "delayed"   # nothing for delay_months, then even over the rest
    RANDOM = "random"     # a seeded random share of the remaining reserve
    DAILY_DIP = "daily-dip"  # within-month daily slices, biased to down days (ADR-0021)
    DAILY_DIP_CARRY = "daily-dip-carry"  # daily-dip; a dip flushes all accrued slices (ADR-0023)


@dataclass(frozen=True)
class StrategyParams:
    """One contribution rule. ``lump_sum`` ignores the four dip knobs; the blind
    ``deploy`` modes (ADR-0020) ignore the drawdown signal (a/b/D0)."""

    label: str
    base_fraction: float    # β ∈ [0,1] — bought immediately each month
    aggressiveness: float   # a ≥ 0 — scales reserve deployment
    curvature: float        # b > 0 — nonlinearity of the drawdown response
    deadzone: float         # D0 ∈ [0,1) — drawdown below which nothing deploys
    lump_sum: bool = False  # invest the whole horizon budget at t0
    deploy: DeployMode = DeployMode.SIGNAL  # deployment schedule (ADR-0020)
    delay_months: int = 0   # DELAYED: months to wait before deploying
    seed: int | None = None  # RANDOM: RNG seed (fixed → reproducible)
    slices: int = 0         # DAILY_DIP: slices per month, C/N each (ADR-0021)


@dataclass(frozen=True)
class SignalSpec:
    """Drawdown reference: a trailing-``lookback``-day high, or ATH when None."""

    lookback: int | None    # trailing trading days for the high; None = all-time high


@dataclass(frozen=True)
class BacktestResult:
    """Outcome of one strategy over one window — terminal wealth split into its parts."""

    label: str
    contributions: int
    contribution: float
    total_invested: float       # ∑ committed = contributions × contribution
    equity_cost: float          # EUR spent buying shares
    reserve_cash: float         # leftover reserve at the horizon (incl. cash growth)
    shares: float
    final_price: float
    equity_value: float         # shares × final_price
    terminal_wealth: float      # equity_value + reserve_cash
    xirr: float | None
    max_drawdown: float         # peak-to-trough of total value, daily (fraction) (ADR-0022)
    reserve_contributed: float  # ∑ (1−β)·C routed into the reserve
    reserve_deployed: float     # ∑ pulled from the reserve into shares


def running_high(closes: list[float], i: int, lookback: int | None) -> float:
    """Highest close over the trailing window ending at index ``i`` (ATH if lookback None)."""
    lo = 0 if lookback is None else max(0, i - lookback + 1)
    return max(closes[lo : i + 1])


def deployment_fraction(drawdown: float, params: StrategyParams) -> float:
    """Fraction of the current reserve to deploy — clamp(a·(D−D0)^b, 0, 1)."""
    d_eff = drawdown - params.deadzone
    if d_eff <= 0.0:
        return 0.0
    return min(1.0, float(params.aggressiveness * d_eff ** params.curvature))


def blind_schedule(
    mode: DeployMode, n: int, delay_months: int, rng: random.Random | None
) -> list[float]:
    """Per-month fraction of the CURRENT reserve a drawdown-blind schedule deploys
    over ``n`` fills (ADR-0020). Every blind mode empties the reserve by the horizon
    (the last fill deploys the whole remainder), so it cannot leave cash unspent and
    its total does not silently depend on ``n``. Every fraction is in [0, 1].

    ``even``/``delayed`` are deterministic. ``random`` draws one weight per month and
    deploys each month in proportion to the weight remaining ahead of it — a genuine
    random *pace* (some seeds front-load, some back-load), not ~50%/month every time,
    so a seed sweep spans deploy-early…deploy-late rather than collapsing to one path.
    """
    if mode == DeployMode.RANDOM:
        assert rng is not None                       # simulate_strategy seeds it
        weights = [rng.random() for _ in range(n)]
        fracs = [0.0] * n
        suffix = 0.0
        for k in range(n - 1, -1, -1):               # f_k = w_k / Σ_{j≥k} w_j
            suffix += weights[k]
            fracs[k] = 1.0 if (k == n - 1 or suffix <= 0.0) else weights[k] / suffix
        return fracs
    fracs = []
    for k in range(n):
        if k >= n - 1:
            fracs.append(1.0)                        # horizon: deploy whatever remains
        elif mode == DeployMode.DELAYED and k < delay_months:
            fracs.append(0.0)                        # wait out the delay
        else:
            fracs.append(1.0 / (n - k))              # even: equal share of remaining months
    return fracs


def monthly_fill_indices(dates: list[str], start_idx: int, end_idx: int) -> list[int]:
    """Trading-day indices for a monthly contribution on the 1st, filled at the first
    close on-or-after each month anchor, within ``[start_idx, end_idx]``.

    One index per calendar month, strictly increasing (a month whose anchor maps
    to an already-used fill is skipped). Empty when the range is empty.
    """
    if not dates or start_idx > end_idx or start_idx < 0:
        return []
    start = date.fromisoformat(dates[start_idx])
    end = date.fromisoformat(dates[end_idx])
    # Anchor on the 1st of the start month: the first fill (first close on-or-after
    # that 1st, searched from start_idx) then lands on the effective start day, and
    # every later month contributes on its own 1st-or-after — one fill per month.
    fills: list[int] = []
    cur = date(start.year, start.month, 1)
    while cur <= end:
        j = bisect.bisect_left(dates, cur.isoformat(), start_idx, end_idx + 1)
        if j <= end_idx and (not fills or j > fills[-1]):
            fills.append(j)
        cur = date(cur.year + 1, 1, 1) if cur.month == 12 else date(cur.year, cur.month + 1, 1)
    return fills


def _max_drawdown(values: list[float]) -> float:
    """Largest peak-to-trough decline of a value series, as a fraction in [0, 1]."""
    peak = float("-inf")
    mdd = 0.0
    for v in values:
        peak = max(peak, v)
        if peak > 0.0:
            mdd = max(mdd, (peak - v) / peak)
    return mdd


def _grow(balance: float, from_day: str, to_day: str, annual_rate: float) -> float:
    """Grow a cash balance at ``annual_rate`` (Actual/365) between two dates."""
    if annual_rate == 0.0 or balance == 0.0:
        return balance
    days = (date.fromisoformat(to_day) - date.fromisoformat(from_day)).days
    return float(balance * (1.0 + annual_rate) ** (days / 365.0))


def _simulate_daily_dip(
    dates: list[str],
    closes: list[float],
    fills: list[int],
    params: StrategyParams,
    contribution: float,
) -> BacktestResult:
    """Within-month daily dip-slice strategy (ADR-0021).

    Each month commits ``contribution`` in full, sliced into ``params.slices``
    equal pieces. Walking the month's trading days, a slice is bought on a **down
    day** (close below the prior trading day's close) or when catch-up pressure
    (``trading_days_left ≤ slices_left``) demands it; the month's **last trading
    day deploys whatever budget remains**. So every month fully deploys ``C``
    inside the month — there is no cross-month reserve and ``--cash-rate`` does
    not apply. Invariance is therefore unconditional: ``equity_cost == N·C`` and
    ``reserve_cash == 0`` for any series and any ``N``.

    Months partition the series as ``[fills[k], fills[k+1]-1]`` (last month runs
    to the horizon), so the days are exactly the trading days from each month's
    first fill up to the day before the next month's.
    """
    n = len(fills)
    end_idx = len(dates) - 1
    n_slices = max(1, params.slices)

    shares = 0.0
    equity_cost = 0.0
    cash_flows: list[tuple[str, float]] = []
    value_curve: list[float] = []

    for k, month_start in enumerate(fills):
        month_end = fills[k + 1] - 1 if k + 1 < n else end_idx
        cash_flows.append((dates[month_start], -contribution))
        budget = contribution
        slice_amt = contribution / n_slices
        slices_left = n_slices
        month_len = month_end - month_start + 1
        for j in range(month_len):
            i = month_start + j
            price = closes[i]
            if j == month_len - 1:
                spend = budget                       # horizon of the month: deploy the rest
            else:
                is_dip = i > 0 and price < closes[i - 1]
                catch_up = (month_len - j) <= slices_left
                if (is_dip or catch_up) and slices_left > 0 and budget > 0.0:
                    spend = min(slice_amt, budget)
                    slices_left -= 1
                else:
                    spend = 0.0
            if spend > 0.0:
                shares += spend / price
                equity_cost += spend
                budget -= spend
            value_curve.append(shares * price)   # daily value point (ADR-0022)

    return _daily_dip_result(
        dates, closes, params, contribution, n, shares, equity_cost, cash_flows, value_curve,
    )


def _daily_dip_result(
    dates: list[str],
    closes: list[float],
    params: StrategyParams,
    contribution: float,
    n: int,
    shares: float,
    equity_cost: float,
    cash_flows: list[tuple[str, float]],
    value_curve: list[float],
) -> BacktestResult:
    """Assemble the ``BacktestResult`` shared by both daily-dip cores (ADR-0021/0023).

    Both deploy each month's ``C`` in full inside the month, so neither holds a
    cross-month reserve: ``reserve_cash`` and the reserve flows are identically zero.
    """
    end_idx = len(dates) - 1
    final_price = closes[end_idx]
    equity_value = shares * final_price
    # value_curve already ends at the horizon day (equity_value); no extra point.
    cash_flows.append((dates[end_idx], equity_value))
    return BacktestResult(
        label=params.label,
        contributions=n,
        contribution=contribution,
        total_invested=contribution * n,
        equity_cost=equity_cost,
        reserve_cash=0.0,
        shares=shares,
        final_price=final_price,
        equity_value=equity_value,
        terminal_wealth=equity_value,
        xirr=xirr(cash_flows),
        max_drawdown=_max_drawdown(value_curve),
        reserve_contributed=0.0,
        reserve_deployed=0.0,
    )


def _simulate_daily_dip_carry(
    dates: list[str],
    closes: list[float],
    fills: list[int],
    params: StrategyParams,
    contribution: float,
) -> BacktestResult:
    """Carry-forward daily dip-slice strategy (ADR-0023).

    A sibling of :func:`_simulate_daily_dip`. Each month commits ``contribution``
    in full, with one slice (``C/N``, ``N = params.slices``) *accruing* per trading
    day. On a **down day** (close below the prior trading day's close) it spends
    **every accrued-but-unspent slice** — the day's own slice plus each earlier
    day's that a buy has not yet consumed — rather than a single slice; on an **up
    day** nothing is bought and the pool grows. The month's **last trading day
    flushes whatever budget remains**, so ``C`` fully deploys inside the month and
    no cash carries across months. Invariance is therefore unconditional
    (``equity_cost == N·C``, ``reserve_cash == 0``) for any series and any ``N``,
    and ``--cash-rate`` does not apply — exactly as ADR-0021.

    ``released`` is the cumulative amount accrued so far (``min(C, (j+1)·C/N)``,
    capped once all ``N`` slices are out); ``released − spent`` is the
    accrued-unspent pool a down day deploys, always within ``[0, budget]``.

    Months partition the series as in :func:`_simulate_daily_dip`
    (``[fills[k], fills[k+1]-1]``, last month to the horizon).
    """
    n = len(fills)
    end_idx = len(dates) - 1
    n_slices = max(1, params.slices)

    shares = 0.0
    equity_cost = 0.0
    cash_flows: list[tuple[str, float]] = []
    value_curve: list[float] = []

    for k, month_start in enumerate(fills):
        month_end = fills[k + 1] - 1 if k + 1 < n else end_idx
        cash_flows.append((dates[month_start], -contribution))
        budget = contribution
        slice_amt = contribution / n_slices
        month_len = month_end - month_start + 1
        for j in range(month_len):
            i = month_start + j
            price = closes[i]
            if j == month_len - 1:
                spend = budget                       # month horizon: flush the rest
            else:
                released = min(contribution, (j + 1) * slice_amt)
                pool = released - (contribution - budget)   # accrued, not yet spent
                is_dip = i > 0 and price < closes[i - 1]
                spend = min(pool, budget) if is_dip and pool > 0.0 else 0.0
            if spend > 0.0:
                shares += spend / price
                equity_cost += spend
                budget -= spend
            value_curve.append(shares * price)   # daily value point (ADR-0022)

    return _daily_dip_result(
        dates, closes, params, contribution, n, shares, equity_cost, cash_flows, value_curve,
    )


def simulate_strategy(
    dates: list[str],
    closes: list[float],
    fills: list[int],
    params: StrategyParams,
    signal: SignalSpec,
    contribution: float,
    cash_rate: float = 0.0,
) -> BacktestResult:
    """Run one strategy over the EUR series and return its outcome.

    ``dates``/``closes`` are the parallel EUR daily series (``closes`` in EUR),
    already capped at the horizon; the last index is the horizon end. ``fills``
    are the monthly contribution indices (``monthly_fill_indices``). Every month
    commits ``contribution`` in full; the reserve model (ADR-0019 decision 2)
    keeps ∑ commitments equal across strategies regardless of deployment timing.

    ``DeployMode.DAILY_DIP`` / ``DAILY_DIP_CARRY`` dispatch to the within-month
    daily cores (ADR-0021/0023), which ignore ``signal`` and ``cash_rate`` (they
    hold no cross-month reserve).
    """
    if params.deploy == DeployMode.DAILY_DIP:
        return _simulate_daily_dip(dates, closes, fills, params, contribution)
    if params.deploy == DeployMode.DAILY_DIP_CARRY:
        return _simulate_daily_dip_carry(dates, closes, fills, params, contribution)

    n = len(fills)
    end_idx = len(dates) - 1
    final_price = closes[end_idx]
    end_day = dates[end_idx]

    shares = 0.0
    equity_cost = 0.0
    reserve_contributed = 0.0
    reserve_deployed = 0.0
    cash_flows: list[tuple[str, float]] = []
    value_curve: list[float] = []

    # The value curve is sampled DAILY, for every strategy, so the max-drawdown
    # column reflects the real intra-month trough (crash bottoms fall mid-month) and
    # is comparable across strategies (ADR-0022). Shares/reserve change only at the
    # monthly fills; the daily walk just revalues the holding at each close.
    if params.lump_sum:
        # The whole horizon budget invested at the first fill — a benchmark ceiling.
        i0 = fills[0]
        budget = contribution * n
        shares = budget / closes[i0]
        equity_cost = budget
        reserve = 0.0
        cash_flows.append((dates[i0], -budget))
        value_curve.extend(shares * closes[i] for i in range(i0, end_idx + 1))
    else:
        reserve = 0.0
        last_day: str | None = None
        # A drawdown-blind schedule is precomputed once (RANDOM needs all its weights);
        # SIGNAL consults the drawdown per month instead (ADR-0020).
        schedule = (
            None if params.deploy == DeployMode.SIGNAL
            else blind_schedule(
                params.deploy, n, params.delay_months,
                random.Random(params.seed) if params.deploy == DeployMode.RANDOM else None,
            )
        )
        fill_k = {idx: k for k, idx in enumerate(fills)}
        for i in range(fills[0], end_idx + 1):
            price = closes[i]
            day = dates[i]
            if last_day is not None:
                reserve = _grow(reserve, last_day, day, cash_rate)
            k = fill_k.get(i)
            if k is not None:                            # a monthly contribution lands today
                immediate = params.base_fraction * contribution
                to_reserve = contribution - immediate
                reserve += to_reserve
                reserve_contributed += to_reserve
                if schedule is None:
                    drawdown = max(0.0, 1.0 - price / running_high(closes, i, signal.lookback))
                    frac = deployment_fraction(drawdown, params)
                else:  # drawdown-blind schedule — never consults the signal
                    frac = schedule[k]
                deploy = frac * reserve
                reserve -= deploy
                reserve_deployed += deploy
                spend = immediate + deploy
                shares += spend / price
                equity_cost += spend
                cash_flows.append((day, -contribution))
            value_curve.append(shares * price + reserve)   # daily value point
            last_day = day

    equity_value = shares * final_price
    terminal_wealth = equity_value + reserve
    # value_curve already ends at the horizon day (== terminal_wealth); no extra point.
    cash_flows.append((end_day, terminal_wealth))
    return BacktestResult(
        label=params.label,
        contributions=n,
        contribution=contribution,
        total_invested=contribution * n,
        equity_cost=equity_cost,
        reserve_cash=reserve,
        shares=shares,
        final_price=final_price,
        equity_value=equity_value,
        terminal_wealth=terminal_wealth,
        xirr=xirr(cash_flows),
        max_drawdown=_max_drawdown(value_curve),
        reserve_contributed=reserve_contributed,
        reserve_deployed=reserve_deployed,
    )
