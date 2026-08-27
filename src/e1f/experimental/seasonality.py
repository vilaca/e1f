#!/usr/bin/env python
"""e1f seasonality — calendar-month analysis (ADR-0026 / ADR-0027 / ADR-0028).

``--isin`` describes all twelve months of one ETF and tests whether
month-to-month differences exceed a calendar-free shuffle. ``--portfolio``
asks whether a common month pattern appears across the configured universe
(inferential cohort only) via a consensus table, a cross-sectional
permutation, and a balanced equal-weight book. ``--evaluate`` scores the
frozen August/November contribution rules against DCA. Evaluator only:
the weakest in-sample month is never promoted into a trading rule.

Usage:
    e1f seasonality --isin IE00B3YLTY66
    e1f seasonality --isin IE00B3YLTY66 --explain
    e1f seasonality --portfolio
    e1f seasonality --isin IE00B3YLTY66 --evaluate
    e1f seasonality --isin IE00B3YLTY66 --rule avoid-month --month 9
    e1f seasonality --isin IE00B3YLTY66 --rule historical-weakest \\
        --training-from 2011-01-01 --training-to 2019-01-01 \\
        --test-from 2019-01-01 --test-to 2026-01-01
"""

from __future__ import annotations

import argparse
import bisect
import random
import sqlite3
import statistics
import sys
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, date, datetime
from enum import StrEnum

from e1f.common import (
    BASE_CURRENCY,
    DEFAULT_CONFIG,
    DEFAULT_CURRENCY_META,
    DEFAULT_DB,
    ConfigManager,
    MetricContract,
    Status,
    _explain_metric,
    load_price_series,
    pinned_quote_currency,
    xirr,
)
from e1f.experimental.common import monthly_fill_indices

_TODAY = datetime.now(UTC).date().isoformat()

SEASONALITY_MIN_N_INFER = 8
SEASONALITY_MIN_N_OOS = 3
SEASONALITY_UNUSUAL_P = 0.05
DEFAULT_PERMUTATIONS = 10_000
DEFAULT_SEED = 0
DEFAULT_CONTRIBUTION = 1000.0
FROZEN_WEAK_MONTH = 8
FROZEN_STRONG_MONTH = 11
FROZEN_SHIFT_MONTHS: tuple[int, ...] = (10, 11)

MONTH_NAMES: tuple[str, ...] = (
    "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
)

INTERPRETATION_FOOTER: tuple[str, ...] = (
    "Descriptive only: monthly rankings describe this sample; they do not "
    "establish a tradable effect.",
    "Inference: statistical inference is unavailable below the minimum "
    "per-month sample floor.",
    "Selection: no month is automatically selected for trading.",
    "Actionability: a seasonal rule requires an explicitly pre-specified "
    "rule and, where applicable, a non-overlapping test period.",
)

SEASONALITY_CONTRACT = MetricContract(
    method_version="calendar_seasonality_v1",
    requires=(
        "a daily EUR close series with complete calendar months",
        "Accumulating distribution when price-mode is total-return",
    ),
    does_not_require=(
        "a September (or any month) prior",
        "look-through holdings",
        "a dividend ledger or a separate total-return index",
        "a dip / drawdown signal",
    ),
    supports=(
        "twelve-month descriptive statistics",
        "permutation omnibus + extreme-month placebo",
        "pre-specified seasonal rules vs constant-DCA",
        "frozen-month out-of-sample evaluation",
        "portfolio consensus + cross-sectional permutation (ADR-0027)",
        "equal-weight balanced-panel book (ADR-0027)",
        "frozen August/November contribution evaluation (ADR-0028)",
    ),
    limitations=(
        "N per month is years of history — mid-teens is typical and thin",
        "total-return is the accumulating NAV, not a reconstructed TR index",
        "evaluator only — no in-sample search, no auto-promoted rule",
        "month-end returns and contribution-fill timing are different samples",
        "no fees/taxes/spread; fractional shares",
        "conditional (month × regime) seasonality is out of scope",
        "cross-section treats correlated funds as separate votes",
        "ADR-0028 freeze used the full-sample book; holdouts are stability checks",
    ),
)


class SeasonalityError(Exception):
    """A usage/data problem that stops a run with a message (never a stack trace)."""


class Rule(StrEnum):
    AVOID_MONTH = "avoid-month"
    SIT_OUT_MONTH = "sit-out-month"
    HISTORICAL_WEAKEST = "historical-weakest"
    HISTORICAL_WEAKEST_SIT_OUT = "historical-weakest-sit-out"


class DeployKind(StrEnum):
    DCA = "dca"
    AVOID = "avoid"
    AVOID_DRAG = "avoid-drag"
    SIT_OUT = "sit-out"
    SHIFT = "shift"


# ---------------------------------------------------------------------------
# Formatting — copied locally (importing a sibling's formatter would break
# the experimental-command isolation contract).
# ---------------------------------------------------------------------------


def _fmt_signed_pct(value: float | None, digits: int = 2) -> str:
    return "n/a" if value is None else f"{value * 100.0:+.{digits}f}%"


def _fmt_pct(value: float | None, digits: int = 1) -> str:
    return "n/a" if value is None else f"{value * 100.0:.{digits}f}%"


def _fmt_pp(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100.0:+.2f}pp"


def _fmt_money(value: float | None) -> str:
    return "n/a" if value is None else f"{value:,.2f}"


def _fmt_signed_money(value: float | None) -> str:
    return "n/a" if value is None else f"{value:+,.2f}"


def _month_name(month: int) -> str:
    return MONTH_NAMES[month - 1]


# ---------------------------------------------------------------------------
# EUR daily-close series (native close × nearest-prior EUR/FX). Duplicated
# from the backtest command — experimental commands must not import each other.
# ---------------------------------------------------------------------------


def _fx_series(db_path: str, quote: str) -> tuple[list[str], list[float]]:
    with closing(sqlite3.connect(db_path)) as conn:
        if conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='fx_rates'"
        ).fetchone() is None:
            return [], []
        rows = conn.execute(
            "SELECT date, rate FROM fx_rates WHERE base = ? AND quote = ? ORDER BY date",
            (BASE_CURRENCY, quote),
        ).fetchall()
    return [str(d)[:10] for d, _ in rows], [float(r) for _, r in rows]


def eur_series(
    db_path: str, isin: str, as_of: str, currency_meta_path: str
) -> tuple[list[str], list[float], str]:
    """``(dates, eur_closes, currency)`` for an ISIN up to ``as_of``."""
    currency = pinned_quote_currency(isin, currency_meta_path)
    dates, closes = load_price_series(db_path, isin, as_of)
    if currency is None or currency == BASE_CURRENCY:
        return dates, closes, currency or BASE_CURRENCY

    fx_dates, fx_rates = _fx_series(db_path, currency)
    eur_dates: list[str] = []
    eur_closes: list[float] = []
    for day, close in zip(dates, closes, strict=True):
        k = bisect.bisect_right(fx_dates, day) - 1
        if k < 0:
            continue
        eur_dates.append(day)
        eur_closes.append(close / fx_rates[k])
    return eur_dates, eur_closes, currency


def price_catalog(db_path: str) -> list[tuple[str, int, str, str]]:
    """``(isin, count, first, last)`` for every ISIN with a stored price series."""
    with closing(sqlite3.connect(db_path)) as conn:
        if conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='prices'"
        ).fetchone() is None:
            return []
        rows = conn.execute(
            "SELECT isin, COUNT(*), MIN(date), MAX(date) FROM prices GROUP BY isin ORDER BY isin"
        ).fetchall()
    return [(r[0], int(r[1]), str(r[2])[:10], str(r[3])[:10]) for r in rows]


def _candidate_listing(db_path: str, config: ConfigManager) -> str:
    catalog = price_catalog(db_path)
    if not catalog:
        return "  (no price series stored — run 'e1f fetch' first)"
    lines = []
    for isin, count, first, last in catalog:
        cfg = config.get(isin) or {}
        dist = (cfg.get("distribution") or "?")[:3].lower()
        name = cfg.get("name") or "?"
        lines.append(f"  {isin}  {first}→{last}  {count:>5}d  {dist:3}  {name}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Month-end returns.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MonthReturn:
    year: int
    month: int
    end_date: str
    prev_end_date: str
    ret: float


@dataclass(frozen=True)
class PartialMonth:
    year: int
    month: int
    reason: str


def _prev_month(year: int, month: int) -> tuple[int, int]:
    return (year - 1, 12) if month == 1 else (year, month - 1)


def _next_month_start(year: int, month: int) -> date:
    return date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)


def month_end_map(
    dates: list[str], closes: list[float]
) -> dict[tuple[int, int], tuple[str, float]]:
    """Last ``(date, close)`` in each calendar month that has at least one close."""
    by_ym: dict[tuple[int, int], tuple[str, float]] = {}
    for day, close in zip(dates, closes, strict=True):
        y, m = int(day[:4]), int(day[5:7])
        by_ym[(y, m)] = (day, close)
    return by_ym


def complete_month_returns(
    dates: list[str],
    closes: list[float],
    window_from: str | None,
    window_to: str,
) -> tuple[list[MonthReturn], list[PartialMonth]]:
    """Month-end-to-month-end returns for complete calendar months.

    A month is complete iff the window's ``--to`` is on or after the first day
    of the next month, at least one close exists in the month, and the previous
    calendar month also has a close. ``--from`` excludes a month that started
    before the window. Partial months are returned for the footnote and never
    enter the primary sample.
    """
    ends = month_end_map(dates, closes)
    to_d = date.fromisoformat(window_to)
    from_d = date.fromisoformat(window_from) if window_from else None
    returns: list[MonthReturn] = []
    partials: list[PartialMonth] = []
    for (year, month), (end_day, end_close) in sorted(ends.items()):
        first = date(year, month, 1)
        if from_d is not None and from_d > first:
            partials.append(PartialMonth(year, month, "starts before --from"))
            continue
        if to_d < _next_month_start(year, month):
            partials.append(PartialMonth(year, month, "window has not elapsed"))
            continue
        py, pm = _prev_month(year, month)
        prev = ends.get((py, pm))
        if prev is None:
            partials.append(PartialMonth(year, month, "no prior month-end"))
            continue
        prev_day, prev_close = prev
        if prev_close == 0.0:
            partials.append(PartialMonth(year, month, "prior month-end close is 0"))
            continue
        returns.append(
            MonthReturn(
                year=year,
                month=month,
                end_date=end_day,
                prev_end_date=prev_day,
                ret=end_close / prev_close - 1.0,
            )
        )
    return returns, partials


# ---------------------------------------------------------------------------
# Descriptive statistics.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MonthStats:
    month: int
    n: int
    mean: float | None
    median: float | None
    stdev: float | None
    min_ret: float | None
    min_year: int | None
    max_ret: float | None
    max_year: int | None
    pct_positive: float | None
    pct_negative: float | None
    mean_excess: float | None
    median_excess: float | None


def _by_month(returns: list[MonthReturn]) -> dict[int, list[MonthReturn]]:
    groups: dict[int, list[MonthReturn]] = {m: [] for m in range(1, 13)}
    for row in returns:
        groups[row.month].append(row)
    return groups


def month_stats(returns: list[MonthReturn]) -> list[MonthStats]:
    groups = _by_month(returns)
    out: list[MonthStats] = []
    for month in range(1, 13):
        rows = groups[month]
        n = len(rows)
        if n == 0:
            out.append(
                MonthStats(
                    month, 0, None, None, None, None, None, None, None,
                    None, None, None, None,
                )
            )
            continue
        rets = [row.ret for row in rows]
        others = [
            row.ret
            for other_month, other_rows in groups.items()
            if other_month != month
            for row in other_rows
        ]
        mean = statistics.mean(rets)
        median = statistics.median(rets)
        stdev = statistics.stdev(rets) if n >= 2 else None
        lo = min(range(n), key=lambda i: (rets[i], rows[i].year))
        hi = max(range(n), key=lambda i: (rets[i], rows[i].year))
        pos = sum(1 for r in rets if r > 0.0)
        neg = sum(1 for r in rets if r < 0.0)
        mean_ex = mean - statistics.mean(others) if others else None
        med_ex = median - statistics.median(others) if others else None
        out.append(
            MonthStats(
                month=month,
                n=n,
                mean=mean,
                median=median,
                stdev=stdev,
                min_ret=rets[lo],
                min_year=rows[lo].year,
                max_ret=rets[hi],
                max_year=rows[hi].year,
                pct_positive=pos / n,
                pct_negative=neg / n,
                mean_excess=mean_ex,
                median_excess=med_ex,
            )
        )
    return out


def _rank_month(
    stats: list[MonthStats],
    key: Callable[[MonthStats], float | None],
    *,
    reverse: bool,
) -> MonthStats | None:
    """Earliest calendar month among those with data that extremise ``key``."""
    scored: list[tuple[float, MonthStats]] = []
    for row in stats:
        value = key(row)
        if row.n > 0 and value is not None:
            scored.append((value, row))
    if not scored:
        return None
    target = max(v for v, _ in scored) if reverse else min(v for v, _ in scored)
    tied = [row for value, row in scored if value == target]
    return min(tied, key=lambda row: row.month)


def weakest_mean_month(stats: list[MonthStats]) -> int | None:
    """Lowest mean month; ties → earliest calendar month. Training-only pick."""
    row = _rank_month(stats, lambda s: s.mean, reverse=False)
    return None if row is None else row.month


def strongest_mean_month(stats: list[MonthStats]) -> int | None:
    """Highest mean month; ties → earliest calendar month."""
    row = _rank_month(stats, lambda s: s.mean, reverse=True)
    return None if row is None else row.month


def complete_year_count(returns: list[MonthReturn]) -> int:
    years: dict[int, set[int]] = {}
    for row in returns:
        years.setdefault(row.year, set()).add(row.month)
    return sum(1 for months in years.values() if months == set(range(1, 13)))


def inferential_floor_met(stats: list[MonthStats], floor: int) -> bool:
    return all(s.n >= floor for s in stats)


# ---------------------------------------------------------------------------
# Kruskal-Wallis + permutation + BH.
# ---------------------------------------------------------------------------


def _average_ranks(values: list[float]) -> list[float]:
    n = len(values)
    order = sorted(range(n), key=lambda i: values[i])
    ranks = [0.0] * n
    i = 0
    while i < n:
        j = i
        while j + 1 < n and values[order[j + 1]] == values[order[i]]:
            j += 1
        avg = (i + j + 2) / 2.0
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    return ranks


def kruskal_wallis(groups: list[list[float]]) -> float:
    """Kruskal-Wallis *H* with the standard tie correction. Empty groups skipped."""
    nonempty = [g for g in groups if g]
    values = [v for g in nonempty for v in g]
    n = len(values)
    if n < 2 or len(nonempty) < 2:
        return 0.0
    if all(v == values[0] for v in values):
        return 0.0
    ranks = _average_ranks(values)
    h = 0.0
    offset = 0
    for group in nonempty:
        size = len(group)
        rank_sum = sum(ranks[offset : offset + size])
        offset += size
        h += rank_sum * rank_sum / size
    h = 12.0 / (n * (n + 1)) * h - 3.0 * (n + 1)
    # Tie correction: divide by 1 - sum(t^3 - t)/(N^3 - N).
    tie_sum = 0
    i = 0
    ordered = sorted(values)
    while i < n:
        j = i
        while j + 1 < n and ordered[j + 1] == ordered[i]:
            j += 1
        t = j - i + 1
        if t > 1:
            tie_sum += t * t * t - t
        i = j + 1
    denom = n * n * n - n
    if tie_sum and denom:
        h /= 1.0 - tie_sum / denom
    return float(h)


def benjamini_hochberg(p_values: list[float]) -> list[float]:
    """BH step-up adjusted p-values, clamped to ``[0, 1]``."""
    m = len(p_values)
    if m == 0:
        return []
    order = sorted(range(m), key=lambda i: p_values[i])
    adj = [0.0] * m
    running = 1.0
    for rank in range(m, 0, -1):
        idx = order[rank - 1]
        running = min(running, p_values[idx] * m / rank)
        adj[idx] = min(1.0, running)
    return adj


def _empirical_p(count_extreme: int, permutations: int) -> float:
    return (1.0 + count_extreme) / (1.0 + permutations)


def _group_values(labels: list[int], values: list[float]) -> list[list[float]]:
    groups: list[list[float]] = [[] for _ in range(12)]
    for month, value in zip(labels, values, strict=True):
        groups[month - 1].append(value)
    return groups


def _month_means(groups: list[list[float]]) -> list[float | None]:
    return [statistics.mean(g) if g else None for g in groups]


def _mean_excesses(groups: list[list[float]]) -> list[float | None]:
    out: list[float | None] = []
    for i, group in enumerate(groups):
        if not group:
            out.append(None)
            continue
        others = [v for j, other in enumerate(groups) if j != i for v in other]
        if not others:
            out.append(None)
            continue
        out.append(statistics.mean(group) - statistics.mean(others))
    return out


@dataclass(frozen=True)
class PermutationResult:
    h_obs: float
    p_omnibus: float
    p_worst: float
    p_best: float
    month_excess_p_raw: tuple[float | None, ...]
    month_excess_p_adj: tuple[float | None, ...]
    permutations: int
    seed: int
    min_mean_obs: float
    max_mean_obs: float


def permutation_test(
    returns: list[MonthReturn],
    permutations: int,
    seed: int,
) -> PermutationResult:
    """Shuffle month labels, preserving per-month *N*. Same shuffles feed H, extrema, excesses."""
    if permutations < 1:
        raise ValueError("permutations must be ≥ 1")
    values = [row.ret for row in returns]
    labels = [row.month for row in returns]
    groups = _group_values(labels, values)
    h_obs = kruskal_wallis(groups)
    means = _month_means(groups)
    present = [m for m in means if m is not None]
    min_mean_obs = min(present)
    max_mean_obs = max(present)
    excess_obs = _mean_excesses(groups)

    rng = random.Random(seed)
    n_h = 0
    n_min = 0
    n_max = 0
    n_ex = [0] * 12
    work = labels[:]
    for _ in range(permutations):
        rng.shuffle(work)
        shuffled = _group_values(work, values)
        if kruskal_wallis(shuffled) >= h_obs:
            n_h += 1
        sh_means = [m for m in _month_means(shuffled) if m is not None]
        if min(sh_means) <= min_mean_obs:
            n_min += 1
        if max(sh_means) >= max_mean_obs:
            n_max += 1
        sh_ex = _mean_excesses(shuffled)
        for i in range(12):
            obs, sh = excess_obs[i], sh_ex[i]
            if obs is None or sh is None:
                continue
            if abs(sh) >= abs(obs):
                n_ex[i] += 1

    raw: list[float | None] = []
    for i in range(12):
        if excess_obs[i] is None:
            raw.append(None)
        else:
            raw.append(_empirical_p(n_ex[i], permutations))
    defined = [p for p in raw if p is not None]
    adjusted_defined = benjamini_hochberg(defined)
    adj: list[float | None] = []
    k = 0
    for p in raw:
        if p is None:
            adj.append(None)
        else:
            adj.append(adjusted_defined[k])
            k += 1

    return PermutationResult(
        h_obs=h_obs,
        p_omnibus=_empirical_p(n_h, permutations),
        p_worst=_empirical_p(n_min, permutations),
        p_best=_empirical_p(n_max, permutations),
        month_excess_p_raw=tuple(raw),
        month_excess_p_adj=tuple(adj),
        permutations=permutations,
        seed=seed,
        min_mean_obs=min_mean_obs,
        max_mean_obs=max_mean_obs,
    )


# ---------------------------------------------------------------------------
# Portfolio consensus + cross-sectional permutation (ADR-0027).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FundSeasonality:
    isin: str
    name: str
    distribution: str
    currency: str
    n_months: int
    complete_years: int
    stats: list[MonthStats]
    infer_ok: bool
    skip_reason: str | None
    strongest: int | None
    weakest: int | None
    years: tuple[int, ...]
    labels: tuple[int, ...]
    values: tuple[float, ...]


@dataclass(frozen=True)
class ConsensusRow:
    month: int
    n_funds: int
    median_fund_mean: float | None
    mean_fund_mean: float | None
    pct_positive: float | None
    n_strongest: int
    n_weakest: int


@dataclass(frozen=True)
class CrossSectionResult:
    n_funds: int
    permutations: int
    seed: int
    strongest_counts: tuple[int, ...]
    weakest_counts: tuple[int, ...]
    top_strongest_month: int
    top_strongest_count: int
    p_max_strongest: float
    p_top_strongest_raw: float
    top_weakest_month: int
    top_weakest_count: int
    p_max_weakest: float
    p_top_weakest_raw: float


def _argextremum_month(means: list[float | None], *, reverse: bool) -> int | None:
    scored: list[tuple[float, int]] = []
    for i, mean in enumerate(means):
        if mean is not None:
            scored.append((mean, i + 1))
    if not scored:
        return None
    target = max(v for v, _ in scored) if reverse else min(v for v, _ in scored)
    tied = [month for value, month in scored if value == target]
    return min(tied)


def consensus_rows(funds: list[FundSeasonality]) -> list[ConsensusRow]:
    infer = [f for f in funds if f.infer_ok]
    rows: list[ConsensusRow] = []
    for month in range(1, 13):
        means: list[float] = [
            mean
            for f in infer
            if (mean := f.stats[month - 1].mean) is not None
        ]
        n_strong = sum(1 for f in infer if f.strongest == month)
        n_weak = sum(1 for f in infer if f.weakest == month)
        pos = sum(1 for value in means if value > 0.0)
        rows.append(
            ConsensusRow(
                month=month,
                n_funds=len(means),
                median_fund_mean=statistics.median(means) if means else None,
                mean_fund_mean=statistics.mean(means) if means else None,
                pct_positive=(pos / len(means)) if means else None,
                n_strongest=n_strong,
                n_weakest=n_weak,
            )
        )
    return rows


def cross_sectional_permutation(
    funds: list[FundSeasonality],
    permutations: int,
    seed: int,
) -> CrossSectionResult | None:
    """Shuffle month labels within each inferential fund; test strongest/weakest concentration."""
    infer = [f for f in funds if f.infer_ok and f.labels]
    if not infer or permutations < 1:
        return None
    obs_strong = [0] * 12
    obs_weak = [0] * 12
    for fund in infer:
        if fund.strongest is not None:
            obs_strong[fund.strongest - 1] += 1
        if fund.weakest is not None:
            obs_weak[fund.weakest - 1] += 1
    top_s = max(range(12), key=lambda i: (obs_strong[i], -i))
    top_w = max(range(12), key=lambda i: (obs_weak[i], -i))
    max_s = obs_strong[top_s]
    max_w = obs_weak[top_w]

    rng = random.Random(seed)
    n_max_s = 0
    n_max_w = 0
    n_top_s = 0
    n_top_w = 0
    packed = [(list(f.labels), list(f.values)) for f in infer]
    for _ in range(permutations):
        counts_s = [0] * 12
        counts_w = [0] * 12
        for labels, values in packed:
            work = labels[:]
            rng.shuffle(work)
            means = _month_means(_group_values(work, values))
            strong = _argextremum_month(means, reverse=True)
            weak = _argextremum_month(means, reverse=False)
            if strong is not None:
                counts_s[strong - 1] += 1
            if weak is not None:
                counts_w[weak - 1] += 1
        if max(counts_s) >= max_s:
            n_max_s += 1
        if max(counts_w) >= max_w:
            n_max_w += 1
        if counts_s[top_s] >= max_s:
            n_top_s += 1
        if counts_w[top_w] >= max_w:
            n_top_w += 1
    return CrossSectionResult(
        n_funds=len(infer),
        permutations=permutations,
        seed=seed,
        strongest_counts=tuple(obs_strong),
        weakest_counts=tuple(obs_weak),
        top_strongest_month=top_s + 1,
        top_strongest_count=max_s,
        p_max_strongest=_empirical_p(n_max_s, permutations),
        p_top_strongest_raw=_empirical_p(n_top_s, permutations),
        top_weakest_month=top_w + 1,
        top_weakest_count=max_w,
        p_max_weakest=_empirical_p(n_max_w, permutations),
        p_top_weakest_raw=_empirical_p(n_top_w, permutations),
    )


def _fund_from_returns(
    isin: str,
    name: str,
    distribution: str,
    currency: str,
    returns: list[MonthReturn],
    skip_reason: str | None = None,
) -> FundSeasonality:
    stats = month_stats(returns) if returns else month_stats([])
    infer_ok = bool(returns) and inferential_floor_met(stats, SEASONALITY_MIN_N_INFER)
    return FundSeasonality(
        isin=isin,
        name=name,
        distribution=distribution,
        currency=currency,
        n_months=len(returns),
        complete_years=complete_year_count(returns),
        stats=stats,
        infer_ok=infer_ok and skip_reason is None,
        skip_reason=skip_reason,
        strongest=strongest_mean_month(stats) if returns else None,
        weakest=weakest_mean_month(stats) if returns else None,
        years=tuple(row.year for row in returns),
        labels=tuple(row.month for row in returns),
        values=tuple(row.ret for row in returns),
    )


def cross_section_caveat(n_funds: int) -> str:
    """Correlated-universe warning; *N* is the inferential cohort size."""
    return (
        "Cross-sectional caveat: funds are not independent observations; "
        "correlated exposures can cause a common market effect to appear "
        "across many funds. Cross-sectional significance measures "
        f"concentration across the configured universe, not {n_funds} "
        "independent replications."
    )


def equal_weight_returns(funds: list[FundSeasonality]) -> list[MonthReturn]:
    """Balanced 1/N panel: a month-year is kept only if every inferential fund has it."""
    infer = [f for f in funds if f.infer_ok]
    n = len(infer)
    if n == 0:
        return []
    buckets: dict[tuple[int, int], list[float]] = {}
    for fund in infer:
        for year, month, value in zip(fund.years, fund.labels, fund.values, strict=True):
            buckets.setdefault((year, month), []).append(value)
    rows: list[MonthReturn] = []
    for (year, month), values in sorted(buckets.items()):
        if len(values) != n:
            continue
        rows.append(
            MonthReturn(
                year=year,
                month=month,
                end_date=f"{year}-{month:02d}-28",
                prev_end_date="panel",
                ret=sum(values) / n,
            )
        )
    return rows


# ---------------------------------------------------------------------------
# Seasonal contribution / sit-out simulator (not a backtest DeployMode).
# ---------------------------------------------------------------------------


def _grow(balance: float, from_day: str, to_day: str, annual_rate: float) -> float:
    if annual_rate == 0.0 or balance == 0.0:
        return balance
    days = (date.fromisoformat(to_day) - date.fromisoformat(from_day)).days
    return float(balance * (1.0 + annual_rate) ** (days / 365.0))


def _max_drawdown(values: list[float]) -> float:
    peak = float("-inf")
    mdd = 0.0
    for v in values:
        peak = max(peak, v)
        if peak > 0.0:
            mdd = max(mdd, (peak - v) / peak)
    return mdd


@dataclass(frozen=True)
class StrategyResult:
    label: str
    kind: DeployKind
    terminal: float
    equity: float
    cash: float
    xirr: float | None
    max_dd: float
    cash_income: float
    total_contributed: float
    equity_cost: float
    n_fills: int


def strategy_fills(
    dates: list[str],
    complete: set[tuple[int, int]],
) -> list[int]:
    """Monthly fills whose calendar month is in the complete-month sample."""
    if not dates:
        return []
    raw = monthly_fill_indices(dates, 0, len(dates) - 1)
    kept: list[int] = []
    for idx in raw:
        day = date.fromisoformat(dates[idx])
        if (day.year, day.month) in complete:
            kept.append(idx)
    return kept


def simulate_seasonal(
    dates: list[str],
    closes: list[float],
    contribution: float,
    cash_rate: float,
    kind: DeployKind,
    selected_month: int | None,
    fills: list[int],
    label: str,
    redeploy_month: int | None = None,
) -> StrategyResult:
    """Roll a fixed-``C`` contribution schedule under one seasonal policy."""
    fill_set = set(fills)
    fill_month = {i: date.fromisoformat(dates[i]).month for i in fills}
    fill_year = {i: date.fromisoformat(dates[i]).year for i in fills}
    shares = 0.0
    cash = 0.0
    last_day = dates[0]
    cash_income = 0.0
    equity_cost = 0.0
    total_contributed = 0.0
    redeploy_next = False
    pending_year: int | None = None
    wealth: list[float] = []

    for i, (day, price) in enumerate(zip(dates, closes, strict=True)):
        if cash > 0.0 and cash_rate:
            grown = _grow(cash, last_day, day, cash_rate)
            cash_income += grown - cash
            cash = grown
        last_day = day

        if i in fill_set:
            total_contributed += contribution
            month = fill_month[i]
            year = fill_year[i]
            if kind == DeployKind.DCA:
                shares += contribution / price
                equity_cost += contribution
            elif kind == DeployKind.AVOID:
                if month == selected_month:
                    cash += contribution
                    redeploy_next = True
                elif redeploy_next:
                    deploy = cash + contribution
                    shares += deploy / price
                    equity_cost += deploy
                    cash = 0.0
                    redeploy_next = False
                else:
                    shares += contribution / price
                    equity_cost += contribution
            elif kind == DeployKind.AVOID_DRAG:
                if month == selected_month:
                    cash += contribution
                else:
                    shares += contribution / price
                    equity_cost += contribution
            elif kind == DeployKind.SHIFT:
                if month == selected_month:
                    cash += contribution
                    pending_year = year
                elif (
                    redeploy_month is not None
                    and month == redeploy_month
                    and pending_year == year
                ):
                    deploy = cash + contribution
                    shares += deploy / price
                    equity_cost += deploy
                    cash = 0.0
                    pending_year = None
                else:
                    shares += contribution / price
                    equity_cost += contribution
            elif kind == DeployKind.SIT_OUT:
                if month == selected_month:
                    cash += shares * price + contribution
                    shares = 0.0
                elif shares == 0.0 and cash > 0.0:
                    deploy = cash + contribution
                    shares = deploy / price
                    equity_cost += deploy
                    cash = 0.0
                else:
                    shares += contribution / price
                    equity_cost += contribution

        wealth.append(shares * price + cash)

    equity = shares * closes[-1]
    terminal = equity + cash
    flows = [(dates[i], -contribution) for i in fills]
    flows.append((dates[-1], terminal))
    return StrategyResult(
        label=label,
        kind=kind,
        terminal=terminal,
        equity=equity,
        cash=cash,
        xirr=xirr(flows),
        max_dd=_max_drawdown(wealth),
        cash_income=cash_income,
        total_contributed=total_contributed,
        equity_cost=equity_cost,
        n_fills=len(fills),
    )


def invariance_holds(result: StrategyResult, *, cash_rate: float) -> bool:
    """``N·C == equity_cost + leftover cash`` at a 0% cash rate (contribution rules)."""
    if cash_rate != 0.0 or result.kind == DeployKind.SIT_OUT:
        return True
    return abs(result.total_contributed - (result.equity_cost + result.cash)) < 1e-9


@dataclass(frozen=True)
class YearScore:
    better: int
    worse: int
    tie: int


@dataclass(frozen=True)
class EvalWindow:
    title: str
    label: str
    caveat: str
    returns: list[MonthReturn]
    results: list[StrategyResult]
    scores: tuple[tuple[str, YearScore], ...]


EVAL_FREEZE_CAVEAT = (
    "Months are frozen from the ADR-0027 configured-universe discovery "
    "(August weak, November strong). They are not re-selected here. "
    "A later window is a stability check of the economic rule, not an "
    "independent discovery: the freeze used the book's full history."
)


def isolated_year_terminals(
    dates: list[str],
    closes: list[float],
    fills: list[int],
    contribution: float,
    cash_rate: float,
    kind: DeployKind,
    selected_month: int | None,
    redeploy_month: int | None = None,
) -> dict[int, float]:
    """Terminal wealth of each 12-fill calendar year, started from zero shares."""
    by_year: dict[int, list[int]] = {}
    for idx in fills:
        by_year.setdefault(date.fromisoformat(dates[idx]).year, []).append(idx)
    out: dict[int, float] = {}
    for year, year_fills in by_year.items():
        if len(year_fills) != 12:
            continue
        start = year_fills[0]
        end = start
        for i in range(start, len(dates)):
            if date.fromisoformat(dates[i]).year != year:
                break
            end = i
        sl_dates = dates[start : end + 1]
        sl_closes = closes[start : end + 1]
        sl_fills = [i - start for i in year_fills]
        result = simulate_seasonal(
            sl_dates, sl_closes, contribution, cash_rate, kind,
            selected_month, sl_fills, "year", redeploy_month,
        )
        out[year] = result.terminal
    return out


def year_scorecard(
    named: dict[int, float],
    baseline: dict[int, float],
) -> YearScore:
    better = worse = tie = 0
    for year, base in baseline.items():
        if year not in named:
            continue
        delta = named[year] - base
        if delta > 1e-9:
            better += 1
        elif delta < -1e-9:
            worse += 1
        else:
            tie += 1
    return YearScore(better, worse, tie)


def _sim(
    dates: list[str],
    closes: list[float],
    contribution: float,
    cash_rate: float,
    kind: DeployKind,
    selected: int | None,
    fills: list[int],
    label: str,
    redeploy: int | None = None,
) -> StrategyResult:
    result = simulate_seasonal(
        dates, closes, contribution, cash_rate, kind, selected, fills, label, redeploy,
    )
    if not invariance_holds(result, cash_rate=cash_rate):
        raise SeasonalityError(
            f"invariance broken for {result.label}: "
            f"N·C={result.total_contributed:g} vs "
            f"equity_cost+cash={result.equity_cost + result.cash:g}"
        )
    return result


def evaluate_battery(
    dates: list[str],
    closes: list[float],
    returns: list[MonthReturn],
    contribution: float,
    cash_rate: float,
) -> tuple[list[StrategyResult], tuple[tuple[str, YearScore], ...]]:
    """Frozen August/November battery. August-skip is the headline rule."""
    complete = {(r.year, r.month) for r in returns}
    fills = strategy_fills(dates, complete)
    if not fills:
        raise SeasonalityError(
            "no contribution fills fall inside the complete-month sample"
        )
    if not any(date.fromisoformat(dates[i]).month == FROZEN_WEAK_MONTH for i in fills):
        raise SeasonalityError("evaluation window has no August contribution fill")

    specs: list[tuple[DeployKind, int | None, int | None, str]] = [
        (DeployKind.DCA, None, None, "A  constant-DCA"),
        (DeployKind.AVOID, FROZEN_WEAK_MONTH, None, "August-skip"),
        (DeployKind.AVOID, FROZEN_STRONG_MONTH, None, "November-skip"),
        (DeployKind.AVOID_DRAG, FROZEN_WEAK_MONTH, None, "cash-drag Aug"),
        (DeployKind.SHIFT, FROZEN_WEAK_MONTH, 10, "Aug->Oct"),
        (DeployKind.SHIFT, FROZEN_WEAK_MONTH, 11, "Aug->Nov"),
        (DeployKind.SIT_OUT, FROZEN_WEAK_MONTH, None, "sit-out Aug"),
    ]
    results = [
        _sim(dates, closes, contribution, cash_rate, kind, selected, fills, label, redeploy)
        for kind, selected, redeploy, label in specs
    ]
    dca_years = isolated_year_terminals(
        dates, closes, fills, contribution, cash_rate, DeployKind.DCA, None,
    )
    scores: list[tuple[str, YearScore]] = []
    for kind, selected, redeploy, label in specs[1:]:
        named_years = isolated_year_terminals(
            dates, closes, fills, contribution, cash_rate, kind, selected, redeploy,
        )
        scores.append((label, year_scorecard(named_years, dca_years)))
    return results, tuple(scores)


# ---------------------------------------------------------------------------
# Windows / overlap.
# ---------------------------------------------------------------------------


def windows_overlap(a: list[MonthReturn], b: list[MonthReturn]) -> bool:
    keys_a = {(r.year, r.month) for r in a}
    return any((r.year, r.month) in keys_a for r in b)


# ---------------------------------------------------------------------------
# Rendering.
# ---------------------------------------------------------------------------


def _header(
    isin: str,
    name: str,
    price_mode: str,
    distribution: str,
    currency: str,
    returns: list[MonthReturn],
    partials: list[PartialMonth],
    infer_ok: bool,
) -> list[str]:
    if returns:
        first = f"{returns[0].year}-{returns[0].month:02d}"
        last = f"{returns[-1].year}-{returns[-1].month:02d}"
        span = f"{first} ... {last}"
    else:
        span = "n/a"
    years = complete_year_count(returns)
    mode_note = (
        "total-return (accumulating NAV)"
        if price_mode == "total-return"
        else "price return (diagnostic)"
    )
    layer = (
        f"Layer: inferential (every calendar month has N>={SEASONALITY_MIN_N_INFER})"
        if infer_ok
        else "Layer: DESCRIPTIVE - insufficient history"
    )
    lines = [
        "Calendar seasonality — ADR-0026",
        f"{isin}  {name}",
        layer,
        f"Price mode: {mode_note}  ·  {currency}  ·  distribution {distribution or 'unknown'}",
        f"Complete months: {span}  ({len(returns)} total, {years} complete years)",
    ]
    if partials:
        bits = [
            f"{p.year}-{p.month:02d} ({p.reason})" for p in partials
        ]
        lines.append("Partial months excluded: " + "; ".join(bits))
    return lines


def _stats_table(stats: list[MonthStats], infer_ok: bool) -> list[str]:
    head = (
        f"{'Month':<6} {'N':>4} {'Mean':>8} {'Median':>8} "
        f"{'Positive':>9} {'Negative':>9} {'Vol':>8} {'vs-other':>9}"
    )
    title = (
        "Calendar seasonality"
        if infer_ok
        else "Calendar seasonality (DESCRIPTIVE - insufficient history)"
    )
    out = ["", title, "-" * len(head), head, "-" * len(head)]
    for s in stats:
        if s.n == 0:
            out.append(
                f"{_month_name(s.month):<6} {0:>4} "
                f"{'n/a':>8} {'n/a':>8} {'n/a':>9} {'n/a':>9} {'n/a':>8} {'n/a':>9}"
            )
            continue
        out.append(
            f"{_month_name(s.month):<6} {s.n:>4} "
            f"{_fmt_signed_pct(s.mean):>8} {_fmt_signed_pct(s.median):>8} "
            f"{_fmt_pct(s.pct_positive):>9} {_fmt_pct(s.pct_negative):>9} "
            f"{_fmt_pct(s.stdev, 2):>8} {_fmt_pp(s.mean_excess):>9}"
        )
    return out


def _rankings(stats: list[MonthStats]) -> list[str]:
    strongest = _rank_month(stats, lambda s: s.mean, reverse=True)
    weakest = _rank_month(stats, lambda s: s.mean, reverse=False)
    strong_med = _rank_month(stats, lambda s: s.median, reverse=True)
    weak_med = _rank_month(stats, lambda s: s.median, reverse=False)
    high_pos = _rank_month(stats, lambda s: s.pct_positive, reverse=True)
    low_pos = _rank_month(stats, lambda s: s.pct_positive, reverse=False)
    spread = None
    if (
        strongest is not None and weakest is not None
        and strongest.mean is not None and weakest.mean is not None
    ):
        spread = strongest.mean - weakest.mean

    def _line(label: str, row: MonthStats | None, attr: str) -> str:
        if row is None:
            return f"  {label:<24} n/a"
        value = getattr(row, attr)
        extra = f"  N={row.n}"
        if attr in {"min_ret", "max_ret"}:
            return f"  {label:<24} {_month_name(row.month)}  {_fmt_signed_pct(value)}{extra}"
        if attr in {"pct_positive", "pct_negative"}:
            return f"  {label:<24} {_month_name(row.month)}  {_fmt_pct(value)}{extra}"
        return f"  {label:<24} {_month_name(row.month)}  {_fmt_signed_pct(value)}{extra}"

    return [
        "",
        "In-sample descriptive rankings",
        _line("strongest mean", strongest, "mean"),
        _line("weakest mean", weakest, "mean"),
        _line("strongest median", strong_med, "median"),
        _line("weakest median", weak_med, "median"),
        _line("highest % positive", high_pos, "pct_positive"),
        _line("lowest % positive", low_pos, "pct_positive"),
        f"  {'strongest-weakest spread':<24} {_fmt_pp(spread)}",
    ]


def _best_worst_obs(stats: list[MonthStats]) -> list[str]:
    """Best/worst single complete-month observations across the whole sample."""
    best: MonthStats | None = None
    worst: MonthStats | None = None
    for s in stats:
        if s.n == 0:
            continue
        if worst is None or (s.min_ret is not None and (
            worst.min_ret is None or s.min_ret < worst.min_ret
        )):
            worst = s
        if best is None or (s.max_ret is not None and (
            best.max_ret is None or s.max_ret > best.max_ret
        )):
            best = s
    lines: list[str] = []
    if worst is not None:
        lines.append(
            f"  worst observation         {_month_name(worst.month)}  "
            f"{worst.min_year}  {_fmt_signed_pct(worst.min_ret)}"
        )
    if best is not None:
        lines.append(
            f"  best observation          {_month_name(best.month)}  "
            f"{best.max_year}  {_fmt_signed_pct(best.max_ret)}"
        )
    return lines


def _statistical_block(
    stats: list[MonthStats],
    perm: PermutationResult | None,
    floor_reason: str | None,
) -> list[str]:
    out = ["", "Seasonality test", "-" * 32]
    if perm is None:
        out.append("UNAVAILABLE - DESCRIPTIVE only; insufficient history")
        out.append(f"Reason: {floor_reason}")
        return out
    out.append("Null: calendar month is exchangeable with the other months")
    out.append(f"Kruskal-Wallis H:          {perm.h_obs:.4f}")
    out.append(
        f"Permutation p-value:       {perm.p_omnibus:.4f}  "
        f"({perm.permutations} permutations, seed {perm.seed})"
    )
    weakest = _rank_month(stats, lambda s: s.mean, reverse=False)
    strongest = _rank_month(stats, lambda s: s.mean, reverse=True)
    out += [
        "",
        "Extreme-month placebo (same permutations)",
        f"  weakest-month mean   {_month_name(weakest.month) if weakest else 'n/a'}  "
        f"{_fmt_signed_pct(perm.min_mean_obs)}   p={perm.p_worst:.4f}",
        f"  strongest-month mean {_month_name(strongest.month) if strongest else 'n/a'}  "
        f"{_fmt_signed_pct(perm.max_mean_obs)}   p={perm.p_best:.4f}",
        "",
        "Month vs other eleven (mean excess; raw p and BH-adjusted)",
    ]
    for s, raw, adj in zip(stats, perm.month_excess_p_raw, perm.month_excess_p_adj, strict=True):
        raw_s = "n/a" if raw is None else f"{raw:.4f}"
        adj_s = "n/a" if adj is None else f"{adj:.4f}"
        out.append(
            f"  {_month_name(s.month):<4} {_fmt_pp(s.mean_excess):>8}   "
            f"raw {raw_s:>7}   adj {adj_s:>7}"
        )
    return out


def _cash_sketch(cash_rate: float) -> list[str]:
    if cash_rate <= 0.0:
        return []
    month_cost = (1.0 + cash_rate) ** (30.0 / 365.0) - 1.0
    return [
        "",
        f"Opportunity-cost sketch (--cash-rate {cash_rate:g}, not a strategy):",
        f"  holding cash for ~30 days forgoes about {_fmt_pct(month_cost, 2)} "
        "vs staying invested (Actual/365).",
        "  This is not a seasonal rule evaluation.",
    ]


def _strategy_table(
    results: list[StrategyResult],
    baseline: StrategyResult,
    cash_rate: float,
    selected_month: int,
    rule_label: str,
    oos: bool,
) -> list[str]:
    title = (
        f"Seasonal rule (out-of-sample, frozen {_month_name(selected_month)})"
        if oos
        else f"Seasonal rule ({rule_label}, {_month_name(selected_month)} pre-specified)"
    )
    head = (
        f"{'Control':<28} {'Terminal':>12} {'XIRR':>8} {'MaxDD':>8} "
        f"{'Cash':>10} {'Income':>10} {'Δ vs DCA':>10}"
    )
    out = ["", title, "─" * len(head), head, "─" * len(head)]
    for r in results:
        delta = r.terminal - baseline.terminal
        out.append(
            f"{r.label:<28} {_fmt_money(r.terminal):>12} {_fmt_signed_pct(r.xirr, 1):>8} "
            f"{_fmt_pct(r.max_dd, 1):>8} {_fmt_money(r.cash):>10} "
            f"{_fmt_money(r.cash_income):>10} {_fmt_signed_money(delta):>10}"
        )
    out.append(
        f"(contribution fills: {baseline.n_fills}; cash-rate {cash_rate:g}; "
        "month-end returns and fill timing are different samples)"
    )
    return out


def _findings(
    stats: list[MonthStats],
    perm: PermutationResult | None,
    strategy: list[StrategyResult] | None,
    *,
    oos_month: int | None,
    oos_refused: str | None,
) -> list[str]:
    weakest = _rank_month(stats, lambda s: s.mean, reverse=False)
    if weakest is None:
        desc = "Descriptive: no complete calendar months in this sample."
    else:
        desc = (
            f"Descriptive: {_month_name(weakest.month)} had the lowest mean return "
            f"in this sample ({_fmt_signed_pct(weakest.mean)}, N={weakest.n}). "
            "In-sample ranking only."
        )
    if perm is None:
        stat = (
            "Statistical: DESCRIPTIVE only - insufficient history "
            f"(need N>={SEASONALITY_MIN_N_INFER} in every calendar month)."
        )
    elif perm.p_omnibus < SEASONALITY_UNUSUAL_P:
        stat = (
            "Statistical: a calendar-month effect was detected at the 5% level "
            f"(Kruskal-Wallis H={perm.h_obs:.3f}, permutation p={perm.p_omnibus:.3f})."
        )
    else:
        stat = (
            "Statistical: no statistically significant calendar-month effect "
            "was detected at the 5% level "
            f"(Kruskal-Wallis H={perm.h_obs:.3f}, permutation p={perm.p_omnibus:.3f}). "
            "That does not establish that the variation is random."
        )
    if oos_refused:
        econ = f"Economic: out-of-sample rule not evaluated ({oos_refused})."
        trade = "Trading: no actionable seasonal strategy is established."
    elif strategy is None:
        econ = (
            "Economic: not evaluated. A negative monthly mean is not a benefit."
        )
        trade = "Trading: no actionable seasonal strategy is established."
    else:
        baseline, named = strategy[0], strategy[1]
        delta = named.terminal - baseline.terminal
        econ = (
            f"Economic: {named.label} vs DCA: terminal Δ = "
            f"{_fmt_signed_money(delta)}. A negative monthly mean is not a benefit."
        )
        if oos_month is not None and delta > 0:
            trade = (
                f"Trading: frozen OOS rule beat DCA on the test window "
                f"(Δ={_fmt_signed_money(delta)}); that does not generalise "
                "past this window."
            )
        else:
            trade = "Trading: no actionable seasonal strategy is established."
    return ["", "Findings", f"  {desc}", f"  {stat}", f"  {econ}", f"  {trade}"]


def _interpretation() -> list[str]:
    return ["", "Interpretation", *[f"  {line}" for line in INTERPRETATION_FOOTER]]


def _roster_line(fund: FundSeasonality) -> str:
    strong = _month_name(fund.strongest) if fund.strongest else "n/a"
    weak = _month_name(fund.weakest) if fund.weakest else "n/a"
    return (
        f"  {fund.isin}  {fund.n_months:>4} mo  {fund.complete_years:>2}y  "
        f"strongest {strong:<3}  weakest {weak:<3}  {fund.name}"
    )


def _portfolio_report(
    funds: list[FundSeasonality],
    consensus: list[ConsensusRow],
    cross: CrossSectionResult | None,
    price_mode: str,
    permutations: int,
    seed: int,
) -> list[str]:
    infer = [f for f in funds if f.infer_ok]
    descriptive = [f for f in funds if not f.infer_ok and f.skip_reason is None]
    excluded = [f for f in funds if f.skip_reason is not None]
    mode_note = (
        "total-return (accumulating NAV)"
        if price_mode == "total-return"
        else "price return (diagnostic)"
    )
    lines = [
        "Portfolio seasonality consensus — ADR-0027",
        f"Universe: configured ETFs with a stored price series  ·  {mode_note}",
        (
            f"Inference floor: N>={SEASONALITY_MIN_N_INFER} complete "
            "observations in every calendar month"
        ),
        f"Funds: {len(infer)} inferential, {len(descriptive)} descriptive, "
        f"{len(excluded)} excluded",
    ]
    if infer:
        lines += ["", "Inferential cohort"]
        lines.extend(_roster_line(f) for f in infer)
    if descriptive:
        lines += ["", "DESCRIPTIVE - insufficient history"]
        lines.extend(_roster_line(f) for f in descriptive)
    if excluded:
        lines += ["", "Excluded"]
        for fund in excluded:
            lines.append(f"  {fund.isin}  {fund.skip_reason}  {fund.name}")

    head = (
        f"{'Month':<6} {'Funds':>5} {'Median':>8} {'Mean':>8} "
        f"{'%Pos':>7} {'Strongest':>10} {'Weakest':>8}"
    )
    lines += ["", "Portfolio seasonality consensus", "-" * len(head), head, "-" * len(head)]
    if not infer:
        lines.append("UNAVAILABLE - no fund met the inference floor")
    else:
        n = len(infer)
        for row in consensus:
            s_pct = f"{row.n_strongest / n * 100:.0f}%" if n else "n/a"
            w_pct = f"{row.n_weakest / n * 100:.0f}%" if n else "n/a"
            lines.append(
                f"{_month_name(row.month):<6} {row.n_funds:>5} "
                f"{_fmt_signed_pct(row.median_fund_mean):>8} "
                f"{_fmt_signed_pct(row.mean_fund_mean):>8} "
                f"{_fmt_pct(row.pct_positive):>7} "
                f"{row.n_strongest:>3} ({s_pct:>3}) {row.n_weakest:>3} ({w_pct:>3})"
            )

    lines += ["", "Cross-sectional test", "-" * 32]
    if cross is None:
        lines.append("UNAVAILABLE - need an inferential cohort")
        lines.append(f"Permutations: {permutations}  (seed {seed})")
    else:
        lines += [
            f"Funds analysed                    {cross.n_funds}",
            f"Minimum history per month         {SEASONALITY_MIN_N_INFER}",
            f"Permutation tests                 {cross.permutations}",
            f"Seed                              {cross.seed}",
            "",
            f"Strongest concentration: {_month_name(cross.top_strongest_month)}",
            f"Observed funds with "
            f"{_month_name(cross.top_strongest_month)} #1: "
            f"{cross.top_strongest_count}/{cross.n_funds}",
            f"Placebo probability (max concentration): {cross.p_max_strongest:.4f}",
            f"Raw p for this month's count:            {cross.p_top_strongest_raw:.4f}",
            "",
            f"Weakest concentration: {_month_name(cross.top_weakest_month)}",
            f"Observed funds with "
            f"{_month_name(cross.top_weakest_month)} last: "
            f"{cross.top_weakest_count}/{cross.n_funds}",
            f"Placebo probability (max concentration): {cross.p_max_weakest:.4f}",
            f"Raw p for this month's count:            {cross.p_top_weakest_raw:.4f}",
        ]

    if infer:
        lines += ["", cross_section_caveat(len(infer))]

    ew_returns = equal_weight_returns(infer)
    ew_stats = month_stats(ew_returns) if ew_returns else []
    ew_perm: PermutationResult | None = None
    ew_floor: str | None = None
    if ew_returns and inferential_floor_met(ew_stats, SEASONALITY_MIN_N_INFER):
        ew_perm = permutation_test(ew_returns, permutations, seed)
    elif infer and not ew_returns:
        ew_floor = "balanced panel is empty (no month-year common to every fund)"
    elif ew_returns:
        short = [s for s in ew_stats if s.n < SEASONALITY_MIN_N_INFER]
        ew_floor = (
            f"need >={SEASONALITY_MIN_N_INFER} observations in every month; "
            + ", ".join(f"{_month_name(s.month)} N={s.n}" for s in short)
        )
    lines += _equal_weight_block(infer, ew_returns, ew_stats, ew_perm, ew_floor)

    if infer:
        strong = _month_name(cross.top_strongest_month) if cross else "n/a"
        weak = _month_name(cross.top_weakest_month) if cross else "n/a"
        k_s = f"{cross.top_strongest_count}/{cross.n_funds}" if cross else "n/a"
        k_w = f"{cross.top_weakest_count}/{cross.n_funds}" if cross else "n/a"
        desc = (
            f"Descriptive: {strong} was the most frequent strongest month ({k_s}); "
            f"{weak} was the most frequent weakest ({k_w}). In-sample ranking only."
        )
    else:
        desc = "Descriptive: no fund met the inference floor."
    if cross is None:
        stat = (
            "Statistical: cross-sectional inference unavailable "
            "(no inferential cohort)."
        )
    elif cross.p_max_strongest < SEASONALITY_UNUSUAL_P:
        stat = (
            "Statistical: a common calendar-month concentration was detected "
            f"at the 5% level (max-concentration p={cross.p_max_strongest:.3f})."
        )
    else:
        stat = (
            "Statistical: no statistically significant calendar-month effect "
            "was detected at the 5% level among funds meeting the inference "
            f"threshold (max-concentration p={cross.p_max_strongest:.3f}). "
            "That does not establish that the variation is random."
        )
    if ew_perm is None:
        ew_stat = (
            "Equal-weight book: UNAVAILABLE "
            f"({ew_floor or 'no balanced panel'})."
        )
    elif ew_perm.p_omnibus < SEASONALITY_UNUSUAL_P:
        ew_stat = (
            "Equal-weight book: a calendar-month effect was detected at "
            f"the 5% level (permutation p={ew_perm.p_omnibus:.3f})."
        )
    else:
        ew_stat = (
            "Equal-weight book: no statistically significant calendar-month "
            "effect was detected at the 5% level "
            f"(permutation p={ew_perm.p_omnibus:.3f})."
        )
    econ = (
        "Economic: not evaluated. Freeze August/November and score them "
        "with --isin ... --evaluate."
    )
    trade = "Trading: no actionable seasonal strategy is established."
    lines += [
        "", "Findings",
        f"  {desc}", f"  {stat}", f"  {ew_stat}", f"  {econ}", f"  {trade}",
    ]
    lines += _interpretation()
    return lines


def _equal_weight_block(
    infer: list[FundSeasonality],
    ew_returns: list[MonthReturn],
    ew_stats: list[MonthStats],
    ew_perm: PermutationResult | None,
    ew_floor: str | None,
) -> list[str]:
    head = f"{'Month':<6} {'N':>4} {'Mean':>8} {'Median':>8} {'%Pos':>7}"
    n_panel = len(infer)
    n_obs = len(ew_returns)
    years = complete_year_count(ew_returns)
    lines = [
        "",
        "Equal-weight book (balanced panel)",
        "-" * len(head),
        (
            f"Each inferential fund weight 1/{n_panel}; a month-year is kept "
            "only if every fund has a complete return."
            if n_panel
            else "No inferential cohort."
        ),
        f"Panel: {n_panel} funds, {n_obs} overlapping month-years "
        f"({years} complete years)",
        "-" * len(head),
        head,
        "-" * len(head),
    ]
    if not ew_stats:
        lines.append("UNAVAILABLE - no balanced-panel month-year")
        return lines
    for s in ew_stats:
        if s.n == 0:
            lines.append(
                f"{_month_name(s.month):<6} {0:>4} "
                f"{'n/a':>8} {'n/a':>8} {'n/a':>7}"
            )
            continue
        lines.append(
            f"{_month_name(s.month):<6} {s.n:>4} "
            f"{_fmt_signed_pct(s.mean):>8} "
            f"{_fmt_signed_pct(s.median):>8} "
            f"{_fmt_pct(s.pct_positive):>7}"
        )
    lines += ["", "Equal-weight seasonality test", "-" * 32]
    if ew_perm is None:
        lines.append("UNAVAILABLE - DESCRIPTIVE only; insufficient panel history")
        if ew_floor:
            lines.append(f"Reason: {ew_floor}")
        return lines
    lines += [
        "Null: calendar month is exchangeable with the other months",
        f"Kruskal-Wallis H:          {ew_perm.h_obs:.4f}",
        (
            f"Permutation p-value:       {ew_perm.p_omnibus:.4f}  "
            f"({ew_perm.permutations} permutations, seed {ew_perm.seed})"
        ),
    ]
    return lines


def _explain_block(
    *,
    isin: str,
    price_mode: str,
    distribution: str,
    currency: str,
    window_from: str | None,
    window_to: str,
    returns: list[MonthReturn],
    stats: list[MonthStats],
    perm: PermutationResult | None,
    rule: str | None,
    selected_month: int | None,
    oos: dict[str, str] | None,
    cash_rate: float,
    contribution: float,
    infer_ok: bool,
) -> list[str]:
    n_per = ", ".join(f"{_month_name(s.month)}={s.n}" for s in stats)
    status = Status.CALCULATED if infer_ok else Status.UNAVAILABLE
    method = (
        "month-end EUR total-return (accumulating NAV) grouped by calendar "
        "month; Kruskal-Wallis H; label-shuffle permutation p = (1 + #extreme) / (1 + P); "
        "BH on 12 month-vs-rest excess tests"
        if price_mode == "total-return"
        else "month-end EUR *price* return (diagnostic); same tests"
    )
    inputs = (
        f"ISIN {isin}; requested {window_from or 'series start'}…{window_to}; "
        f"effective N={len(returns)}; currency {currency}; distribution {distribution or 'unknown'}"
    )
    result = (
        f"inferential floor {'met' if infer_ok else 'not met'} "
        f"(SEASONALITY_MIN_N_INFER={SEASONALITY_MIN_N_INFER})"
    )
    lines = [""]
    lines += _explain_metric(
        "Calendar seasonality",
        status,
        result,
        inputs,
        method,
        SEASONALITY_CONTRACT,
    )
    extra = [
        f"    Observations/month: {n_per}",
        f"    Complete years:     {complete_year_count(returns)}",
        "    Partial handling:   first/last incomplete months excluded "
        "from the primary analysis",
        "    Ties:               descriptive rankings break ties toward "
        "the earlier calendar month",
    ]
    if perm is not None:
        extra += [
            f"    Permutations:       {perm.permutations}  (seed {perm.seed})",
            f"    Omnibus:            Kruskal-Wallis H={perm.h_obs:.4f}  "
            f"(statistic)  permutation p={perm.p_omnibus:.4f}  "
            "(add-one; chi-squared approximation is not the reported p-value)",
            "    Multiple testing:   Benjamini-Hochberg FDR, 12 month-vs-rest tests",
        ]
    extra.append(
        "    Layers:             descriptive + statistical"
        + (" + backtested" if rule else "")
        + (" + out-of-sample" if oos else "")
    )
    if rule:
        extra += [
            f"    Rule:               {rule}"
            + (f"  month={selected_month}" if selected_month else ""),
            f"    Fills:              first close on-or-after the 1st "
            f"(monthly_fill_indices); C={contribution:g}; cash-rate={cash_rate:g}",
            "    Invariance:         contribution rules at cash-rate 0 satisfy "
            "N·C == equity_cost + leftover cash",
        ]
    if oos:
        extra += [
            f"    Training:           {oos['training']}",
            f"    Test:               {oos['test']}",
            f"    Frozen month:       {oos['month']}  (selected on training only; no test leakage)",
        ]
    lines.extend(extra)
    return lines


# ---------------------------------------------------------------------------
# CLI.
# ---------------------------------------------------------------------------


def _positive(value: str) -> float:
    f = float(value)
    if f <= 0.0:
        raise argparse.ArgumentTypeError(f"must be > 0: {value}")
    return f


def _positive_int(value: str) -> int:
    i = int(value)
    if i <= 0:
        raise argparse.ArgumentTypeError(f"must be a positive integer: {value}")
    return i


def _nonneg(value: str) -> float:
    f = float(value)
    if f < 0.0:
        raise argparse.ArgumentTypeError(f"must be ≥ 0: {value}")
    return f


def _valid_date(value: str) -> str:
    try:
        date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"must be YYYY-MM-DD: {value}") from exc
    return value


def _valid_month(value: str) -> int:
    i = int(value)
    if i < 1 or i > 12:
        raise argparse.ArgumentTypeError(f"must be 1-12: {value}")
    return i


def _holdout_label(
    train_from: str | None, train_to: str | None, test_from: str, test_to: str,
) -> str:
    if train_from is None or train_to is None:
        return "holdout of the frozen rule"
    if test_from >= train_to:
        return "chronological holdout of the frozen rule"
    if test_to <= train_from:
        return "reverse-era evaluation (not prospective)"
    return "holdout of the frozen rule"


def _score_text(score: YearScore | None) -> str:
    if score is None:
        return "n/a"
    return f"{score.better}/{score.worse}/{score.tie}"


def _eval_table(
    title: str,
    results: list[StrategyResult],
    baseline: StrategyResult,
    score_map: dict[str, YearScore],
    cash_rate: float,
) -> list[str]:
    head = (
        f"{'Control':<22} {'Terminal':>12} {'XIRR':>8} {'MaxDD':>8} "
        f"{'vs DCA':>10} {'Y+/Y-/tie':>10}"
    )
    out = ["", title, "-" * len(head), head, "-" * len(head)]
    for row in results:
        delta = row.terminal - baseline.terminal
        score = score_map.get(row.label)
        out.append(
            f"{row.label:<22} {_fmt_money(row.terminal):>12} "
            f"{_fmt_signed_pct(row.xirr, 1):>8} {_fmt_pct(row.max_dd, 1):>8} "
            f"{_fmt_signed_money(delta):>10} {_score_text(score):>10}"
        )
    out.append(
        f"(contribution fills: {baseline.n_fills}; cash-rate {cash_rate:g}; "
        "Y+/Y-/tie = isolated 12-fill years vs DCA)"
    )
    return out


def _make_eval_window(
    title: str,
    label: str,
    dates: list[str],
    closes: list[float],
    returns: list[MonthReturn],
    contribution: float,
    cash_rate: float,
) -> EvalWindow:
    results, scores = evaluate_battery(
        dates, closes, returns, contribution, cash_rate,
    )
    return EvalWindow(
        title=title,
        label=label,
        caveat=EVAL_FREEZE_CAVEAT,
        returns=returns,
        results=results,
        scores=scores,
    )


def _evaluate_report(
    isin: str,
    name: str,
    price_mode: str,
    distribution: str,
    currency: str,
    windows: list[EvalWindow],
    cash_rate: float,
    contribution: float,
) -> list[str]:
    mode_note = (
        "total-return (accumulating NAV)"
        if price_mode == "total-return"
        else "price return (diagnostic)"
    )
    weak = _month_name(FROZEN_WEAK_MONTH)
    strong = _month_name(FROZEN_STRONG_MONTH)
    lines = [
        "Seasonal strategy evaluation - ADR-0028",
        f"{isin}  {name}",
        f"Frozen from ADR-0027 book discovery: weak={weak}  strong={strong}",
        "These months are not re-selected on this series or this window.",
        f"Price mode: {mode_note}  ·  {currency}  ·  "
        f"distribution {distribution or 'unknown'}",
        f"Contribution: {contribution:g}  cash-rate: {cash_rate:g}",
        EVAL_FREEZE_CAVEAT,
    ]
    headline = windows[-1]
    for window in windows:
        span = (
            f"{window.returns[0].year}-{window.returns[0].month:02d} ... "
            f"{window.returns[-1].year}-{window.returns[-1].month:02d}"
            if window.returns
            else "n/a"
        )
        lines += [
            "",
            window.title,
            f"Label: {window.label}",
            f"Complete months: {span}  ({len(window.returns)} total)",
        ]
        by_label = {r.label: r for r in window.results}
        score_map = dict(window.scores)
        dca = by_label["A  constant-DCA"]
        lines += _eval_table(
            "Test A - contribution skip (primary)",
            [
                by_label["A  constant-DCA"],
                by_label["August-skip"],
                by_label["November-skip"],
                by_label["cash-drag Aug"],
            ],
            dca, score_map, cash_rate,
        )
        lines += _eval_table(
            "Test B - contribution shift (Aug->Sep is Test A)",
            [by_label["Aug->Oct"], by_label["Aug->Nov"]],
            dca, score_map, cash_rate,
        )
        lines += _eval_table(
            "Test C - full-portfolio sit-out (secondary)",
            [by_label["sit-out Aug"]],
            dca, score_map, cash_rate,
        )

    dca = headline.results[0]
    skip = next(r for r in headline.results if r.label == "August-skip")
    placebo = next(r for r in headline.results if r.label == "November-skip")
    shift_oct = next(r for r in headline.results if r.label == "Aug->Oct")
    shift_nov = next(r for r in headline.results if r.label == "Aug->Nov")
    sit = next(r for r in headline.results if r.label == "sit-out Aug")
    lines += [
        "",
        "Findings",
        (
            f"  Economic (primary): August-skip vs DCA on the {headline.label}: "
            f"terminal difference {_fmt_signed_money(skip.terminal - dca.terminal)}."
        ),
        (
            f"  Placebo: November-skip vs DCA: terminal difference "
            f"{_fmt_signed_money(placebo.terminal - dca.terminal)}."
        ),
        (
            f"  Shift: Aug->Oct difference "
            f"{_fmt_signed_money(shift_oct.terminal - dca.terminal)}; "
            f"Aug->Nov difference "
            f"{_fmt_signed_money(shift_nov.terminal - dca.terminal)}."
        ),
        (
            f"  Sit-out (secondary): sit-out Aug vs DCA: terminal difference "
            f"{_fmt_signed_money(sit.terminal - dca.terminal)}."
        ),
        "  Trading: no actionable seasonal strategy is established.",
    ]
    lines += _interpretation()
    return lines


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="e1f seasonality",
        description=(
            "Calendar-month seasonality analysis "
            "(ADR-0026 / ADR-0027 / ADR-0028). "
            "All twelve months; no month is special."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
A month can look bad without being a season, and a season can be real without
being a strategy. --isin is one fund; --portfolio is the configured universe
(consensus + cross-sectional permutation + equal-weight book). --evaluate
scores the frozen August/November contribution rules against DCA.

  e1f seasonality --isin IE00B3YLTY66
  e1f seasonality --portfolio
  e1f seasonality --isin IE00B3YLTY66 --evaluate
  e1f seasonality --isin IE00B3YLTY66 --rule avoid-month --month 9
  e1f seasonality --isin IE00B3YLTY66 --rule historical-weakest \\
      --training-from 2011-01-01 --training-to 2019-01-01 \\
      --test-from 2019-01-01 --test-to 2026-01-01
""",
    )
    parser.add_argument("--isin", help="One ETF to analyse (mutually exclusive with --portfolio)")
    parser.add_argument(
        "--portfolio", action="store_true",
        help="Consensus + cross-sectional test over configured ETFs with prices (ADR-0027)",
    )
    parser.add_argument(
        "--evaluate", action="store_true",
        help=(
            "Score frozen August/November contribution rules vs DCA "
            "(ADR-0028; requires --isin)"
        ),
    )
    parser.add_argument(
        "--from", dest="from_date", type=_valid_date, metavar="YYYY-MM-DD",
        help="Window start (default: series start; the first partial month is excluded)",
    )
    parser.add_argument(
        "--to", type=_valid_date, default=_TODAY, metavar="YYYY-MM-DD",
        help="Window end (default today); a month that has not elapsed is excluded",
    )
    parser.add_argument(
        "--price-mode", choices=("total-return", "price"), default="total-return",
        help="Return definition (default total-return; requires Accumulating)",
    )
    parser.add_argument(
        "--permutations", type=_positive_int, default=DEFAULT_PERMUTATIONS, metavar="N",
        help=f"Label shuffles for the permutation test (default {DEFAULT_PERMUTATIONS})",
    )
    parser.add_argument(
        "--seed", type=int, default=DEFAULT_SEED, metavar="N",
        help=f"RNG seed for permutations (default {DEFAULT_SEED})",
    )
    parser.add_argument(
        "--rule", choices=[r.value for r in Rule],
        help="Pre-specified seasonal policy (omit for descriptive + statistical only)",
    )
    parser.add_argument(
        "--month", type=_valid_month, metavar="M",
        help="Calendar month 1-12 for --rule avoid-month / sit-out-month (not a table filter)",
    )
    parser.add_argument(
        "--training-from", type=_valid_date, metavar="YYYY-MM-DD",
        help="Training window start (historical-weakest* or --evaluate split)",
    )
    parser.add_argument(
        "--training-to", type=_valid_date, metavar="YYYY-MM-DD",
        help="Training window end (required with historical-weakest*)",
    )
    parser.add_argument(
        "--test-from", type=_valid_date, metavar="YYYY-MM-DD",
        help="Test window start (required with historical-weakest*)",
    )
    parser.add_argument(
        "--test-to", type=_valid_date, metavar="YYYY-MM-DD",
        help="Test window end (required with historical-weakest*)",
    )
    parser.add_argument(
        "--cash-rate", type=_nonneg, default=0.0, metavar="RATE",
        help="Annual return on idle cash, e.g. 0.03 (default 0)",
    )
    parser.add_argument(
        "--contribution", type=_positive, default=DEFAULT_CONTRIBUTION,
        help=f"Fixed monthly contribution in EUR (default {DEFAULT_CONTRIBUTION:g})",
    )
    parser.add_argument("--db", "-d", default=DEFAULT_DB, help="Database file path")
    parser.add_argument("--config", "-c", default=DEFAULT_CONFIG, help="ETF universe config")
    parser.add_argument(
        "--currency-meta", default=DEFAULT_CURRENCY_META, help="Pinned currency metadata YAML",
    )
    parser.add_argument(
        "--explain", action="store_true", help="Add the provenance block (ADR-0014)",
    )
    return parser


def _validate_args(args: argparse.Namespace) -> str | None:
    if args.evaluate:
        if args.portfolio:
            return "--evaluate is single-ISIN only; do not pass --portfolio"
        if not args.isin:
            return "--evaluate requires --isin"
        if args.rule:
            return "--evaluate is the frozen August/November battery; do not pass --rule"
        if args.month is not None:
            return "--evaluate freezes August/November; do not pass --month"
        has_train = args.training_from is not None or args.training_to is not None
        has_test = args.test_from is not None or args.test_to is not None
        if has_train and None in (
            args.training_from, args.training_to, args.test_from, args.test_to,
        ):
            return (
                "--evaluate with a split requires --training-from/--training-to "
                "and --test-from/--test-to"
            )
        if has_test and not has_train and (args.test_from is None or args.test_to is None):
            return "--evaluate --test-from requires --test-to"
        return None
    if bool(args.isin) == bool(args.portfolio):
        return "exactly one of --isin or --portfolio is required"
    if args.portfolio and args.rule:
        return "--rule is single-ISIN only; use --isin to evaluate a frozen month"
    rule = args.rule
    if args.month is not None and rule is None:
        return "--month requires --rule (it is not a filter on the twelve-month table)"
    if rule in (Rule.AVOID_MONTH.value, Rule.SIT_OUT_MONTH.value) and args.month is None:
        return f"--rule {rule} requires --month (1-12)"
    if rule in (Rule.HISTORICAL_WEAKEST.value, Rule.HISTORICAL_WEAKEST_SIT_OUT.value):
        if args.month is not None:
            return f"--rule {rule} discovers the month; do not pass --month"
        needed = (
            args.training_from, args.training_to, args.test_from, args.test_to,
        )
        if any(v is None for v in needed):
            return (
                f"--rule {rule} requires --training-from/--training-to "
                "and --test-from/--test-to"
            )
    return None


def _price_mode_or_raise(args: argparse.Namespace, distribution: str) -> None:
    if args.price_mode != "total-return":
        return
    if distribution != "Accumulating":
        label = distribution or "unknown"
        raise SeasonalityError(
            f"{args.isin} distribution is {label}; --price-mode total-return "
            "requires an Accumulating share class (no dividend ledger is stored). "
            "Use --price-mode price for a diagnostic run."
        )


def _run_strategies(
    dates: list[str],
    closes: list[float],
    returns: list[MonthReturn],
    contribution: float,
    cash_rate: float,
    selected_month: int,
    sit_out: bool,
) -> list[StrategyResult]:
    complete = {(r.year, r.month) for r in returns}
    fills = strategy_fills(dates, complete)
    if not fills:
        raise SeasonalityError("no contribution fills fall inside the complete-month sample")
    kinds: list[tuple[DeployKind, str]]
    if sit_out:
        kinds = [
            (DeployKind.DCA, "A  constant-DCA"),
            (DeployKind.SIT_OUT, f"B  sit-out {_month_name(selected_month)}"),
        ]
    else:
        kinds = [
            (DeployKind.DCA, "A  constant-DCA"),
            (DeployKind.AVOID, f"B  avoid {_month_name(selected_month)}"),
            (DeployKind.AVOID_DRAG, f"C  cash-drag {_month_name(selected_month)}"),
        ]
    results = [
        simulate_seasonal(
            dates, closes, contribution, cash_rate, kind, selected_month, fills, label,
        )
        for kind, label in kinds
    ]
    for result in results:
        if not invariance_holds(result, cash_rate=cash_rate):
            raise SeasonalityError(
                f"invariance broken for {result.label}: "
                f"N·C={result.total_contributed:g} vs "
                f"equity_cost+cash={result.equity_cost + result.cash:g}"
            )
    return results


def _oos_or_refuse(
    dates: list[str],
    closes: list[float],
    args: argparse.Namespace,
) -> tuple[
    int | None,
    list[MonthReturn] | None,
    list[StrategyResult] | None,
    str | None,
    dict[str, str] | None,
]:
    """Discover the weakest training month and evaluate it on the test window."""
    sit_out = args.rule == Rule.HISTORICAL_WEAKEST_SIT_OUT.value
    train, _ = complete_month_returns(
        dates, closes, args.training_from, args.training_to,
    )
    test, _ = complete_month_returns(
        dates, closes, args.test_from, args.test_to,
    )
    if windows_overlap(train, test):
        return None, None, None, "training and test complete months overlap", None
    train_stats = month_stats(train)
    if not inferential_floor_met(train_stats, SEASONALITY_MIN_N_INFER):
        return None, None, None, (
            f"training window has a month with N<{SEASONALITY_MIN_N_INFER}"
        ), None
    test_stats = month_stats(test)
    if not inferential_floor_met(test_stats, SEASONALITY_MIN_N_OOS):
        return None, None, None, (
            f"test window has a month with N<{SEASONALITY_MIN_N_OOS}"
        ), None
    picked = weakest_mean_month(train_stats)
    if picked is None:
        return None, None, None, "training window has no complete months", None
    results = _run_strategies(
        dates, closes, test, args.contribution, args.cash_rate, picked, sit_out,
    )
    oos = {
        "training": f"{args.training_from}…{args.training_to}",
        "test": f"{args.test_from}…{args.test_to}",
        "month": _month_name(picked),
    }
    return picked, test, results, None, oos


def _cmd_portfolio(args: argparse.Namespace) -> int:
    config = ConfigManager(args.config)
    catalog = {row[0] for row in price_catalog(args.db)}
    if not catalog:
        raise SeasonalityError("no price series stored — run 'e1f fetch' first")
    configured = [isin for isin, _ in config.list()]
    universe = [isin for isin in configured if isin in catalog] if configured else sorted(catalog)
    if not universe:
        raise SeasonalityError(
            "no configured ETF has a stored price series — "
            "add ISINs with e1f config / e1f fetch"
        )

    funds: list[FundSeasonality] = []
    for isin in universe:
        cfg = config.get(isin) or {}
        name = str(cfg.get("name") or isin)
        distribution = str(cfg.get("distribution") or "")
        if args.price_mode == "total-return" and distribution != "Accumulating":
            funds.append(
                _fund_from_returns(
                    isin, name, distribution, "?", [],
                    skip_reason=(
                        f"distribution is {distribution or 'unknown'}; "
                        "total-return requires Accumulating"
                    ),
                )
            )
            continue
        dates, closes, currency = eur_series(args.db, isin, args.to, args.currency_meta)
        if not dates:
            funds.append(
                _fund_from_returns(
                    isin, name, distribution, currency, [],
                    skip_reason=f"no EUR/{currency} FX rate stored up to {args.to}",
                )
            )
            continue
        returns, _partials = complete_month_returns(
            dates, closes, args.from_date, args.to,
        )
        if not returns:
            funds.append(
                _fund_from_returns(
                    isin, name, distribution, currency, [],
                    skip_reason="no complete calendar months in the window",
                )
            )
            continue
        funds.append(_fund_from_returns(isin, name, distribution, currency, returns))

    consensus = consensus_rows(funds)
    infer = [f for f in funds if f.infer_ok]
    cross = cross_sectional_permutation(infer, args.permutations, args.seed)
    lines = _portfolio_report(
        funds, consensus, cross, args.price_mode, args.permutations, args.seed,
    )
    if args.explain:
        lines.append("")
        n_per = ", ".join(
            f"{f.isin}={f.n_months}" for f in infer
        ) or "none"
        lines += _explain_metric(
            "Portfolio seasonality consensus",
            Status.CALCULATED if infer else Status.UNAVAILABLE,
            (
                f"{len(infer)} inferential funds; "
                + (
                    f"strongest concentration {_month_name(cross.top_strongest_month)} "
                    f"{cross.top_strongest_count}/{cross.n_funds}"
                    if cross else "no cross-sectional test"
                )
            ),
            (
                f"configured ISINs with prices; requested "
                f"{args.from_date or 'series start'}...{args.to}"
            ),
            (
                "per-fund month-end EUR returns; consensus of fund-level monthly means; "
                "within-fund label shuffle; max-concentration add-one p-value "
                "(raw per-month count p is not the headline)"
            ),
            SEASONALITY_CONTRACT,
        )
        lines += [
            f"    Observations/fund: {n_per}",
            f"    Permutations:       {args.permutations}  (seed {args.seed})",
            "    Cohorts:            inferential vs DESCRIPTIVE - insufficient history",
            "    Layers:             descriptive consensus + cross-sectional "
            "permutation + equal-weight book",
            "    No test leakage:    --portfolio does not evaluate a trading rule",
            "    Independence:       correlated funds are not independent replications",
        ]
    print("\n".join(lines))
    return 0


def _cmd_seasonality(args: argparse.Namespace) -> int:
    if args.portfolio:
        return _cmd_portfolio(args)
    if args.evaluate:
        return _cmd_evaluate(args)
    return _cmd_one(args)


def _load_one_series(
    args: argparse.Namespace,
) -> tuple[str, str, str, list[str], list[float]]:
    """``(name, distribution, currency, dates, closes)`` for ``args.isin``."""
    config = ConfigManager(args.config)
    catalog_isins = {row[0] for row in price_catalog(args.db)}
    if not catalog_isins:
        raise SeasonalityError("no price series stored — run 'e1f fetch' first")
    if args.isin not in catalog_isins:
        raise SeasonalityError(
            f"no stored price series for {args.isin}. Available series:\n"
            f"{_candidate_listing(args.db, config)}"
        )
    cfg = config.get(args.isin) or {}
    name = str(cfg.get("name") or args.isin)
    distribution = str(cfg.get("distribution") or "")
    _price_mode_or_raise(args, distribution)
    dates, closes, currency = eur_series(args.db, args.isin, args.to, args.currency_meta)
    if not dates:
        raise SeasonalityError(
            f"{args.isin} is priced in {currency} but no EUR/{currency} FX rate "
            f"is stored up to {args.to} — fetch the pair, or pick a "
            "EUR/USD-priced series."
        )
    return name, distribution, currency, dates, closes


def _cmd_evaluate(args: argparse.Namespace) -> int:
    name, distribution, currency, dates, closes = _load_one_series(args)
    windows: list[EvalWindow] = []
    if args.test_from and args.test_to and args.training_from and args.training_to:
        train, _ = complete_month_returns(
            dates, closes, args.training_from, args.training_to,
        )
        test, _ = complete_month_returns(
            dates, closes, args.test_from, args.test_to,
        )
        if not train:
            raise SeasonalityError("discovery-era window has no complete months")
        if not test:
            raise SeasonalityError("holdout window has no complete months")
        if windows_overlap(train, test):
            raise SeasonalityError("training and test complete months overlap")
        windows.append(
            _make_eval_window(
                "Discovery-era window (not a test)",
                "discovery-era (not a test)",
                dates, closes, train, args.contribution, args.cash_rate,
            )
        )
        windows.append(
            _make_eval_window(
                "Holdout window",
                _holdout_label(
                    args.training_from, args.training_to,
                    args.test_from, args.test_to,
                ),
                dates, closes, test, args.contribution, args.cash_rate,
            )
        )
    elif args.test_from and args.test_to:
        test, _ = complete_month_returns(
            dates, closes, args.test_from, args.test_to,
        )
        if not test:
            raise SeasonalityError("holdout window has no complete months")
        windows.append(
            _make_eval_window(
                "Holdout window",
                _holdout_label(None, None, args.test_from, args.test_to),
                dates, closes, test, args.contribution, args.cash_rate,
            )
        )
    else:
        returns, _ = complete_month_returns(
            dates, closes, args.from_date, args.to,
        )
        if not returns:
            raise SeasonalityError(
                f"{args.isin} has no complete calendar months in "
                f"{args.from_date or 'series start'}…{args.to}"
            )
        windows.append(
            _make_eval_window(
                "In-sample window",
                "in-sample (requested window)",
                dates, closes, returns, args.contribution, args.cash_rate,
            )
        )

    lines = _evaluate_report(
        args.isin, name, args.price_mode, distribution, currency,
        windows, args.cash_rate, args.contribution,
    )
    if args.explain:
        lines.append("")
        headline = windows[-1]
        skip = next(r for r in headline.results if r.label == "August-skip")
        dca = headline.results[0]
        lines += _explain_metric(
            "Frozen seasonal evaluation",
            Status.CALCULATED,
            (
                f"August-skip vs DCA terminal difference "
                f"{_fmt_signed_money(skip.terminal - dca.terminal)} "
                f"on the {headline.label}"
            ),
            (
                f"ISIN {args.isin}; frozen weak="
                f"{_month_name(FROZEN_WEAK_MONTH)} strong="
                f"{_month_name(FROZEN_STRONG_MONTH)}; "
                f"requested {args.from_date or 'series start'}...{args.to}"
            ),
            (
                "contribution-level skip/shift/sit-out vs constant-DCA; "
                "months are module constants, not selected on this window; "
                "isolated 12-fill years for the scorecard"
            ),
            SEASONALITY_CONTRACT,
        )
        lines += [
            f"    Frozen months:      weak={_month_name(FROZEN_WEAK_MONTH)} "
            f"strong={_month_name(FROZEN_STRONG_MONTH)}",
            f"    Contribution:       {args.contribution:g}  "
            f"cash-rate={args.cash_rate:g}",
            "    No test leakage:    months are not re-selected on this ISIN",
            "    Holdout honesty:    freeze used the ADR-0027 full-sample book",
        ]
    print("\n".join(lines))
    return 0


def _cmd_one(args: argparse.Namespace) -> int:
    name, distribution, currency, dates, closes = _load_one_series(args)

    returns, partials = complete_month_returns(dates, closes, args.from_date, args.to)
    if not returns:
        raise SeasonalityError(
            f"{args.isin} has no complete calendar months in "
            f"{args.from_date or 'series start'}…{args.to}"
        )

    stats = month_stats(returns)
    infer_ok = inferential_floor_met(stats, SEASONALITY_MIN_N_INFER)
    perm: PermutationResult | None = None
    floor_reason: str | None = None
    if infer_ok:
        perm = permutation_test(returns, args.permutations, args.seed)
    else:
        short = [s for s in stats if s.n < SEASONALITY_MIN_N_INFER]
        floor_reason = (
            f"need ≥{SEASONALITY_MIN_N_INFER} observations in every month; "
            + ", ".join(f"{_month_name(s.month)} N={s.n}" for s in short)
        )

    strategy: list[StrategyResult] | None = None
    oos_month: int | None = None
    oos_refused: str | None = None
    oos_meta: dict[str, str] | None = None
    selected_month = args.month
    rule_label = args.rule

    if args.rule in (Rule.AVOID_MONTH.value, Rule.SIT_OUT_MONTH.value):
        strategy = _run_strategies(
            dates, closes, returns, args.contribution, args.cash_rate,
            args.month, args.rule == Rule.SIT_OUT_MONTH.value,
        )
    elif args.rule in (Rule.HISTORICAL_WEAKEST.value, Rule.HISTORICAL_WEAKEST_SIT_OUT.value):
        oos_month, _test, strategy, oos_refused, oos_meta = _oos_or_refuse(
            dates, closes, args,
        )
        selected_month = oos_month
        if oos_refused:
            strategy = None

    lines = _header(
        args.isin, name, args.price_mode, distribution, currency, returns, partials,
        infer_ok,
    )
    lines += _stats_table(stats, infer_ok)
    lines += _rankings(stats)
    lines += _best_worst_obs(stats)
    lines += _statistical_block(stats, perm, floor_reason)
    if args.rule is None:
        lines += _cash_sketch(args.cash_rate)
    if strategy is not None and selected_month is not None:
        lines += _strategy_table(
            strategy, strategy[0], args.cash_rate, selected_month,
            rule_label or "", oos=oos_meta is not None,
        )
    elif oos_refused:
        lines += ["", f"Out-of-sample rule: UNAVAILABLE — {oos_refused}"]
    lines += _findings(
        stats, perm, strategy, oos_month=oos_month, oos_refused=oos_refused,
    )
    lines += _interpretation()
    if args.explain:
        lines += _explain_block(
            isin=args.isin,
            price_mode=args.price_mode,
            distribution=distribution,
            currency=currency,
            window_from=args.from_date,
            window_to=args.to,
            returns=returns,
            stats=stats,
            perm=perm,
            rule=args.rule,
            selected_month=selected_month,
            oos=oos_meta,
            cash_rate=args.cash_rate,
            contribution=args.contribution,
            infer_ok=infer_ok,
        )
    print("\n".join(lines))
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    err = _validate_args(args)
    if err:
        _build_parser().error(err)
    try:
        return _cmd_seasonality(args)
    except SeasonalityError as exc:
        print(f"✗ {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
