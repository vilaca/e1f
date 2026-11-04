# ADR-0061 — Scenarios: category targets in a sleeve plan

**Scope:** a sleeve scenario (ADR-0060) can group its funds into named **categories**
instead of listing flat targets. Each category has a percent of the sleeve and fixed
shares for its funds. A fund's target is the category's percent × its share. The plan
maths does not change. `rebalance` and `scenario show` display the grouping, and
`correlation --scenario` (ADR-0017 §4) sees only the flat targets.

## Context

On 2026-10-30 the active scenario had seven sleeve targets that split into three
groups: a core world fund, two regional funds and four sector funds. Moving the
sector sleeve from 20% to 18% meant recomputing four targets by hand so that they
kept their ratios and the whole still summed to 100. The user thinks in groups first
("20% in sectors") and in the split inside a group second. Fixed shares inside each
category express that, with no new maths: the flat targets are a product of the two.

## Decisions

### 1. Shape

```yaml
scenarios:
  growth:
    months: 12
    monthly: 2500
    frozen: [IE00BM67HM91]
    categories:
      core:    {pct: 63, funds: {IE0003XJA0J9: 100}}
      regions: {pct: 17, funds: {IE0006WW1TQ4: 70, IE00B4K48X80: 30}}
      sectors: {pct: 20, funds: {IE00BM67HK77: 37, IE00BMC38736: 33, IE00BM67HQ30: 30}}
```

- A scenario has **either** `targets` **or** `categories`, never both.
- `categories` needs `frozen` (which may be empty). The category percents are of the
  sleeve and sum to 100, so a category scenario is always a sleeve plan. There is no
  residual (Σ ≤ 100) form, because it would bring back the budget nobody chose that
  ADR-0060 removed.
- Each category's fund shares sum to 100. Sums are checked to 1e-6 and never
  renormalised, so the stored numbers are the ones the plan uses.
- A fund sits in only one category, and a frozen fund in none. A one-fund category
  is written `{ISIN: 100}`. There is no shorthand.
- A share of 0 lists a fund under its category without buying it, such as a held
  fund of that kind. It has no target and counts as frozen: `Scenario.from_categories`
  adds it to `frozen`, and a save leaves it out of the stored `frozen` list, so the
  category is its one home. It must be a valid ISIN in no other category. A 0
  category percent is still refused.
- The effective sleeve target is `pct × share / 100`. Load, save and every consumer
  use these flat `targets`, so ADR-0060's plan, feasibility checks and `--explain`
  apply unchanged.

The category tree is kept on the loaded scenario and written back as it was loaded,
in YAML order. Saving any scenario rewrites the whole file, so the tree would
otherwise be lost.

### 2. Editing

Categories are edited only in the YAML file. `scenario save --target` still writes
flat scenarios, and saving under a category scenario's name replaces it with a flat
one, as any save replaces the stored scenario.

### 3. Display

- **`rebalance`** puts each category's subtotal row above its funds, in scenario
  order, between the header and the `SLEEVE` subtotal. The row shows `Current€`,
  `Sleeve%`, `Tgt%` (the category percent), `Buy€`, `Monthly€`, `Final€` and `Book%`
  for the category. Fund rows are indented under it and show their effective
  sleeve `Tgt%`. A share-0 fund's row follows its funds, shown as frozen, and is
  left out of the subtotal and of the frozen block below `SLEEVE`. `--sort` and `--reverse` reorder funds within their category, and
  categories keep scenario order. When the plan is UNAVAILABLE, the target recap is
  grouped the same way.
- **`scenario show`** prints the tree: each category with its percent, then its
  funds with the effective `Tgt%` and their `Share%` of the category. Share-0 funds
  are not repeated under `Frozen`. `scenario
  list` adds the category count.

## Consequences

- `src/e1f/common/scenarios.py`: `Category`, `Scenario.categories`,
  `Scenario.from_categories` and the category checks at every load and save.
- `src/e1f/rebalance.py` and `src/e1f/scenario.py` display the grouping.
  `correlation` is unchanged. The layer contract is unchanged (ADR-0003).
- `tests/test_rebalance.py` pins a hand-computed category plan (subtotals, indented
  fund rows, sorting within a category, the grouped UNAVAILABLE recap) and a test
  that exact-fraction categories with the residual plan's `C_min` as budget
  reproduce the flat residual buys of the 2026-10-30 basket. `tests/test_scenario.py`
  covers the schema, the round trip through an unrelated save, and `show`/`list`.
