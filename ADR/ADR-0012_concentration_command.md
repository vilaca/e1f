# ADR-0012 — `concentration` command (coverage-aware, within-fund)

**Scope:** the `concentration` subcommand — per-fund security concentration
(HHI, effective holdings, top-N share), sector concentration, and asset-class
split for the held ETFs, each reported with an explicit coverage figure and, for
the security dimension, rank-constrained bounds on the unobserved tail. New
module `src/e1f/concentration.py` at the command layer (ADR-0003:
`cli → command → common`). Builds on the yfinance dependency (ADR-0001) and the
EUR valuation / weight path from `performance` (ADR-0011).

This ADR takes the ADR-0012 slot, replacing the shelved Monte Carlo `forecast`
design. The working name `diversify` is retired (see decision 1).

## Context

The portfolio is mostly overlapping global / US-equity funds, and the user's
actual question is *how concentrated is my exposure, and where.* The honest
obstacle — documented at length during design — is that **look-through data for
European UCITS funds has no clean, free, automated source**: yfinance gives
complete sector and asset-class weightings but only **top-10** named holdings and
**no** country/region breakdown, and per-issuer holdings files are a fragile,
partly-blocked fetcher fleet the user rejected.

The design pass that produced this ADR resolved the resulting tension not by
acquiring more data but by **scoping the claims to what the data supports** and
making every gap explicit. The prior shelving blocker — "region look-through is
unavailable" — dissolves once each dimension is independently available: region
is simply marked unavailable, not a gate on the command.

The governing principle is therefore: **usefulness, not completeness.** Ship the
subset that is authoritative today; never emit a precise number whose underlying
security identity or coverage is actually ambiguous.

## Decision

**Governing invariant — the type-safety rule for the analytics layer:**

> **No analytical result may imply information that its provenance does not
> establish.**

Every decision below is an instance of it: top-10 names do not establish complete
overlap; matching strings do not establish security identity; domicile does not
establish revenue geography; swap collateral does not establish underlying
exposure; unavailable region data is not zero region exposure; an HHI *bound* is
not an *estimated* HHI. When adding a metric or a source, the test is this one
sentence — not whether a number *can* be produced, but whether its evidence
*entitles* it.

### 1. Two commands on an honesty boundary; `concentration` is v1a

The single `diversify` command is replaced by a deliberate split along the line
where data quality changes:

- **`concentration` (this ADR, v1a)** — reliable **within-fund** analysis, one
  fund at a time. Ships now.
- **`overlap` (deferred, v1b)** — **cross-fund** single-name overlap, which
  requires resolving source holding names to canonical securities. A separate
  later command (see Deferred), not a mode of this one.

The split is load-bearing, not cosmetic. Asking "how diversified is my
portfolio?" implicitly promises cross-fund identity resolution; scoping v1a's
command to one fund keeps it from ever implying that `VWCE: Apple 4.1%` and
`CSPX: Apple 7.2%` are known to be the same security when they are only matching
strings.

### 2. Claim boundary — the output contract

`concentration` **may assert** (authoritative): per-fund security concentration,
top-N concentration, HHI / effective holdings, sector concentration, asset-class
concentration, coverage + confidence, and rank-constrained HHI bounds for the
unobserved tail.

It **must not assert**: cross-fund overlap, aggregate exposure to a company, or
any portfolio-level single-name concentration. A normalized-name match across
funds may appear **only as an unresolved signal** — e.g. `"Apple" in the top 10
of 3 funds — canonical identity unresolved` — whose sole purpose is to point at
where manual resolution (v1b) would pay off. It is never summed into a number.

Two invariants make that boundary enforceable rather than aspirational:

- **Name/ticker match may *suggest* identity; only canonical resolution
  *establishes* it.** Ticker and name matching are hints, never identity — they
  are wrong under ticker reuse, dual/cross listings, ADRs, share classes, and
  renames. v1a solves none of those and its schema must not accidentally claim it
  has (see decision 5).
- **Unknown is distinct from zero, everywhere.** `region = unknown` is not
  `region = 0%`; `Apple overlap = unresolved` is not `Apple overlap = 0%`. The
  analytical layer and every rendered figure preserve the distinction — a missing
  observation is never coerced to a zero that reads as a known fact.

### 3. Coverage-aware concentration — observed, bounded, never false-precise

Because only the top 10 holdings are named, a single point HHI would imply full
observation the tool does not have. Instead each fund reports **observed** figures
plus a **bound** on the unobserved tail. With known top-10 weights `w₁..w₁₀`,
unobserved remainder `R = 1 − Σᵢ wᵢ`, and an optional reported total holding
count `N`:

```
HHI_observed = Σᵢ wᵢ²
HHI_min      = HHI_observed + (R² / (N − 10)   if N known, else 0)
HHI_max      = HHI_observed + R · w₁₀
```

- **Max uses the rank constraint.** Holdings are ordered by weight, so every
  unobserved security weighs `≤ w₁₀`; packing the tail at that cap gives
  `R·w₁₀` — a hard upper bound, not an estimate. The naïve `R²` (whole remainder
  as one security) is a valid supremum but practically vacuous (for a ~1500-holding
  fund it is ~65× looser), so it is not used.
- **Min tightens only with `N`.** Spreading `R` evenly minimises Σ of squares;
  without `N` the infimum is just `HHI_observed`, so the min bound degrades
  gracefully to the observed value.

Effective holdings is reported as the range `1/HHI_max .. 1/HHI_min`. Each metric
carries a four-state **status** (defined in decision 7): the security HHI here is
**BOUNDED** — defensible bounds, no point value — while sector and asset-class
weightings sum to 100% from yfinance and are **CALCULATED**.

**Coverage is an explicit NAV denominator, not a vague score.** Every result
answers "concentration *of what fraction of the portfolio?*" with numbers, not a
`confidence = medium` label:

```
Observed:    31.2% NAV   (top 10)
Unobserved:  68.8% NAV
```

A numeric confidence hides *why* the data is weak; an observed/unobserved NAV
split shows it immediately. A short confidence word may accompany the figures but
never replaces the denominator.

**The cumulative concentration curve is the flagship security-dimension output** —
what fraction of NAV the largest X holdings represent (Top 1 / 5 / 10 / 25 / 50).
It is cheaper to read than HHI and works well in a terminal. Crucially, its rungs
are **coverage-bounded**: with a yfinance-only source the named data stops at the
top 10, so Top-25 and Top-50 render as `—` (**unknown**, per decision 2), never
`0`. A manually-imported full-holdings observation (higher tier) lights up the
deeper rungs; yfinance alone does not. The curve therefore never implies holdings
it did not observe.

### 4. Per-dimension availability, no fabricated exposure

Each concentration dimension has its own coverage and confidence, and a missing
dimension does not fail the command:

- **Sector** — complete (yfinance `sector_weightings`), high confidence for all
  funds including swap and holdings-blocked ones. v1 uses the **source's own
  taxonomy verbatim** — no remap to a canonical sector scheme. Canonicalization is
  another identity problem, and it only becomes necessary if funds using *different*
  classification systems are ever combined; it is not paid for pre-emptively.
- **Asset-class** — complete (yfinance `asset_classes`).
- **Security** — top-10 observed, tail bounded (decision 3).
- **Region / country** — **unavailable in v1**, printed as such. It is *deferred,
  not dropped* (see Deferred); it must never be gated on, and it must never be
  fabricated from swap collateral, which discloses collateral rather than index
  exposure and would be actively misleading.

### 5. Data source and storage — yfinance, cached, additive for v1b

The source is yfinance `funds_data` (ADR-0001 dependency; snapshot only, no
history). Look-through is cached so `concentration` runs offline, refreshed on
fetch. The immutable unit is a **snapshot**, split header/children so provenance
is recorded once per observation rather than repeated per holding:

- `holdings_snapshot(id, fund_id, as_of, source, tier, retrieved_at,
  reported_holding_count)` — one observation of one fund from one source. `tier`
  is `provider` for yfinance, later `curated` / `issuer` / `inferred`. Mirrors the
  `prices` / `fx_rates` provenance shape; higher-tier data lands as **new
  snapshots, additively without a rewrite** — the single most important storage
  constraint for v1b. `reported_holding_count` is the nullable N (factsheet-derived
  when trivially available, no acquisition machinery) that tightens `HHI_min`; null
  falls back to the observed value.
- `holding(snapshot_id, raw_name, normalized_name, weight, rank)` — the child rows.
  `raw_name` (`"Apple Inc."`) is the **evidence, preserved non-destructively**;
  `normalized_name` (`"apple"`) is an analysis aid only; `rank` makes the weight
  ordering **first-class**, so the `w₁₀` rank constraint (decision 3) and the
  cumulative curve read it directly instead of re-deriving it.
- **Snapshots are immutable and append-only.** A corrected file for the same
  `(fund, as_of, source)` lands as a **new** snapshot — the original is never
  overwritten — and analysis selects the latest / highest-tier snapshot while the
  prior is retained as evidence. An **identical** re-observation is not
  re-inserted, so the auto-refresh never becomes a fetch log; only a genuine change
  creates a snapshot. **Coverage** (Σ observed weight) is a derivable property of a
  snapshot and, since the snapshot is immutable, may be cached on the header.
- **Weights** for a fund's portfolio share reuse the ADR-0011 EUR valuation path
  (`convert_to_eur`, the position timeline), cost-basis fallback for unvaluable
  holdings.

Canonical identity lives in a **separate, deliberately modest** table —
`security_alias(raw_name, canonical_name, canonical_key, reviewed_at)`, *not* a
"security master" (the name would imply more ambition than needed). Empty in v1a,
populated only by v1b — incrementally from the overlap-candidate report (decision
6). `canonical_key` may gain an ISIN later if one is obtained. The load-bearing
property: **canonicalization is an explicit reviewed fact** (`reviewed_at` records
the human review), never the automatic output of a string-similarity algorithm.

### 6. Output style and honesty caveats

Output mirrors `_cmd_portfolio` / `performance` — a formatted point-in-time table,
no CSV export in v1. It leads with the coverage denominator and the concentration
curve, marks unavailable rungs/dimensions as `—`, and ends with the unresolved
overlap-candidate signal. Shape:

```
e1f concentration VWCE

Coverage       31.4% NAV (top 10)   Unobserved 68.6%
Top 1           7.1%
Top 5          20.8%
Top 10         31.4%
Top 25          —     (unknown: top-10 source)
HHI            0.021 observed / 0.024–0.087 bounded
Sector         <sector HHI + weights, Yahoo taxonomy>
Region         unavailable  (no reliable free source)

Potential overlap candidates — identity unresolved:
  Apple Inc.       3 funds
  Microsoft Corp.  3 funds
```

The **overlap-candidate report is a first-class v1a output**, labelled *identity
unresolved* in full: it lists raw names appearing in multiple funds' top-10 and is
never summed. Its job is strategic — it turns v1b from a large up-front project
into an **evidence-driven, incremental** one: the user canonicalizes (via
`security_alias`) only the handful of names that materially affect the portfolio,
and only once v1a shows they recur. Printed caveats state plainly that look-through
is a periodic snapshot, the security dimension is top-10-only, the sector taxonomy
is Yahoo's, and there is no region dimension — so a reader never mistakes a bounded
or partial figure for a complete one.

### 7. Provenance is reconstructed, not logged — `--explain` and metric status

The governing goal: for any number e1f prints, you can ask **What** (the figure),
**How** (formula/method), **From what** (the exact snapshot + source), and **With
what limitations** (coverage, assumptions, unresolved identity, unavailable
dimensions). This is what makes the intentionally-incomplete data architecture
*trustworthy* — uncertainty is made impossible to overlook rather than eliminated.

**Reconstruct, don't persist.** The concentration math is pure and deterministic
over the immutable snapshots (decision 5), so a result's full provenance is
**recomputable on demand** — it is *not* written to an audit table at compute
time. This is deliberate: persisting the computed `result` / `method` /
`limitations` (a classic `audit_log` table) denormalizes a derived value, so the
moment the bound formula or a snapshot-selection rule changes, every logged row
disagrees with what the code now produces — two sources of truth for one HHI. The
snapshot identity is therefore the *only* new provenance persistence. A historical
"what e1f claimed on date X, before the formula changed" trail is a distinct,
niche want (regulatory-style memory) and is **deferred**, not built.

**`e1f concentration <fund> --explain`** renders the reconstructed chain per
metric — Result, Inputs (the exact ranked weights, source, `as_of`,
`retrieved_at`, coverage), Method, and the paired limiting factors below. It
references the snapshot id, so the exact weights are reproducible without
duplicating them into a log. This vocabulary (Result / Inputs / Method /
Limitations / limited-by) is defined **independently of any HHI or top-10
specifics**, which is the precondition for graduating it cleanly (ADR-0013 does
exactly this — it moves the vocabulary into `common` so `overlap` can reuse it).
Human-readable in v1; `--explain --json` deferred. Kept as a flag, not a separate
`e1f audit` command, to avoid CLI surface sprawl.

**Method carries a version id, not just the code.** The Method line prints the
methodology identifier from the metric's contract — e.g.
`method = hhi_rank_capped_tail_v1` — so an explanation states *which* methodology
produced it. Without this, "reconstructable" silently degrades into
"reconstructable per today's code": improve the bound formula and old
reconstructions change methodology invisibly. The id does not let today's code
reproduce yesterday's *numbers* (that needs the old code) — it makes the
methodology **explicit and detectable**, and it is the key a future
persisted-claims trail would carry.

**`limited by` and `not limited by` are a first-class pair, not prose.** They are
rendered directly from the contract's `requires` (what, if improved, would tighten
or unblock the result) versus `does_not_require` (what would not help). This is
strictly more actionable than a confidence score — it says *which additional data
buys accuracy*:

```
Status:         BOUNDED   (method = hhi_rank_capped_tail_v1)
Result:         HHI ∈ [0.0201, 0.0264]
Limited by:     top-10 holdings only ; reported holding count unavailable
Not limited by: sector classification
```

**Four-state status per metric** (this is the single status vocabulary — it
replaces the earlier Observed/Bounded/Estimated figure-labels so there are not two
overlapping schemes):

- **CALCULATED** — enough evidence for a point value (sector HHI, asset-class).
- **BOUNDED** — no exact value, but defensible math bounds exist (security HHI).
- **UNAVAILABLE** — not enough reliable info for even a useful bound (region).
- **UNRESOLVED** — identity is the blocker, not coverage (cross-fund single-name
  overlap; v1b territory).

A run reads e.g. `Security HHI BOUNDED · Sector HHI CALCULATED · Region HHI
UNAVAILABLE · Apple overlap UNRESOLVED` — more informative than one confidence
score, and each status is the entry point for "what would change it."

**Negative knowledge is audited too — arguably first.** `--explain` makes the
*deliberate non-claims* explicit: not merely `region = UNAVAILABLE` but the
reason, what inputs exist, and what was **available-but-refused** and why — e.g.
*swap collateral available but excluded: collateral is not underlying index
exposure*. This `status` + `reason` pairing is **derived at analysis time** from
availability (not a stored column), so it can never go stale against the data.

**Each metric declares a data contract** — a small in-code declaration
(`method_version` / `requires` / `does_not_require` / `supports` / `limitations`)
that is the single source driving the calculation's data requirements, its
`--explain` limited-by / not-limited-by split (from `requires` / `does_not_require`),
and its printed method id. Keeping it in code, consumed by all of these, stops the
limitation prose from drifting out of sync with the calc, and makes overclaiming
structurally hard: a metric whose contract `requires` canonical identity **cannot
ship in v1a**, where that contract is unmet.

**Scope note:** this provenance/status/`--explain` model is general — it applies
equally to `performance` and `portfolio`. It is recorded here scoped to
`concentration`; if it is generalized across commands (rewiring `performance` /
`portfolio` to report *through* this model) it graduates to its own ADR — the
next available number (0014+). Note this is distinct from ADR-0013 (`overlap`),
which merely relocates the vocabulary into `common` for reuse without retrofitting
any other command.

## Rationale

- **Usefulness over completeness** — v1a answers real questions today (how
  concentrated is each fund, by security and sector; which funds have poor
  coverage; where manual resolution would pay off) without waiting on data that
  has no free source.
- **The claim boundary prevents the worst failure mode** — a mathematically
  precise portfolio-level number over ambiguous identity is worse than an honest
  "unresolved," because a precise-but-wrong number gets acted on.
- **Bounded beats hidden and beats false-precise** — a caveat that travels with
  the number (the ADR-0011 "flag, never suppress" ethos) is more honest than
  either an empty cell or a single HHI pretending to full observation.
- **Additive storage** — dated/sourced/tiered observations mean v1b (and any
  future higher-tier source) layers in without migrating history.
- **Provenance that cannot lie** — reconstructing `--explain` from immutable
  inputs (rather than logging computed results) means the explanation is always
  what the code actually did; there is no second, drifting record. The trace
  doubles as an expansion map: its `limited by` lines are the prioritized list of
  what to fix or add, which is how the intentionally-incomplete design stays
  legible instead of opaque.

## Consequences

- `concentration` depends on yfinance being reachable to refresh the cache;
  once cached it runs offline. A fund yfinance cannot resolve degrades to
  `unavailable` per dimension rather than failing the command.
- The command is **explicitly not** portfolio diversification analysis; it is
  within-fund concentration with coverage-aware evidence. Cross-fund exposure
  claims wait for v1b.
- New module + three tables (`holdings_snapshot`, `holding`, `security_alias`), a
  `--explain` flag, and the four-state status enum; `cli.py` `COMMANDS` /
  `PARSER_FACTORIES`, the import-linter layers list, and autocomplete gain an
  entry (ADR-0003). Coverage floor 90%.
- The concentration math (HHI, effective-N, the bounds of decision 3) is pure and
  tested in isolation, like `performance`'s return math — which is also what lets
  `--explain` reconstruct provenance instead of logging it (decision 7).
- **No `audit_log` / computed-result table** — provenance is reconstructed on
  demand from immutable snapshots, so there is nothing to keep in sync with the
  code.

## Deferred (not in this ADR)

- **`overlap` command (v1b)** — cross-fund single-name overlap. Its only new
  conceptual dependency is the `security_alias` table (decision 5), populated
  **incrementally** from v1a's overlap-candidate report — the user reviews only the
  names that recur and matter, rather than canonicalizing hundreds up front. Even
  fully resolved it reports **observed** overlap, keeping *two* qualifiers: identity
  resolved, but top-10-only inputs mean coverage is still partial (unobserved tails
  may hold more of the same name). Slogan: **v1a detects candidates for overlap;
  v1b establishes it.**
- **Region / country concentration** — deferred, not dropped. The recommended
  route is name-derived structural tags (S&P 500 ⇒ US) with a small curated
  override sidecar (~22 rows, ADR-0002 pinning pattern), added as an independent
  classification layer if it proves trivial. Never fabricated from swap collateral.
- **Rebalance / new-ETF suggestions** — proposing re-weightings or funds outside
  the universe to reduce concentration. Needs the concentration picture (this ADR)
  as its input; a design of its own.
- **Valuation screening** ("ETFs valued low for long-term") — the original
  feature 2, dropped during design; may return as a separate ADR. Fund-level P/E
  etc. are available from yfinance (as inverted yields) but are a snapshot only
  and composition-confounded, so a naïve ranking would mislead.
- **Higher-tier holdings** (per-issuer files, paid API) — only if v1a shows the
  bounded/top-10 picture genuinely changes a decision. The tiered, dated schema
  (decision 5) is designed to absorb them without a rewrite.
- **`--explain --json`** — machine-readable provenance for a future consumer;
  v1's trace is human-readable only.
- **Persisted historical-claims trail** — a record of what e1f *told you* at a
  past moment, surviving later formula changes (regulatory-style memory). Distinct
  from the reconstructed `--explain` (decision 7); deferred unless a concrete need
  appears, since it reintroduces the drift risk that decision consciously avoids.
- **Generalizing provenance across commands** — if the decision-7 model is applied
  to `performance` / `portfolio`, it graduates to its own ADR (the next available
  number, 0014+ — **not** ADR-0013, which is `overlap`).
