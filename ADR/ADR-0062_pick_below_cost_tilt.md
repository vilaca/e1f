# ADR-0062 — `pick`: tilt a month's rebalance buys toward funds below average cost

**Scope:** an experimental command (ADR-0024), `e1f pick --scenario NAME --tilt PCT`,
takes one month of a scenario's buy-only rebalance plan (ADR-0016, ADR-0060) and moves
part of it from the other funds to the funds below their average cost. It gives a
buy-limit (a maximum price per share) for every order. `--record` stores each month's pick and
`--review` reports what the tilts have gained or lost since. The rebalance targets,
the plan maths and the month's total are unchanged.

## Context

On 2026-11-05 the user asked for a way to "direct more funds to opportunities to
buy": among the funds the active scenario buys, put more of the month's money into
the ones trading below the user's own average cost, while keeping the rebalance
targets as the goal.

The average cost is not a price signal. It records when the user happened to buy,
and two holders of the same fund get opposite readings. Both dip strategies in
`backtest` lost to constant DCA on the stored history (ADR-0020, ADR-0021). The
command therefore treats the tilt as a user choice: the tilt share is required and
has no default, the rule is fixed here rather than fitted, the output says the
signal is untested, and `--review` builds the evidence out of sample.

What limits the cost of being wrong is that the tilt stays inside the plan. It moves
money between funds the plan already buys this month, never adds money, and never
buys a fund the plan leaves out. A fund bought ahead of plan moves closer to its
target, so the next `rebalance` run buys less of it. Over the plan's months the tilt
changes when each fund is bought, not where the book ends up.

## Decisions

### 1. The base is one month of the rebalance plan

`Plan€` is `rebalance`'s `Monthly€` for the scenario: the plan's buy divided by its
months, with the scenario's `monthly` budget and the same `--months` / `--monthly`
overrides. A plan that is UNAVAILABLE gives an UNAVAILABLE pick with the plan's
reason code (`PickRun.reason`), and `rebalance` explains it. Frozen funds are not
rows.

### 2. The tilt rule

For each fund the plan buys, `vsCost = price / average cost − 1`. Price is the
latest stored close on or before the as-of day in EUR. Average cost is the EUR cost
basis over shares, netted across brokers (`position_timeline`). It comes from fill
prices, so it includes the spread paid. There are no fees.

- **Recipients**: `Plan€ > 0` and `vsCost ≤ −min-depth`.
- **Donors**: `Plan€ > 0` and any other `vsCost`, including a dip shallower than
  the minimum depth.
- **Neutral**: `Plan€ = 0` (frozen, or overweight against the plan), or no average
  cost because the fund is not held yet. A neutral fund keeps its `Plan€`.

The offer is `tilt × Σ donor Plan€`. Recipients share it in proportion to
`Plan€ × depth`, where `depth = −vsCost`. Each recipient can take at most
`pull × Plan€`, i.e. `pull` months of its own plan buys pulled forward. The cash a
capped fund cannot take goes to the others, and what no recipient can take stays
with the donors. Every donor gives the same fraction of its `Plan€`, so
`Σ Pick€ = Σ Plan€`.

Weighting by `Plan€` as well as depth keeps the tilt in proportion to how much the
plan wants of a fund, so a small satellite slightly below cost does not take the
core fund's money. If every bought fund is a recipient, or none is, nothing moves.

### 3. Parameters

- `--tilt PCT`, in [0, 100], is required and has no default. The user decides it.
- `--pull N`, an integer ≥ 0, defaults to 1, so no fund is bought at more than twice
  its `Plan€` in one month (the user's limit). It is capped at the plan's months − 1,
  since a fund cannot pull forward buys the plan does not have; the output says when
  it is cut. A fund the plan does not buy this month cannot be picked, which keeps
  the targets binding.
- `--min-depth PCT`, in [0, 100), defaults to 2. A dip shallower than that is inside
  the gap between the broker's quote and the official close plus an ordinary day's
  move, so it does not count. The default is fixed here and not fitted.

Neither parameter is fitted on history, and the command does not rank settings.

### 4. Buy-limits

Every order gets a buy-limit, a maximum price per share, from the latest stored close:

- **`MaxPlan€`** for the `Plan€` order is the last close × (1 + buffer).
  `--buffer PCT`, in [0, 100), defaults to 0.5. It fills at once in a normal market
  and only guards against a bad quote or a jump since the close.
- **`MaxΔ€`** for a recipient's extra `Δ€` is
  `min(average cost × (1 − min-depth), MaxPlan€)`. The first bound is the highest
  price at which the fund is still a recipient. If the fund has recovered by the
  time the order reaches the market, the extra does not fill, because the reason for
  it is gone. The second bound keeps the extra from being a more eager order than
  the plan buy. Where the two limits are equal, one order of `Pick€` does both.

For a recipient the last close is at or below both limits, so every order is
marketable when placed. No limit is a price under the market meant to catch a dip:
such limits lost on average on the stored history, even with daily lows
(`limitbuy`, ADR-0055, ADR-0057). A fund with no stored price gets no limit. When a
limit comes from a close before the as-of day, the output says to fetch first.

### 5. Record and review

`--record` stores the pick in the `picks` table of the price DB, keyed by month,
scenario and fund, with `Plan€`, `Pick€`, the EUR price used and the parameters.

- A month already recorded for the scenario is refused. `--replace` overwrites it.
  The delete and the insert run in one transaction, so a failed replace keeps the
  old record.
- An UNAVAILABLE pick cannot be recorded.

`--review` prices every record as of a day (default today). It skips records made
after that day. A month's gain against its plain plan is
`Σ (Pick€ − Plan€) × (price now / recorded price − 1)`: both are taken as bought at
the recorded close, so the review measures the rule, not the user's fills. A month
with a moved fund that has no price now is UNAVAILABLE, listed and kept out of the
total. Below 24 priced months (the same bar as ADR-0055 §4) the output says the
result is an anecdote, not evidence. The months overlap in time and share market
moves, so they are not independent.

### 6. Output

One row per non-frozen plan fund: `Avg€`, `Price€` (with `~` when the close predates
the as-of day), `vsCost`, `Plan€`, `Pick€`, `Δ€`, `MaxPlan€`, `MaxΔ€` and the outcome (`Pick`:
below cost, below and capped, donor, not held, no buy). The summary states the cash
moved and offered, which funds' pull limits bound and how much stayed with the
donors, and how to place the orders. It also says when nothing moves, when `--pull`
was cut, and names any fund below cost that the plan does not buy. `--explain`
prints the provenance block (methods `below_cost_tilt_v2` and
`below_cost_tilt_review_v1`). These columns are outside the metric glossary.

## Consequences

- `src/e1f/experimental/pick.py`: the pure `tilt_buys`, `plan_limit`,
  `extra_limit` and `tilt_gain`, plus `evaluate`, `record`, `review`, the renderers and the CLI. It
  reuses `plan_rebalance` and the holdings primitives from `e1f.common`. No stable
  module imports it.
- `tests/test_pick.py` pins the tilt by hand: the proportional split, a capped fund
  passing cash on, pull limits returning cash to donors, the minimum depth, the
  neutral cases and the limit. A property test checks that the total is kept, every
  buy stays within its pull limit and only recipients gain. A seeded book pins one
  month end to end (a sleeve plan with frozen, unheld and cross-broker funds), and
  one record's review gain. Tests also cover the refused, replaced and failed record
  paths and the UNAVAILABLE, stale-price and summary disclosures.
- Not done: a backtest of the tilt. Testing it before use would need a multi-fund
  `backtest` with per-fund average costs.
