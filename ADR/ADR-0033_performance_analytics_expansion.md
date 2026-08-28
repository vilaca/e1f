# ADR-0033 — `performance` analytics expansion: clean-only scope, phasing, and deferrals

**Scope:** expand `performance` with a battery of additional return/risk metrics
and a benchmark-comparison mode, but admit **only metrics that are computable from
data already in the DB with no convention choice and no risk-free rate** ("clean"
metrics). Everything requiring €STR, a debatable definition, a rolling series, a
window the book is too young to fill, or data e1f does not hold is recorded here as
**deliberately deferred**, so a future session returns to a decision, not a blank
page. Delivered in phases (A → B → C), each of which may take
its own follow-on ADR as it lands. This ADR is the roadmap and the deferral log;
it does not itself add a metric.

## Context

A request came in for ~40 metrics and six benchmarks (MSCI World, MSCI Europe,
WEBN, S&P 500, MSCI ACWI, FTSE All-World) plus €STR. Auditing them against the
codebase showed three things:

1. **The backbone already exists.** `risk_metrics` (ADR-0011) already builds the
   portfolio's daily time-weighted return series, wealth index, TWR, annualized
   Vol, MaxDD, and CAGR; `xirr` gives the money-weighted return.
   `eur_return_series` (currently in `correlation.py`, ADR-0015) already produces
   any fund's EUR daily returns — the exact vector a benchmark comparison needs.
   So most requested metrics are statistics over series the code already computes,
   and two asks are **already shipped, only unlabelled**: "deposit-adjusted XIRR"
   is the existing `XIRR` column, and "performance excluding cash deposits" is the
   existing `TWR`/`CAGR` (time-weighting neutralizes contribution timing).

2. **Two data gaps.** There is no MSCI World fund in the universe (only World
   *sector* / *ex-USA* / *small-cap*), and there is **no rate data at all** — the
   `fx_rates` table holds EUR/USD only; €STR lives in the ECB data portal, not
   ftgo/yfinance.

3. **Many of the requested metrics carry a convention choice** (Calmar's window,
   Average Drawdown's definition, capture-ratio frequency, trade-level vs per-day
   win/profit stats) where reasonable practitioners differ.

Per the project's rigor stance (no synthetic data, no silent convention picks,
disclose coverage), we scope this to the metrics that are unambiguous and
data-backed today, and defer the rest **on the record** rather than approximating.

## Decisions

**Clean-only scope, in three phases.**

- **Phase A — own-return-series metrics (no benchmark, no rate, no new data):**
  MaxDD Duration, Underwater period, Recovery Factor, Best Day, Worst Day,
  Max-Gain/Max-Loss ratio. Reuses the existing daily return series and wealth
  index; MaxDD, Vol, TWR, CAGR, XIRR are reused, not re-derived. Surfaced via a
  new `performance --metrics` view, which composes with `--series N` to tabulate
  the same figures one row per trading day (cumulative since inception, the same
  contract as the plain `--series`; the trailing-window reading is the deferred
  rolling family below, not this). Ships first.

  **Later additions (same clean scope, no new data — landed this session):**
  - Days Since High — calendar days since the *current* running peak (0 = the last
    day is itself a new high), distinct from MaxDD Duration (which tracks the
    *deepest* episode); the two coincide only while the deepest drawdown is also the
    open one. Surfaced in `--metrics` and as the `SinceHi` column under `--series`.
  - Best/Worst Month — calendar-month buckets of the daily time-weighted return
    series, chain-linked (partial first/last months included), labelled `YYYY-MM`.
    In the `--metrics` "Extremes" block alongside Best/Worst Day.
  - Trailing 1M / 3M / 6M returns — time-weighted return over each trailing
    calendar-month window ending at the latest valued day, read off the wealth
    index; a window whose start predates inception shows `n/a` (so a ~2-month book
    reports 1M and leaves 3M/6M blank until it has the history). This lands the
    **short end** of the trailing family; **1Y/2Y stay deferred** as young-book
    (they would only emit `n/a` today — see the window-too-short note below).
  - Per-holding **return contribution** (`performance --contrib`) — each holding's
    Cariño-linked (ADR reference: Cariño 1999) contribution to the book's total
    time-weighted return. Each day's portfolio return is decomposed into
    `w_{i,prev}·r_{i,t}` (which sums to the day's portfolio return by construction);
    the daily arithmetic contributions are Cariño log-linked so the per-holding
    totals sum **exactly** to the multi-period portfolio TWR (reconciles to the
    `performance` TOTAL, the reconcile-to-the-cent bar). Cariño is a convention, but
    **the** standard additive-linking one — chosen as a disclosed default (the
    output names it), not a silent pick. The primitive `contribution_to_return`
    graduates into `common/returns.py`. This fills the return-contribution-**by-
    holding** gap; contribution **by sector/region/factor** stays data-blocked below.

- **Phase B — benchmark comparison (needs the MSCI World fund):** Beta, R²,
  Tracking Error, Information Ratio, Relative Strength, against one or more
  benchmark ISINs. Delivered as a **new `benchmark` command**
  (`e1f benchmark --against ISIN[,ISIN…]`), not a `performance` flag — the
  one-command-per-module norm, and `performance` is already large. Because a peer
  command may not import `performance` (ADR-0003), both `eur_return_series` (from
  `correlation`) and the portfolio daily-return-series builder **graduate into
  `common`** as the shared primitives the new command consumes. MSCI World enters
  the universe **directly** as
  iShares Core MSCI World USD (Acc), **IE00B4L5Y983** — the investable fund itself,
  not a stand-in; USD-accumulating (total return), EUR/USD FX already present,
  history from 2009 (the longest clean overlap with the book). The other five
  benchmarks are already priced (VWCE, SPYY/iShares S&P 500, iShares Core MSCI
  Europe, ACWI, WEBN), all Accumulating and thus total-return.
  **Landed** (this session): the graduated primitives live in `common/returns.py`;
  `--against` **defaults to all six** broad benchmarks, shown by friendly index
  name (not the cryptic config names) with a `*` marking any that are also a
  current holding; there is **no minimum-overlap floor by default** (only the
  mathematical floor of two shared returns) because the book may be young — the
  sample `n` is always shown, and `--min-overlap` raises the bar for rigor. The
  reported figures are Beta, R², Tracking Error, Information Ratio, Relative
  Strength, and window outperformance (Out%); risk-free-dependent metrics stay
  deferred.

- **Phase C — deposit/organic analysis:** ROIC, organic-vs-reported value split,
  per-deposit impact on total return. Deposit-adjusted XIRR and ex-deposit TWR are
  **relabels of existing columns**, not new computation. **Landed** as a new
  `deposits` command: a summary header (invested / reported / organic gain / ROIC =
  gain÷invested) plus a per-deposit table valuing each buy's shares to the as-of
  date (amount, value, gain, own return, share of total P&L). It reuses the same
  EUR valuation as `performance` (FX at the as-of day), so the totals reconcile with
  the `performance` TOTAL to the cent. The book is buy-and-hold (contributions only,
  ADR-0011), so per-deposit values sum to the portfolio market value; a SELL would
  break that reconciliation and is disclosed. A deposit whose fund has no price/FX is
  excluded (never zero-valued) and warned.

**Benchmarks are investable funds, net of TER — disclosed, not hidden.** A
benchmark price is the fund's, carrying its 7–20 bps TER and tracking error, so a
comparison is net-of-cost vs net-of-cost (the fair comparison for an investor),
not against the raw index level. Phase B output labels this.

**Pervasive caveats travel with the numbers as output labels** (accepted, not
deferred): the daily return series bridges missing closes, so a "daily" return can
span >1 calendar day while every `×√252` annualization (Vol, Tracking Error, Info
Ratio) treats it as uniform; each benchmark comparison covers only the overlap of
the two histories (window + n printed per benchmark); a whole-book beta/R² against
all-equity MSCI World mixes asset classes (EM, small-cap, bonds, REIT, sectors).

## Deferred / not implemented (with the reason — this is the return-to list)

**Needs €STR / a MAR reference (a risk-free or minimum-acceptable-return term;
deliberately not faked — no rate data exists and a flat assumption was rejected):**
- Sharpe Ratio
- Rolling Sharpe Ratio
- Treynor Ratio — `(Rp − rf)/β`
- Jensen's Alpha — `(Rp − rf) − β(Rm − rf)`; the `(1−β)·rf` term does not cancel,
  so it genuinely needs `rf` (there is no rf-free "alpha" that means the same thing)
- Downside Deviation — RMS of returns below a MAR. A MAR=0 variant needs **no** rate
  and is pure return-series math; nonetheless **postponed by decision** (this session)
  so the whole MAR family ships once, consistently, on the risk-free reference —
  rather than a one-off MAR=0 version now and a rate-based one later.
- Sortino Ratio — `(Rp − MAR) / DownsideDeviation(MAR)`; same gate. MAR=rf is the
  standard form once €STR lands; the MAR=0 form was buildable today but is held with
  the family by the same decision.

Unblocking the whole group is one decision: fetch daily €STR from the ECB into a new
rate series, then a `--rf` / MAR source. Until then they are out of scope — **all
MAR/rf-dependent metrics gate on €STR together** (this session's call), even the ones
a MAR=0 convention could deliver rate-free.

**Convention choice — code-ready, decision-blocked (deferred to avoid a silent
pick; each is a pure statistic over series/benchmark legs the code already has —
what's missing is a chosen definition, not data or a rate):**
- Calmar Ratio — textbook 3-year window vs since-inception; most holdings have <3y
  history, so neither is clean today.
- Average Drawdown — mean of drawdown *episodes* vs mean of the daily *underwater*
  series (different numbers).
- Up/Down Capture and Capture Ratio — standard on **monthly** returns; on the
  daily gappy series they are noisy and non-standard. Revisit if/when a
  monthly-resampled return basis exists.
- Win Rate / Profit Factor / Average Win / Average Loss — trade-level metrics by
  origin; on a buy-and-hold book they become per-day stats, a reinterpretation we
  chose not to ship silently.

**Rolling series (deprioritized; not in the clean scalar scope):**
- Rolling 5-day return, Rolling Volatility, Rolling outperformance vs benchmark
  (Rolling Sharpe additionally needs €STR). A `performance --rolling N` view is a
  clean later increment once the scalar batteries are in.
- Rolling 252-day metrics specifically are also **premature on a young book** — a
  252-trading-day window cannot even form until the book is ~a year old, so shipping
  the code now would only emit `n/a`. This is a window-length gate on top of the
  deprioritization above (see the young-book note below), not a separate design.

**Window too short — premature regardless of code (young-book gate, revisit as the
book ages; no ADR needed, just enough history):**
- Trailing-period returns **1Y / 2Y** — the long end of the trailing table. The
  short end (**1M / 3M / 6M**) **landed** in Phase A (see the later-additions list
  above); the long windows stay out because they are **meaningless on a book only a
  couple of months old** and would only render `n/a` (a window auto-skips when its
  start predates inception). They earn their place once there is history to fill
  them — no code decision, just enough time. Distinct from `--diff N` (signed change
  over N days) and `--series N` (cumulative-since-inception per day).

**Blocked on data e1f does not hold (the genuinely hard one — not a convention, a
rate, or a window, but a missing dataset):**
- Attribution of return/exposure by **sector / region / factor**. e1f has no
  security-level classification: region has been reported UNAVAILABLE since
  ADR-0018 (portfolio-level country/region HHI is designed but not landed, needing a
  reviewed sidecar), and there is **no sector or factor data at all**. Unblocking
  this needs a look-through classification source and a review pipeline — a much
  larger effort than any metric above, and out of scope for this analytics
  expansion. Per-holding P&L (`performance`) and per-deposit P&L share (`deposits`)
  are the contribution views that *are* possible without that data.

## Consequences

`performance` grows a `--metrics` view (Phase A); the default
snapshot/`--diff`/`--series` output is untouched. Phase B landed as a separate
`benchmark` command — the first time this work adds an ISIN to the universe
(iShares Core MSCI World `IE00B4L5Y983`, resolved via `e1f config add` and fetched
with `e1f fetch … --fallback`; the config + pinned currency are committed, the
price rows stay in the gitignored DB). Phase C landed as a separate `deposits`
command, reusing the same EUR valuation so its totals reconcile with the
`performance` TOTAL to the cent. All three clean-scope phases are now shipped; the
deferral list above is the authoritative record of what was intentionally left out
and why, sorted by what unblocks it: a future "let's add Sharpe/Sortino/downside
deviation" starts by **fetching €STR** (the whole rate/MAR family gates on it
together — no interim MAR=0 pick); "add Calmar / capture / win-rate" starts by
**choosing a definition** (code is ready); "add 1Y/2Y or rolling-252" just needs the
**book to age** (unbuilt but unblocked, would only emit `n/a` today); and
sector/region/factor attribution needs a **look-through classification dataset** e1f
does not have — the one genuinely hard gap. Each is already framed here so the return
is to a decision, not a blank page.
