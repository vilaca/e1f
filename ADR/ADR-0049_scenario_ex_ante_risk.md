# ADR-0049 — `performance --metrics --scenario`: ex-ante risk for a proposed book

**Scope:** let `--scenario NAME` re-subject the `performance --metrics` report to
the **post-rebalance book a scenario implies**, so a plan can be evaluated for its
risk consequence, not just its cash requirement. Static-weight, in-sample,
explicitly not a backtest. Reuses ADR-0017 scenario resolution and the same
final-EUR weighting `correlation --scenario` (ADR-0015) already established.

## Context

The scenario toolchain currently answers *what to buy* and *what moves together*,
and stops there:

| Command | Question | Scenario-aware |
| --- | --- | --- |
| `rebalance --scenario` | what must I buy to get there | yes |
| `correlation --scenario` | what moves together once I do | yes |
| `performance --metrics` | what is this book's risk | **no — holdings only** |

So the decision loop has no closing half. `rebalance --scenario factor-tilt`
reports a €5,927.89 injection across eight funds; nothing in e1f says whether the
resulting book is more or less volatile than the one held today. In practice that
gap gets filled by hand-rolled SQL over `prices` and `fx_rates` — which is
unreviewed, unpinned, and silently re-derives weighting rules the codebase already
owns.

`correlation --scenario` established the precedent that a *hypothetical* book is a
legitimate subject for a read-only analytic, and fixed how it is weighted. This
ADR extends the same subject to portfolio-level risk rather than inventing a
second notion of "the book after a plan."

## Decisions

### 1. The subject is the scenario's final EUR book, resolved exactly as `correlation --scenario` resolves it

Targeted funds at their targets, untargeted funds diluted, weighted by final EUR
value. One resolution path, shared in `common`, so the two commands can never
disagree about what `factor-tilt` *means*. A same-fixture reconciliation test
pins that agreement (a project convention for shared financial primitives).

### 2. Static weights, and the report says so

The scenario book's return series is `Σ wᵢ · rᵢ,ₜ` with weights **fixed at their
final target** across the whole historical window. This is a deliberate
simplification with two consequences that must be disclosed, not buried:

- It is **not the path you travelled**. It ignores when contributions actually
  landed — the very thing XIRR exists to capture.
- It is **in-sample**. The window is history the weights were chosen after seeing.

The banner therefore reads `scenario 'NAME' (static weights, in-sample)`, and
`--explain` states both limits. This is the ADR-0012/0015 invariant applied to a
hypothetical: the number must not imply out-of-sample validity it does not have.

### 3. Cash-flow metrics are UNAVAILABLE, not zero and not omitted

XIRR is money-weighted over *actual* transactions; TWR is time-weighted over
*actual* holding periods. A hypothetical book has no transactions, so both are
undefined — and a plausible-looking number in those fields would be the worst
possible output. They render `UNAVAILABLE` with `limited-by: no cash flows for a
hypothetical book`. Silently dropping the rows would let a reader assume the
report simply has fewer metrics rather than that two headline figures cannot
exist here.

What **is** defined, and is reported: annualized volatility, MaxDD and its
duration, underwater time, recovery factor, best/worst day and calendar month,
trailing 1M/3M/6M returns, and CAGR of the static-weight wealth index.

### 4. Common-window intersection, with the binding fund named

ADR-0015's pairwise-overlap alignment cannot serve a *portfolio* series: a
weighted sum needs every constituent priced on the same day. The window is
therefore the intersection of all weighted funds' EUR series, and the fund that
binds it is named in the output. This matters — adding one young fund to a
scenario can silently truncate the whole report's history, and a scenario holding
`IE0001UQQ933` (100 observations) would bound the analysis to ~5 months without
saying so.

### 5. Status is BOUNDED

Static weights, in-sample estimation, and a window bounded by the shortest
constituent. Every one of those is a real limit on what the figure establishes.
`BOUNDED` (ADR-0014) is the honest state; `CALCULATED` would overclaim. A scenario
carrying an ADR-0048-flagged fund stays `BOUNDED` and additionally inherits the
attenuation disclosure, since an understated covariance understates the book's
volatility.

### 6. `--scenario` composes with `--series`, and is refused elsewhere

`--metrics --series N --scenario X` gives one metrics row per trading day, as
`--metrics --series` already does. `--scenario` is rejected with the snapshot
table, `--diff` and `--contrib`, all of which are per-holding views of realized
positions and have no hypothetical reading.

## Implementation

`common/scenario_book.py` (new): `resolve_scenario_book(name, ...) -> dict[str,
float]` — the final-EUR weights, lifted out of `correlation.py` so both commands
import one implementation.

`common/static_book.py` (new): `static_weight_series(weights, window) ->
list[tuple[date, float]]` plus the common-window intersection and its binding
ISIN.

`performance.py`: `--scenario` / `--scenarios-file`; `_cmd_performance_metrics`
branches on subject (held book vs scenario book); the cash-flow rows route to
`Status.UNAVAILABLE` with a `MetricContract` naming the missing input.

`data/glossary.md`: **Scenario book**, **Static-weight series**, **Binding fund**,
each with its `Where` contract.

## Invariance

A scenario whose targets exactly reproduce today's weights yields a metrics report
equal to `performance --metrics` on the held book for every non-cash-flow metric —
a pinned same-fixture test asserts this, and is the reconciliation test the shared
`resolve_scenario_book` primitive requires. XIRR and TWR are `UNAVAILABLE` in
every scenario run, asserted by a typed-outcome test rather than output matching
(a project convention for partial results). The reported binding fund is the
argmin of first-close date over the weighted set, pinned on a fixture where a
young fund truncates an otherwise decade-long window.
