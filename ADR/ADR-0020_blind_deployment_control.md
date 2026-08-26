# ADR-0020 — blind-deployment control (timing-value isolation)

**Scope:** add a **signal-free deployment control** to the `backtest` command so a
dip strategy can be decomposed into *why* it wins or loses, not just *whether* it
does. ADR-0019 compares a dip strategy against constant-DCA and a cash-drag
control; that answers "did the reserve strategy beat DCA?" but conflates two
different effects — the **cost of holding a reserve** and the **value of the
timing signal**. This ADR adds a third rung, a reserve that deploys on a
**deterministic, drawdown-blind schedule**, turning the comparison into a causal
decomposition. No new module: new deployment modes graduate into the pure core in
`common`, the command owns the new benchmark construction, the decomposition
render, and a supplementary randomized robustness block.

Slogan: **hold the budget fixed *and* the amount reinvested comparable — then the
only thing left varying is *when*, which is exactly the signal's claim.**

## Context

ADR-0019's `backtest` measures `dip − constant-DCA` (total outcome) and
`dip − cash-drag` (benefit of deploying the reserve *at all* vs hoarding it). On
the real 2011–2026 all-world history the second number is large and positive
while the first is negative — the reserve, once deployed, recovers most of its
own drag but never beats being fully invested. That is suggestive but not
conclusive, because neither number isolates the **timing signal itself**:
`dip − cash-drag` credits the signal for merely *re-entering the market*, which a
mechanical schedule would also do.

The missing control is a reserve that deploys the **same way a passive investor
would if they simply refused to look at drawdowns** — a neutral schedule. With it,
the economics decompose cleanly (all comparisons at a **matched `β`**, so reserve
size is held constant):

| Quantity | Comparison | Question it answers |
|---|---|---|
| Reserve cost | `constant-DCA − cash-drag(β)` | What does holding the reserve cost? |
| Deployment benefit | `blind-even(β) − cash-drag(β)` | What is reinvesting the reserve worth? |
| **Timing benefit** | **`dip − blind-even(β)`** | **Does watching drawdowns beat a blind schedule?** |
| Total outcome | `dip − constant-DCA` | What did the investor actually get? |

The governing invariant of ADR-0019 still binds: **no analytical result may imply
information its provenance does not establish.** A timing claim requires a control
that removes the timing; this ADR supplies it, and keeps the headline number
**deterministic** so the decomposition is reproducible to the cent.

## Decision

### 1. Deployment is a mode, not a hard-wired signal

`simulate_strategy` gains a `DeployMode` — `signal` (the ADR-0019 default,
behaviour unchanged), `even`, `delayed`, `random`. The month loop asks the mode
for a **fraction of the current reserve** to deploy; only `signal` consults the
drawdown. Because deployment is still `fraction ∈ [0,1]` times the current reserve
balance, ADR-0019 decision 2's invariance (`total_invested == equity_cost +
reserve_cash == N·C` at `--cash-rate 0`) holds **by construction for every mode** —
the centerpiece test is simply extended to cover them.

### 2. The blind schedules empty the reserve by the horizon

The three blind modes share one contract: **they fully reinvest the reserve by the
horizon** (the last contribution deploys whatever remains). This makes them the
"you *will* get invested, just not by watching dips" control.

```
even       f_k = 1/(n−k)                    # equal share of the remaining months
delayed    f_k = 0 for k < L, else 1/(n−k)  # wait L months, then even
random     f_k = U(0,1)                     # seeded; a random share of the remainder
last fill  f_{n−1} = 1                       # all blind modes empty at the horizon
```

`f` is always a fraction of the **current** reserve, so the schedule consumes the
remaining balance and does **not** accidentally depend on the contribution count.
`even` is the primary control; `delayed` and `random` are variations on "what if I
simply wait?".

**Interpretation, stated so it cannot be misread:** blind controls reinvest the
*whole* reserve, whereas a dip rule typically leaves some undeployed. The timing
benefit `dip − blind-even` therefore compares the signal against **full neutral
reinvestment** — a deliberately harsh bar. A negative timing benefit means the
signal did not beat mechanically reinvesting everything; it does **not** by itself
separate "held cash back" from "timed badly". A deployment-*matched* control (same
total euros deployed as the dip) is a possible future refinement, noted in
Deferred.

### 3. `blind-even` is the deterministic headline; `blind-random` is supplementary

The decomposition's timing-benefit number uses **`blind-even` only** — one
deterministic schedule, reproducible to the cent. `random` never substitutes for
it. Instead `--blind-seeds N` (default **500**) runs the random schedule over
fixed seeds `0…N−1` and reports the **distribution** of blind outcomes plus **where
the dip falls within it** (its percentile). This answers "is the dip
distinguishable from the cloud of equally-blind deployment paths?" without letting
Monte-Carlo noise into the headline. 500 is enough to smooth the empirical
distribution while staying honest that the real uncertainty is
historical/model, not sampling — thousands of seeds would manufacture false
precision. `--blind-seeds 0` disables the block; the override stays for
sensitivity checks.

### 4. Matched controls per distinct `β`

Every comparison must hold reserve size constant, so the benchmark set carries a
`cash-drag(β)` **and** a `blind-even(β)` for **each distinct `β` among the dip
strategies** (not just the top-level `--base-fraction`). This fixes the gap where a
`β=0.9` dip was silently compared against a `β=0.75` cash-drag. Order is
`lump-sum, constant-DCA, [cash-drag(β), blind-even(β)]…, dips…` — benchmarks then
dips, as before. Blind-benchmark labels join the reserved set the collision guard
rejects.

### 5. CLI surface

- **Auto:** every single run prints the four-rung ladder (DCA → cash-drag →
  blind-even → dip) and, below the table, the decomposition block above per dip
  (signal-mode dips only). The random block prints when `--blind-seeds > 0`.
- **Explicit:** `--strategy` gains `deploy=signal|even|delayed|random`, `delay=L`,
  `seed=S`, so a blind strategy can be placed in the table directly (a custom
  `label=` is required if it would collide with an auto control).
- **Window mode is unchanged:** `_run_window` already filters to `aggressiveness >
  0`, so the `a=0` blind/cash-drag benchmarks never appear as dip rows; the
  decomposition and random block are single-run only.

### 6. Provenance

`--explain` records the blind contract (schedules empty the reserve), the seed
count and range (`0…N−1`), and that the timing benefit is measured against full
neutral reinvestment. `method_version` stays `contribution_timing_backtest_v1` —
the EUR valuation and reserve accounting are unchanged; the blind control is an
added *benchmark and decomposition*, not a new valuation method. The anti-overfit
note stands: the decomposition is descriptive arithmetic over pre-specified
strategies — it selects no winner and fits nothing.

## Rationale

- **It turns a verdict into a mechanism.** `dip − blind-even` is the number the
  timing thesis actually lives or dies on; ADR-0019 could not produce it.
- **Invariance is free.** Deploying a bounded fraction of the current reserve
  preserves ADR-0019's conservation law for every mode — the blind modes cannot
  break the accounting.
- **Deterministic headline, distributional check.** Keeping `blind-even` as the
  single reported control makes the decomposition reproducible; the seeded random
  cloud is a robustness overlay, never the point estimate.
- **Matched `β` everywhere.** Holding reserve size constant is what makes each
  comparison a controlled one rather than a confound.

## Consequences

- **`common.py` gains** `DeployMode`, the `_blind_fraction` schedule, `deploy` /
  `delay_months` / `seed` fields on `StrategyParams` (defaulting to today's
  behaviour), and the extended `simulate_strategy`. No new module; the layer
  contract (ADR-0003) is untouched.
- **`backtest.py` gains** blind-aware `--strategy` parsing, per-`β` matched
  benchmark construction, the decomposition render, the `--blind-seeds` random
  block, and the CLI flag; `build_strategies` label order changes (benchmarks now
  include the blind-even rungs).
- **Tests:** invariance extended to `even`/`delayed`/`random`; `even`/`delayed`
  empty the reserve by the horizon; `random` is reproducible by seed and also
  empties; `delayed` deploys nothing before `L`; the decomposition arithmetic and
  the random percentile render; `--blind-seeds 0` disables the block. Coverage
  floor stays 90%.
- **README** gains a blind-control description; **CLAUDE.md** Layout/Key-decisions
  gain this ADR when the code lands.
- No new storage — `backtest` still only *reads* prices, FX, and config.

## Deferred (not in this ADR)

- **Deployment-matched control.** A blind schedule that deploys the *same total*
  euros as a reference dip (rather than emptying the reserve), for a timing
  isolation that does not also penalise the dip for holding cash back. A cleaner
  second control once the headline decomposition is in use.
- **Blind controls in `--window` mode.** Per-window timing-benefit distributions
  (dip − blind-even across start dates); mechanical once single-run lands.
- **Proxy history + walk-forward** remain as deferred by ADR-0019 — the blind
  control is what makes those future regime tests interpretable, not a substitute
  for them.
