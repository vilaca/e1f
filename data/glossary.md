# e1f metrics glossary

Four pairs this file exists to keep apart:

- **XIRR vs TWR** — quote XIRR to yourself (what *your cash* earned, given when
  you paid in); quote TWR against a fund or a benchmark (what *the holdings*
  earned).
- **P&Lctr vs Ctr%** — whose euros vs who drove the time-weighted return. A new,
  large, slightly-up position dominates P&Lctr and barely shows in Ctr%.
- **Out% vs RelStr** — arithmetic gap vs compounded growth. RelStr 1.05 means €1
  in the book became 5% more than €1 in the benchmark.
- **DDdur vs SinceHi** — how long the *worst* hole lasted vs how long you've been
  off the *current* peak.

Then the rest: P&L% is still cost-basis (not TWR); RecFac is cumulative TWR ÷
|MaxDD| (not Calmar); ROIC is not annualized XIRR; Weight is cost in `portfolio`
and value in `--contrib`.

## Metric families

A quick map before the detail:

| Family | Core question | Key metrics |
|---|---|---|
| Personal return | What did my cash earn? | XIRR |
| Investment return | What did the holdings earn? | TWR, CAGR |
| Money outcome | How many euros up/down? | P&L€, P&L%, MktVal€, Cost€ |
| Attribution | Which holdings drove the return? | Ctr%, P&Lctr |
| Allocation | Where is capital/risk? | Weight |
| Risk | How rough was the ride? | Vol, MaxDD, DDdur, SinceHi, Underwtr, RecFac |
| Benchmark-relative | Did I beat the alternative? | Out%, RelStr, IR, Beta, TE, R² |
| Fees | What does the fund cost annually? | WTER, Fee€/yr |
| Diversification | Do holdings move alike? | ρ, clusters |

Covers `performance` (table, `--metrics`, `--series`, `--contrib`, `--diff`),
`portfolio` (value / weight / fees), `deposits`, `benchmark`, and `correlation`.
Experimental commands (`concentration`, `overlap`, `backtest`, `seasonality`)
and rebalance plan columns are out of scope.

    e1f glossary            # list every term, grouped
    e1f glossary TWR        # show one term (case-insensitive, matched at word starts)
    e1f glossary "P&L"      # the P&L family: P&L€, P&L%, P&Lctr, ΔP&L€, %P&L (quote — & is a shell operator)

## Start here

New to the book? Five metrics answer most of the questions, in this order:

1. **TWR** — how did the *holdings* do, with your deposit timing stripped out?
   The foundation: every risk, extreme, trailing, and benchmark number is
   computed from this series.
2. **XIRR** — how did *your cash* do, timing and all? The gap to CAGR — its
   time-weighted twin, both annualized — *is* the cost (or gift) of when you paid
   in (compare rates to rates, not to the cumulative TWR).
3. **P&L€** — how many euros up or down? The plain "rent or retirement" read,
   not a rate and not a judgement of the funds.
4. **MaxDD** — what's the worst drop you'd have had to sit through? The
   gut-check before adding more of the same.
5. **Out%** — did leaving the market beat just buying it? Read `n` first — a
   short window makes any gap a coin flip.

Each has its own entry below with what *not* to conclude from it.

## Return

XIRR is what your cash earned; TWR is what the holdings earned. CAGR is TWR per year.

### XIRR
- **Where:** `performance` table, `--series`, `--metrics`
- **Type:** money-weighted, annualized (Actual/365)
- **Definition:** The internal rate of return of your actual cash flows: each buy
  is an outflow (shares × price + fee), current market value is the final inflow.
  Actual/365. `n/a` when the flows have no sign change. Contributions-only — e1f
  does not model sells, so they never enter the cash-flow list (ADR-0011); **XIRR
  is therefore valid only for a buy-and-hold book** — sell any holding and the
  figure silently misstates your return. ("Contributions" here means capital
  buys, not performance attribution — see Ctr%.)
- **Useful for:** the number to quote when someone asks how *your* investing went.
  It is honest about a late deposit that has barely moved — that drag is real,
  it's your money sitting. It is a poor way to judge whether a fund was a good
  pick: a colleague who bought the same ETF on a different schedule will print a
  different XIRR.
- **Don't:** compare two investors' XIRRs unless their cash-flow schedules are
  similar — timing differences alone explain most divergence.
- **Read with:** CAGR (the annualized time-weighted twin — both per-year, so
  `XIRR << CAGR` means cash arrived late or at the wrong times; TWR is cumulative,
  a different unit, so don't read its raw gap to XIRR as timing). ROIC (same
  money, not a rate per year). P&L€ and Cost€ (the euro gap and the capital the
  rate was earned on).

### TWR
- **Where:** `performance`, `--metrics`, `--series`, `--contrib`; `benchmark`
  (used on both legs internally to derive RelStr/Out% — not shown as a column)
- **Type:** time-weighted, cumulative
- **Definition:** Chain-linked product of daily sub-period returns
  `r_t = V_t/(V_prev+CF_t) − 1`, so deposits don't inflate or flatten it.
  Contributions-only: e1f does not model sells, so — like XIRR — TWR is valid
  only for a buy-and-hold book ("contributions" means capital buys, not
  performance attribution — see Ctr%).
- **Useful for:** judging the *mix* — a fund, an allocation, a benchmark comparison —
  independent of when you paid in. Quote this against a factsheet or another
  investor in the same funds. Every risk, extreme, trailing, contribution, and
  benchmark metric is computed from this series, not from the euro P&L line: if
  TWR looks odd, the rest of the report is odd for the same reason.
- **Don't:** use it to claim the return you earned on your personal cash — it
  strips out the effect of *when* your money arrived.
- **Read with:** CAGR (this number per year — compare *that* to XIRR, both
  annualized: the gap *is* timing). XIRR (your personal money-weighted rate).
  MaxDD and Vol (what it cost to earn). Ctr% (which holdings produced it). Out% /
  RelStr (the same TWR versus a benchmark).

### CAGR
- **Where:** `performance` table, `--series`, `--metrics`
- **Type:** time-weighted, annualized
- **Definition:** TWR as a constant annual growth rate: `(1+TWR)^(365/days) − 1`
  (calendar days, not trading days). Flagged `*` when the window is under a year
  *or* the first stored close is after the first trade — both are extrapolations,
  not an observed year.
- **Useful for:** talking in per-year terms ("this book compounds at ~x%") so TWR
  and XIRR are in the same units. On a young book the `*` is the whole point —
  don't treat a nine-month CAGR as something you have "earned annually."
- **Don't:** treat a `*`-flagged CAGR as an established annual rate — it is an
  extrapolation, not an observed full year.
- **Read with:** TWR (the cumulative number this annualizes). XIRR (also
  annualized, but money-weighted). Vol (annualizes with √252 trading days, not
  365 calendar days — don't compare the two as if they used the same year).
  RecFac (uses cumulative TWR, not CAGR).

## Value & P&L

Snapshots in euros. P&L% is still cost-basis, not TWR. P&Lctr is whose euros; Ctr% (under Attribution) is who drove the return.

### MktVal€
- **Where:** `performance` table, `--series`, `--metrics`; `portfolio` (`Value€`)
- **Type:** money, snapshot
- **Definition:** Shares × close × FX. `performance` / `deposits` convert FX on
  the valuation (as-of) day; `portfolio` converts on the close's own date — they
  agree when the close is fresh, and can differ when a stale close is carried
  forward (`~`).
- **Useful for:** what the position is worth right now — the "can I retire /
  rebalance / spend" number. A `~` means you're looking at yesterday's (or last
  week's) close dressed as today; fetch before making a cash decision.
- **Don't:** make a cash decision on a `~`-marked value; fetch fresh data first.
- **Read with:** Cost€ (what you paid; the gap is P&L€). Weight (where this sits
  in the book). Fee€/yr (the TER bill scales with this, not with cost). Deposits
  **Reported** is the same TOTAL.

### Cost€
- **Where:** `performance` table, `--series`, `--metrics`
- **Type:** money, snapshot
- **Definition:** Cost basis — the EUR you paid in, including fees
  (shares × price + fee). The same TOTAL as deposits **Invested** (valuable set).
- **Useful for:** the "am I up" baseline and the denominator of P&L%. It is how
  much cash you put to work, not a market number — a holding can be a huge Cost€
  and a small live risk if it has fallen, or the reverse if it has run.
- **Don't:** read a large Cost€ as large current risk — a fallen position still
  carries its full cost basis.
- **Read with:** MktVal€ (what it's worth). P&L€ / P&L% (the gap, raw and
  size-normalized). Weight in `portfolio` (a share of *this*, not of market
  value). Invested (the deposits name for the same TOTAL).

### P&L€
- **Where:** `performance` (`P&L€`); `deposits` per-row (`Gain€`); deposits total
  is **Organic gain**
- **Type:** money, snapshot
- **Definition:** Unrealized profit/loss = MktVal€ − Cost€. On a buy-and-hold
  book the performance TOTAL equals deposits Organic gain.
- **Useful for:** the plain "how many euros up or down" read — rent, a car, a
  year of contributions. It doesn't tell you if the *funds* did well (a late
  deposit of a large sum that's slightly up can dominate) and it isn't
  annualized.
- **Don't:** use it to judge fund quality — a large recent deposit slightly up
  will dominate P&L€ even if the fund itself is mediocre.
- **Read with:** P&L% (same gap, comparable across holdings). P&Lctr (whose
  euros). TWR / XIRR (rates, not euros). Organic gain (the deposits TOTAL).
  `--diff` ΔP&L€ (the change over N days, not the level).

### P&L%
- **Where:** `performance` table, `--series`; `--metrics` (note on P&L€)
- **Type:** money, snapshot
- **Definition:** P&L€ as a percentage of cost basis.
- **Useful for:** comparing holdings that are different sizes: +€2k on a €5k
  lot is not the same story as +€2k on a €80k lot. It is still cost-basis
  (money-weighted). A position bought yesterday at +2% looks "hot" here yet
  barely drove the book's return (its Ctr% is tiny — its own TWR is a separate
  question).
- **Don't:** describe it as an annual or time-weighted return — it is a simple
  cost-basis ratio with no time dimension.
- **Read with:** TWR (the time-weighted percentage — use that to judge the
  fund). P&L€ (the euros this percentage is of). P&Lctr (size *and* gap; a
  modest P&L% on a huge lot can still be most of the book's euros). Ret% (the
  same idea per deposit, not per holding).

### P&Lctr
- **Where:** `performance` table
- **Type:** money-weighted share (sums to 100%)
- **Definition:** This holding's P&L€ as a fraction of the whole book's P&L€.
  `n/a` when net P&L is zero.
- **Useful for:** "whose euros made (or lost) the money" — useful when you want
  to know which line item actually moved the account. A big, recently-bought
  holding that's up a little can dominate it; that is not a reason to crown the
  fund, it's a reason to look at Ctr%.
- **Don't:** crown a fund for dominating P&Lctr without also checking Ctr% —
  size, not skill, may be the whole story.
- **Read with:** Ctr% (who drove the *time-weighted return*; the pair is the
  whole point of `--contrib` vs the main table). P&L€ / P&L% (the raw gap).
  Weight (size without the gain). Deposits **%P&L** (same idea sliced by lot,
  not by holding).

### ΔMktVal€ / ΔCost€ / ΔP&L€
- **Where:** `performance --diff N`
- **Type:** money, signed change
- **Definition:** Signed change in market value, cost basis, and P&L€ over N
  calendar days (money columns only). Union of ISINs held at either endpoint;
  held-but-unpriceable → unavailable. Composes with `--as-of`.
- **Useful for:** "what happened to the account this month" in euros, without
  mixing in rates. A deposit shows up as ΔCost€ and ΔMktVal€ together — that's
  cash in, not a return. A quiet month with no contributions and a falling
  market shows up as ΔP&L€ ≈ ΔMktVal€.
- **Don't:** mistake a ΔCost€ spike for investment gain — it means cash was
  deposited, not that the holdings appreciated.
- **Read with:** the main table's TWR / XIRR (the rates this deliberately omits).
  `--series` (the same days as a path of full snapshots, not as a delta).
  Organic gain (the since-inception euro gap these deltas add up toward).

### Weight
- **Where:** `portfolio` (cost-basis); `performance --contrib` (market-value)
- **Type:** share
- **Definition:** A holding's share of the book — by cost basis in `portfolio`,
  by current market value in `--contrib`.
- **Useful for:** allocation at a glance. Cost-weight says how you *deployed*
  cash ("I meant this to be 40% world"). Value-weight says where the risk *sits*
  now ("it has become 55% because it ran"). Rebalance decisions want value-weight;
  "did I follow the plan" wants cost-weight.
- **Don't:** assume cost-weight and value-weight agree — they diverge as positions
  run, and the two commands use different bases on purpose.
- **Read with:** Ctr% (return contribution — not the same as weight × TWR).
  P&Lctr (euro-P&L share). WTER (the blend is value-weighted, like `--contrib`).
  MktVal€ / Cost€ (the two denominators).

## Risk & drawdown

From the TWR wealth index, not the euro line (deposits would paper over a hole). DDdur is the worst hole's length; SinceHi is how long you've been off the current peak.

### Volatility (Vol)
- **Where:** `performance` table (`Vol`), `--series`, `--metrics`
- **Type:** time-weighted, annualized
- **Definition:** Standard deviation of daily TWR returns × √252. Flagged `*` on
  short history or when the first stored close is after the first trade.
- **Useful for:** how bumpy a typical stretch is — useful for "could I ignore
  this for a year without opening the app." Higher means wider swings in *both*
  directions; it does not say the worst hole was deep (that's MaxDD) and it
  treats upside noise as risk. The `*` means the annualization is a guess.
- **Don't:** read it as maximum possible loss or as a forecast of the next crash.
- **Read with:** MaxDD (the worst hole, not typical bumpiness). TWR / CAGR (was
  the bumpiness paid for). TE (bumpiness *versus a benchmark*, not versus zero).
  CAGR (different year-length: 365 calendar days vs 252 trading days).

### Max Drawdown (MaxDD)
- **Where:** `performance` table (`MaxDD`), `--series`, `--metrics`
- **Type:** time-weighted
- **Definition:** The deepest peak-to-trough decline of the time-weighted wealth
  index, `min(wealth_t / peak_t − 1)` over the path (sampled daily), where
  `peak_t` is the running maximum up to `t`. Reported as a negative number
  (`0` = no drawdown); RecFac divides by its magnitude, `|MaxDD|`.
- **Useful for:** the gut-check before you add more of the same allocation —
  "could I sit through that again?" A deep MaxDD on a broad index is historically
  normal; the question is whether *you* would have held. It is not a forecast of
  the next crash, and it is silent on how long the hole lasted and on whether
  you're in one *right now*.
- **Don't:** use MaxDD alone to assess your current situation — pair it with
  SinceHi to know whether you're still in a hole, and DDdur to know how long the
  worst episode lasted.
- **Read with:** DDdur (how long that worst episode lasted). SinceHi (the current
  dip, which may be shallower). Vol (typical bumpiness). RecFac (did TWR pay you
  for sitting through it?). TWR (the return this is the cost of).

### MaxDD Duration (DDdur)
- **Where:** `performance --metrics` (`DDdur`); `--metrics --series`
- **Type:** calendar days
- **Definition:** Length of the *deepest* drawdown episode, peak → recovery (or
  peak → the as-of / last valued day if still underwater). Can jump when a new,
  deeper (and younger) episode becomes the deepest.
- **Useful for:** whether the worst hole was a sharp crash-and-rebound or a
  years-long grind — those feel different even at the same MaxDD. A jump in DDdur
  is not "the old hole got longer"; it often means a *new* episode just became
  the deepest.
- **Don't:** assume a sudden jump in DDdur means the same hole got longer — it
  usually means a new, deeper episode has just become the worst on record.
- **Read with:** MaxDD (the depth of that same episode). SinceHi (how long you've
  been off the *current* peak, regardless of depth). Underwater (every episode
  added up, not just this one). RecFac (return per unit of that depth).

### Days Since High (SinceHi)
- **Where:** `performance --metrics` (`SinceHi`); `--metrics --series`
- **Type:** calendar days
- **Definition:** Days since the *current* running peak (0 = the as-of / last
  valued day is a new high). Resets at every new high.
- **Useful for:** "am I in a dip *right now*, and for how long?" — the mood
  number. A long SinceHi with a small MaxDD is a dull sideways stretch; a short
  SinceHi with a huge MaxDD is "the crash was last month." Neither is DDdur.
- **Don't:** confuse it with DDdur — SinceHi is the current open dip; DDdur is
  the worst historical episode (which may be long closed).
- **Read with:** DDdur (the worst episode's length, not this one). MaxDD (how
  deep the worst hole was). Underwater (lifetime time spent recovering). TWR
  (whether new highs have been worth waiting for).

### Underwater (Underwtr)
- **Where:** `performance --metrics` (`Underwtr`); `--metrics --series`
- **Type:** calendar days (a count, not a share)
- **Definition:** Total days spent below a prior peak, summed across all
  drawdown episodes. The command prints days, not a percentage of history —
  divide by the window if you want a share.
- **Useful for:** how much of the path was spent recovering rather than making
  new highs. A book that grinds up with frequent small dips can print more
  underwater days than one dramatic crash-and-recovery; that's a different
  temperament, not necessarily a worse TWR.
- **Don't:** compare the raw day count across books of different ages — divide
  by the window length to get a comparable share.
- **Read with:** DDdur (one episode — the deepest). SinceHi (only the open dip).
  MaxDD (depth, not time). TWR (the return earned in the time *not* underwater).

### Recovery Factor (RecFac)
- **Where:** `performance --metrics` (`RecFac`); `--metrics --series`
- **Type:** ratio (cumulative TWR, not CAGR)
- **Definition:** Cumulative TWR ÷ |Max Drawdown|. `n/a` when there is no
  drawdown. Above 1 means the cumulative TWR has more than offset the deepest
  hole.
- **Useful for:** "was the ride worth the worst pain?" A high TWR with a −40%
  MaxDD and a RecFac below 1 means you have not yet been paid for the scar.
  Don't annualize the numerator in your head — this is not Calmar (CAGR ÷
  |MaxDD|), which e1f does not report.
- **Don't:** call it Calmar — Calmar divides CAGR by |MaxDD|; this divides
  cumulative TWR. The two can rank funds in opposite order over short windows.
- **Read with:** TWR and MaxDD (the two inputs). CAGR (the annualized return
  Calmar would use instead). DDdur (pain that lasted years vs a week, at the
  same RecFac). Vol (average bumpiness, not the worst hole).

## Extremes

Tails of the same TWR series. A "day" is a gap-bridged period, not always a calendar day.

### Best Day / Worst Day (Best / Worst)
- **Where:** `performance --metrics`; `--metrics --series` (`Best`, `Worst`)
- **Type:** time-weighted, single period
- **Definition:** The single best and worst TWR *period* on the gap-bridged daily
  series, with its date. A weekend or missing FX close becomes one "day."
- **Useful for:** the size of the tails you've actually lived through — useful
  as a "this is what a bad print looks like on this book," not as a VaR. One
  gap-bridged period can span a long weekend; don't treat it as a calendar day
  when comparing to a news headline.
- **Don't:** compare a gap-bridged "day" directly to a single calendar-day
  headline from a news source — a Friday-to-Monday gap is reported as one period.
- **Read with:** Best/Worst Month (the same tails, chain-linked inside a
  `YYYY-MM` — steadier). G/L (|best| ÷ |worst| of this pair). Vol / MaxDD (how
  typical vs how deep the path is around those prints). Trailing 1M (recent, not
  the record).

### Best Month / Worst Month
- **Where:** `performance --metrics`
- **Type:** time-weighted, calendar month
- **Definition:** Best and worst calendar-month return (daily returns chain-linked
  within each `YYYY-MM`; partial first/last months included).
- **Useful for:** a good/bad *stretch* rather than a single print — closer to
  how a year actually felt. Partial first/last months are included as-is, so a
  two-week inception month can look like a hero or a villain without being a
  full month of risk.
- **Don't:** treat a partial inception month as a full month of risk — note
  whether the best/worst month is the first or last entry in the series.
- **Read with:** Best/Worst Day (one period vs a bucketed month). Trailing 1M
  (*the last* month, not the best/worst in history). TWR (the full-sample
  compound those months add up to). Vol (whether months this wild are typical).

### Max Gain / Max Loss (G/L)
- **Where:** `performance --metrics` (`Max Gain / Max Loss`); `--metrics --series`
  (`G/L`)
- **Type:** ratio
- **Definition:** |Best Day| ÷ |Worst Day|. Above 1 means the best period beat
  the worst period in size.
- **Useful for:** a quick "are the tails lopsided?" read. It ignores how *often*
  those days happen — one lucky +8% and a −7% crash still print > 1. Don't use
  it as a substitute for RecFac (return per unit of *drawdown*) or for Vol
  (everyday bumpiness).
- **Don't:** use it as a substitute for RecFac or Vol — it ignores frequency
  entirely and is based on just two observations.
- **Read with:** Best/Worst Day (the two inputs, with dates). RecFac (pain as
  MaxDD, not as one day). Vol (frequency-blind here, frequency-aware there).

## Trailing returns

Recent TWR at fixed calendar windows. n/a until the book is old enough; not annualized.

### Trailing 1M / 3M / 6M (1 Month / 3 Months / 6 Months)
- **Where:** `performance --metrics` (`1 Month`, `3 Months`, `6 Months`)
- **Type:** time-weighted
- **Definition:** TWR over the trailing 1-, 3-, and 6-calendar-month window
  ending at the latest valued day, read off the wealth index. `n/a` when the
  window starts before inception. 1Y/2Y aren't reported yet for the same reason.
- **Useful for:** recent momentum at a fixed horizon — "is this a hot quarter or
  a dead one" — without pretending it's since-inception skill. On a young book
  the longer windows stay `n/a` until there's enough history; that's honesty,
  not a bug. Not annualized: a +4% trailing 1M is not "48% a year."
- **Don't:** annualize these — a +4% trailing 1M is not "48% a year," and the
  windows are too short for annualization to be meaningful.
- **Read with:** TWR / CAGR (since inception, not trailing). Best/Worst Month
  (the record month *anywhere* in the history, not the last one). `--series`
  (the path that produced the window). XIRR (recent deposits will move XIRR
  more than these).

## Attribution & contribution

Who drove the book's TWR — not who has the euros (that's P&Lctr).

**Return, Weight, and Contribution are three distinct things:**
Return = how a holding performed. Weight = how much of the book it represented.
Contribution = how much of the book's return it drove. Weight × return ≠ contribution:
Ctr% uses beginning-of-period weights along the full path, not today's weight.

### Ctr%
- **Where:** `performance --contrib`
- **Type:** time-weighted contribution (sums to the TOTAL TWR)
- **Definition:** Each holding's share of the book's TWR, computed via **Cariño**
  linking. Each day splits into arithmetic contributions
  `weight_prev × holding_return` that sum to that day's portfolio return; Cariño
  log-smoothing (the `ln(1+r)/r` coefficient) rescales those daily contributions
  so each holding's total sums exactly to the multi-period TOTAL TWR (they sum,
  they do not chain-multiply).
- **Useful for:** which holdings actually *produced* the book's return — the
  right tool for "should I keep this line." A fund you bought last month at high
  weight contributes little, because its beginning-of-period weights were small;
  a small early holding that ran the whole way can dominate Ctr% while looking
  modest on P&Lctr. Current weight × TWR is not this.
- **Don't:** multiply today's Weight × TWR and call it contribution — the path
  of beginning-of-period weights is what matters, not today's snapshot.
- **Read with:** P&Lctr (whose euros — the pair you should always look at
  together). TWR (the holding's own return, which Ctr% is not). Weight on
  `--contrib` (value-weight *now*, not the path of weights Ctr% used). %P&L
  (lot-level euros, not holding-level TWR).

## Deposits & capital

The same TOTAL P&L as performance, sliced by contribution instead of by holding.

### Invested
- **Where:** `deposits`
- **Type:** money
- **Definition:** Total contributions — shares × price + fee across valuable
  buys. Same TOTAL as performance **Cost€**. Unpriceable deposits are excluded,
  not zeroed.
- **Useful for:** the capital base — "how much cash have I actually put in."
  Denominator of ROIC. Growing this on purpose (DCA) is not the same as the
  account growing; that's why Organic gain exists.
- **Don't:** confuse growth in Invested with investment return — DCA makes it
  climb by design, independent of market performance.
- **Read with:** Reported (what it's worth). Organic gain / ROIC (the market
  part). Cost€ (the performance name for the same TOTAL). XIRR (the rate earned
  *on* this capital, including *when* it arrived).

### Reported (market value)
- **Where:** `deposits`
- **Type:** money, snapshot
- **Definition:** Current market value of everything you hold. Reconciles with
  the `performance` TOTAL **MktVal€**.
- **Useful for:** the account value next to what you put in — the two-line
  "broker homepage" read. If Reported jumped because you made another buy,
  Organic gain will not jump with it; that's working as designed. (e1f ingests
  ETF buys only, so a bare cash transfer moves neither line.)
- **Don't:** attribute a jump in Reported to investment performance if Organic
  gain didn't rise with it — the jump was a new buy.
- **Read with:** Invested (the capital). Organic gain (the market-driven gap).
  MktVal€ (the performance name for the same TOTAL). `--diff` ΔMktVal€ (the
  short-window change).

### Organic gain
- **Where:** `deposits`
- **Type:** money
- **Definition:** Reported − Invested. Same number as the performance TOTAL
  **P&L€** (buy-and-hold, valuable set). Per-lot it's `Gain€`.
- **Useful for:** separating growth *earned* from growth that's just fresh
  contributions — the number to watch if you DCA monthly and don't want to fool
  yourself that the account "did well" because you just bought more. A new buy
  raises Reported and Invested together and leaves this (almost) unchanged.
- **Don't:** mistake a flat Organic gain for "no return" if you've been DCA-ing
  heavily — the Invested base grew too, and ROIC or TWR tells the rate story.
- **Read with:** P&L€ (the performance TOTAL). ROIC (this ÷ Invested). XIRR (the
  annualized rate on the same cash). Ret% / %P&L (which lots produced it).

### ROIC
- **Where:** `deposits`
- **Type:** money-weighted ratio
- **Definition:** Organic gain ÷ invested.
- **Useful for:** a single "am I ahead on the capital I deployed" ratio, since
  inception. Quick, and easy to explain. It is *not* a rate per year: 20% in two
  months and 20% in two years look the same, and it does not care *when* the
  capital arrived (a late lump sits in the denominator immediately).
- **Don't:** treat it as an annual rate — 20% in two months and 20% in two years
  look identical; use XIRR when time and scheduling matter.
- **Read with:** XIRR (same money, but annualized and timing-aware — use that
  when the schedule matters). Organic gain and Invested (the two inputs). TWR
  (holdings skill, not capital skill). P&L% (per-holding version of a similar
  idea).

### Ret% (per deposit)
- **Where:** `deposits` per-deposit table
- **Type:** money-weighted return, buy-and-hold
- **Definition:** How much one buy's shares have grown: (Value€ − Amount€) ÷
  Amount€.
- **Useful for:** spotting which *purchases* did well or badly — "the March
  dip-buy vs the September top-up." An early lot can show a huge Ret% while the
  fund's TWR is modest, because the lot's own entry date and price differ from
  the whole holding period; that's lot luck, not a reason to overweight the fund
  further without looking at Ctr%.
- **Don't:** attribute a strong lot-level Ret% to fund quality — it reflects one
  buy's entry date and price, not the fund's time-weighted return (TWR
  neutralizes contribution timing, so later buys don't dilute it).
- **Read with:** TWR (the fund's time-weighted path). %P&L (this lot's share of
  *euros*, not its own return). XIRR (the whole-book rate those lots jointly
  produced). Organic gain (the TOTAL they sum to).

### %P&L (per deposit)
- **Where:** `deposits` per-deposit table
- **Type:** money-weighted share
- **Definition:** One buy's share of the portfolio's total P&L. Undefined when
  net P&L is zero.
- **Useful for:** which specific deposits made (or lost) the money — two buys of
  the same ISIN can have very different %P&L if one was larger or earlier. Use
  it to audit contribution timing; use P&Lctr when you care about the fund, not
  the lot.
- **Don't:** compare lots of different sizes by Ret% alone — %P&L shows how much
  of the book's total gain a lot actually represents.
- **Read with:** P&Lctr (same idea by holding). Ret% (this lot's own return).
  Organic gain (the TOTAL). Ctr% (time-weighted, not euros).

## Fees

TER drag on current market value, not cost basis. Missing TER dilutes WTER toward 0.

e1f reports returns net of fund TER (it is already embedded in fund NAV). Transaction
fees paid at purchase are included in Cost€ but are a one-time cost, not an ongoing
drag. Tax, bid/ask spread, and FX transaction costs are not modeled. Do not describe
reported TWR or CAGR as "gross" unless you know what the fund factsheet embeds; do not
describe it as "after-tax net" either — e1f has no tax model.

### WTER (weighted TER)
- **Where:** `performance --series` (`WTER`); `portfolio` total line
  (`weighted avg TER`) — not a column there
- **Type:** market-value-weighted ratio
- **Definition:** Market-value-weighted average total expense ratio. Holdings
  with no TER metadata contribute 0, which dilutes the average downward.
- **Useful for:** the blended annual fee drag on the whole book — useful when
  you're choosing between a 0.12% core and a 0.45% satellite and want the
  *portfolio* number, not a per-fund trivia. A missing TER is not "free"; it
  quietly pulls WTER down, so a suspiciously cheap blend is a cue to fill in
  metadata.
- **Don't:** treat a suspiciously low WTER as a cheap blend — missing TER
  metadata silently drags the average toward zero.
- **Read with:** Fee€/yr (the same drag in euros, which scales as the book
  grows). per-fund TER on `portfolio` (the inputs). Weight on `--contrib`
  (value-weight, the same basis). TWR / CAGR (the return this is a haircut on;
  e1f does not compute a gross-vs-net pair).

### Fee€/yr
- **Where:** `performance --series` (`Fee€/yr`); `portfolio` per-row (`Fee/yr`)
  and the total line
- **Type:** money, annual estimate
- **Definition:** Estimated annual fee = WTER × market value at that date
  (TER × AUM per holding, then summed).
- **Useful for:** the bill in euros — "is this a dinner a month or a holiday a
  year." More tangible than 0.22%, and it grows automatically as MktVal€ grows,
  which a rate hides. It is an estimate at today's AUM, not a cash debit e1f
  observed.
- **Don't:** confuse this estimate with a cash debit e1f has observed — it is a
  projection from current AUM and the TER metadata you have entered.
- **Read with:** WTER (the rate). MktVal€ (the AUM it scales with). Organic gain
  (fees are small next to a good year and loud next to a flat one). `--series`
  (watch the euro fee climb as the book compounds).

## Benchmark-relative

Was leaving the market worth it? Out% is the arithmetic gap; RelStr is compounded. Read n; don't trust Beta without R².

Statistical benchmark metrics (Beta, R², TE, IR, ρ) are estimated from shared daily
observations. They are descriptive, not predictive, and become more meaningful as n
grows. A high value on thin n is still thin n.

### Beta
- **Where:** `benchmark`
- **Type:** regression, over the shared window
- **Definition:** Sensitivity of the book to the benchmark = cov(rp, rb) /
  var(rb). ~1 tracks it, <1 is defensive, >1 is amplified.
- **Useful for:** "if that index sneezes, how hard do I catch it?" A world-heavy
  book should sit near 1 vs MSCI World; a big EM or small-cap sleeve pulls it
  around. Without R², Beta is a lonely slope — a 0.4 Beta vs a poor mirror does
  not mean you are defensive, it means you picked the wrong benchmark.
- **Don't:** interpret Beta without first checking R² — a low Beta against a
  benchmark you barely resemble does not mean you are defensive.
- **Read with:** R² (whether this Beta means anything). n (whether the window is
  ripe). TE (how far you *drift*, not how much you *move with*). RelStr / Out%
  (the return gap that Beta does not capture). Vol (absolute bumpiness).

### R²
- **Where:** `benchmark`
- **Type:** regression, over the shared window
- **Definition:** Share of the book's variance the benchmark explains =
  corr(rp, rb)².
- **Useful for:** picking which row on the benchmark table to trust. High R²:
  Beta, TE, and IR are about that index. Low R²: you are looking in the wrong
  mirror — don't congratulate yourself on "alpha" against a fund you don't
  resemble. On a young book, wait for n before believing a high R² either.
- **Don't:** congratulate yourself on outperformance (Out%, IR) when R² is low —
  you may simply be measuring against the wrong benchmark.
- **Read with:** Beta (the slope R² licenses — the one metric a low R² truly
  undercuts). n (sample size). TE / IR (well-defined at any R²; a low R² doesn't
  void them, it means they measure drift from a benchmark you don't resemble, so
  they aren't evidence of skill). ρ (pairwise fund co-movement; R² is the book
  vs one benchmark).

### Tracking Error (TE)
- **Where:** `benchmark`
- **Type:** annualized
- **Definition:** Standard deviation of the active return (rp − rb) × √252.
- **Useful for:** how far you drift from the benchmark — "hug" vs "do your own
  thing." A dedicated satellite sleeve *should* print higher TE vs a world
  index; a "world tracker plus one cousin" that prints high TE is accidental
  drift. High TE with a tiny Out% is wandering without being paid.
- **Don't:** assume high TE is bad — a deliberate satellite sleeve should drift;
  the question is whether you are being paid for it (check IR).
- **Read with:** IR (return earned *per unit* of this). Out% / RelStr (the gap
  itself, not its volatility). R² / Beta (are you even on this benchmark). Vol
  (absolute bumpiness; TE is relative). n.

### Information Ratio (IR)
- **Where:** `benchmark`
- **Type:** annualized ratio
- **Definition:** √252 × mean(daily rp − rb) / stdev(daily rp − rb) — annualized
  mean active return ÷ tracking error (the same √252 as TE).
- **Useful for:** "was the deviation worth it?" A noisy 2% Out% with huge TE is
  a low IR: you took tracking risk and didn't get paid. A small Out% with tiny
  TE can be a fine IR. On a young book this is the easiest number to overfit —
  always read n.
- **Don't:** trust IR without checking n and R² — it is the easiest statistic
  to overfit on a young book against a poorly-matched benchmark.
- **Read with:** TE (the denominator). Out% / RelStr (the raw gap). R² (a low R²
  doesn't void IR — it means the gap is measured against a benchmark you don't
  resemble). n. TWR (the book return IR is not a substitute for).

### Relative Strength (RelStr)
- **Where:** `benchmark`
- **Type:** compounded ratio, over the window
- **Definition:** (1 + portfolio TWR) ÷ (1 + benchmark TWR).
- **Useful for:** did €1 in your book grow to more than €1 in the benchmark —
  the geometric form of outperformance. RelStr 1.05 means the book turned €1
  into 5% more wealth than the benchmark did over the *same* shared window.
  Prefer this when compounding matters; it is not annualized, and each row's
  window can differ, so read n.
- **Don't:** compare RelStr across benchmark rows without checking n — each row's
  window can differ, so the numbers are not on the same footing.
- **Read with:** Out% (the same comparison as an arithmetic gap — 0.12 − 0.10 =
  2 points vs 1.12 / 1.10 ≈ 1.018). IR (was the gap worth the drift). TWR on
  both legs. n. R² (a RelStr vs a poor mirror is trivia).

### Out%
- **Where:** `benchmark`
- **Type:** cumulative, over the window
- **Definition:** Portfolio TWR − benchmark TWR (active return, arithmetic). Not
  annualized; each benchmark's window differs.
- **Useful for:** a simple over/under you can say in a sentence ("+2.1% vs
  World"). Easy to over-read on a short window: 3% Out% on 40 days is a coin
  flip. Use it to scan the table; use RelStr when you care how €1 compounded,
  and IR when you care whether the gap was worth the tracking risk.
- **Don't:** over-read a short-window Out% — a 3% gap on 40 shared days is noise,
  not skill.
- **Read with:** RelStr (compounded). IR / TE (gap per unit of drift, and the
  drift). n (mandatory). R² (right mirror?). TWR (both legs).

### n (observations)
- **Where:** `benchmark`, `correlation`
- **Type:** count
- **Definition:** Number of shared daily-return observations the statistic was
  estimated from.
- **Useful for:** trust. A young book gives small n — Beta, IR, and ρ are
  preliminary until it grows. Raising `--min-overlap` *hides* thin rows; it does
  not make a short history true. When two benchmarks print very different n,
  their Out%/IR are not comparable.
- **Don't:** raise `--min-overlap` thinking it makes a short history more
  reliable — it only hides thin rows; the underlying n is unchanged.
- **Read with:** whatever column you're about to believe (Beta, IR, ρ, Out%,
  RelStr). R² (a high R² on tiny n is still tiny n). SinceHi / TWR window (how
  long the *book* has been alive — n is shared days, which can be shorter).

## Correlation

Do two funds move together? That's redundancy, not shared holdings (that's overlap, experimental).

### ρ (Pearson correlation)
- **Where:** `correlation`
- **Type:** over each pair's shared window
- **Definition:** Correlation of two funds' daily EUR returns, in [−1, 1].
- **Useful for:** redundancy and diversification of *behaviour*. Near 1 means
  two funds move together — one may be a second helping of the same bet even if
  the names on the factsheet differ. Low or negative means they actually
  diversify each other. Combined with real weight: two 2% sleeves at ρ = 0.95
  are trivia; two 30% sleeves at ρ = 0.95 are the book.
- **Don't:** infer common holdings from high correlation — two funds can move
  together without overlapping; correlation measures behaviour, not portfolio
  contents.
- **Read with:** Correlation distance / clusters (the same ρ, grouped). n (thin
  pairs lie). Weight (is the pair big enough to matter). R² vs a benchmark (the
  book-level cousin). Overlap is experimental and asks what they *hold*, not how
  they *move*.

### Correlation distance & clusters
- **Where:** `correlation` — distance is the internal clustering basis (not a
  printed column); what the report emits is the **clusters** it produces, each
  with its weight as a **% of the correlation universe**, plus a **combined
  weight** on every redundant-pair row
- **Type:** distance (internal) → cluster membership + weight (emitted)
- **Definition:** The distance `√(½(1 − ρ))` — a proper metric that shrinks as
  ρ → 1 — feeds average-linkage clustering, cut at a dendrogram height ≈
  `--cluster-rho`. The output shows cluster membership and each cluster's combined
  weight; redundant-pair rows show the pair's combined weight. Both weights are
  normalized over the **correlation universe** (held funds with a usable EUR
  return series), so a shown % is a share of that correlated sub-portfolio and can
  exceed the fund's true portfolio weight.
- **Useful for:** seeing how many genuinely distinct bets you hold, instead of
  counting tickers. Average-linkage clusters of "moves alike" are a map of
  sleeves: three world-equity funds in one cluster are one bet. A cluster is
  not a shared-holdings claim.
- **Don't:** read cluster membership as a shared-holdings claim — clusters reflect
  return behaviour, not underlying portfolio overlap (that's the experimental
  `overlap` command). And don't read a cluster's % as a slice of the whole book —
  it's a share of the correlation universe, not of total portfolio value.
- **Read with:** ρ (the pairwise number this is built from). the cluster table
  (the cut at `--cluster-rho`). Weight (a tight cluster of tiny sleeves is
  still tiny). Ctr% (do those lookalike funds also *drive* the TWR together).

## What isn't measured

These concepts are not covered by any stable metric in this glossary (some have
experimental commands, noted inline):

- **Liquidity** — how quickly a position can be sold at or near the quoted price
- **Tax drag** — investor-specific; not modeled (e1f has no tax engine)
- **Bid/ask spread and execution quality** — transaction costs beyond the explicit fee
- **FX transaction costs** — currency conversion friction on cross-currency buys
- **Sequence-of-returns risk** — sensitivity of future withdrawals to return ordering
- **Factor exposure** — growth/value/size/momentum tilts (needs look-through data)
- **Country/sector concentration** — geographic and sector HHI (see experimental `concentration`; region UNAVAILABLE since ADR-0018)
- **Underlying exposure overlap** — common holdings across funds (see experimental `overlap`)
- **Sharpe, Sortino, Treynor, Jensen alpha** — gated on a risk-free rate (€STR); deferred (ADR-0033)
- **Rolling risk metrics** — 252-day rolling Vol/MaxDD; deferred until the book matures (ADR-0033)
