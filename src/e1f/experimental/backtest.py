#!/usr/bin/env python
"""e1f backtest — contribution-timing backtest over one ETF's real history (ADR-0019).

Runs pre-specified contribution strategies (constant DCA, a dip-reserve rule, and
lump-sum / cash-drag benchmarks) over one ETF's EUR daily-close history and reports
terminal wealth, money-weighted return (XIRR), and interim drawdown per strategy —
holding the total contributed identical across strategies (a reserve model), so the
comparison measures *timing*, not size. Evaluator only: strategies are never fitted
or ranked in-sample (walk-forward deferred to v2).

Usage:
    e1f backtest --isin IE00B3YLTY66
    e1f backtest --isin IE00B3YLTY66 --strategy "a=5,b=2" --strategy "a=10,b=2"
    e1f backtest --isin IE00B3YLTY66 --window 120 --explain
"""

import argparse
import bisect
import sqlite3
import statistics
import sys
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, date, datetime

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
)
from e1f.experimental.common import (
    BACKTEST_MIN_CONTRIBUTIONS,
    BacktestResult,
    DeployMode,
    SignalSpec,
    StrategyParams,
    monthly_fill_indices,
    simulate_strategy,
)

_NAME_W = 22
_MONEY_W = 12
_TODAY = datetime.now(UTC).date().isoformat()

# Known global-equity crash windows, reported (not special-cased in the math) so
# every run states which fall inside its span and which are excluded (ADR-0019 §8).
_CRASHES: tuple[tuple[str, str, str], ...] = (
    ("dot-com 2000-2002", "2000-03-24", "2002-10-09"),
    ("GFC 2007-2009", "2007-10-09", "2009-03-09"),
    ("COVID 2020", "2020-02-19", "2020-03-23"),
    ("2022 bear", "2022-01-03", "2022-10-12"),
)

# Below this many overlapping windows the distribution is illustrative, not significant.
_WINDOW_ILLUSTRATIVE_THRESHOLD = 24

# Investment horizons whose availability is reported so the 10-20y question is explicit.
_HORIZONS_YEARS = (10, 15, 20)


BACKTEST_CONTRACT = MetricContract(
    method_version="contribution_timing_backtest_v1",
    requires=("a daily EUR close series spanning the lookback + a minimum contribution count",),
    does_not_require=(
        "return forecasts",
        "look-through holdings",
        "a covariance estimate",
        "synthetic or proxy history",
    ),
    supports=(
        "terminal wealth & XIRR per strategy",
        "excess vs constant-DCA",
        "rolling-window outcome distribution",
    ),
    limitations=(
        "one real ETF price series — no proxy/synthetic history (v1; proxy deferred to v2)",
        "reserve earns 0% by default (--cash-rate to sensitivity-test)",
        "no fees/taxes/spread; fractional shares; monthly fills",
        "evaluator only — strategies are pre-specified, never fitted or ranked in-sample "
        "(walk-forward deferred to v2)",
        "distributing funds understate total return — prefer an accumulating series",
        "overlapping windows are not independent; small N is disclosed, not overcome",
    ),
)


class BacktestError(Exception):
    """A usage/data problem that stops a run with a message (never a stack trace)."""


@dataclass(frozen=True)
class BacktestSpan:
    """Semantic decomposition of the effective test start."""

    signal_warmup_closes: int
    from_index: int
    start_index: int


# ---------------------------------------------------------------------------
# Formatting — copied locally, exactly as the sibling commands do (importing a
# sibling's formatter would break ADR-0003's layer contract).
# ---------------------------------------------------------------------------


def _fmt_money(value: float | None) -> str:
    return "n/a" if value is None else f"{value:,.2f}"


def _fmt_signed_money(value: float | None) -> str:
    return "n/a" if value is None else f"{value:+,.2f}"


def _fmt_pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100.0:.1f}%"


def _fmt_signed_pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100.0:+.1f}%"


# ---------------------------------------------------------------------------
# EUR daily-close series assembly (native close × nearest-prior EUR/FX).
# ---------------------------------------------------------------------------


def _fx_series(db_path: str, quote: str) -> tuple[list[str], list[float]]:
    """All stored EUR→``quote`` rates, sorted by date (quote units per 1 EUR)."""
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
    """``(dates, eur_closes, currency)`` for an ISIN up to ``as_of``.

    EUR funds pass through; a foreign fund converts each close at the nearest-prior
    stored EUR/FX rate. A day preceding the FX series (no usable rate) is dropped —
    an as-of valuation must never use a later rate (ADR-0010).
    """
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
            continue  # no rate on/before this day — cannot value it
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
    """Human-readable candidate list for the missing/unknown-ISIN error."""
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
# Strategy construction — benchmarks + configurable dip strategies.
# ---------------------------------------------------------------------------

# Float knobs routed through _PARAM_VALIDATORS; deploy/delay/seed/label handled apart.
_STRATEGY_KEYS = {
    "beta": "base_fraction", "base": "base_fraction",
    "a": "aggressiveness", "agg": "aggressiveness",
    "b": "curvature", "curv": "curvature",
    "d0": "deadzone", "deadzone": "deadzone",
}
_DEPLOY_MODES = {m.value: m for m in DeployMode}   # signal / even / delayed / random


def _dip_label(base: float, agg: float, curv: float, d0: float) -> str:
    tail = f",D0={d0:g}" if d0 else ""
    return f"dip(β={base:g},a={agg:g},b={curv:g}{tail})"


def _blind_label(mode: DeployMode, base: float, delay: int, seed: int | None) -> str:
    if mode == DeployMode.EVEN:
        return f"blind-even(β={base:g})"
    if mode == DeployMode.DELAYED:
        return f"blind-delayed(β={base:g},L={delay:g})"
    return f"blind-random(β={base:g},seed={seed})"


def _daily_dip_label(slices: int) -> str:
    return f"daily-dip(N={slices})"


def _daily_dip_carry_label(slices: int) -> str:
    return f"daily-dip-carry(N={slices})"


# Both within-month daily cores hold no cross-month reserve (ADR-0021/0023), so
# neither gets β-matched controls and both count as dip rows in --window.
_DAILY_DIP_MODES = (DeployMode.DAILY_DIP, DeployMode.DAILY_DIP_CARRY)


def _strategy_int(key: str, raw: str, *, nonneg: bool = False) -> int:
    try:
        value = int(raw)
    except ValueError as exc:
        raise BacktestError(f"--strategy: {key}={raw!r} is not an integer") from exc
    if nonneg and value < 0:
        raise BacktestError(f"--strategy: {key} must be ≥ 0: {raw}")
    return value


def parse_strategy(spec: str, defaults: dict[str, float]) -> StrategyParams:
    """Parse a ``k=v,k=v`` ``--strategy`` spec, falling back to the top-level knob defaults."""
    fields: dict[str, float | str] = dict(defaults)
    label: str | None = None
    deploy = DeployMode.SIGNAL
    delay_months = 0
    seed: int | None = None
    slices = int(defaults.get("slices", 20))
    for token in spec.split(","):
        token = token.strip()
        if not token:
            continue
        if "=" not in token:
            raise BacktestError(f"--strategy term {token!r} is not key=value")
        key, _, raw = token.partition("=")
        key = key.strip().lower()
        raw = raw.strip()
        if key == "label":
            label = raw
            continue
        if key == "deploy":
            if raw not in _DEPLOY_MODES:
                raise BacktestError(
                    f"--strategy: deploy must be one of {', '.join(_DEPLOY_MODES)}"
                )
            deploy = _DEPLOY_MODES[raw]
            continue
        if key == "delay":
            delay_months = _strategy_int("delay", raw, nonneg=True)
            continue
        if key == "seed":
            seed = _strategy_int("seed", raw)
            continue
        if key in ("slices", "n"):
            slices = _strategy_int("slices", raw)
            if slices < 1:
                raise BacktestError(f"--strategy: slices must be ≥ 1: {raw}")
            continue
        if key not in _STRATEGY_KEYS:
            raise BacktestError(
                f"--strategy: unknown key {key!r} "
                "(use beta/base, a/agg, b/curv, d0/deadzone, deploy, delay, seed, slices/n, label)"
            )
        dest = _STRATEGY_KEYS[key]
        # Route the value through the SAME validator argparse applies to the matching
        # top-level knob, so a value the CLI rejects (beta=2, a=-10, b=0, d0=1.5)
        # can't slip through --strategy either (review P1).
        try:
            fields[dest] = _PARAM_VALIDATORS[dest](raw)
        except argparse.ArgumentTypeError as exc:
            raise BacktestError(f"--strategy: {key} {exc}") from exc
        except ValueError as exc:
            raise BacktestError(f"--strategy: {key}={raw!r} is not a number") from exc
    base = float(fields["base_fraction"])
    agg = float(fields["aggressiveness"])
    curv = float(fields["curvature"])
    d0 = float(fields["deadzone"])
    if deploy == DeployMode.RANDOM and seed is None:
        seed = 0   # a bare deploy=random is still reproducible run-to-run
    if deploy == DeployMode.DAILY_DIP:
        auto = _daily_dip_label(slices)
    elif deploy == DeployMode.DAILY_DIP_CARRY:
        auto = _daily_dip_carry_label(slices)
    elif deploy == DeployMode.SIGNAL:
        auto = _dip_label(base, agg, curv, d0)
    else:
        auto = _blind_label(deploy, base, delay_months, seed)
    return StrategyParams(
        label=label or auto,
        base_fraction=base, aggressiveness=agg, curvature=curv, deadzone=d0,
        deploy=deploy, delay_months=delay_months, seed=seed, slices=slices,
    )


def build_strategies(args: argparse.Namespace) -> list[StrategyParams]:
    """lump-sum, constant-DCA, then a matched cash-drag + blind-even (ADR-0020) for
    each distinct β among the dips, then the dips themselves."""
    defaults = {
        "base_fraction": args.base_fraction,
        "aggressiveness": args.aggressiveness,
        "curvature": args.curvature,
        "deadzone": args.deadzone,
        "slices": args.slices,
    }
    if args.strategy:
        dips = [parse_strategy(spec, defaults) for spec in args.strategy]
    else:
        dips = [parse_strategy("", defaults)]  # one dip from the top-level knobs
    benchmarks = [
        StrategyParams("lump-sum", 1.0, 0.0, 1.0, 0.0, lump_sum=True),
        StrategyParams("constant-DCA", 1.0, 0.0, 1.0, 0.0),
    ]
    # Every comparison holds reserve size (β) constant, so each distinct dip β gets
    # its own matched controls — never one top-level cash-drag standing in for all.
    # Daily-dip strategies hold no reserve (ADR-0021/0023), so they get no β controls.
    reserve_dips = [d for d in dips if d.deploy not in _DAILY_DIP_MODES]
    controls: list[StrategyParams] = []
    for beta in _distinct_betas(reserve_dips):
        controls.append(StrategyParams(f"cash-drag(β={beta:g})", beta, 0.0, 1.0, 0.0))
        controls.append(
            StrategyParams(f"blind-even(β={beta:g})", beta, 0.0, 1.0, 0.0, deploy=DeployMode.EVEN)
        )
    _reject_label_collisions([*benchmarks, *controls], dips)
    return [*benchmarks, *controls, *dips]


def _distinct_betas(dips: list[StrategyParams]) -> list[float]:
    """The β values present among the dips, in first-appearance order."""
    betas: list[float] = []
    for d in dips:
        if d.base_fraction not in betas:
            betas.append(d.base_fraction)
    return betas


def _reject_label_collisions(
    benchmarks: list[StrategyParams], dips: list[StrategyParams]
) -> None:
    """Reject a dip label that shadows a benchmark or repeats another dip.

    Labels are user-settable presentation metadata, never identity — so a dip
    labelled ``constant-DCA`` (or two dips sharing a label) must error rather than
    silently produce a misleading row or, worse, let a label-based lookup pick the
    wrong series (review P1).
    """
    reserved = {b.label for b in benchmarks}
    seen: set[str] = set()
    for d in dips:
        if d.label in reserved:
            raise BacktestError(f"--strategy: label {d.label!r} is reserved (benchmark)")
        if d.label in seen:
            raise BacktestError(f"--strategy: duplicate label {d.label!r}")
        seen.add(d.label)


# ---------------------------------------------------------------------------
# Window arithmetic.
# ---------------------------------------------------------------------------


def _warmup_idx(n: int, signal: SignalSpec) -> int:
    """First index at which a full signal window exists (0 for ATH)."""
    if signal.lookback is None:
        return 0
    return signal.lookback - 1


def _needs_warmup(strategies: list[StrategyParams]) -> bool:
    """True if any strategy consults the drawdown signal (ADR-0021 §5).

    Only a signal-mode dip needs the lookback warm-up; daily-dip and the blind /
    benchmark rows do not, so a run without a signal dip starts at the series start.
    """
    return any(
        s.deploy == DeployMode.SIGNAL and s.aggressiveness > 0.0 and not s.lump_sum
        for s in strategies
    )


def _backtest_span(
    dates: list[str],
    strategies: list[StrategyParams],
    signal: SignalSpec,
    from_date: str | None,
) -> BacktestSpan:
    """Separate signal warm-up from an explicit ``--from`` constraint."""
    warmup = _warmup_idx(len(dates), signal) if _needs_warmup(strategies) else 0
    from_index = _from_idx(dates, from_date)
    return BacktestSpan(
        signal_warmup_closes=warmup,
        from_index=from_index,
        start_index=max(warmup, from_index),
    )


def _from_idx(dates: list[str], from_date: str | None) -> int:
    return 0 if from_date is None else bisect.bisect_left(dates, from_date)


# ---------------------------------------------------------------------------
# Rendering.
# ---------------------------------------------------------------------------


def _years(first: str, last: str) -> float:
    return (date.fromisoformat(last) - date.fromisoformat(first)).days / 365.25


def _crash_split(first: str, last: str) -> tuple[list[str], list[str]]:
    """Crash windows overlapping ``[first, last]`` (marked ~ if only partial) vs excluded."""
    included, excluded = [], []
    for name, cstart, cend in _CRASHES:
        if cend >= first and cstart <= last:
            partial = cstart < first or cend > last
            included.append(f"{name}{'~' if partial else ''}")
        else:
            excluded.append(name)
    return included, excluded


def _signal_label(signal: SignalSpec) -> str:
    if signal.lookback is None:
        return "drawdown vs all-time high"
    return f"drawdown vs {signal.lookback}d rolling high"


def _horizons_line(years: float) -> str:
    """Availability of each fixed investment horizon over a span of ``years``."""
    marks = " · ".join(f"{h}y {'✓' if years >= h else '✗'}" for h in _HORIZONS_YEARS)
    return f"Horizons:      {marks}"


def _window_horizon_line(n_fills: int) -> str:
    """Feasible window counts for each fixed horizon (one fill ≈ one month).

    Labelled "Feasible contribution windows" — deliberately *not* "Horizons" —
    because these are contribution-count windows, not the settled calendar-horizon
    definition the single-run line reports (review #4 / ADR-0019 §8).
    """
    counts = " · ".join(
        f"{h * 12}-month: {max(0, n_fills - h * 12 + 1)}" for h in _HORIZONS_YEARS
    )
    return f"Feasible contribution windows: {counts}"


def _window_crash_coverage(
    starts: list[str], ends: list[str], n_windows: int
) -> list[str]:
    """Per-crash count of windows whose ``[start, end]`` test interval overlaps it.

    Uses the same overlap rule as ``_crash_split``; every crash is listed (a 0/N
    line makes an absent crash — e.g. dot-com for a post-2011 series — impossible
    to miss). Full vs partial exposure is broken out only when partials occur.
    """
    lines = ["Crashes:  coverage across windows (test interval overlaps the crash):"]
    for cname, cstart, cend in _CRASHES:
        full = partial = 0
        for ws, we in zip(starts, ends, strict=True):
            if cend >= ws and cstart <= we:  # overlap
                if cstart >= ws and cend <= we:
                    full += 1
                else:
                    partial += 1
        exposed = full + partial
        tail = f"  ({full} full, {partial} partial)" if partial else ""
        lines.append(f"  {cname:<18} {exposed:>4}/{n_windows}{tail}")
    return lines


def _run_header(
    isin: str, name: str, dates: list[str], fills: list[int],
    contribution: float, signal: SignalSpec, cash_rate: float,
    span: BacktestSpan,
) -> list[str]:
    first, last, eff = dates[0], dates[-1], dates[fills[0]]
    # Crash inclusion uses the effective TEST span (first contribution → valuation),
    # not the data span — so --from and the warm-up burn are honoured (ADR-0019 §8).
    tested, absent = _crash_split(eff, last)
    warmup = (
        f", warm-up burned {span.signal_warmup_closes} closes"
        if span.signal_warmup_closes
        else ""
    )
    return [
        f"\nContribution-timing backtest — {name} ({isin}) · {BASE_CURRENCY}",
        f"Data:          {first} → {last}  ({_years(first, last):.1f}y, {len(dates)} closes)",
        f"Test:          {eff} → {last}  ({_years(eff, last):.1f}y, {len(fills)} monthly × "
        f"€{contribution:,.0f}{warmup})",
        _horizons_line(_years(eff, last)),
        f"Crashes:       tested: {', '.join(tested) or '—'}  ·  "
        f"absent: {', '.join(absent) or '—'}",
        f"Signal:        {_signal_label(signal)}  ·  reserve cash-rate {cash_rate * 100:.1f}%",
    ]


def _table(
    results: list[BacktestResult], dca_terminal: float, show_status: bool
) -> list[str]:
    head = (
        f"\n{'Strategy':<{_NAME_W}} {'Invested€':>{_MONEY_W}} {'Equity€':>{_MONEY_W}} "
        f"{'Cash€':>{_MONEY_W}} {'Terminal€':>{_MONEY_W}} {'vsDCA€':>{_MONEY_W}} "
        f"{'vsDCA%':>8} {'XIRR':>8} {'MaxDD':>7}"
    )
    if show_status:
        head += f" {'Status':>11}"
    lines = [head, "-" * len(head)]
    for r in results:
        is_dca = r.label == "constant-DCA"
        excess = r.terminal_wealth - dca_terminal
        excess_pct = (excess / dca_terminal) if dca_terminal else None
        row = (
            f"{r.label:<{_NAME_W}} {_fmt_money(r.total_invested):>{_MONEY_W}} "
            f"{_fmt_money(r.equity_value):>{_MONEY_W}} {_fmt_money(r.reserve_cash):>{_MONEY_W}} "
            f"{_fmt_money(r.terminal_wealth):>{_MONEY_W}} "
            f"{('—' if is_dca else _fmt_signed_money(excess)):>{_MONEY_W}} "
            f"{('—' if is_dca else _fmt_signed_pct(excess_pct)):>8} "
            f"{_fmt_pct(r.xirr):>8} {_fmt_pct(r.max_drawdown):>7}"
        )
        if show_status:
            row += f" {Status.CALCULATED.value:>11}"
        lines.append(row)
    return lines


def _reserve_diagnostics(results: list[BacktestResult]) -> list[str]:
    lines = []
    for r in results:
        if r.reserve_contributed <= 0.0:
            continue
        share = r.reserve_deployed / r.reserve_contributed if r.reserve_contributed else 0.0
        lines.append(
            f"  {r.label}: reserve contributed €{r.reserve_contributed:,.2f}, "
            f"deployed €{r.reserve_deployed:,.2f} ({share * 100:.0f}%), "
            f"leftover €{r.reserve_cash:,.2f}"
        )
    return (["\nReserve use:", *lines] if lines else [])


def _signal_dips(
    strategies: list[StrategyParams], results: list[BacktestResult]
) -> list[tuple[StrategyParams, BacktestResult]]:
    """The (params, result) pairs that are actual dip strategies — signal-driven,
    reserve-holding. Blind and cash-drag controls (a=0) are excluded."""
    return [
        (s, r) for s, r in zip(strategies, results, strict=True)
        if s.deploy == DeployMode.SIGNAL and s.aggressiveness > 0.0 and not s.lump_sum
    ]


def _decomposition_block(
    strategies: list[StrategyParams], results: list[BacktestResult]
) -> list[str]:
    """Per dip, the ADR-0020 economics against its matched-β controls: reserve cost,
    deployment benefit, timing benefit (vs blind-even), total. Descriptive only."""
    dips = _signal_dips(strategies, results)
    if not dips:
        return []
    dca = next(r.terminal_wealth for r in results if r.label == "constant-DCA")
    cash_drag: dict[float, float] = {}
    blind_even: dict[float, float] = {}
    for s, r in zip(strategies, results, strict=True):
        if s.aggressiveness != 0.0 or s.lump_sum or s.label == "constant-DCA":
            continue
        (blind_even if s.deploy == DeployMode.EVEN else cash_drag)[s.base_fraction] = \
            r.terminal_wealth
    w = 15
    lines = [
        "\nDecomposition (matched β, descriptive — ADR-0020):",
        f"  {'Strategy':<{_NAME_W}} {'ReserveCost€':>{w}} {'DeployBenefit€':>{w}} "
        f"{'TimingBenefit€':>{w}} {'Total€':>{w}}",
    ]
    for s, r in dips:
        cd = cash_drag.get(s.base_fraction)
        be = blind_even.get(s.base_fraction)
        reserve_cost = None if cd is None else dca - cd
        deploy_benefit = None if (be is None or cd is None) else be - cd
        timing_benefit = None if be is None else r.terminal_wealth - be
        lines.append(
            f"  {s.label:<{_NAME_W}} {_fmt_signed_money(reserve_cost):>{w}} "
            f"{_fmt_signed_money(deploy_benefit):>{w}} {_fmt_signed_money(timing_benefit):>{w}} "
            f"{_fmt_signed_money(r.terminal_wealth - dca):>{w}}"
        )
    lines.append(
        "  reserve cost = DCA−cash-drag · deploy benefit = blind-even−cash-drag · "
        "timing benefit = dip−blind-even · total = dip−DCA"
    )
    lines.append(
        "  (timing benefit is vs FULL neutral reinvestment: blind-even redeploys the "
        "whole reserve; a dip that holds cash back is charged for it here.)"
    )
    return lines


def _percentile_of(sorted_vals: list[float], value: float) -> float:
    """Percentile of ``value`` within ``sorted_vals`` (share at or below it)."""
    return 100.0 * bisect.bisect_right(sorted_vals, value) / len(sorted_vals)


def _random_block(
    dates: list[str], closes: list[float], fills: list[int], signal: SignalSpec,
    contribution: float, cash_rate: float, strategies: list[StrategyParams],
    results: list[BacktestResult], blind_seeds: int,
) -> list[str]:
    """Supplementary robustness: the distribution of drawdown-blind RANDOM deployment
    over fixed seeds, and where each dip falls within it. Never the headline number."""
    dips = _signal_dips(strategies, results)
    if blind_seeds <= 0 or not dips:
        return []
    lines = [
        f"\nBlind-random robustness ({blind_seeds} seeds 0…{blind_seeds - 1}, "
        "drawdown-blind — supplementary):",
    ]
    for beta in _distinct_betas([s for s, _ in dips]):
        terminals = sorted(
            simulate_strategy(
                dates, closes, fills,
                StrategyParams("br", beta, 0.0, 1.0, 0.0, deploy=DeployMode.RANDOM, seed=seed),
                signal, contribution, cash_rate,
            ).terminal_wealth
            for seed in range(blind_seeds)
        )
        med = statistics.median(terminals)
        lines.append(
            f"  β={beta:g}: random terminal median {_fmt_money(med)}  "
            f"[worst {_fmt_money(terminals[0])}, best {_fmt_money(terminals[-1])}]"
        )
        for s, r in dips:
            if s.base_fraction == beta:
                pct = _percentile_of(terminals, r.terminal_wealth)
                lines.append(
                    f"    {s.label}: dip {_fmt_money(r.terminal_wealth)} → "
                    f"P{pct:.0f} of blind-random"
                )
    lines.append(
        "  (a dip near P50 is indistinguishable from blind deployment; the headline "
        "timing benefit uses deterministic blind-even, not this cloud.)"
    )
    return lines


_ANTI_OVERFIT = (
    "\nNote: strategies are pre-specified and only tabulated — never fitted or ranked "
    "on this\nhistory (ADR-0019). Out-of-sample / walk-forward evaluation is deferred to v2."
)


def _explain_block(
    isin: str, name: str, dates: list[str], fills: list[int], signal: SignalSpec,
    cash_rate: float, results: list[BacktestResult], blind_seeds: int,
) -> list[str]:
    # Descriptive, never ranked: list every strategy's terminal, pick no winner
    # (in-sample selection is exactly what ADR-0019 §6/§10 refuses).
    terminals = "; ".join(f"{r.label} €{r.terminal_wealth:,.0f}" for r in results)
    result = f"terminal values (descriptive, not ranked) — {terminals}"
    inputs = (
        f"{name} ({isin}) EUR closes — data {dates[0]}→{dates[-1]}, "
        f"test {dates[fills[0]]}→{dates[-1]} ({len(fills)} monthly contributions)"
    )
    blind = (
        "blind controls (ADR-0020) fully reinvest the reserve by the horizon; timing "
        "benefit = dip − blind-even (full neutral reinvestment)"
    )
    blind += (
        f"; blind-random over {blind_seeds} fixed seeds 0…{blind_seeds - 1}"
        if blind_seeds > 0 else "; blind-random disabled (--blind-seeds 0)"
    )
    method = (
        f"reserve model, {_signal_label(signal)}, reserve cash-rate {cash_rate * 100:.1f}%; "
        "EUR valuation: native close × nearest-prior EUR/quote FX (EUR funds pass through); "
        "reserve grows daily Actual/365; terminal = equity + leftover cash; "
        f"XIRR on monthly outflows; {blind}"
    )
    if any(
        r.label.startswith("daily-dip") and not r.label.startswith("daily-dip-carry")
        for r in results
    ):
        method += (
            "; daily-dip (ADR-0021) slices each month's C into N equal pieces bought on "
            "down days (close < prior close), catch-up + last-day rules deploy C fully "
            "inside the month, so it holds no reserve and --cash-rate does not apply to it"
        )
    if any(r.label.startswith("daily-dip-carry") for r in results):
        method += (
            "; daily-dip-carry (ADR-0023) accrues one slice per trading day and a down "
            "day spends every accrued-but-unspent slice (the last day flushes the rest), "
            "still deploying C fully inside the month with no reserve and no --cash-rate"
        )
    return [
        "\nProvenance (--explain) — reconstructed from source, not a log:",
        *_explain_metric(
            "Contribution-timing backtest", Status.CALCULATED, result, inputs, method,
            BACKTEST_CONTRACT,
        ),
    ]


# ---------------------------------------------------------------------------
# Single-run and rolling-window drivers.
# ---------------------------------------------------------------------------


def _run_single(
    isin: str, name: str, dates: list[str], closes: list[float], span: BacktestSpan,
    strategies: list[StrategyParams], signal: SignalSpec, contribution: float, cash_rate: float,
    *, blind_seeds: int, show_status: bool, explain: bool,
) -> list[str]:
    fills = monthly_fill_indices(dates, span.start_index, len(dates) - 1)
    if len(fills) < BACKTEST_MIN_CONTRIBUTIONS:
        raise BacktestError(
            f"only {len(fills)} usable monthly contributions after warm-up "
            f"(need ≥ {BACKTEST_MIN_CONTRIBUTIONS}); widen --from/--to or pick a longer series"
        )
    results = [
        simulate_strategy(dates, closes, fills, s, signal, contribution, cash_rate)
        for s in strategies
    ]
    dca_terminal = next(r.terminal_wealth for r in results if r.label == "constant-DCA")
    out = _run_header(
        isin,
        name,
        dates,
        fills,
        contribution,
        signal,
        cash_rate,
        span,
    )
    out += _table(results, dca_terminal, show_status)
    out += _reserve_diagnostics(results)
    out += _decomposition_block(strategies, results)
    out += _random_block(
        dates, closes, fills, signal, contribution, cash_rate, strategies, results, blind_seeds
    )
    if explain:
        out += _explain_block(isin, name, dates, fills, signal, cash_rate, results, blind_seeds)
    out.append(_ANTI_OVERFIT)
    return out


def _run_window(
    isin: str, name: str, dates: list[str], closes: list[float], span: BacktestSpan,
    window_months: int, strategies: list[StrategyParams], signal: SignalSpec,
    contribution: float, cash_rate: float,
) -> list[str]:
    """Sweep every feasible start; summarise excess-vs-DCA distribution per dip strategy."""
    end_idx = len(dates) - 1
    # Index the per-window results by position in ``strategies`` — never by label.
    # Labels are user-settable presentation metadata; keying the results dict by
    # them would let a dip labelled ``constant-DCA`` shadow the benchmark and corrupt
    # the comparison (review P1). ``build_strategies`` already rejects such
    # collisions; keying by identity makes the corruption impossible by construction.
    dca_idx = next(i for i, s in enumerate(strategies) if s.label == "constant-DCA")
    dip_idxs = [
        i for i, s in enumerate(strategies)
        if not s.lump_sum and i != dca_idx
        and (s.deploy in _DAILY_DIP_MODES or s.aggressiveness > 0.0)
    ]
    excess: dict[int, list[float]] = {i: [] for i in dip_idxs}
    xirr_delta: dict[int, list[float]] = {i: [] for i in dip_idxs}
    wins: dict[int, int] = {i: 0 for i in dip_idxs}
    starts: list[str] = []
    ends: list[str] = []

    # One monthly-fill list; each window is a length-``window_months`` slice of it
    # (starting one month later each step). Slicing the shared list avoids the
    # anchor-recomputation that would skip any month whose 1st is not a trading day.
    all_fills = monthly_fill_indices(dates, span.start_index, end_idx)
    for k in range(0, len(all_fills) - window_months + 1):
        window_fills = all_fills[k : k + window_months]
        # each window's own horizon end = its last fill (not the whole series end).
        # NOTE: contribution-count semantics (ADR-0019 §8 / review #1, left as-is).
        w_end = window_fills[-1]
        w_dates, w_closes = dates[: w_end + 1], closes[: w_end + 1]
        results = [
            simulate_strategy(
                w_dates, w_closes, window_fills, s, signal, contribution, cash_rate
            )
            for s in strategies
        ]
        dca = results[dca_idx]
        starts.append(dates[window_fills[0]])
        ends.append(dates[w_end])
        for i in dip_idxs:
            r = results[i]
            excess[i].append(r.terminal_wealth - dca.terminal_wealth)
            if r.xirr is not None and dca.xirr is not None:
                xirr_delta[i].append(r.xirr - dca.xirr)
            if r.terminal_wealth > dca.terminal_wealth:
                wins[i] += 1

    if not starts:
        raise BacktestError(
            f"no feasible {window_months}-month window in the series after warm-up; "
            f"pick a shorter --window or a longer series"
        )

    n_windows = len(starts)
    out = [
        f"\nRolling-window backtest — {name} ({isin}) · {BASE_CURRENCY} · "
        f"window {window_months} months",
        f"Data:     {dates[0]} → {dates[-1]}  "
        f"({_years(dates[0], dates[-1]):.1f}y, {len(dates)} closes)",
        f"Windows:  {n_windows} × {window_months}-month, step 1 month "
        f"(starts {starts[0]} → {starts[-1]})"
        + (
            f", warm-up burned {span.signal_warmup_closes} closes"
            if span.signal_warmup_closes
            else ""
        ),
        _window_horizon_line(len(all_fills)),
        *_window_crash_coverage(starts, ends, n_windows),
    ]
    if n_windows < _WINDOW_ILLUSTRATIVE_THRESHOLD:
        out.append(
            f"WARNING: only {n_windows} overlapping windows — illustrative, not significant "
            "(overlapping windows are not independent)."
        )
    head = (
        f"\n{'Strategy':<{_NAME_W}} {'Win%':>7} {'Median€':>{_MONEY_W}} "
        f"{'Worst€':>{_MONEY_W}} {'Best€':>{_MONEY_W}} {'MedΔXIRR':>9}"
    )
    out += [head, "-" * len(head)]
    for i in dip_idxs:
        label = strategies[i].label
        vals = sorted(excess[i])
        med = statistics.median(vals)
        deltas = sorted(xirr_delta[i])
        med_delta = statistics.median(deltas) if deltas else None
        out.append(
            f"{label:<{_NAME_W}} {wins[i] / n_windows * 100:>6.1f}% "
            f"{_fmt_signed_money(med):>{_MONEY_W}} {_fmt_signed_money(vals[0]):>{_MONEY_W}} "
            f"{_fmt_signed_money(vals[-1]):>{_MONEY_W}} "
            f"{(_fmt_signed_pct(med_delta) if med_delta is not None else 'n/a'):>9}"
        )
    out.append(
        "\n(vs constant-DCA per window: Win% = share of windows the dip strategy ends ahead; "
        "€ columns = excess terminal wealth; MedΔXIRR = median XIRR difference.)"
    )
    out.append(_ANTI_OVERFIT)
    return out


# ---------------------------------------------------------------------------
# CLI.
# ---------------------------------------------------------------------------


def _cmd_backtest(args: argparse.Namespace) -> int:
    config = ConfigManager(args.config)
    catalog_isins = {row[0] for row in price_catalog(args.db)}
    if not catalog_isins:
        raise BacktestError("no price series stored — run 'e1f fetch' first")
    if args.isin not in catalog_isins:
        raise BacktestError(
            f"no stored price series for {args.isin}. Available series:\n"
            f"{_candidate_listing(args.db, config)}"
        )

    dates, closes, currency = eur_series(args.db, args.isin, args.to, args.currency_meta)
    if not dates:
        raise BacktestError(
            f"{args.isin} is priced in {currency} but no EUR/{currency} FX rate is stored "
            f"up to {args.to} — fetch the pair, or pick a EUR/USD-priced series."
        )

    cfg = config.get(args.isin) or {}
    name = cfg.get("name") or args.isin
    if (cfg.get("distribution") or "").lower().startswith("dist"):
        print(
            f"⚠ {args.isin} is Distributing — its close understates total return; "
            "prefer an Accumulating series for a faithful backtest.",
            file=sys.stderr,
        )

    signal = SignalSpec(lookback=None if args.drawdown_ref == "all-time-high" else args.lookback)
    strategies = build_strategies(args)
    # The warm-up burn exists only to give the drawdown signal a full lookback
    # window; a run with no signal-consulting strategy (e.g. daily-dip only,
    # ADR-0021) needs none and starts at the series start. All strategies in one
    # run share this start, so the comparison stays controlled.
    span = _backtest_span(dates, strategies, signal, args.from_date)
    if span.start_index > len(dates) - 1:
        raise BacktestError(
            f"warm-up ({span.signal_warmup_closes} closes) / --from leaves no room "
            f"in a {len(dates)}-close series"
        )

    if args.window is not None:
        lines = _run_window(
            args.isin, name, dates, closes, span, args.window, strategies, signal,
            args.contribution, args.cash_rate,
        )
    else:
        lines = _run_single(
            args.isin, name, dates, closes, span, strategies, signal,
            args.contribution, args.cash_rate,
            blind_seeds=args.blind_seeds,
            show_status=args.show_status or args.explain, explain=args.explain,
        )
    print("\n".join(lines))
    return 0


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


def _fraction(value: str) -> float:
    f = float(value)
    if not 0.0 <= f <= 1.0:
        raise argparse.ArgumentTypeError(f"must be in [0, 1]: {value}")
    return f


def _nonneg(value: str) -> float:
    f = float(value)
    if f < 0.0:
        raise argparse.ArgumentTypeError(f"must be ≥ 0: {value}")
    return f


def _nonneg_int(value: str) -> int:
    i = int(value)
    if i < 0:
        raise argparse.ArgumentTypeError(f"must be ≥ 0: {value}")
    return i


# The dip parameters and the validators argparse applies to their top-level knobs.
# parse_strategy() reuses these SAME callables for --strategy terms, so the two
# entry points cannot diverge (review P1) — keep each entry aligned with the
# matching add_argument(type=...) below.
_PARAM_VALIDATORS: dict[str, Callable[[str], float]] = {
    "base_fraction": _fraction,
    "aggressiveness": _nonneg,
    "curvature": _positive,
    "deadzone": _fraction,
}


def _valid_date(value: str) -> str:
    try:
        date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"must be YYYY-MM-DD: {value}") from exc
    return value


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="e1f backtest",
        description="Contribution-timing backtest over one ETF's real EUR history (ADR-0019).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
Same total money, different timing. A fixed monthly contribution C is split: a base
fraction β buys shares immediately, the rest accrues to a reserve that a dip rule
deploys on drawdowns. ∑ contributions is identical across strategies, so the test
measures timing, not size. Benchmarks (lump-sum, constant-DCA, cash-drag) always show.

  e1f backtest --isin IE00B3YLTY66
  e1f backtest --isin IE00B3YLTY66 --base-fraction 0.7 --aggressiveness 8 --curvature 2
  e1f backtest --isin IE00B3YLTY66 --strategy "a=5,b=2" --strategy "a=10,b=2,label=steep"
  e1f backtest --isin IE00B3YLTY66 --strategy "deploy=delayed,delay=6,label=wait6"
  e1f backtest --isin IE00B3YLTY66 --slices 20 --strategy "deploy=daily-dip"
  e1f backtest --isin IE00B3YLTY66 --window 120 --explain

--strategy "k=v,..." keys: beta/base (β), a/agg, b/curv, d0/deadzone, label,
deploy (signal|even|delayed|random|daily-dip|daily-dip-carry), delay (DELAYED months),
seed (RANDOM seed), slices/n (DAILY-DIP[-CARRY] slices per month).
daily-dip (ADR-0021) spreads each month's C over N slices bought on the month's
down days (close < prior close), fully deployed inside the month — no reserve.
daily-dip-carry (ADR-0023) accrues one slice per day and spends every accrued-but-
unspent slice on each down day (last day flushes the rest) — also no reserve.
Every single run also prints matched cash-drag + blind-even controls (ADR-0020) and
the reserve-cost / deployment-benefit / timing-benefit / total decomposition.
(missing keys fall back to the --base-fraction/--aggressiveness/--curvature/--deadzone knobs).
""",
    )
    parser.add_argument("--isin", required=True, help="ETF to backtest (required; no default)")
    parser.add_argument(
        "--contribution", type=_positive, default=1000.0,
        help="Fixed monthly contribution in EUR (default 1000)",
    )
    parser.add_argument(
        "--base-fraction", type=_fraction, default=0.75, metavar="BETA",
        help="β — fraction bought immediately each month; rest funds the reserve (default 0.75)",
    )
    parser.add_argument(
        "--aggressiveness", type=_nonneg, default=5.0, metavar="A",
        help="a — scales reserve deployment: f = clamp(a·(D−D0)^b, 0, 1) (default 5)",
    )
    parser.add_argument(
        "--curvature", type=_positive, default=2.0, metavar="B",
        help="b — nonlinearity of the drawdown response (default 2)",
    )
    parser.add_argument(
        "--deadzone", type=_fraction, default=0.0, metavar="D0",
        help="D0 — drawdown below which the reserve stays put (default 0)",
    )
    parser.add_argument(
        "--slices", type=_positive_int, default=20, metavar="N",
        help="daily-dip[-carry]: slices per month (C/N each), spent on down days "
             "(ADR-0021/0023; default 20)",
    )
    parser.add_argument(
        "--drawdown-ref", choices=("rolling-high", "all-time-high"), default="rolling-high",
        help="Reference for drawdown (default rolling-high)",
    )
    parser.add_argument(
        "--lookback", type=_positive_int, default=252, metavar="DAYS",
        help="Trailing trading days for the rolling high (default 252 ≈ 12 months)",
    )
    parser.add_argument(
        "--cash-rate", type=_nonneg, default=0.0, metavar="RATE",
        help="Annual return on idle reserve cash, e.g. 0.03 (default 0 — the conservative case)",
    )
    parser.add_argument(
        "--strategy", action="append", metavar="SPEC",
        help="A dip strategy 'k=v,...' (repeatable). Omit for one dip from the knobs above.",
    )
    parser.add_argument(
        "--window", type=int, metavar="MONTHS",
        help="Sweep every rolling MONTHS-long window, summarise excess-vs-DCA (default: one run)",
    )
    parser.add_argument(
        "--blind-seeds", type=_nonneg_int, default=500, metavar="N",
        help="Seeds for the blind-random robustness block (ADR-0020; default 500, 0 disables)",
    )
    parser.add_argument(
        "--from", dest="from_date", type=_valid_date, metavar="YYYY-MM-DD",
        help="Earliest contribution date (default: series start + warm-up)",
    )
    parser.add_argument(
        "--to", type=_valid_date, default=_TODAY, metavar="YYYY-MM-DD",
        help="Horizon end / valuation date (default today)",
    )
    parser.add_argument("--db", "-d", default=DEFAULT_DB, help="Database file path")
    parser.add_argument("--config", "-c", default=DEFAULT_CONFIG, help="ETF universe config")
    parser.add_argument(
        "--currency-meta", default=DEFAULT_CURRENCY_META, help="Pinned currency metadata YAML",
    )
    parser.add_argument("--show-status", action="store_true", help="Add a Status column (ADR-0014)")
    parser.add_argument(
        "--explain", action="store_true", help="Add the provenance block (implies --show-status)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.window is not None and args.window < BACKTEST_MIN_CONTRIBUTIONS:
        _build_parser().error(
            f"--window must be ≥ {BACKTEST_MIN_CONTRIBUTIONS} months"
        )
    try:
        return _cmd_backtest(args)
    except BacktestError as exc:
        print(f"✗ {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
