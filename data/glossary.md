# e1f metrics glossary

Every metric e1f reports, what it means, and what it is good for. Read it straight
through, or query one term from the CLI:

    e1f glossary            # list every term, grouped
    e1f glossary TWR        # show one term (case-insensitive substring match)
    e1f glossary P&L        # matches P&L€, P&L%, P&Lctr

Entries are grouped by the question they answer. Each names where it appears and
whether it is money-weighted (reflects when you paid in) or time-weighted
(contribution timing neutralized). Metrics from the experimental commands
(`concentration`, `overlap`, `backtest`, `seasonality`) are out of scope here.

## Return

### XIRR
- **Where:** `performance` table, `performance --metrics`
- **Type:** money-weighted, annualized
- **Definition:** The internal rate of return of your actual cash flows (each buy
  as an outflow, current market value as the final inflow).
- **Useful for:** the honest headline — what your money actually earned per year,
  accounting for *when* you deposited. A big late deposit that barely moved drags it.

### TWR
- **Where:** `performance`, `--metrics`, `--series`, `--contrib`; `benchmark` (both legs)
- **Type:** time-weighted, cumulative
- **Definition:** Chain-linked product of daily sub-period returns
  `r_t = V_t/(V_prev+CF_t) − 1`, so deposits/withdrawals don't distort it.
- **Useful for:** judging the *investments* (fund/allocation skill) independent of
  deposit timing, and comparing against a benchmark. It's the basis every risk
  metric below is computed from.

### CAGR
- **Where:** `performance`, `--metrics`
- **Type:** time-weighted, annualized
- **Definition:** The TWR expressed as a constant annual growth rate.
- **Useful for:** a per-year "how fast is this compounding" number. Flagged `*` on
  under a year of history — it's then an extrapolation, not an observed year.

## Value & P&L

### MktVal€
- **Where:** `performance`, `--metrics`; `portfolio` (`Value€`)
- **Type:** money, snapshot
- **Definition:** Market value in EUR = shares × close × FX, valued at the as-of date.
- **Useful for:** what the position is worth right now. A stale close carried
  forward is flagged `~`.

### Cost€
- **Where:** `performance`, `--metrics`
- **Type:** money, snapshot
- **Definition:** Cost basis — the EUR you paid in, including fees.
- **Useful for:** the denominator of P&L% and the baseline for "am I up".

### P&L€
- **Where:** `performance` (`P&L€`); `deposits` (`Gain€`)
- **Type:** money, snapshot
- **Definition:** Unrealized profit/loss = MktVal€ − Cost€.
- **Useful for:** the plain "how many euros up or down am I" read.

### P&L%
- **Where:** `performance`
- **Type:** money, snapshot
- **Definition:** P&L€ as a percentage of cost basis.
- **Useful for:** the same, size-normalized so holdings are comparable. Note it's
  cost-basis (money-weighted), not time-weighted like TWR.

### P&Lctr
- **Where:** `performance` table
- **Type:** money-weighted share (sums to 100%)
- **Definition:** This holding's P&L€ as a fraction of the whole book's P&L€.
- **Useful for:** "whose euros made (or lost) the money." A big, recently-bought
  holding that's up a little can dominate it. Contrast Ctr%, which is time-weighted.
  Undefined (`n/a`) when net P&L is zero.

### Weight
- **Where:** `portfolio` (cost-basis); `performance --contrib` (market-value)
- **Type:** share
- **Definition:** A holding's share of the book — by cost basis in `portfolio`, by
  current market value in `--contrib`.
- **Useful for:** allocation at a glance. Which basis matters: cost-weight says how
  you *deployed* cash, value-weight says where the risk *sits* now.

## Risk & drawdown

### Volatility
- **Where:** `performance` (`Vol`), `--metrics`
- **Type:** time-weighted, annualized
- **Definition:** Standard deviation of daily TWR returns × √252.
- **Useful for:** how bumpy the ride is. Higher = wider swings. Flagged `*` on short
  history (annualization extrapolates).

### Max Drawdown
- **Where:** `performance` (`MaxDD`), `--metrics`
- **Type:** time-weighted
- **Definition:** The deepest peak-to-trough decline of the time-weighted wealth
  index (sampled daily).
- **Useful for:** the worst loss you'd have had to sit through — the gut-check for
  whether you could actually hold this allocation.

### MaxDD Duration
- **Where:** `performance --metrics` (`DDdur`)
- **Type:** calendar days
- **Definition:** Length of the *deepest* drawdown episode, peak → recovery (or peak
  → today if still underwater). Ongoing-aware.
- **Useful for:** how long the worst decline took to heal. Can jump when a new,
  deeper (and younger) episode becomes the deepest — that's why Days Since High
  exists as a separate number.

### Days Since High
- **Where:** `performance --metrics` (`SinceHi`)
- **Type:** calendar days
- **Definition:** Days since the *current* running peak (0 = today is a new high).
- **Useful for:** "how long have I been off my peak right now." Unlike MaxDD
  Duration it tracks the current dip regardless of depth and resets at every new high.

### Underwater (total)
- **Where:** `performance --metrics` (`Underwtr`)
- **Type:** calendar days
- **Definition:** Total days spent below a prior peak, summed across all drawdown
  episodes.
- **Useful for:** what share of the whole history was spent recovering rather than
  making new highs.

### Recovery Factor
- **Where:** `performance --metrics` (`RecFac`)
- **Type:** ratio
- **Definition:** TWR ÷ |Max Drawdown|.
- **Useful for:** return earned per unit of worst pain. Above 1 means you've made
  more than the deepest drawdown cost; higher is better.

## Extremes

### Best Day / Worst Day
- **Where:** `performance --metrics`
- **Type:** time-weighted, single period
- **Definition:** The single best and worst daily TWR return, with its date.
- **Useful for:** the size of the daily tails — the biggest one-day moves you've seen.

### Best Month / Worst Month
- **Where:** `performance --metrics`
- **Type:** time-weighted, calendar month
- **Definition:** Best and worst calendar-month return (daily returns chain-linked
  within each `YYYY-MM`; partial first/last months included).
- **Useful for:** the monthly tail — a steadier read on good/bad stretches than a
  single day.

### Max Gain / Max Loss
- **Where:** `performance --metrics`
- **Type:** ratio
- **Definition:** |Best Day| ÷ |Worst Day|.
- **Useful for:** asymmetry of the extremes — above 1 means the best day beat the
  worst day in size.

## Trailing returns

### Trailing 1M / 3M / 6M
- **Where:** `performance --metrics`
- **Type:** time-weighted
- **Definition:** Return over the trailing 1-, 3-, and 6-calendar-month window ending
  at the latest valued day, read off the wealth index. A window whose start predates
  inception shows `n/a`.
- **Useful for:** recent momentum at fixed horizons. On a young book the longer
  windows are `n/a` until there's enough history (1Y/2Y aren't reported yet for the
  same reason).

## Attribution & contribution

### Ctr%
- **Where:** `performance --contrib`
- **Type:** time-weighted contribution (sums to the TOTAL TWR)
- **Definition:** Each holding's Cariño-linked share of the book's time-weighted
  return. Each day's portfolio return is split into `weight_prev × holding_return`;
  the daily pieces are log-linked so the per-holding totals sum exactly to the
  multi-period TWR.
- **Useful for:** which holdings actually *drove the return*. Differs from current
  weight × return: a holding bought recently at high weight contributes little
  because its beginning-of-period weights were small. Contrast P&Lctr (money-weighted).

## Deposits & capital

### Invested
- **Where:** `deposits`
- **Type:** money
- **Definition:** Total contributions — the EUR you've paid in across all buys.
- **Useful for:** the capital base. Denominator of ROIC.

### Reported (market value)
- **Where:** `deposits`
- **Type:** money, snapshot
- **Definition:** Current market value of everything you hold (reconciles with the
  `performance` TOTAL).
- **Useful for:** what the account is worth today, next to what you put in.

### Organic gain
- **Where:** `deposits`
- **Type:** money
- **Definition:** Market-driven gain = reported market value − invested.
- **Useful for:** separating growth *earned* from growth that's just fresh deposits.

### ROIC
- **Where:** `deposits`
- **Type:** money-weighted ratio
- **Definition:** Organic gain ÷ invested.
- **Useful for:** simple return on the capital you've deployed, since inception.

### Ret% (per deposit)
- **Where:** `deposits` per-deposit table
- **Type:** money, buy-and-hold
- **Definition:** How much one buy's shares have grown: (Value€ − Amount€) ÷ Amount€.
- **Useful for:** spotting which individual purchases did well or badly.

### %P&L (per deposit)
- **Where:** `deposits` per-deposit table
- **Type:** money-weighted share
- **Definition:** One buy's share of the portfolio's total P&L.
- **Useful for:** which specific deposits made (or lost) the money.

## Fees

### WTER (weighted TER)
- **Where:** `performance --series` (`WTER`); `portfolio`
- **Type:** market-value-weighted ratio
- **Definition:** Market-value-weighted average total expense ratio; holdings with
  no TER metadata contribute 0 (which dilutes the average downward).
- **Useful for:** the blended annual fee drag on the whole book.

### Fee€/yr
- **Where:** `performance --series`; `portfolio`
- **Type:** money, annual estimate
- **Definition:** Estimated annual fee = WTER × market value at that date.
- **Useful for:** the fee in euros per year — more tangible than a percentage.

## Benchmark-relative

### Beta
- **Where:** `benchmark`
- **Type:** regression, over the shared window
- **Definition:** Sensitivity of the book to the benchmark = cov(rp, rb) / var(rb).
- **Useful for:** how much you move with the market — ~1 tracks it, <1 is defensive,
  >1 is amplified.

### R²
- **Where:** `benchmark`
- **Type:** regression, over the shared window
- **Definition:** Share of the book's variance the benchmark explains = corr(rp, rb)².
- **Useful for:** how well that benchmark *describes* your book — high R² means Beta
  is meaningful; low R² means the benchmark is a poor mirror.

### Tracking Error (TE)
- **Where:** `benchmark`
- **Type:** annualized
- **Definition:** Standard deviation of the active return (rp − rb) × √252.
- **Useful for:** how far you drift from the benchmark. Low = you hug it, high = you
  do your own thing.

### Information Ratio (IR)
- **Where:** `benchmark`
- **Type:** annualized ratio
- **Definition:** Mean active return ÷ tracking error.
- **Useful for:** active return earned per unit of tracking risk — was the deviation
  from the benchmark worth it.

### Relative Strength (RelStr)
- **Where:** `benchmark`
- **Type:** compounded ratio, over the window
- **Definition:** (1 + portfolio TWR) ÷ (1 + benchmark TWR).
- **Useful for:** did €1 in your book grow to more than €1 in the benchmark — the
  geometric/compounded form of outperformance.

### Out%
- **Where:** `benchmark`
- **Type:** cumulative, over the window
- **Definition:** Portfolio TWR − benchmark TWR (active return, arithmetic).
- **Useful for:** simple over/underperformance versus each benchmark. Not annualized;
  each benchmark's window differs, so read it with `n`.

### n (observations)
- **Where:** `benchmark`, `correlation`
- **Type:** count
- **Definition:** Number of shared daily-return observations the statistic was
  estimated from.
- **Useful for:** trust. A young book gives small `n` — the Beta/IR/ρ are
  "preliminary" until it grows.

## Correlation

### ρ (Pearson correlation)
- **Where:** `correlation`
- **Type:** over each pair's shared window
- **Definition:** Correlation of two funds' daily EUR returns, in [−1, 1].
- **Useful for:** redundancy and diversification — near 1 means two funds move
  together (one may be redundant); low or negative means they diversify each other.

### Correlation distance
- **Where:** `correlation` (clustering)
- **Type:** distance
- **Definition:** √(½(1 − ρ)) — a proper distance that shrinks as ρ → 1.
- **Useful for:** grouping funds into clusters of things that move alike, so you can
  see how many genuinely distinct bets you hold.
