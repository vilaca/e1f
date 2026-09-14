# ADR-0054 — `deposits --against`: same-cash replay onto an ISIN

**Scope:** add `--against ISIN[,ISIN…]`, `--against-portfolio`, and `--against-all` to
`e1f deposits`, so each actual buy's EUR cash is spent into a candidate fund on
that buy's date and the resulting P&L is compared to the book. No TWR, no beta.
This is not a `benchmark` mode.

## Context

`deposits` already knows each BUY's `Amount€` (shares × price + fee) and what
those shares became by `--as-of`. `benchmark` answers a different question: did
the book's *time-weighted* daily returns beat ETF X over a shared window. TWR
strips out contribution timing, so a positive `Out%` does not mean "I would have
had more euros if every savings-plan buy had gone into X."

That money-weighted counterfactual is the question `deposits` is already
structured to answer: the cashflows exist, the as-of valuation primitive exists
(`unit_value_on`), and the buy-and-hold refusal already applies. Putting the
replay on `benchmark` would mix two estimators in one table. A new command would
split the cashflow owner from the counterfactual.

## Decisions

### 1. The flag lives on `deposits`, not `benchmark`

`--against` / `--against-portfolio` / `--against-all` replace the per-deposit (or
`--group`) table with one row per candidate. The labelled Invested / Reported /
Organic / ROIC block stays: that is the real book, unchanged. `--group` and any
replay flag are mutually exclusive — grouping is a coarser view of actual lots;
replay is a different subject.

### 2. Cash deployed is `Amount€`, filled at the stored EUR close

On each valuable deposit's date, buy `Amount€ / unit_value_on(candidate, date)`
shares of the candidate. Value the accumulated shares at `--as-of` with the
same `unit_value_on` (nearest-prior close, FX on the valuation day, ADR-0010).
No second transaction fee is modelled: the fee already sitting in `Amount€` is
treated as cash that bought shares. That is slightly optimistic versus paying
another fee to buy the alternative, and it is disclosed in the footer.

The fill is the **stored close**, not the broker print. Replaying a book onto
the ISIN it actually bought therefore yields ΔGain€ = 0 only when every buy's
`price` equals that day's EUR close and the fee is 0. A gap is execution vs
close, not a bug.

### 3. Compare on overlapping capital, never silent zeros

A deposit the candidate cannot fill (no pinned currency, no close, or no FX on
or before the buy date) is dropped from **both** legs of that row. `Invested€`,
`BookGain€` and `AltGain€` are the overlapping lots only. The row is
`Status.BOUNDED` when any valuable book deposit was skipped, `UNAVAILABLE`
when none could be filled or the candidate has no as-of unit value. Dropped
lots are disclosed; they are never filled at €0.

The top summary block is still the full valuable book. A BOUNDED row's
`BookGain€` can therefore be smaller than Organic gain — the footer says so.

### 4. Three candidate lists, pairwise exclusive

No default basket. This is not "beat the market"; it is "replay into X".

- `--against` is an explicit ISIN list.
- `--against-portfolio` is the distinct ISINs among this as-of book's deposits
  (the holdings the report already covers — not `portfolio_isins()`, which is
  not as-of capped).
- `--against-all` is every ISIN in `prices`, held or not.

The three are mutually exclusive. An empty prices table under `--against-all` prints
that there is nothing to replay onto and exits 0.

### 5. Columns are money-weighted P&L, not TWR

`Lots`, `Invested€`, `AltValue€`, `AltGain€`, `BookGain€`, `ΔGain€`, `AltROIC`.
Positive `ΔGain€` means the alternative finished ahead of the overlapping
book lots, in euros. `AltROIC` is `AltGain€ / Invested€`, the same ratio as
ROIC, on the replay. Sort tokens are the ADR-0037 overlap (`isin`, `name`,
`cost`, `value`, `pnl`, `pnl_pct`) plus local `lots` and `delta`. Default
sort under `--against` / `--against-portfolio` / `--against-all` is `delta`.

XIRR of the replayed terminal value is deferred: the asked question is euro
P&L, and ROIC already sits next to it.

## Implementation

`deposits.py`: `replay_deposits`, `--against`, `--against-portfolio`, `--against-all`;
`_priced_isins` stays local (same shape as `benchmark._priced_isins`, not a
shared primitive). Held candidates come from the as-of deposit set, not a
second holdings query.

`data/glossary.md`: **Lots**, **AltValue€**, **AltGain€**, **BookGain€**,
**ΔGain€**, **AltROIC**, and the Out% vs ΔGain€ distinction.

## Invariance

On a fixture whose only buy is 10 shares at 10.00 on a day whose stored EUR
close is 10.00, fee 0, as-of close 14.00: replaying that ISIN onto itself
gives `ΔGain€ = 0`, `AltValue€ = BookGain€ + Invested€ = 140`. A second
candidate whose EUR unit value doubles from 20 to 30 over the same dates,
same 100.00 cash, yields `AltValue€ = 150`, `ΔGain€ = +10`.
