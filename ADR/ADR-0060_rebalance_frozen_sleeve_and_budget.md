# ADR-0060 — `rebalance`: frozen sleeve and budget

**Scope:** a scenario (or `rebalance --frozen`) can name **frozen** funds, which are
never bought. Its targets are then percents of the non-frozen **sleeve**, summing to
100. An optional budget (`monthly` × months) sets the cash to invest instead of the
minimum `C_min`. Scenarios and `--target` without frozen funds keep ADR-0016's
residual plan unchanged. `correlation --scenario` runs the same plan (ADR-0017 §4).

## Context

ADR-0016 targets are percents of the whole book. A fund left untargeted with
Σ targets < 100 shares the residual `R = 1 − Σ t_i`, and the residual bound
`v_rest / R` often sets `V'_min`. That is how a fund was frozen in practice. The
residual then sets the size of the plan without anyone choosing it.

On 2026-10-30 the active scenario targeted seven funds at Σ 95%, and nine frozen
funds held €2,115.53. To bring them down to 5%, the plan had to inject €29,480.16
(€2,456.68 a month over 12 months). Σ 98% would have needed a book of about €120k.
Moving one target point changed the monthly amount more than any fund's split did.

Without the residual, the cheapest plan that reaches the same targets in the
sleeve invests €3,341.98. Its size is set by the most overweight fund (STOXX 600
Health Care, €443.91 against a 3.16% sleeve target). So a frozen sleeve needs a
budget the user chooses, or the plan shrinks to a size nobody meant either.

## Decisions

### 1. Frozen funds and sleeve targets

A scenario with a `frozen` list, even an empty one, is a **sleeve plan**:

```yaml
scenarios:
  growth:
    months: 12
    monthly: 2500
    frozen: [IE00BM67HM91, IE00B3YCGJ38]
    targets:
      IE0003XJA0J9: 70
      IE00BM67HK77: 30
```

- **Frozen funds** are never bought and have no target. Their share of the book only
  shrinks.
- **Targets** `s_i` are percents of the sleeve, i.e. of every fund not frozen. They
  must sum to 100, and no fund may be both targeted and frozen.
- **`monthly`** is allowed only in a sleeve plan.

`rebalance --frozen ISIN` (repeatable) does the same with `--target`, and
`scenario save --frozen ISIN --monthly EUR` stores it. `--frozen` and `--target`
are mutually exclusive with `--scenario`. A scenario without `frozen` keeps
ADR-0017's Σ ≤ 100 basket, so no stored scenario has to change.

### 2. One rule for the minimum, a budget above it and a budget below it

Let `S = Σ v_i` be the sleeve's current value and `B` the cash to invest. The final
sleeve value `L` solves

    Σ_i max(0, s_i·L − v_i) = B,    and fund i buys c_i = max(0, s_i·L − v_i).

- **No budget.** `B = C_min = max_i(v_i / s_i) − S`. Every fund reaches its target,
  and the fund with the largest `v_i / s_i` binds with a zero buy. This is
  ADR-0016's rule applied to the sleeve alone.
- **A budget at or above `C_min`.** `L = S + B`. Every fund ends at its target share
  of a bigger sleeve.
- **A budget below `C_min`.** The rule closes the largest gaps first. Funds whose
  `v_i / s_i` is below `L` are bought up to `s_i·L`, and the rest get nothing and stay
  above target. The plan is still CALCULATED, because each amount is exact. The
  shortfall `C_min − B` is a typed field (`RebalancePlan.shortfall`), and the summary
  line states it.

`B = monthly × months`, or `monthly` alone without months. `rebalance --monthly`
overrides the stored value. A budget needs a sleeve plan: the residual plan has no
budget (`--monthly` without frozen funds is a usage error).

A residual plan whose residual bound binds, and the sleeve plan of the same targets
with that plan's cash as its budget, give the same buys. With `t_i = s_i · Σt`, the
residual plan's `V'_min` is `v_rest / R`, so its pinned funds end at
`t_i · V'_min = s_i · (V'_min − v_rest)`. That is the sleeve's `s_i · L`, with
`L = S + B` and `B` the residual plan's `C_min`. A pinned test checks this, so
converting a scenario keeps its buys when its monthly amount is kept.

### 3. Feasibility

Checks in order, first match wins:

- **`target_unvaluable`.** A held, targeted fund has no close or FX rate (ADR-0016).
- **`unclassified`.** A held fund is neither targeted nor frozen. A new fund bought by
  a savings plan, or a stray buy into a fund outside the plan, stops the plan until it
  is given a target or frozen, instead of being diluted silently.
- **`empty_sleeve`.** No targeted fund is held and no budget is set. With a budget, an
  empty sleeve opens at `c_i = s_i · B`.

A frozen fund that is not held is ignored. A held frozen fund with no price is left
out of the book and listed in the footer, as ADR-0016 does for untargeted funds.

### 4. Output

The sleeve table has one row per targeted fund, then a `SLEEVE` subtotal, then one
row per held frozen fund (`(frozen)` after the name), then the book `TOTAL`:

- `Sleeve%` is the fund's current share of the sleeve and `Tgt%` its sleeve target.
  Both are `—` for a frozen fund.
- `Book%` is the fund's share of the whole book after the buys. It shows how far a
  frozen fund has been diluted.
- `◄ binds` marks the fund that sets `C_min`, and `◄ no buy` marks the funds a short
  budget leaves above target.
- The summary states the cash: the `C_min` to inject, a budget that reaches the
  targets (with `C_min` beside it), or a budget's shortfall. It also states the frozen
  funds' share of the book before and after.

`--explain` reports `V`, `S`, `L`, `C_min`, the budget and the binding funds
(method `buy_only_sleeve_v1`). Rebalance columns are outside the metric glossary
(`data/glossary.md`).

## Consequences

- `src/e1f/common/rebalance.py`: `compute_sleeve_rebalance` and `plan_rebalance`,
  which picks the residual or the sleeve plan. `RebalancePlan` gains the sleeve
  fields and the `injection`, `v_final` and `shortfall` properties.
- `src/e1f/common/scenarios.py`: `Scenario.frozen`, `Scenario.monthly` and
  `Scenario.budget`, validated at every load and save.
- `src/e1f/rebalance.py`, `src/e1f/scenario.py` and `src/e1f/correlation.py` take the
  new fields. The layer contract is unchanged (ADR-0003).
- `tests/test_rebalance.py` pins hand-computed plans: the minimum, a budget above
  and below `C_min` (one crossing a fund's bound), the residual equivalence, every
  UNAVAILABLE reason and the output disclosures. `tests/test_scenario.py` and
  `tests/test_correlation.py` cover the schema and the post-rebalance universe.
