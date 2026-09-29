# ADR-0055 — `limitbuy`: close-only worst-case bound on a buy-limit order

**Scope:** add an experimental `e1f limitbuy` command (ADR-0024 tier). It compares a
one-off buy-limit order placed under the price with buying at the price, over every
start day of one ETF's stored EUR close history. The DB holds only daily closes, so
the result is a *bound*, not an estimate, and the command reports it as one
(`Status.BOUNDED`). The EUR price-series, catalog and crash-window helpers that
`backtest` and `seasonality` each carried move to `e1f.experimental.common`
(§8), and all three commands use them.

## Context

Before a planned buy, the question was whether to place limit orders under the
current price (e.g. €13.00 against a €13.07 close) instead of buying at once.
`backtest` answers monthly contribution-timing questions (ADR-0019 through
ADR-0023), not a single order's fill risk.

A limit order fills on any intraday touch of its price, but `prices` stores one
close per day: ftgo returns each day's high and low, but `fetch` keeps only the
close. A close-only replay therefore
cannot say how often an order *would* have filled — only that it filled at least
whenever a close went through the limit. The ADR-0019 invariant applies: no
result may imply information its provenance does not establish. So the command
reports what closes do establish — a worst case for the limit order — and marks
every figure as one.

## Decisions

### 1. The comparison — one order against buying at the start close

For each start day `t` (a stored close at or after `--from` with `Expiry` later
closes stored):

- **Buy now:** the amount buys at close `P_t`.
- **Limit:** a buy-limit at `L = P_t × (1 − Below)` is placed after close `t` and
  stays open for the next `Expiry` closes, `t+1 … t+Expiry`. If it has not filled
  by then, the amount buys at close `P_{t+Expiry}`.

The outcome is **ΔShares** = limit shares ÷ buy-now shares − 1. That is
`1 / (1 − Below) − 1` when the order fills and `P_t / P_{t+Expiry} − 1` when it
does not. After expiry both paths hold the same fund, so ΔShares is the permanent
difference and no valuation date has to be chosen.

### 2. Close-only fill rule — strictly below the limit

The order fills, at exactly `L`, on the first close in `t+1 … t+Expiry` that is
**strictly below** `L`. A close below `L` means the market traded through the
limit, and price priority fills a resting bid at `L` before any trade below it. A
close *at* `L` does not guarantee a fill (queue position), so it does not count.
Filling at `L` rather than at that lower close never credits the order with a price
it was not guaranteed.

### 3. Why every figure is a lower bound

Compare the rule with the same order in the real market, one start day at a time:

- **The rule fills.** Some close in the window is below `L`, so the market traded
  through `L` and the real order filled too, at `L` or better (a gap-down open
  fills below `L`). Real ΔShares ≥ the rule's.
- **The rule does not fill, the real order does** (an intraday touch no close
  shows). Every close in the window is ≥ `L`, including `P_{t+Expiry}`. The real
  order bought at a price ≤ `L` ≤ `P_{t+Expiry}`, so it holds at least as many
  shares as the rule's expiry purchase.
- **Neither fills.** The two are identical.

So on every start day the real order fills whenever the rule does, and its
ΔShares is at least the rule's. Every summary in §4 is monotone in those per-day
values: the fill share, the share of days with ΔShares ≥ 0, the mean, the median,
the nearest-rank P10 and the minimum. Each is therefore a lower bound for the
real order. Rows carry `Status.BOUNDED` and every figure's header carries `≥`.

The bound assumes no fees or spread, a full fill whenever the market trades
through `L`, and an accumulating series (§5).

### 4. Summaries and overlap disclosure

Each `(Expiry, Below)` row summarises its `Windows` start days:

- `Fill%` — share of start days on which the order filled.
- `Win%` — share with ΔShares ≥ 0 (at least as many shares as buying at once).
- ΔShares `Mean`, `Median`, `P10` and `Worst`. `P10` is nearest rank, the
  `⌈0.1 × Windows⌉`-th smallest; `Worst` is the minimum.

No best case is shown: a filled order's ΔShares is always `1 / (1 − Below) − 1`,
so the maximum carries no information.

Consecutive start days share most of their window, so they are not independent.
Each row reports `Indep = ⌈Windows / Expiry⌉`, the number of non-overlapping
windows (every `Expiry`-th start day). A row with `Indep < 24` is marked `*` —
illustrative, not evidence. A row with no complete window is `UNAVAILABLE` and
shows `n/a`, never zero. Rows are tabulated in a fixed order; none is fitted,
ranked or recommended (ADR-0019 §6).

### 5. The series — the shared EUR close, with two disclosed caveats

Closes come from the same EUR series as `backtest` and `seasonality`: EUR funds
pass through, and a foreign-currency fund's close converts at the nearest-prior
stored EUR/FX rate (ADR-0010). Two cases weaken §3, and both are disclosed:

- **Converted series.** The bound is exact for the series itself, but a real
  order rests on one listing in one currency. A converted USD series only
  approximates a EUR-quoted listing of the same fund, so the header names the
  conversion.
- **Distributing funds.** An ex-dividend drop looks like a dip, and the buy-now
  path's dividend is not counted, which flatters the limit order. The command
  warns on stderr, as `backtest` does.

### 6. CLI surface

```
e1f limitbuy --isin ISIN [--below PCT …] [--limit PRICE …] [--expiry DAYS …]
             [--from YYYY-MM-DD] [--to YYYY-MM-DD] [--show-status] [--explain]
```

- `--below` takes percents under the start close; the default is 0.5, 1, 2, 3
  and 5.
- `--limit` takes a EUR price and converts it against the last close as
  `1 − PRICE ÷ last close`. A price above the last close is refused: it would
  fill at once, which is buying now.
- `--expiry` is in trading days; the default is 5 and 21 (about a week and a
  month).

There is one row per `(Expiry, Below)` pair, sorted, with duplicates dropped.

### 7. Provenance

`MetricContract` `limit_buy_close_bound_v1` is limited by intraday lows, which
would turn the bound into a point estimate. Its limitations list the fill
assumptions, the converted-series and distributing caveats, and window overlap.
`--show-status` adds each row's `Status`; `--explain` adds the provenance block.

### 8. The price-history helpers move to `e1f.experimental.common`

`eur_series`, `price_catalog`, `candidate_listing` (was `_candidate_listing`),
`CRASH_WINDOWS` and `crash_split` move out of `backtest`, and `seasonality`'s
duplicated copies are deleted. A third copy for `limitbuy` would have given one
fact three homes; ADR-0024 §2 puts primitives used only by experimental commands
in `e1f.experimental.common`. Behaviour is unchanged.

## Rationale

- **A bound is all the stored data supports.** Treating closes as fills would
  report a fill rate the data cannot establish; labelling the result a worst
  case keeps it honest and still answers the practical question — how bad can
  waiting for a lower price be.
- **ΔShares isolates the order.** It is the same after expiry whatever the market
  does next, so the table measures the order, not the later market.
- **Strictly-below makes §3 rest on price priority alone,** with no assumption
  about queue position at the limit price.

## Consequences

- New module `src/e1f/experimental/limitbuy.py` (pure core + command), registered
  in `cli.py`, in the experimental import-linter layer, and in the frozen
  CLI-surface test.
- `experimental/common.py` gains the shared price-history helpers;
  `backtest.py` and `seasonality.py` lose their copies.
- `tests/test_limitbuy.py`: a pinned-date regression with hand-computed fills and
  every summary; the strictly-below boundary; fill rate monotone in depth and
  expiry (property test); UNAVAILABLE and illustrative rows; the
  converted-series and distributing disclosures; `--limit` conversion and
  refusal; `--explain`.
- README and CLAUDE.md list the command. `data/glossary.md` adds it to the
  experimental commands it leaves out of scope (ADR-0034 §5).

## Deferred (not in this ADR)

- **Intraday lows.** ftgo's `get_historical_prices` already returns daily
  open/high/low/volume from the call `fetch` makes; `fetch` keeps only the close.
  Storing the low (a schema change) would turn these bounds into point
  estimates. yfinance is not a source for it.
- **Fees and spread.** Both brokers' stored trades carry no fee today; a spread
  model would shift both paths and is left out.
- **Ladders.** Splitting one amount across several limits combines rows; it is
  not modelled.
