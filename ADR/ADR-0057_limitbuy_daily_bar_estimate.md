# ADR-0057 — `limitbuy`: an estimate from daily bars beside the close-only bound

**Scope:** `limitbuy` (experimental tier, ADR-0024) gains a second table. It
estimates the same buy-limit orders from each day's stored open and low (ADR-0056)
and reports the result as `Status.CALCULATED`. ADR-0055's close-only bound, its
fill rule and its table are unchanged. When no bars are stored, the estimate is
`UNAVAILABLE` with a typed reason.

## Context

ADR-0055 answers "buy now or place a limit under the price?" with a worst case: a
fill needs a later close strictly below the limit, so intraday fills are missed.
ADR-0056 now stores each day's open, high, low and volume from ftgo. With those,
the same orders can be replayed against the day's actual range. The result is an
estimate under stated assumptions rather than a bound.

## Decisions

### 1. The fill rule on each open day

The order and the buy-now path are as in ADR-0055 §1: limit `L = P_t × (1 − Below)`
placed after close `t`, open on days `t+1 … t+Expiry`, buying at close
`P_{t+Expiry}` if unfilled. On each open day, in order:

- **The day opens below `L`: it fills at the open.** The resting bid meets a lower
  opening price, so the order buys at that price. ΔShares = `P_t / open − 1`.
- **Otherwise, the low is strictly below `L`: it fills at `L`.** A trade below the
  limit means price priority has already filled the resting bid at `L`.
  ΔShares = `1 / (1 − Below) − 1`.
- **A low exactly at `L` does not fill,** for the queue-position reason in
  ADR-0055 §2. An open exactly at `L` is not below it, so the low decides.

### 2. What the estimate assumes, and why it is CALCULATED rather than BOUNDED

The estimate assumes that:

- the order rests on the listing whose bars ftgo reports;
- a trade through the limit fills the whole order;
- there are no fees, spread or partial fills;
- each stored bar is correct.

A broker that fills against its own bid/ask instead of the exchange's trades, such
as XTB, can fill differently, and no stored data shows how. The figures are
therefore a point estimate under these assumptions, not a guarantee. The contract
`limit_buy_bar_estimate_v1` names the broker's own fills or quotes as what it is
limited by.

### 3. Only usable bars, and whole windows only

A bar is **usable** when three things hold:

- **`volume > 0`.** A day with no trades carries a price nobody traded at.
- **`low ≤ min(open, close)`.**
- **`high ≥ max(open, close)`.** A bar whose range does not contain its own open
  and close contradicts itself.

The check runs on the stored (native) values. A start day whose window contains
any open day without a usable bar is **excluded** and counted in the `Excl`
column. Such a window is never completed from closes, because a hybrid would mix
two methods in one figure. ADR-0056's quality table shows why this matters: older
London bars are largely unusable, so the estimate covers fewer years than the
bound there.

### 4. EUR conversion at the close's rate

Each bar converts at the rate its EUR close used: `eur_close / native_close`. A bar
and its close therefore never disagree on FX, and there is no second FX path. EUR
funds pass through unchanged.

### 5. Output and typed outcomes

- **A second table** sits under the bound, with the same rows plus `Excl`, and each
  row reports `Status.CALCULATED`.
- **Coverage lines.** The header says how many days carry a bar and how many are
  usable. It also gives the estimate's first covered start day and the crashes that
  some covered window overlaps. These are read from the covered windows themselves,
  because excluded windows can sit anywhere in the series.
- **A row with no figures is `UNAVAILABLE` with a typed `Gap`:**
  - `NO_BARS`: nothing stored. The table is replaced by one line pointing to
    `e1f fetch --backfill`.
  - `NO_WINDOW`: the series is too short for the expiry.
  - `NO_USABLE_WINDOW`: every window was excluded.
- **Disclosure tests** pin each of these outcomes.

### 6. The estimate is at least the bound on every window it covers

On one start day, with every open day's bar usable (so `low ≤ close` each day):

- **The bound fills** on the first day `k` with `close_k < L`. Since
  `low_k ≤ close_k < L`, the estimate has filled by day `k` at the latest, at a
  price at or below `L`. Its ΔShares is therefore at least `1 / (1 − Below) − 1`,
  which is the bound's.
- **The bound does not fill.** Every close is at or above `L`, including
  `P_{t+Expiry}`. The estimate either does not fill either, which gives the same
  ΔShares, or it fills at a price at or below `L`, which is at or below
  `P_{t+Expiry}`. Either way it ends with at least as many shares.

A property test checks this over random usable bars. The guarantee holds per
window. Where `Excl > 0`, the estimate summarises fewer windows than the bound, so
its row figures can fall below the bound's, and the footnote says so.

## First reading

For Amundi Prime All Country World (IE0003XJA0J9), 2026-09-29: bars are usable on
all 574 days and no window is excluded.

| Expiry | Below | Fill% bound → estimate | Mean ΔShares bound → estimate |
|---|---|---|---|
| 5d | 0.5% | 49% → 65% | −0.44% → −0.13% |
| 5d | 1% | 33% → 45% | −0.40% → −0.19% |
| 21d | 1% | 56% → 68% | −0.80% → −0.37% |
| 21d | 2% | 34% → 47% | −1.00% → −0.53% |

Even with intraday fills, the limit orders lost to buying at once on average in
every default row. The history is 2.3 years with no crash in it, and there are 27
non-overlapping windows at 21 days. These figures are tabulated, not fitted or
ranked (ADR-0019 §6).

## Rationale

- **The bound stays.** It needs no assumption about the venue, so it remains the
  answer for a broker whose fills the bars do not describe.
- **Excluding windows is stricter than repairing them.** Mixing bar and close rules
  in one window would produce a figure that neither method stands behind.

## Consequences

- `src/e1f/experimental/limitbuy.py` gains:
  - `usable_bar`, `align_bars`, `bar_outcome` and `summarize_bars`;
  - `Gap`, and the `estimated`/`excluded`/`gap`/`covered` fields on `LimitBuyRow`;
  - the estimate table, coverage lines, footnote and `--explain` block.
- `src/e1f/experimental/common.py` gains `DailyBar` and `load_daily_bars`. A DB
  from before ADR-0056 yields no bars.
- `tests/test_limitbuy.py` adds:
  - a date-pinned bar regression with hand-computed fills, including a gap-down
    fill at the open and an excluded window;
  - tie cases at the limit, the usability check and FX alignment;
  - the "estimate at least the bound" property test;
  - disclosure tests for `NO_BARS` and `NO_USABLE_WINDOW`, and a converted-series
    case.
- ADR-0055's deferred "Intraday lows" item points here. The README and CLAUDE.md
  describe the second table.

## Deferred

- **Ladders:** one amount split across several limits (ADR-0055).
- **Broker quotes:** modelling fills against a broker's own bid/ask needs data the
  DB does not hold.
- **`validate` bar checks:** these could reuse `usable_bar`'s definition.
