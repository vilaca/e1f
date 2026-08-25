# ADR-0016 — `rebalance` command (buy-only target rebalance & DCA plan)

**Scope:** a new read-only `rebalance` subcommand that takes **user-supplied
target weights** for one or more funds and reports (a) the **minimum-cash,
buy-only** rebalance that reaches those targets without selling anything, and
(b) an **N-month dollar-cost-averaging (DCA) schedule** that spreads that plan
into equal monthly slices. A single self-contained command module
`src/e1f/rebalance.py`, fitting the existing flat layout and ADR-0003's
`cli → command modules → common` contract unchanged — it joins the command
siblings and imports from `common` only the shared valuation/FX/status core
(decision 7).

Slogan: **you name the destination; `rebalance` computes the cheapest way to
walk there without ever taking a step backward (a sale).**

## Context

Every analytical command so far — `concentration` (ADR-0012), `overlap`
(ADR-0013), `correlation` (ADR-0015) — is **diagnostic**: it measures a property
of the portfolio and says nothing prescriptive. ADR-0015 deliberately deferred
*any allocator / optimizer* (HRP, MVO, …) because a prescriptive allocator would
need a coherent PSD covariance estimate, return forecasts, and their whole
validation burden.

`rebalance` is prescriptive but is **explicitly not** that deferred optimizer,
and this ADR draws the line sharply so the two are never confused. The optimizer
ADR-0015 refused would *choose* weights from forecasts; `rebalance` **is given**
the weights by the user and only does deterministic arithmetic to reach them.
There is no forecast, no covariance, no objective function — nothing to be wrong
about beyond the numbers the user typed and the prices e1f already stores. That
keeps it squarely inside the family's philosophy rather than across the line
ADR-0015 drew.

The governing invariant carries over unchanged:

> **No analytical result may imply information that its provenance does not
> establish.**

For a rebalance plan, provenance is **the current EUR valuation** each buy
amount is derived from (the same `shares × close × FX` core `performance` uses,
ADR-0010/0011) and the **snapshot date** it was computed on. A plan computed
today at today's closes is not a promise about tomorrow's prices, and the command
makes that a structural caveat, not a footnote.

## Decision

### 1. The user supplies the targets — this is arithmetic, not optimization

`rebalance` never chooses a weight. The user names target weights for the funds
they care about; the command computes the unique buy-only plan that reaches them.
There is no return model, no risk model, and no objective being maximized, so the
command cannot silently encode a forecast or a house view. This is the property
that distinguishes it from the allocator ADR-0015 deferred, and it is a hard
design boundary: **if a future change would make the command *pick* a weight
(from momentum, from risk parity, from anything), that is a different command and
a different ADR.**

### 2. Goals are passed as CLI arguments; no persistence in v1

Targets are supplied per invocation as repeatable `--target ISIN:PCT` arguments
and recomputed each run — there is no goals file and no new DB table. The command
stays a pure reader of the existing price/FX/transaction store, exactly like its
diagnostic siblings; nothing about a rebalance run mutates state. A persisted,
hand-editable goals sidecar (the `etf_universe.yaml` / `currency_metadata.yaml`
pattern) was considered and **deferred** — it is a UX convenience that can wrap
this same core later without changing the math or the contract.

```
e1f rebalance --target IE00B4L5Y983:30 --target IE00BK5BQT80:40 --months 10
```

### 3. Buy-only minimum-cash rebalance — the load-bearing math

Let each held fund `f` have a current EUR value `v_f ≥ 0` (from the valuation
core, decision 7) and let `V = Σ_f v_f`. The user pins a target weight `t_i` for
each fund `i` in the **pinned set** `P`. The untargeted funds form the
**residual set** `U = held \ P`; the residual weight is `R = 1 − Σ_{i∈P} t_i`,
and `v_rest = Σ_{j∈U} v_j`.

The only lever is **non-negative buys** `c_f ≥ 0`; final values are
`v_f' = v_f + c_f`, the final total is `V' = V + C` with `C = Σ_f c_f ≥ 0`, and
the goal is `v_i'/V' = t_i` for every pinned `i`, with `U` sharing `R` pro-rata
to current value (`v_j'/V' = R · v_j / v_rest`).

Because no sale is allowed, a fund that is **already overweight** cannot come
down except by **dilution** — growing the denominator `V'` by buying *other*
funds. Rearranging each non-negativity constraint `c_f ≥ 0` gives a lower bound
on `V'`:

- pinned `i`:  `c_i = t_i·V' − v_i ≥ 0`  ⟺  `V' ≥ v_i / t_i`
- residual bucket:  `c_j ≥ 0 ∀ j∈U`  ⟺  `V' ≥ v_rest / R` (one bound for the
  whole bucket — every untargeted fund shares it because they scale together)

The minimum feasible total is therefore the tightest of these bounds:

```
V'_min = max(  max_{i∈P} ( v_i / t_i ) ,   [ v_rest / R ]  )
```

**The residual term `v_rest / R` is conditional — it must not be transcribed as a
literal division.** It is included in the `max` *only* when `R > 0` **and**
`v_rest > 0`. The other combinations are not "residual = 0"; they are handled by
decision 5:

| `R` | `v_rest` | residual term | outcome |
| --- | --- | --- | --- |
| `> 0` | `> 0` | `v_rest / R` | included in the `max` |
| `= 0` | `> 0` | — (do **not** divide) | `residual_full` (decision 5) |
| `> 0` | `= 0` | — (do **not** use `0/R = 0`) | `residual_unallocable` (decision 5) |
| `= 0` | `= 0` | — (no residual) | fully specified — `V'_min` from pins alone |

`P` must be non-empty (decision 7 requires ≥ 1 `--target`), so `max_{i∈P}` is
never a max over an empty set. The plan is then

```
c_i    = t_i · V'_min − v_i             for each pinned i        (≥ 0 by construction)
c_rest = R · V'_min − v_rest            split over U pro-rata by v_j   (≥ 0)
C_min  = V'_min − V                     total cash to inject
```

The fund(s) that *achieve* `V'_min` (the argmax) are the binding constraint and
receive **zero buys** — diluted to target by everything else growing around them.
**Ties are expected and normal**: several funds can bind `V'_min` simultaneously,
all get `c = 0`, and `--explain` lists *all* binders, never "the" binder.

**A non-negativity floor guards float noise.** Each `c_f` is clamped up to `0`
when it lands within `~1e-9` below zero (the argmax fund's `c` is analytically
`0` but may compute as a tiny negative). A genuinely negative `c` beyond that
tolerance is a bug in the bound selection, not something to clamp away.

**Already on target is a success, not a failure.** If the current weights already
satisfy the targets, `V'_min = V`, every `c_f = 0`, and `C_min = 0`. This is a
`CALCULATED` "nothing to do" plan — never `UNAVAILABLE`. Worked example — A=€6000, B=€3000, C=€1000 (C untargeted); pin
A→30%, B→40% (`R = 30%`):

```
V'_min = max( 6000/0.30, 3000/0.40, 1000/0.30 ) = max(20000, 7500, 3333) = 20000
c_A = 0.30·20000 − 6000 = 0        (A is the binding constraint — buy nothing)
c_B = 0.40·20000 − 3000 = 5000
c_C = 0.30·20000 − 1000 = 5000
C_min = 20000 − 10000 = 10000
final: A 6000 (30%), B 8000 (40%), C 6000 (30%)  — nothing sold
```

A was so overweight (60% vs a 30% target) that reaching the target buy-only
**doubles the portfolio**. That sobering-but-true result is exactly what the
command exists to surface rather than hide.

**Invariant (into the ADR):**

> A buy-only rebalance plan reports only non-negative buys whose resulting
> weights equal the targets, computed at the minimum total value `V'_min` for
> which every buy is non-negative. When no finite `V'_min` exists the plan is
> reported UNAVAILABLE with the reason — never a plan containing an implied sale.

### 4. Untargeted holdings share the residual, pro-rata to current value

Untargeted funds are **not ignored and not zeroed** — collectively they occupy
the residual weight `R = 1 − Σt_i`, and the residual buy `c_rest` is split among
them **pro-rata to current EUR value**, preserving their relative proportions.
This matches the user's mental model ("the other holdings' percentages get
adjusted") and is the only split that needs no extra input. An untargeted fund
that **cannot be valued** (no close/FX on or before the as-of date) is excluded
from `v_rest`, **kept out of the table rows**, and disclosed only in the footer —
the same "exclude-and-disclose, never silently drop" discipline `performance` uses
for its TOTAL (ADR-0011). The table's row universe is therefore **(valued held ∪
targeted)**, not all held. (A *targeted* held fund that cannot be valued is a
different, fatal case — decision 5's `target_unvaluable`.)

**Untargeted funds can receive buys — deliberately.** In the worked example
(decision 3) the untargeted C gets a €5 000 buy. Buying *into* untargeted names is
what keeps a plan feasible whenever `V'_min` is finite: the alternative policy —
**freeze** unmentioned names (`c_j = 0`, dilute-only) — is stricter and often
infeasible (it cannot absorb the residual an overweight pin forces). v1 chooses
the pro-rata-buy policy on purpose; a later reader must not "fix" it into freeze
without a new ADR.

**Identity is net-ISIN, not per-broker (ADR-0011).** Values, targets, buys, and
the DCA schedule are all keyed by **ISIN**, netted across brokers — one EUR buy
amount per ISIN. This command must **not** copy `portfolio`'s `(broker, symbol)`
row model; which broker a buy is executed at (cross-broker routing) is deferred.

### 5. Feasibility is binary and honest — infeasible targets are UNAVAILABLE, never approximated

Buy-only reachability has exact, enumerable failure modes. The command reports a
plan **only** when a finite `V'_min` and a real starting mix exist; otherwise it
reports the reason and what the user must change, and never emits a partial or
nearest-feasible plan that would imply a sale. The **whole-plan** UNAVAILABLE
reasons (each is a distinct, separately-tested state):

- **`target_unvaluable`** — a **targeted, currently-held** fund (shares > 0 as-of)
  cannot be valued (no close/FX). Its `v_i` is *unknown*, not `0`; planning it as
  `€0` and buying more would imply a sale of the unknown lot, so the **entire
  plan** is UNAVAILABLE. **Every** such targeted ISIN is named (like binder ties),
  not just the first. This is the case decision 4's exclude-and-disclose must
  **not** be applied to — that path is for *untargeted* unvaluables only.
  Held-ness is decided by `position_asof` (shares > 0), independently of the
  valuation `None` (decision 7's recipe).
- **`empty_portfolio`** — no fund has a positive **valued** anchor, i.e. `V = 0`
  (an empty book, *or* every held fund unvaluable). The min-cash formula degenerates
  to weights `0/0` — that is **not a rebalance**; there is no current mix to walk
  from. Reported UNAVAILABLE. Opening a book from nothing is the deferred **budget
  mode**, not a degenerate success.
- **`residual_full`** — `Σt_i = 100%` (`R = 0`) while valued untargeted funds
  exist (`v_rest > 0`): the residual has no room. Reported UNAVAILABLE: either
  target those funds or lower `Σt_i` below 100%.
- **`residual_unallocable`** — `R > 0` but no *valued* untargeted fund exists to
  absorb it (`v_rest = 0`, `U` empty or all-unvaluable). The residual weight has
  nowhere to go. Reported UNAVAILABLE: add a target covering the gap so
  `Σt_i = 100%`, or hold a valuable untargeted fund.

**Reason precedence (deterministic, first match wins).** Evaluate in this order:
`target_unvaluable` → `empty_portfolio` → `residual_full` / `residual_unallocable`.
`target_unvaluable` is checked first because it is the actionable one — the user
*has* a book and merely needs prices (fetch to fix); it fires whenever any
**held** target lacks a price, even if the rest of the book is empty. A targeted
**unheld** fund never triggers it (that is a feasible open-a-position, `v = 0`).
`empty_portfolio` then catches any remaining `V = 0`.

When `Σt_i = 100%` **and** there are no valued untargeted holdings, `R = 0` with
no residual term and the plan is feasible — the fully-specified case.

**`target_zero` is not in this list — it is a CLI rejection, not a plan reason.**
Targets are constrained to `(0, 100]` at argparse level (decision 7), so a `0%`
target dies before any math runs and never reaches the feasibility check. Do not
give it a `Status` reason or a math-level test; test it as an argparse rejection.

**A target fund need not currently be held.** Pinning `t_i > 0` on a fund with
**no position** (`v_i = 0`, distinct from *held-but-unvaluable* above) is a
feasible *open-a-position*: its bound `v_i/t_i = 0` never binds, and
`c_i = t_i·V'_min` is a straight buy. Buy amounts are stated in **EUR**, needing
no price for the unheld fund; only a name lookup uses the config, and its absence
is non-fatal. Such a row is `CALCULATED` with a current value of `€0` (the honest
status — the target is reachable) — not UNAVAILABLE.

### 6. The DCA schedule is equal monthly slices of the one snapshot plan

Given `--months N`, the plan is sliced into `N` equal monthly contributions:
inject `C_min / N` per month, buying `c_f / N` of each fund each month. Buys are
expressed in **EUR** (fractional cents; this is a plan, not a broker order —
no share rounding or final-month reconciliation in v1). The schedule is computed
**once at the as-of snapshot** and is explicitly flagged as a today's-prices
plan: real prices drift between months, so the realized weights will not land
exactly on target and the user is told to **re-run to refresh** — mirroring
`performance`'s treatment of as-of / stale valuations (ADR-0011). No future
price is modelled; there is no glide-path re-optimization (deferred).

**Stale closes carry forward, exactly as `performance` does.** A fund valued from
a close *before* the as-of day (no price on the day itself) is still valued — its
current value is flagged `~` (carried-forward) and the plan built from it stays
`CALCULATED`, carrying the snapshot limitation. A carried-forward close is *not*
`target_unvaluable`; only the total absence of any usable close/FX is.

### 7. A single self-contained command module, reusing the valuation core

`rebalance` is **one module**, `src/e1f/rebalance.py`, like every other e1f
command. It owns all its own logic — target parsing/validation, the `V'_min`
computation, the residual pro-rata split, the feasibility verdict, the DCA
slicing, the report, and `--explain`. From `common` it imports **only what is
genuinely shared**: the EUR valuation core (`position_timeline`, `load_trades`,
`position_asof`, `build_series`, `value_on`, `price_date_asof` — ADR-0010/0011),
the `ConfigManager` for fund names, and the `Status` / `MetricContract` /
`_explain_metric` provenance vocabulary (ADR-0013/0014). It does **not** import
`fund_eur_value` (it collapses never-held and unpriceable into one `None`;
decision 7's recipe needs them distinct) and imports **nothing from sibling
command modules** (`performance` / `portfolio`) — that would break ADR-0003's
layers gate. `_SHARE_EPSILON` (`1e-9`) and the money/percent formatting are
**copied locally**, exactly as `performance` and `portfolio` each copy them, not
imported. **Nothing is added to `common`** — the plan math is rebalance-specific
and stays in the command. `rebalance` only *reads* prices, FX, and transactions;
it writes nothing.

Layer placement is unchanged from ADR-0003 — `rebalance` joins the command
siblings, no new layer or contract:

```toml
layers = [
    "e1f.cli",
    "… | e1f.correlation | e1f.rebalance",   # rebalance joins the command siblings
    "e1f.common",
]
```

**The valuation recipe (not just function names) — the silent-bug edge.**
`fund_eur_value` collapses *never-held* and *held-but-unpriceable* into a single
`None`, so the plan does **not** use it. Instead it decides **held-ness from
`position_asof` (which returns a `(shares, cost)` tuple — unpack it)**, and values
only held funds via `value_on` (for a held fund, number-or-`None`; `value_on`
returns `0.0` only for a zero-share fund, never for a held one).

**Seed the held set from `position_timeline(load_trades(db))` with each ISIN's
events capped at `--as-of`, exactly as `performance` does — not
`portfolio_isins()`.** `portfolio_isins()` collapses *all* transactions to one
current-net set with no as-of parameter, so a name sold *after* a historical
`--as-of` would be wrongly dropped from that snapshot. (This is where `correlation`
/ `overlap` differ — they seed from the current-net helper because they are
inherently as-of-now; `rebalance` honours `--as-of` like `performance`, so it must
use the timeline. The `position_asof` shares check in the recipe below is then
correct per fund.)

```
universe = held funds (position_asof shares > 0 as-of)  ∪  targeted ISINs
for each fund f in universe:
    shares, _ = position_asof(events_f, as_of)      # tuple — NOT .shares
    held_f    = shares > _SHARE_EPSILON
    ┌ not held_f                      → v_f = 0  (open-a-position if targeted; CALCULATED)
    └ held_f:
        value_f = value_on(build_series(...), as_of, db)   # number or None
        ├ value_f is a number  → valued: v_f = value_f      (may be ~ carried-forward)
        └ value_f is None      → UNVALUABLE:
              · targeted   → target_unvaluable (abort whole plan)
              · untargeted → exclude from v_rest AND from the table, footer-disclose
```

Do **not** call `fund_eur_value` and treat every `None` as "unvaluable" — that
would misclassify a never-held target as a fatal error and an open-a-position as a
sale. `V = Σ` valued `v_f` over held funds only.

**Percents are of the whole valued book, not of the named sleeve** — and the help
text must say so out loud. `--target A:60 --target B:40` while *any other* valued
fund is held is `residual_full` (Σ = 100% with a non-empty residual), not a
60/40 split of A and B alone. This is consistent and it is the invocation people
will reach for first, so silence here makes v1 look broken.

**CLI parameter validation:**

```
--target ISIN:PCT  repeatable. PCT is a PERCENT in (0, 100] (30 = 30%, not 0.30).
                   Per-arg type= validates the "ISIN:PCT" format and the (0,100] bound.
                   Cross-arg checks run POST-parse via parser.error (they cannot live
                   in a per-argument type=): at least one --target, no duplicate ISIN,
                   Σ PCT ≤ 100.   (matches correlation's post-parse pair-length check)
--months N         integer ≥ 1 (default 1 — the whole plan in one buy), per-arg type=
--as-of            YYYY-MM-DD (default today); copy performance's _validate_as_of
--db / --config / --currency-meta   as on the sibling commands
```

A "valid ISIN token" is checked **loosely, no fixed length and no check digit** —
match the rest of e1f, which does not format-check ISINs. A non-empty token (at
most an optional "two leading letters + alphanumerics") is enough; the synthetic
fixtures are **13 characters** (`IE00EUR000001`, `IE00FUND000A0`, `IE00USD000001`),
so a hard 12-char rule would reject the very ISINs the tests treat as valid. What
must be rejected is a malformed `ISIN:PCT` *split* (missing colon, non-numeric
PCT), not the ISIN's shape. A `PCT` in `(0, 1)` is *accepted* (0.30 is a legal
0.30% target) but is a common unit mistake; an optional hint is UX-nice, not
required — documenting the unit is.

### 8. Provenance — Status per row, `--explain` reconstructs the plan

Following ADR-0014, provenance is **off by default** and opt-in:

- A plan row is `CALCULATED` when its fund is valued (including an unheld target,
  `v = 0`) — the target is reachable and the buy amount is exact. An **untargeted
  unvaluable** holding is `UNAVAILABLE`, excluded from `V`/`v_rest`, and disclosed
  (decision 4). A **targeted unvaluable held** fund or any whole-plan infeasibility
  (decision 5: `empty_portfolio`, `target_unvaluable`, `residual_full`,
  `residual_unallocable`) makes the **whole plan** `UNAVAILABLE` with the reason,
  and no buy amounts are shown.
- `--show-status` adds a Status column; `--explain` adds **one** provenance block
  (implying `--show-status`) — like `portfolio`'s single `render_holdings_explain`,
  **not** `performance`'s per-row blocks. It **recomputes** the plan from source —
  which bound produced `V'_min`, **all** binding fund(s) (ties listed, never "the"
  binder), the residual split, and `C_min` — never read from a persisted log
  (there is none; ADR-0012 decision 7's reconstruct-don't-log philosophy).

The rebalance metric contract:

```python
MetricContract(
    method_version="buy_only_rebalance_v1",
    requires=("a current EUR valuation per held and targeted fund",),
    does_not_require=("return forecasts", "a covariance estimate", "look-through holdings"),
    supports=("minimum-cash buy plan", "per-fund buy amounts", "N-month DCA schedule",
              "feasibility verdict"),
    limitations=(
        "buy-only: an overweight holding is diluted, never sold",
        "plan computed at the as-of snapshot; realized weights drift as prices move — re-run",
        "targets are user-supplied, not optimized",
        "untargeted holdings share the residual pro-rata by current EUR value",
        "C_min is fresh cash to inject on top of current fund values; idle broker "
        "cash (e.g. XTB cash operations) is not a holding and is not counted",
        "keyed by net-ISIN across brokers; which broker to buy at is not chosen",
    ),
)
```

### 9. Output contract — a target recap, the plan table, DCA as a column, a summary footer

The report opens with a **target recap** block, then the plan table (**one row per
fund in (valued held ∪ targeted)** — decision 4, untargeted unvaluables are
footer-only, never rows), a `TOTAL` row, and a plain-language summary. Sibling ADRs
fix their report shapes (ADR-0011's table, ADR-0015's taxonomy); this one does too so
the implementer is not guessing.

**Target recap (`render_target_summary`) — the sum and the normalized sleeve.**
Because targets are percents *of the whole book* (decision 7) they need not sum to
100% — the remainder is the residual. Two things a user reaches for are (a) **the
target sum itself** (did I ask for 70% or 100%?) and (b) **the targeted funds scaled
to 100% among themselves** (the sleeve composition, independent of the residual). The
recap is a compact table — `ISIN · Name · Tgt% · Scaled%` — where `Tgt%` is the
of-book weight (as in the plan table) and `Scaled%` is `t_i / Σt` (the targeted
sleeve normalized to 1). Its `TOTAL` row states the **target sum** in the `Tgt%`
column (`< 100%` ⇒ a residual exists) and `100.0%` in `Scaled%` by construction.
Sorted by weight descending, ties by ISIN. This is **plan-input recap, not
provenance** — so it is shown by default (unlike the ADR-0014 `--show-status` /
`--explain` opt-ins), on both feasible and infeasible plans (an UNAVAILABLE verdict
still recaps what was asked).

**Formatting is copied into `rebalance.py`, not imported** — money `,.2f`
(2 decimals) with a trailing `~` for a carried-forward value, percent `.1f%`
(1 decimal). These mirror `performance._fmt_money` / `_fmt_pct` but importing them
would be a command→command dependency (ADR-0003 layers gate); copy the conventions,
like the siblings do.

**Columns:**

```
ISIN · Name · Current€ · Cur% · Tgt% · Buy€ · [Monthly€] · Final€ · [Status]
```

There is **no `Fin%` column**: by construction every buy lands the fund exactly on
its target, so a final-weight percent would be identical to `Tgt%` for every row
(and 100% on TOTAL) — a duplicate. `Final€` (the absolute resulting value) carries
the only non-redundant "after" information; the resulting *weight* is `Tgt%`.

- `Current€` carries a trailing `~` when its close is carried-forward (before the
  as-of day), exactly as `performance` flags `MktVal`; the stale price date(s) are
  listed under the table (via `price_date_asof` from `common`, **not** from
  `performance`). A `~` row is still `CALCULATED` (decision 6).
- `Tgt%` shows the **pinned target** for a pinned fund; for a residual (untargeted)
  fund it shows the **implied residual target** `R · v_j / v_rest` (as a percent),
  and its name is marked `(resid)`. Showing the implied target — rather than a
  bare `—` — makes the `Tgt%` column **add up to 100%** and teaches where the
  residual weight lands.
- `Monthly€` (= `Buy€ / N`) is a **single column**, present only when
  `--months N` has `N > 1` — the DCA schedule is one column, **never** `N`
  repeated month rows. At `N = 1` the column is omitted.
- The **binding fund(s)** (achieving `V'_min`, `Buy€ = 0`) are marked (e.g. a
  trailing `◄ binds`); ties mark every binder.
- An unheld target renders `Current€ = 0.00`, `Cur% = 0.0%`, and is `CALCULATED`
  (decision 8) — a real, reachable row, not `UNAVAILABLE`.
- `Status` column only under `--show-status` / `--explain`.

**Default sort:** pinned funds first by `Tgt%` descending, then residual funds by
`Current€` descending, ties broken by ISIN — deterministic. A `--sort` flag is
deferred (not needed for v1).

**`TOTAL` row** sums `Current€` (= `V`), `Buy€` (= `C_min`), `Monthly€`
(= `C_min/N`), `Final€` (= `V'_min`), with `Tgt%`/`Cur%` = 100%. A summary
line states the headline in words:

```
Buy-only rebalance to target as of 2026-08-25 (EUR)   ·   DCA: 10 months

Targets (Tgt% = of book · Scaled% = of targeted sleeve, normalized to 100%):

ISIN          Name              Tgt%  Scaled%
IE00…B        Fund B           40.0%    57.1%
IE00…A        Fund A           30.0%    42.9%
TOTAL                          70.0%   100.0%

ISIN          Name             Current€    Cur%   Tgt%      Buy€  Monthly€    Final€
IE00…A        Fund A           6,000.00   60.0%  30.0%      0.00      0.00  6,000.00  ◄ binds
IE00…B        Fund B           3,000.00   30.0%  40.0%  5,000.00    500.00  8,000.00
IE00…C        Fund C (resid)   1,000.00   10.0%  30.0%  5,000.00    500.00  6,000.00
-----------------------------------------------------------------------------------
TOTAL                         10,000.00  100.0% 100.0% 10,000.00  1,000.00 20,000.00

Inject €10,000.00 total (€1,000.00 / month × 10) to reach targets buy-only.
Untargeted (unvaluable, excluded): —
```

(The `C` row's `Tgt%` is its implied residual target `30% · 1000/1000 = 30%`.) The
success footer discloses only untargeted **unvaluable** funds excluded from the
plan — there is no "held-but-unvaluable targets" line on a success, because a
targeted-unvaluable held fund is `target_unvaluable` and prints **no table**.

**Infeasible plans print no plan table** — just the as-of header, the target recap
block (above — it needs only the targets, not a feasible plan), an `UNAVAILABLE`
line naming the decision-5 reason and the offending fund(s), and the concrete fix
(the message each reason carries in decision 5). **`--explain` on an infeasible
plan still emits the single provenance block** — Status `UNAVAILABLE`, which bound
/ which reason, no buy amounts. Exit code is `0` — an infeasible verdict is an
honest result, like `correlation`'s UNAVAILABLE pairs, **not** an error. Only
argparse failures and IO/parse exceptions exit non-zero (ADR-0009 is the
*validate*-only error/warning contract; it does not govern this command).

## Rationale

- **Prescriptive without being an optimizer.** The user supplies the weights, so
  the command is deterministic arithmetic over stored prices — no forecast to be
  wrong about, staying inside the family's honesty philosophy and clear of the
  allocator ADR-0015 deferred (decisions 1, 7).
- **The no-sell constraint is the whole point.** It reduces to one closed-form
  `V'_min`, and it makes the sometimes-painful truth visible: a badly overweight
  holding can force a large injection to rebalance around (decision 3).
- **Feasibility is binary and disclosed.** Buy-only has exact failure modes;
  reporting them as UNAVAILABLE-with-reason beats an approximate plan that hides
  an implied sale (decision 5) — the same exclude-and-disclose discipline as the
  siblings.
- **DCA is a slice, not a simulation.** Equal monthly slices of one honest
  snapshot avoid modelling unknowable future prices; the re-run caveat is
  structural (decision 6).
- **One self-contained command.** Reuses the shared valuation/FX/status core and
  adds nothing to `common` (decision 7).

## Consequences

- One new flat module `src/e1f/rebalance.py`; wired into `cli.py` `COMMANDS` /
  `PARSER_FACTORIES`, the CLI epilog, `autocomplete`,
  `tests/test_contracts.py::test_cli_commands_surface`, and added to the
  import-linter `layers` command siblings (one line, no new layer or contract).
- **`common.py` is unchanged** — `rebalance` reuses its existing valuation/FX/
  `Status`/holdings surface and defines its own plan math. No new storage —
  `rebalance` only *reads* prices, FX, and transactions.
- The pure math (target parsing/validation → held-vs-unvaluable classification →
  `V'_min` bound selection → per-fund buys → residual pro-rata split → feasibility
  verdict → DCA slicing) is tested in isolation. Required regression fixtures:
  - each whole-plan reason: `empty_portfolio` (`V=0`), `target_unvaluable`
    (pinned held fund with no close/FX), `residual_full`, `residual_unallocable`;
  - the overweight-binding worked example (A/B/C → binding fund `Buy€ = 0`);
  - **ties** — two funds bind `V'_min`, both `Buy€ = 0`, both listed in `--explain`;
  - **already on target** — `V'_min = V`, all `Buy€ = 0`, `C_min = 0`, `CALCULATED`
    (not UNAVAILABLE);
  - an unheld target (open-a-position: `v=0`, `CALCULATED`, `Buy€ = t·V'_min`);
  - **as-of seed**: a fund bought before and **sold after** a historical `--as-of`
    still appears in that snapshot (guards against a `portfolio_isins()` seed);
  - the fully-specified `Σt=100%` no-residual case, and a case where the residual
    fund actually **receives** a buy;
  - a carried-forward (`~`) stale close still yielding a `CALCULATED` plan;
  - the implied residual `Tgt%` (`R·v_j/v_rest`) makes the `Tgt%` column sum to 100%;
  - the **target recap** (decision 9): `Scaled%` normalizes the targeted sleeve to
    100% and the recap `TOTAL` reports the target sum; shown on feasible **and**
    infeasible plans;
  - the `~1e-9` negative-`c` float clamp to `0` on the binding fund;
  - **output shape**: `--months 1` omits the `Monthly€` column; `--months N>1`
    includes it;
  - `--explain` on an infeasible plan still emits its single `UNAVAILABLE` block;
  - argparse rejections: no `--target` at all, `Σ PCT > 100`, duplicate ISIN,
    `PCT = 0` (`target_zero` lives here, **not** in the math), malformed
    `ISIN:PCT` split (missing colon / non-numeric PCT). The ISIN itself is checked
    **loosely (no fixed length)** — the synthetic fixtures are 13-char ISINs like
    `IE00EUR000001`, which must validate.
  - a `--db` integration test running `main` end-to-end (feasible + one infeasible)
    asserting exit code `0` in both.
  Coverage floor 90%.
- README gains a `rebalance` behaviour description and CLAUDE.md's Layout tree /
  Key decisions gain the module and this ADR **when the code lands** — not before
  (avoids doc drift).

## Deferred (not in this ADR)

- **A persisted goals sidecar** — a hand-editable `goals.yaml` (the
  `etf_universe.yaml` pattern) with a `rebalance set` helper, so targets are
  stored and reused rather than re-typed. A UX wrapper over this same core;
  chosen against for v1 (decision 2).
- **A budget / overshoot mode** — injecting *more* than `C_min` (e.g. a fixed
  monthly amount) and distributing the surplus, or the inverse query "given
  €X/month, how many months to reach the goal?". Both are straightforward
  extensions of the `V'_min` core.
- **Share-denominated buys** — converting EUR buy amounts to whole/fractional
  shares at current prices (needs a price for every targeted fund and a rounding
  policy); v1 states buys in EUR only.
- **A glide-path / path-aware DCA** — re-optimizing each month against realized
  prices instead of slicing one snapshot (decision 6). Needs either live re-runs
  or a price model.
- **Sell-enabled rebalancing / tax-lot awareness / cross-broker buy routing** —
  the no-sell constraint is intrinsic to this command; a sell-enabled variant
  (with realized-gain / tax-lot consequences) is a separate decision.
</content>
</invoke>
