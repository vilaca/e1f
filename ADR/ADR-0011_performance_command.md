# ADR-0011 — `performance` command (XIRR-first)

**Scope:** the `performance` subcommand — current EUR market value, unrealized
P&L, and return/risk metrics (XIRR, TWR, volatility, max drawdown, CAGR) per
held ISIN and for the portfolio as a whole — plus the shared
`common.position_timeline` primitive it is built on. Builds directly on the
daily FX series and `convert_to_eur` helper from ADR-0010.

## Context

`portfolio` (ADR-0005) answers *what do I hold and what did it cost*. It stops
at cost basis: it never values a position at market, so it cannot say whether
the portfolio is up or down, or what it has actually earned. ADR-0010 supplied
the missing half — a daily FX series and a tested `convert_to_eur` — so a
mixed-currency portfolio can now be summed into one base currency (EUR).

This command adds the valuation and return layer on top. The settled scope for
the whole effort (ADR-0010) holds: base currency **EUR**; **buy-and-hold** (no
sells, no realized P&L, no tax lots); all held funds **accumulating** so the
price close is an accurate total-return proxy (no distribution ingest).

The load-bearing correctness rule is the **currency asymmetry**: the
cost/cash-flow leg is already EUR (`transactions.price` is EUR/share for both
brokers), so it needs no conversion; the value leg (`prices.close`) is in each
fund's native quote currency and **must** be FX-converted. Converting the cost
leg, or forgetting to convert the value leg, is the classic silent XIRR bug —
so both currencies are tested.

## Decision

### 1. XIRR is the headline metric; TWR and CAGR sit beside it

For a DCA / savings-plan portfolio the question that matters is *what did my
money earn* — a **money-weighted** return, which XIRR answers by discounting the
dated contributions against the terminal market value. It is reported first.

Three return numbers appear together, deliberately distinct so none is
redundant:

- **XIRR** — money-weighted annualized rate (contribution timing matters).
- **TWR** — time-weighted **cumulative** total return over the window
  (contribution timing neutralized; "what the market did to the holdings").
- **CAGR** — the **annualized** form of that TWR: `(1 + TWR)^(365/days) − 1`.

XIRR vs. CAGR is then the natural comparison — money-weighted vs.
time-weighted annualized — and TWR shows the raw cumulative figure both derive
from.

### 2. Holding identity: net across brokers, per ISIN

`portfolio` keys positions per `(broker, symbol)`; `common.portfolio_isins`
nets across brokers per ISIN. Market value is broker-agnostic (the same ISIN at
two brokers is one economic position priced by one close), so the value series
and every metric **net across brokers per ISIN**. The per-ISIN rows and the
portfolio total both follow this convention. `position_timeline` therefore keys
on `symbol` (the ISIN) alone, summing contributions from all brokers.

### 3. Cost / cash-flow leg from the stored EUR price — no ingest change

The XIRR cash-flow series and the cost leg of unrealized P&L come from
`transactions` (never from `prices`): each BUY is an outflow of
`shares × price + fee`, already in EUR. This reuses the average-cost total the
`portfolio` command already computes.

**Divergence from the handoff, recorded deliberately.** The handoff proposed
booking Trade Republic's all-in EUR `amount` field as the buy cost ("Option A"),
which would require ingest to persist that field. ADR-0010 §6 already **deferred**
persisting TR's `amount`/`fx_rate`/`original_*` fields — the XTB export has no
counterpart, so the column would be asymmetric and half-populated — and left the
ADR-0004 transaction schema untouched. Since `transactions.price` is already
EUR/share, `Σ(shares × price + fee)` reproduces the euros debited without a
schema change, so this ADR builds on the existing column. The only cost is
cent-level rounding drift versus TR's exact all-in `amount`, negligible over a
multi-year XIRR; closing that gap (persist `amount`, symmetric with an XTB
solution) stays deferred to a future ADR.

### 4. Daily EUR value series: FX-converted, forward-filled, trading-day grained

The value series is built on the union of `prices` dates (trading days) within
`[first contribution, as-of]`. On each day a holding is valued as
`shares_held × close × 1/rate` via `convert_to_eur`, where:

- `shares_held` is the step function from `position_timeline` (last event on or
  before the day) — buy-and-hold, so monotonic.
- `close` is that ISIN's **nearest-prior** close (forward-filled across days its
  own listing did not trade but another held ISIN did), mirroring `fetch`'s
  forward-fill.
- the FX rate is nearest-prior via `fx_rate_asof` (ADR-0010 §4).

Contribution dates that are not trading days are added as extra series
breakpoints so a mid-period contribution starts its own TWR sub-period.

### 5. Metric definitions

- **Market value / cost / unrealized P&L** — terminal EUR value (as-of),
  average-cost basis, and their difference (€ and %). When the terminal value
  falls back to a nearest-prior close (§4) because the ISIN has no close on the
  as-of day itself, the value is flagged `~` with its price date and staleness,
  so a carried-forward number is never mistaken for an on-the-day valuation.
- **XIRR** — solve `Σ cfᵢ / (1+r)^(tᵢ/365) = 0` (Actual/365 from the first cash
  flow) over dated contributions plus the terminal value. Newton–Raphson with a
  bisection fallback; non-convergence yields `n/a`, never a wrong number.
- **TWR** — chain-linked sub-period returns `rₜ = Vₜ / (Vₜ₋₁ + CFₜ) − 1`
  (contribution treated as start-of-day), `TWR = Π(1+rₜ) − 1`.
- **Volatility** — `stdev(rₜ) × √252` (annualized from trading-day returns).
- **Max drawdown** — largest peak-to-trough decline of the TWR wealth index
  `Wₜ = Π(1+rᵢ)`, **not** of the raw value series (contributions would otherwise
  masquerade as recoveries and understate drawdown).
- **CAGR** — annualized TWR (per decision 1).

### 6. Full metric set on every per-ISIN row and the total

Every per-ISIN row carries the complete set (value, cost, P&L, XIRR, TWR, vol,
max drawdown, CAGR), and so does the `TOTAL` row. This needs a per-ISIN daily
value series, which the design already produces. Output mirrors `_cmd_portfolio`
— a formatted point-in-time table, sortable, no CSV export in v1.

### 7. `--as-of DATE` for historical snapshots

`--as-of` values the portfolio at any past date (default: today). It is nearly
free: `fx_rate_asof` / `convert_to_eur` are already nearest-prior, and the
series is date-parameterized. It also makes the return math testable against
fixed endpoints rather than a moving "today".

### 8. Short/absent history: flag, never suppress

Metrics are never hidden. A holding whose valuation window is under one year, or
whose price history begins after its first contribution, keeps its row; the
**annualized** columns (CAGR, volatility) are marked `*` with a footnote, since
annualizing thin data extrapolates. A holding with **no** price on or before the
as-of date (unfetched, or as-of precedes its first price) shows `n/a` for the
value-derived metrics and is **excluded from the `TOTAL` with a visible
warning**, so the aggregate is never silently understated.

## Rationale

- **Money-weighted headline** — XIRR reflects the investor's actual experience
  including contribution timing; for a savings-plan portfolio that is the honest
  "what did I earn". TWR/CAGR are kept for benchmark-style comparison, not as the
  primary number.
- **Cost from `transactions`, value from `prices`** — the two tables play
  different roles; a price-derived cost would fold market movement into the
  basis and corrupt every return metric.
- **Net across brokers** — value is a property of the security, not the venue;
  keying per broker would split one economic position into two half-rows.
- **Drawdown on the TWR index, not raw value** — for a contributing portfolio
  the raw value line almost never falls (new money masks losses); only the
  contribution-neutralized index states the real drawdown.
- **Flag, never suppress** — hiding a metric hides information; a caveat that
  travels with the number is more honest than an empty cell.

## Consequences

- `performance` joins `transactions`, `prices`, and `fx_rates`; it requires all
  three to be populated. With no `transactions` it prints the same "ingest
  trades" guidance as `portfolio`; with prices/FX missing for a held ISIN, that
  row degrades to `n/a` rather than failing the command.
- The broker fill and ftgo close legitimately differ (intraday timing, bid/ask
  spread, execution venue); this surfaces as a small day-0 P&L on a
  freshly-bought position (basis points, not real movement) and is **not**
  reconciled — it is captured friction. The only genuine bug to watch is a
  systematic, persistent per-ISIN gap, which would mean ftgo is pricing a
  different listing than the held ISIN.
- No schema change: `transactions` (ADR-0004), `prices`, and `fx_rates`
  (ADR-0010) are untouched.

## Deferred (not in this ADR)

- **Sharpe ratio** — needs a risk-free-rate source (a hardcoded constant goes
  stale; €STR/bund is a new fetch + pinned-resolution path). Volatility and max
  drawdown give the risk picture without it. Additive follow-up once a rate
  source is worth pinning.
- **Benchmark comparison** — choosing a reference (a held MSCI World ETF vs. a
  fetched index), the comparison method, and missing-benchmark-window handling
  is a design of its own. TWR is in v1 precisely so a benchmark can layer on
  cleanly later.
- **Sells / realized P&L** — buy-and-hold is the settled scope. `position_timeline`
  mirrors `compute_holdings`' average-cost SELL handling for share/cost parity,
  but return math treats cash flows as contributions only (`PositionEvent.cash_flow`
  is `0.0` for a SELL); a sell would need realized-P&L and sale-proceeds modeling —
  its own ADR.

  **Known limitation, not just an absent feature:** because a sell reduces
  `shares_held` but books `cash_flow = 0.0`, the TWR series treats the resulting
  drop in market value as a market *loss* rather than an external outflow. On a
  flat-market day, selling half a position registers as a ≈ −50% sub-period
  return (`rₜ = Vₜ / Vₜ₋₁ − 1` with no proceeds subtracted from the denominator),
  and a full mid-history liquidation drives the wealth index toward zero. This
  silently corrupts TWR — and everything derived from the same return series
  (CAGR, volatility, max drawdown) — for any portfolio containing a SELL. It is
  **latent**, not active: the current DB is buy-only (verified 2026-08-24: 38
  BUYs, 0 SELLs), but both broker importers and `position_timeline` accept sells,
  so the first sale produces a wrong TWR with no error. A correct fix must treat
  sale proceeds as a genuine outflow (`rₜ = (Vₜ + proceeds) / Vₜ₋₁ − 1`, or
  subtract proceeds from the start-of-day base) — deferred to the sells ADR above.
- **Cent-exact cost from TR's all-in `amount`** — see decision 3.
- **CSV / time-series export** of the daily value series — deferred until a
  consumer needs it.
