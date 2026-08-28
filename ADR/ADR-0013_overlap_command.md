# ADR-0013 — `overlap` command (cross-fund single-name exposure floor)

**Scope:** the `overlap` subcommand — the v1b command deferred by ADR-0012.
Where `concentration` (v1a) reports *within-fund* concentration one fund at a
time and deliberately refuses to sum a name across funds, `overlap` establishes
*cross-fund* single-name exposure: "how much Apple do I really hold when several
funds each carry it?" New module `src/e1f/overlap.py` at the command layer
(ADR-0003: `cli → command → common`). Builds directly on v1a's immutable
look-through snapshots (ADR-0012 decision 5) and the EUR valuation path from
`performance` (ADR-0011), both consumed through `common`.

Slogan carried from ADR-0012: **v1a detects candidates for overlap; v1b
establishes it.**

## Context

v1a shipped within-fund concentration and a first-class *unresolved* signal:
names appearing in ≥2 funds' top-10, grouped by a coarse normalized string,
explicitly **never summed**. Summing them would have claimed a cross-fund
identity that a string match does not establish (ticker reuse, dual/cross
listings, ADRs, share classes, renames). ADR-0012's governing invariant —

> **No analytical result may imply information that its provenance does not
> establish.**

— is what forbade it. `overlap` does not weaken that invariant; it satisfies it
by adding the one missing input: a **human-reviewed** canonical identity
(`security_alias`, created empty by v1a). Only a reviewed identity may be summed.

But resolving identity removes only *one* of two qualifiers. The look-through
source is still yfinance top-10-only, so even a fully resolved name is observed
over a partial slice of each fund — an unobserved tail may hold more of the same
security. `overlap` therefore keeps **two qualifiers, always**: identity is
resolved, *and* coverage is partial. The design below makes that second qualifier
the *type of the number itself*, not a footnote.

## Decision

### 1. The headline is a floor (`≥`), because single-name overlap is monotonic

For a canonical security `s`, with `Vf` the EUR value held in fund `f` and
`w^obs_{f,s}` the observed (top-10) weight of `s` within `f`:

```
E_observed(s) = Σ_f  Vf · w^obs_{f,s}
```

Because a name's observed top-10 weight can only be *part* of its true weight in
a fund (`w^true_{f,s} ≥ w^obs_{f,s}`, an absent-from-top-10 name may still sit at
rank 11), the aggregate is a strict **lower bound**:

```
E_true(s) ≥ E_observed(s)          (given correct identity + valid weights)
```

So the headline renders as a floor — `Apple: ≥ €3,240 (≥ 2.1% of valued
portfolio)` — never a bare point value. This is *not* a third framing competing
with v1a's coverage model; it is the natural expression of that model for a
**monotonic** metric.

**The asymmetry with v1a's HHI is deliberate and recorded.** Within-fund HHI has
an unknown tail that moves the metric in *both* directions, so it needs two-sided
mathematical bounds (`HHI_min ≤ HHI_true ≤ HHI_max`). Single-name overlap is
monotonic, so it gets a *one-sided* floor and no useful upper bound (a ceiling
would require knowing something about the unobserved tail, which we do not).
"Observed" describes the **input slice**; `≥` describes the **output's meaning** —
two different words doing two different jobs. The headline is therefore never
called "observed exposure"; it is a floor computed *from* the observed slice.

**Operationalized invariant (ADR-0013's instance of the governing rule):**

> If the true value can only be greater than or equal to the calculated value,
> render `≥`; never render an unqualified point estimate.

Status mapping (reusing v1a's four-state `Status`, decision 6):

- **UNRESOLVED** identity → no overlap number emitted (worklist entry only).
- **RESOLVED** identity + observed weights → **CALCULATED** floor (an *exact*
  aggregate of the observed slice; the slice is real, the arithmetic is exact —
  the `≥` lives in the interpretation, carried alongside as the coverage caveat).
- RESOLVED identity + *complete* holdings → CALCULATED *exact* exposure (a future
  higher-tier state; not reachable on yfinance top-10 data).

### 2. `canonical_key` is the sole identity/join key; identity is asserted, not inferred

The overlap math groups **strictly by `canonical_key`** and by nothing else. The
`security_alias(raw_name, canonical_name, canonical_key, reviewed_at)` table
(ADR-0012 decision 5) already expresses both directions of the identity problem
with zero schema change — merge and split are just shared vs distinct keys:

| Field             | Purpose                          | May affect math? |
| ----------------- | -------------------------------- | ---------------- |
| `raw_name` (PK)   | source evidence (one per string) | **No**           |
| `normalized_name` | candidate matching / search hint | **No**           |
| `canonical_name`  | human-readable display label     | **No**           |
| `canonical_key`   | resolved security identity       | **Yes**          |

Worked against the load-bearing four-string fixture (a regression test):

```
novartis-ord  ← "Novartis AG", "Novartis AG Registered Shares"   (merge)
roche-ord     ← "Roche Holding AG Ordinary Shares new"           (split: same
roche-drc     ← "Roche Holding AG Dividend Right Cert."           issuer, two
                                                                  securities)
```

→ **Novartis = one exposure group; Roche = two.** The key is per-*security*, not
per-issuer; a naïve issuer-name fold is wrong in both directions.

**Invariant: `canonical_key` is an identity *assertion*, not a similarity
*result*.** A future migration from a slug (`novartis-ord`) to an ISIN
(`CH0012005267`) changes only the identity representation, never the analytical
model. `canonical_name` has **zero mathematical authority** — display only.

*Known limitation, not solved preemptively:* `raw_name` alone is the PK, which
assumes one reviewed source string maps to exactly one canonical security. v1a
look-through is single-source (yfinance/`provider`), so this holds today. If real
multi-source data ever needs the *same* string to mean different securities per
source, the key widens to `(source, raw_name)` — a tested invariant guards it;
we expand the key only when data breaks it.

### 3. Resolution UX — two tiers; the human asserts, the tool never guesses

Identity resolution is the authoritative operation; candidate detection is only a
convenience for prioritizing it. `e1f overlap` exposes:

- **`e1f overlap resolve "<raw-name>" <canonical-key> [--name <display>]`** — an
  idempotent upsert into `security_alias`. `reviewed_at` is stamped automatically
  at write time and is mandatory *because the tool sets it*: running the command
  **is** the human review act (ADR-0012 decision 5). Re-resolving updates the key
  and bumps `reviewed_at`. `canonical_name` defaults to the `raw_name`.
- **`e1f overlap candidates`** — the resolution worklist, in two tiers:
  - **Tier 1 — co-occurrence seed** (v1a's scan): normalized names in ≥2 funds.
    Fast, high-precision, **deliberately non-exhaustive**.
  - **Tier 2 — complete observed-name roster**: *every* observed security name
    across held funds, aggregated by name (fund count + resolution status), built
    from all snapshots — **not** from the candidate detector. On resolution, an
    alias group collapses to one line (`canonical_key`, alias count, fund count),
    making the effect of the resolution immediately visible.

**Why Tier 2 is mandatory, not optional.** The Tier-1 scan groups by
`normalize_security_name`, which is token-set based. It *splits* exactly the merge
case: `"Novartis AG"` → `novartis` but `"Novartis AG Registered Shares"` →
`novartis registered shares`, so a genuine cross-fund merge hiding behind
share-class/registration wording **never trips the ≥2 threshold** and never
surfaces as a candidate. A strictly candidate-driven resolver would let the
normalizer's weaknesses implicitly define "things worth resolving." The full
roster is the escape hatch.

**Standing limitation, recorded verbatim:**

> Candidate discovery is non-exhaustive. The normalized-name co-occurrence scan
> is a convenience for prioritizing manual review. It may fail to co-locate
> distinct source names that refer to the same security. It must never be treated
> as an exhaustive identity-detection mechanism. The complete observed-name roster
> provides the escape hatch for manual resolution.

The escalation ladder stays fixed: **the normalizer suggests, the scanner
prioritizes, the human asserts, and only `canonical_key` enters the math.** The
normalizer is explicitly *not* grown into a smarter identity engine — that path
leads to increasingly sophisticated, increasingly opaque failure modes ("reviewed,
not inferred").

### 4. Valuation graduates into `common` — the load-bearing refactor

`Vf` (a held fund's EUR value) lives in `performance.py`'s point-in-time valuation
core, which `overlap` cannot import (ADR-0003 forbids command→command). Every
dependency of that core (`PositionEvent`, `position_timeline`, `load_trades`,
`pinned_quote_currency`, `convert_to_eur`) **already lives in `common`**, so the
graduation is a clean *downward* move with no circular-import risk — a mechanical
relocation, not a redesign.

**Moves to `common`:** `HoldingSeries`, the price-series loader, `build_series`,
`value_on`, and their position/price `*_asof` helpers (leading underscores
dropped where they become public API). **Stays in `performance`:** the
return-metric machinery that is genuinely its own — breakpoint-day assembly,
per-point series, XIRR/TWR, P&L rows. `performance` then imports the valuation
core from `common`; **its existing tests must stay green** (verified before the
build is called done). This changes ADR-0011's module shape; ADR-0011 gains a
back-reference note.

**New `common` entry point `overlap` consumes:**

```
common.fund_eur_value(isin, as_of, db_path, currency_meta_path) -> float | None
```

wrapping `load_trades → position_timeline → build_series → value_on`. It returns
`None` when a fund cannot be valued (no pinned currency, no price on/before the
day, or no FX rate — the existing `value_on` contract). A `None`-valued fund is
**excluded from the floor and disclosed** (a distinct coverage gap from the
top-10 one).

A valued fund with no look-through snapshot is different: its value remains in
the valued denominator, but it contributes no observed weights. The report
therefore states look-through snapshot coverage separately from valuation
coverage and names every valued fund lacking a snapshot.

**The `as_of` mismatch is documented, not reconciled.** The *weights* come from a
look-through snapshot (its own `as_of`, possibly weeks old); the *€ value* comes
from prices/positions as of the valuation date (default: latest/today). These are
two different dates by nature — latest-known composition valued at current money.
The ADR names this as a standing limitation; `--explain` surfaces **both** dates
(snapshot `as_of` per fund vs valuation `as_of`) so a reader sees the blend.

### 5. Observation eligibility — the floor's second precondition

The floor is a *monotonicity* claim, so its precondition is narrow: not "the
holdings dataset is valid," but **every weight included in the floor must be a
valid non-negative long portfolio weight**. For each fund/security observation:

```
valid weight  (0 ≤ w ≤ 1 + ε)   → include in floor
None / unavailable               → exclude + disclose
negative weight                  → exclude + disclose
weight > 100%                    → exclude + disclose
```

A **negative** weight is unsupported regardless of whether its source is rounding,
a short/synthetic leg, or another artifact; diagnostics disclose the value without
classifying its cause. Folding a short in as positive would both inflate the floor
and invert the exposure it represents, making the number **wrong in direction** —
the one outcome this design refuses. A **>100%** weight is not a legitimate NAV
portfolio weight under `overlap`'s `Vf · w` contract (it would be a derivative
notional, a different and unsupported concept). The zero lower bound is exact;
`+ε` absorbs source-rounding noise on the upper bound only.

**Never clamp, never net, never infer.** No `max(0, w)` / `min(w, 1)` — clamping
launders bad source data into apparently valid evidence. No netting of shorts
against longs — that is a different exposure concept v1b does not support. One bad
observation is dropped individually; it **cannot contaminate** the otherwise-valid
observations of the same security.

**Invariant (into the ADR):**

> A calculated overlap floor may include only observations whose resolved identity
> is authoritative and whose individual security weight satisfies the long-weight
> validity constraint `0 ≤ w ≤ 1+ε`. Invalid or unavailable observations are
> excluded individually and disclosed; they are never clamped, netted, or inferred.

### 6. The percentage floor and its valued-portfolio denominator

The € floor is an unimpeachable lower bound. The **percentage** is not
automatically one: `E_true / V_true` computed as `E_observed / V_computed` has an
*under-stated numerator* (`E_observed ≤ E_true`) **and**, if unvaluable funds are
dropped, an *under-stated denominator* — two under-statements make the ratio's
direction ambiguous, letting a `≥%` silently exceed the truth. The fix is one
consistent basis via **two separate eligibility filters**:

1. **Fund-valuation eligibility** — can we establish a EUR value for the fund?
2. **Security-observation eligibility** — may this fund/security weight contribute
   (decision 5)?

```
V_valued = Σ_{f ∈ F_valued}                     Vf          (denominator)
E_floor  = Σ_{f ∈ F_valued ∩ F_valid-security}  Vf · w^obs  (numerator)
```

```
fund valued?  ──NO──▶ excluded entirely (neither numerator nor denominator)
     │
    YES ──▶ in DENOMINATOR
             │
        security weight valid?  ──NO──▶ disclosed exclusion (denominator only)
             │
            YES ──▶ in NUMERATOR
```

Within the valued sub-portfolio, `E_floor / V_valued ≤ E_true,valued /
V_valued`, so the ratio **is** a genuine floor. The headline is therefore
`≥ X% of valued portfolio`, collapsing to `of portfolio` only at 100% valuation
coverage. Disclosure names the gap: `Valued portfolio: €154,000 · valuation
coverage 96.4%`.

**The load-bearing sentence:** a valued fund whose *security* observation is
invalid keeps its `Vf` in the **denominator** but not the numerator — otherwise the
denominator trap returns from the other direction (a fund's real value would
vanish from the base). Rejected: mixing cost-basis into the denominator to cover
unvaluable funds (mixes market € with cost € — the ratio stops meaning anything);
better to expose the missing valuation than invent a denominator.

**Invariant (into the ADR):**

> A percentage overlap floor may only be computed when its numerator and
> denominator are based on the same valued-fund set. Unvalued funds are excluded
> from both; excluded security observations remain in the denominator but not the
> numerator.

### 7. Report scope — resolved identity in ≥2 funds, gated *after* resolution

`overlap`'s semantic contract: **it reports securities for which e1f has
established the same canonical identity in at least two held funds.** That keeps
the word "overlap" meaning cross-fund, not "all resolved holdings."

The ≥2-fund gate is applied **after** canonical resolution — the exact v1a
limitation being fixed:

```
raw names → canonical_key → GROUP BY canonical_key → count(distinct fund) → ≥2
```

never `count(funds) → resolve`. A resolved security in only one fund is simply
**absent** from `overlap` (not "degenerate overlap" — that muddies the API); it is
still visible through `concentration` and the resolution roster (`overlap status:
not an overlap`). Main report is sorted by **€ floor descending** (the most useful
portfolio-level ordering).

The **unresolved remainder** is a strictly-separated worklist beneath the report —
v1a's UNRESOLVED co-occurring names, reframed as "resolve these to establish
overlap." **No guessed floor** is ever shown for them; it is a worklist, not a
second-class overlap report. The progression is the whole point:

```
UNRESOLVED ──human resolution──▶ canonical_key ──appears in ≥2 funds──▶
CALCULATED floor ──▶ main overlap report
```

Shape:

```
Cross-fund security overlap — ADR-0013 v1b
Valued portfolio: €154,000 · valuation coverage 96.4%

  Apple Inc.       ≥ €3,240 (≥ 2.1% of valued portfolio)   [CALCULATED floor]
    Identity resolved in 3 funds · floor from 3 · excluded 0
  ASML Holding NV  ≥ €1,870 (≥ 1.2% of valued portfolio)   [CALCULATED floor]
    Identity resolved in 2 funds · floor from 2 · excluded 0

Unresolved co-occurring names — resolve to establish overlap   [UNRESOLVED]:
  Novartis AG / Novartis AG Registered Shares   (2 source names · 3 funds)
```

`--explain` per resolved security reconstructs the full floor chain — each
contributing fund's `Vf × w`, the excluded observations with reasons, the valued
denominator, and both `as_of` dates per fund — so the cross-fund number is
independently inspectable, reconstructed (never logged), consistent with ADR-0012
decision 7.

### 8. Three shared-primitive graduations into `common`, all forced by the layer contract

Because `overlap` may import only `common` (never `concentration` or
`performance`), ADR-0013 moves three shared primitives down into `common`:

1. **Valuation core** → `fund_eur_value` + `HoldingSeries` / `build_series` /
   `value_on` (decision 4).
2. **Co-occurrence scan** → `overlap_candidates` (v1a's Tier-1 seed), so both
   `concentration` (its unresolved signal) and `overlap` (its worklist) consume
   one home for the logic.
3. **Provenance vocabulary** → the four-state `Status`, `MetricContract`, and the
   `--explain` rendering helpers (`_explain_metric`, `_limited_by`,
   `_snapshot_provenance`). Per-metric *contract instances* stay in their command
   modules (v1a's `SECURITY_CONTRACT`, etc.; `overlap`'s new floor contract).

This is the **mechanism** graduating, not the **generalization**: ADR-0013 does
*not* retrofit `performance` / `portfolio` to report through this model. That
broader work — making every command speak the contract/status vocabulary — remains
a distinct future ADR (the next available number, 0014+), as ADR-0012 decision 7
now states. Repointing ADR-0012's stale "graduates to its own ADR-0013" references
(they predated `overlap` taking the 0013 slot) is part of this change, so there is
no numbering collision and no gap (CLAUDE.md: one ADR per decision, no gaps).

## Rationale

- **The floor is honesty made structural.** For a monotonic metric, `≥` is the
  provenance-honest type: a reader cannot mistake it for a complete figure, so the
  worst failure mode (a precise-but-partial number acted on as if complete) is
  impossible by construction, not by footnote.
- **Two qualifiers, always.** Resolving identity removes coverage's *identity*
  blocker but not its *top-10* one; conflating the two is exactly what this command
  exists to prevent.
- **Reviewed, not inferred.** Summation is unlocked only by a human `reviewed_at`
  assertion via `canonical_key`; no string algorithm ever writes identity.
- **Conservative eligibility beats coverage.** Excluding-and-disclosing a bad
  observation keeps the floor a valid bound; clamping or netting would trade a true
  `≥` for a larger but meaningless number.
- **Consistent-basis percentage.** Anchoring the ratio to the valued-fund set on
  both sides preserves the `≥` guarantee for the percentage, not just the euro.
- **Forced graduations pay a real debt.** The three moves into `common` are not
  speculative generality — each is required by the layer contract the moment a
  second command needs the primitive, and each leaves one home for its fact.

## Consequences

- New module `src/e1f/overlap.py` with subcommands `resolve`, `candidates`, and
  the bare floor report; wired into `cli.py` `COMMANDS` / `PARSER_FACTORIES`, the
  import-linter `layers` list (`pyproject.toml`), the CLI epilog, and autocomplete;
  `tests/test_contracts.py::test_cli_commands_surface` updated.
- Three `common` graduations (decision 8); `performance` re-imports the valuation
  core and its tests stay green; `concentration` re-imports `overlap_candidates`
  and the provenance vocabulary from `common`.
- `security_alias` gains read/write usage (empty until the user resolves names);
  no new observation storage — `overlap` only *reads* immutable snapshots +
  aliases (snapshots stay immutable and append-only, ADR-0012 decision 5).
- The pure floor math (resolve → group by `canonical_key` → filter eligible →
  `Σ Vf·w` → `≥` + valued-denominator %) is tested in isolation like v1a's bounds
  math; the four-string identity fixture is a regression test. Coverage floor 90%.
- ADR-0011 module shape changes (valuation core relocated); ADR-0012's forward
  references to "ADR-0013" for the provenance *generalization* are repointed to
  0014+.

## Deferred (not in this ADR)

Unchanged from ADR-0012's Deferred section unless noted:

- **Provenance generalization** — retrofitting `performance` / `portfolio` to
  report through the `Status` / `MetricContract` / `--explain` model. Distinct from
  the mechanism relocation in decision 8; a future ADR (0014+).
- **Upper bound / exact exposure** — reachable only with complete (higher-tier)
  holdings that light up the unobserved tail; on yfinance top-10 data the floor is
  the strongest honest claim. Higher-tier holdings remain deferred (ADR-0012).
- **ISIN-valued `canonical_key`** — the slug is sufficient now; an ISIN can replace
  it later without changing the analytical model (decision 2).
- **Region / country, rebalance / new-ETF suggestions, valuation screening,
  `--explain --json`, persisted historical-claims trail** — all unchanged from
  ADR-0012's Deferred section. `overlap` needs none of them to ship; it establishes
  overlap over the observed top-10 slice and says so.
