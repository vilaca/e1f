# Performance metrics roadmap

Extends the `performance` command (ADR-0011) toward the metric set that matters for a
**20-year buy-and-hold ETF portfolio**. Ordered by effort-vs-value: cheap wins first,
then metrics gated by data depth, then genuinely new machinery.

Today `performance` ships XIRR, TWR, CAGR, volatility, and max drawdown. Everything
below is missing or partial. Each phase is independently shippable.

## Guiding lens: what a 20-year investor actually needs

A two-decade ETF plan is a *drift-and-hold* exercise, not a trading one. The questions
that matter are: **am I being paid for the risk I carry, how deep is the hole I must sit
through, what does fee and turnover leakage cost me over 20 years, and how much of my
outcome is luck of timing vs. a durable result.** The roadmap is prioritized against
those questions, not against metric popularity.

---

## Phase 1 — Risk-adjusted ratios (cheap wins)

**Metrics:** Calmar, Sharpe, Sortino.
**Effort:** small. All inputs already exist in `_metrics_from_series` (`performance.py`).

- **Calmar** = CAGR ÷ |MaxDD|. Both operands already computed; ~one line.
- **Sharpe** = (CAGR − rf) ÷ volatility. Only new input is a risk-free rate.
- **Sortino** = (CAGR − rf) ÷ downside deviation. Needs one new stat (stdev over
  negative daily returns only) on the return series that already exists.

**Why it matters over 20 years.** A raw CAGR hides whether the return was earned cheaply
or by carrying punishing risk. Over a 20-year hold you *live through* every drawdown, so
Calmar (return per unit of worst-case pain) is arguably the most honest single number
for a buy-and-hold investor. Sortino refines volatility by only penalizing downside —
upside "volatility" is not a risk you need compensating for. These three turn "did it go
up" into "was it worth the ride," which is the whole point of holding for two decades.

**Design notes.** Introduce a risk-free rate input — start with a config constant (e.g.
`data/currency_metadata.yaml` or a `--risk-free` flag), leave a fetched cash/T-bill
series as a later refinement. Extend the `MetricContract` `supports` tuple and the
`--explain` vocabulary. Default output stays unchanged unless columns fit; likely
surface via `--show-status`-style opt-in or a widened row.

---

## Phase 2 — Windowed history (worst / rolling periods)

**Metrics:** worst 1/3/5-year period, rolling 10/15/20-year returns.
**Effort:** medium — one shared rolling-window helper. **Gated by data depth.**

Compute the return over every N-day rolling window across the value series; take the
minimum (worst period) or emit the distribution (rolling returns).

**Why it matters over 20 years.** The single scariest fact for a long-horizon investor
is the *worst outcome they could have been handed* — the worst rolling 3- or 5-year
stretch tells you what a bad entry actually cost historically, which sets realistic
expectations and stops panic-selling. Rolling 10/15/20-year returns are the closest
empirical answer to "what does holding for my actual horizon tend to deliver," because
they measure return over the horizon you care about rather than a calendar year.

**Caveat — your data can't feed the long windows yet.** Holdings history is at most a
few years, so 5-year-worst and all 10/15/20-year rolling figures return `n/a` today.
Two options, sequenced:

1. Ship the helper now; long windows correctly report `n/a` (honest, and ready when
   history accrues).
2. Later, allow a *proxy* series — fetch the ETF's own long price history (not just the
   post-transaction window) so rolling-return backtests run on the fund, independent of
   when you personally bought. Worth its own ADR; changes what the number *means*
   (fund backtest vs. your realized path).

---

## Phase 3 — Leakage: fee drag and turnover

**Metrics:** fee drag (as a return haircut), turnover.
**Effort:** medium. Fee ingredients partly exist; turnover needs a new pass.

- **Fee drag.** `portfolio` already estimates annual fee (TER × cost basis,
  `portfolio.py:166`) and shows a `Fee/yr` column. Promote this into `performance` as a
  *gross vs. net CAGR* pair — the return you'd have without the TER haircut vs. with it.
- **Turnover** = (buys + sells) ÷ average holdings over a period, from the
  `transactions` table.

**Why it matters over 20 years.** Fees and turnover are the one cost the investor fully
controls, and compounding makes them brutal over two decades: a 0.4% annual TER drag on
a 20-year hold quietly eats a double-digit percentage of terminal wealth. Expressing it
as a CAGR haircut (not just euros/year) makes the 20-year compounding cost visible.
Turnover is the early-warning signal that a "buy-and-hold" plan is drifting into
trading — every round-trip adds spread + tax + fee leakage that a passive plan is
supposed to avoid.

---

## Phase 4 — Timing robustness (start-date sensitivity)

**Metric:** sensitivity of the headline result to the exact start date.
**Effort:** medium. Reuses the per-window machinery from Phase 2, looped over a sweep of
start dates; report the spread (min/median/max XIRR or CAGR).

**Why it matters over 20 years.** This is the humility check. If shifting the start date
by a few weeks swings the CAGR by several points, the result is a timing artifact, not a
durable property of the portfolio — and a 20-year investor should discount it. Reporting
the *spread* of outcomes across plausible start dates separates "this portfolio is
genuinely good" from "I happened to buy at a lucky moment," which is exactly the
distinction that survives or sinks a long-horizon thesis.

---

## Phase 5 — Forward-looking projection (probabilities)

**Metrics:** probability of loss, probability of reaching a target.
**Effort:** large — first *projection* capability in the codebase; everything today is
historical/realized.

Requires either a Monte Carlo simulation over a return-distribution model or a
parametric model, plus a target-value input. Warrants its own ADR (assumptions,
distribution choice, how uncertainty is disclosed) because projected numbers are a
different epistemic class from realized ones and must be flagged as such.

**Why it matters over 20 years.** Realized metrics answer "what happened"; a 20-year
plan is fundamentally a bet about "what will happen." Probability of loss over the
remaining horizon, and probability of hitting a retirement/target number, are the
questions that actually drive contribution and allocation decisions. Even a rough,
clearly-caveated distribution beats the implicit "assume the average forever" that
investors fall back on.

---

## Phase 6 — Factor attribution (largest gap)

**Metric:** contribution of each factor.
**Effort:** largest — new external data source **and** new modeling.

Needs factor return series (e.g. Fama-French: market/size/value/momentum) that e1f does
not fetch, plus a regression of portfolio returns on those factors. Distinct from the
holding-level P&L contribution (`P&Lctr`) `performance` already shows — that attributes
to *positions*, this attributes to *risk factors*.

**Why it matters over 20 years.** Over a long hold, most of your return is explained by
a handful of factor exposures, not by the specific funds you picked. Knowing you're
really running (say) a market + small-value tilt tells you where the return and the risk
are *coming from*, exposes unintended concentration (three "different" ETFs that are all
the same factor bet), and informs whether the portfolio is diversified in the way that
actually matters. It's the deepest insight here and the most work.

---

## Suggested sequencing

1. **Phase 1** now — highest value-per-line, zero new data.
2. **Phase 3** next — fee drag is a controllable, compounding 20-year cost and the
   ingredients half-exist.
3. **Phase 2 + Phase 4** together — they share the rolling-window helper.
4. **Phase 5**, then **Phase 6** — each needs its own ADR (new data / new epistemics)
   and is only worth it once the cheaper metrics are in daily use.
