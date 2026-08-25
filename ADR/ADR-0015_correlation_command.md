# ADR-0015 — `correlation` command (return co-movement redundancy & clustering)

**Scope:** a new read-only `correlation` subcommand that measures **return
co-movement** across held funds — the second, statistical axis of portfolio
redundancy. It reports (a) highly-correlated fund pairs carrying meaningful
combined weight, and (b) a hierarchical clustering of held funds into
move-together groups. A single self-contained command module
`src/e1f/correlation.py`, fitting the existing flat layout and ADR-0003's
`cli → command modules → common` contract unchanged — it joins the command
siblings and imports from `common` only what is genuinely shared (decision 7).

Slogan: **`overlap` asks what the funds *hold* in common; `correlation` asks how
the funds *move* in common.**

## Context

ADR-0012 (`concentration`) and ADR-0013 (`overlap`) measure redundancy through
**look-through holdings** — do two funds hold the same underlying security? That
axis is bounded by the yfinance top-10 constituent limit: a genuine overlap
sitting outside every fund's top-10 is invisible to it, and the honest floor
`overlap` reports says so.

Return co-movement is an independent axis that sidesteps that data gap entirely.
Two funds that hold overlapping-but-unobserved global-equity baskets will still
*move together*, and their daily EUR returns reveal it directly from price data
e1f already stores. For a portfolio that is "mostly overlapping global/US-equity
exposure," this is often the redundancy that matters most — and the one
look-through data cannot see. The two axes are complementary: neither is a
superset of the other, and this ADR adds the second without weakening the first.

The governing invariant carries over unchanged from ADR-0012:

> **No analytical result may imply information that its provenance does not
> establish.**

For a correlation, provenance is the **sample it was estimated from** — its date
window and its length. A ρ from 6 weeks of shared history is not the same kind of
evidence as a ρ from 10 years, and this command makes that difference structural,
not a footnote.

## Decision

### 1. Correlation is a distinct redundancy axis, reported alongside — never merged into — look-through overlap

`correlation` and `overlap` answer different questions and must not be conflated
in output or in a combined "redundancy score." A high ρ is **statistical**
evidence (funds moved together, for whatever reason — shared holdings, shared
sector, shared macro factor); a resolved `overlap` floor is **structural**
evidence (a named security is demonstrably held in both). Fusing them into one
number would let statistical co-movement masquerade as established shared
holdings, the exact category error the ADR-0013 provenance model exists to
prevent. They stay separate commands with separate vocabularies.

### 2. Pairwise-overlap alignment, because young funds must stay in the picture

To correlate two funds' returns they must be aligned on shared trading days. With
funds of very different inception dates, three alignment policies were weighed:

| Policy | Effect | Verdict |
| --- | --- | --- |
| **Intersection** (single window = youngest fund's life) | one consistent matrix | **rejected** — one young fund truncates *every* pair's history |
| **Fixed lookback** (trailing N years) | recency-focused | **rejected as default** — drops funds younger than N; shrinking N to fit them collapses back to intersection |
| **Pairwise overlap** (each pair uses its own shared window) | max data per pair | **chosen** — the only policy that keeps young funds *and* preserves long history for old pairs |

Each pair `(i, j)` is correlated over exactly the days both funds have EUR
returns (an exact-date inner join). This is the base policy; a `--window Ny`
recency view (the fixed-lookback flavour) is deferred (see Deferred).

### 3. The pair result is a typed contract carrying its own coverage qualifier

Pairwise windows make sample length vary per cell, so sample length becomes part
of each number's provenance. The command therefore never emits a bare ρ; every
pair produces a `PairwiseOverlap` value (defined in `correlation.py`; `Status`
reused from `common`, ADR-0013) whose `status` and `reason` make the coverage
qualifier the **type of the result**, not a footnote:

```python
@dataclass(frozen=True)
class PairwiseOverlap:
    status: Status          # CALCULATED | UNAVAILABLE  (Status from common, ADR-0013)
    returns_a: list[float]  # aligned, date-sorted (may be empty when UNAVAILABLE/insufficient)
    returns_b: list[float]  # aligned, date-sorted (len == len(returns_a))
    start: date | None      # first aligned date (None if UNAVAILABLE for insufficient overlap)
    end: date | None        # last aligned date
    n: int                  # len(returns_a) == len(returns_b), may be 0
    rho: float | None       # Pearson coefficient; not None iff CALCULATED
    reason: str | None      # None if CALCULATED; else "insufficient_overlap" |
                            #   "zero_variance" | "numerical_error"
```

**Invariants (regression-tested):**

- `len(returns_a) == len(returns_b) == n`, always.
- `CALCULATED` → `n >= MIN_OVERLAP`, `rho is not None`, `start`/`end` not None,
  `reason is None`.
- `UNAVAILABLE` + `"insufficient_overlap"` → `n < MIN_OVERLAP`, `rho is None`;
  `returns_a`/`returns_b` may be empty.
- `UNAVAILABLE` + `"zero_variance"` → `n >= MIN_OVERLAP`, `rho is None`; the aligned
  series are **retained** (diagnostic for `--explain`).
- `UNAVAILABLE` + `"numerical_error"` → `n >= MIN_OVERLAP`, `rho is None`; aligned
  series retained.

**Enforcement — by construction discipline, not `__post_init__`.** Matching the
codebase convention (every existing `@dataclass(frozen=True)` is a plain frozen
dataclass with no validating `__post_init__`), `PairwiseOverlap` has a **single
construction site**: `pairwise_overlap()` is the only function that builds one, and
its frozen pipeline (below) is the sole path to each status/reason combination.
Invariants are therefore guaranteed by that one path and pinned by tests over every
branch — including the subtle trap that a `zero_variance` (and `numerical_error`)
result must **retain** its aligned vectors (`len(returns_a) == len(returns_b) == n`,
not `[]`), since those states occur only after alignment succeeded.

`MIN_OVERLAP` defaults to **60 return observations** (roughly one quarter of
trading days), a floor below which a daily-return correlation is dominated by noise. Every emitted ρ is rendered with
its `[start … end]` window and `n` so a 90-day estimate visibly reads as weaker
evidence than a 4000-day one. An `insufficient_overlap` pair is reported
UNAVAILABLE, never as a point estimate.

`n` is the number of **aligned return observations** for the pair — not a global
property of either fund. "Trading days" is presentational shorthand; the
mathematical quantity is observations (a return dated `t` derives from prices at
`t−1` and `t`). Correspondingly, **the zero-variance check is evaluated on the
pair's aligned sample, never on a fund's global history.** A fund may vary across
its full history yet be constant over one pair's shared window (e.g. a stale/pinned
price stretch); that pair alone is `zero_variance`/UNAVAILABLE. An implementer must
**not** cache a single "fund has variance" property and reuse it across pairs.

**The pair pipeline (frozen):**

```
1. Load EUR daily returns for i and j → [(date, float)], each date-sorted.
2. Exact-date inner join → dates, r_i, r_j; n = len(dates).
3. n < MIN_OVERLAP  → PairwiseOverlap(UNAVAILABLE, [], [], None, None, n, None,
                                       "insufficient_overlap")
4. σ²_i = np.var(r_i), σ²_j = np.var(r_j)   (variance on the ALIGNED sample, not
                                             the global series; population vs sample
                                             is irrelevant — used only as a >0 guard,
                                             and Pearson's scaling cancels either way)
5. σ²_i < 1e-12 or σ²_j < 1e-12 → PairwiseOverlap(UNAVAILABLE, r_i, r_j,
                                    dates[0], dates[-1], n, None, "zero_variance")
6. ρ = pearson_correlation(r_i, r_j)   (precondition: r_i, r_j finite, equal-length,
                                        each with variance > 1e-12 — guaranteed by
                                        steps 1–5; may still return NaN on extreme
                                        inputs, caught in step 7)
7. Validate ρ, in this order (the ordering is load-bearing — see below):
     not isfinite(ρ)          → "numerical_error"   (catches NaN/±inf FIRST)
     |ρ − 1.0| <= 1e-12        → ρ = 1.0             (clamp)
     |ρ + 1.0| <= 1e-12        → ρ = −1.0            (clamp)
     ρ < −1.0 or ρ > 1.0       → "numerical_error"
     otherwise                → keep ρ
   (a "numerical_error" here returns UNAVAILABLE with r_i, r_j, dates[0], dates[-1],
    n, rho=None, reason="numerical_error")
8. → PairwiseOverlap(CALCULATED, r_i, r_j, dates[0], dates[-1], n, ρ, None)
```

**The finite check must come first, and both clamp comparisons use `<= 1e-12`
(never `<`) — the ADR uses one inequality throughout.** A NaN result (possible from
degenerate NumPy inputs even after the variance guard) is *not* caught by the range
test, because `nan < −1.0` and `nan > 1.0` are **both false**; it would otherwise
slip through as a spurious CALCULATED value. Float noise is clamped **only** within
`1e-12` of ±1; anything further out of range is a `numerical_error`, never silently
clamped into an apparently valid coefficient (ADR-0013's "never clamp, never infer").

**Invariant (into the ADR):**

> A return correlation may be reported only from a shared sample of at least
> `MIN_OVERLAP` observations with non-zero variance in both legs, and always
> carries its window and sample length. Any pair failing these preconditions is
> reported UNAVAILABLE with an explicit reason — never as a point estimate.

### 4. Returns are computed on EUR-converted prices, because that is the investor's real experience

Each fund's return series is the simple daily return of its **EUR-converted**
close (local price × FX, via the `convert_to_eur` path in `common`, ADR-0010).
Correlating EUR returns folds in the shared-currency co-movement a EUR investor
actually bears — two USD funds share USD/EUR FX risk, and that *is* real
redundancy for this portfolio. Local-currency returns would hide it.

**`eur_return_series` — the exact emission rule (resolves the missing-data
ambiguity).** A fund's EUR close on a date exists only when *both* a local price and
an FX rate exist for it. Returns are the simple return between **consecutive
*available* EUR closes**, in date order; a return is dated at the **later** date `t`
and equals `close_t / close_{prev} − 1`, where `close_{prev}` is the immediately
preceding *available* EUR close. Gaps are **bridged, not filled**:

```
EUR close:  2026-01-01 → 100 ; 2026-01-02 → missing ; 2026-01-03 → 102
→ one return, dated 2026-01-03, = 102/100 − 1   (computed against Jan 1;
   the missing Jan 2 is skipped, NOT forward/zero-filled)
```

No forward-fill or zero-fill is ever performed (ADR-0013's "never clamp, never
infer" applied to returns); a missing observation simply means the adjacent return
spans it and narrows the overlap for pairs involving this fund. Missing FX/price
days are expected to be rare, so bridging avoids needlessly discarding real data.

### 5. Idea 1 — pairwise redundancy flags, weighted by EUR value

Redundancy is the product of *co-movement* and *concentration*: two funds that
move together matter only insofar as you hold enough of both. For each `CALCULATED`
pair the command computes the combined EUR portfolio weight `w_i + w_j` (weights
from the `fund_eur_value` core, ADR-0013 decision 4) and flags a pair when **both**
cross thresholds:

```
ρ ≥ RHO_FLAG (default 0.90)   AND   w_i + w_j ≥ WEIGHT_FLAG (default 0.20)
```

**Weights normalize over the correlation universe** (defined precisely in
decision 8: held, net-across-brokers, positive EUR value, *and* has a usable return
series): `w_f = value_f / Σ_{k ∈ universe} value_k`. A fund excluded from that
universe — non-positive/unvaluable, or positive-but-no-usable-history — is in
neither numerator nor denominator and is disclosed in its own category (decision 8),
the same consistent-basis discipline as ADR-0013 decision 6.

Output is the flagged pairs sorted by `ρ × (w_i + w_j)` descending — "these two
funds are ~the same bet and you hold a lot of both." A portfolio-level scalar
(e.g. an eigenvalue-based *effective number of independent bets*) is **deferred**:
it needs a single coherent PSD matrix, which pairwise windows do not guarantee
(decision 6). Pairwise flags are robust to that inconsistency; a scalar is not.

### 6. Idea 2 — clustering on the *all-peer-valid subset*; no fabricated distances

**Two nested universes, evaluated in order.** First the *correlation universe* is
fixed (decision 8: held, net-across-brokers, positive EUR value, usable return
series). Then a fund is *clustering-eligible* iff it has **at least one**
`CALCULATED` pair within that universe; a fund with no calculated pair at all (e.g. too little history to clear
`MIN_OVERLAP` against anyone) is disclosed as UNAVAILABLE and **does not enter the
peer-completeness test** — it can neither be included nor cause another fund's
exclusion. Only then is the all-peer-valid subset computed over the
clustering-eligible funds.

Held funds are clustered on the standard correlation distance
`d_ij = √(½(1 − ρ_ij))` using scipy's agglomerative linkage. Pearson ρ itself is
computed by the pure-NumPy `pearson_correlation` in `correlation.py` (decision 7,
**not** `scipy.stats.pearsonr`); scipy is used **only** for the clustering step
(`squareform`/`linkage`/`fcluster`). To guarantee **zero fabricated distances**,
clustering runs only on the **all-peer-valid subset** — defined precisely:

> A fund is included in the clustering set **iff it has a `CALCULATED` pair with
> every other clustering-eligible fund.**

This is deliberately *not* called a "fully-connected subset" — that phrasing
suggests clique/maximal-clique detection, which this is not. It is the set of
funds each individually connected to all others (a universal-vertex rule), and it
is intentionally conservative. Worked example (eligible = A, B, C, D; valid pairs
A-B, A-C, A-D, B-C; B-D and C-D missing):

```
A → connected to B,C,D → included
B → missing D          → excluded
C → missing D          → excluded
D → missing B,C        → excluded
cluster_funds = {A}  → size 1 → no cluster
```

So an otherwise-valid {A,B,C} triangle **can be destroyed by one unrelated sparse
fund D**. That outcome is accepted for v1: we sacrifice clustering completeness for
the guarantee that every distance fed to `linkage` is real. True maximal-clique
selection (which would keep {A,B,C}) is NP-hard and deferred.

Algorithm:

```
Phase 1  cluster_funds = { f ∈ eligible : ∀ g ∈ eligible\{f}, (f,g) is CALCULATED }
         excluded_funds = eligible \ cluster_funds
Phase 2  if |cluster_funds| ≥ 2:
             D[i][j] = √(½(1 − ρ_ij)) ; D[i][i] = 0        (complete by construction)
             Z = linkage(squareform(D), method="average")
             clusters = fcluster(Z, √(½(1 − cluster_rho)), criterion="distance")
             report clusters of size ≥ 2 (singletons omitted)
Phase 3  report excluded_funds as [UNAVAILABLE for clustering] —
             "incomplete pairwise distances to all peers"
```

Clusters are cut at a distance threshold (default corresponding to ρ ≈ 0.80,
exposed as `--cluster-rho`) and each reported cluster lists its members and
combined EUR weight.

**"Cut at ρ ≈ 0.80" is the dendrogram cut *height*, not a per-pair postcondition.**
Average linkage merges clusters on the *mean* inter-cluster distance, so a resulting
cluster may contain an individual pair with `ρ < cluster_rho` (or, symmetrically,
exclude a pair above it). This is the deliberate, standard behaviour of hierarchical
clustering and the key semantic difference from decision 5's **pairwise flags**,
which *are* a hard per-pair `ρ ≥ RHO_FLAG` predicate. The report label is acceptable
shorthand for the cut, but the implementation must **not** enforce or test "every
intra-cluster pair has ρ ≥ cluster_rho" as an invariant — that would be a false
postcondition. A caller wanting hard per-pair guarantees reads the flags, not the
clusters.

**Correctness note, recorded because it reverses an intuition:** hierarchical
clustering requires only a valid pairwise *dissimilarity* matrix, **not** a
positive-semi-definite one. Restricting to the all-peer-valid subset makes `D`
complete and symmetric with a zero diagonal — a valid dissimilarity — so the
per-pair windows of decision 2 are legitimate input. The PSD requirement applies
to *optimizers* (covariance inversion, MVO), which this command deliberately does
not do (Deferred). One nuance is accepted openly: because each `ρ_ij` comes from its
own window (different `n`, `start`, `end`), `D` need not have the properties it would
if every correlation came from one common observation matrix — e.g. its entries are
not mutually consistent estimates from a single period. That is a deliberate v1
trade-off (decision 2's price for keeping young funds), not a defect; the clustering
is read as a co-movement grouping heuristic, not a rigorous single-period statistic.

**Linkage choice:** default **average** linkage (groups funds by typical
co-movement; avoids single-linkage's chaining, where one intermediate fund
silently merges two otherwise-distinct clusters). Recorded as a default, not a
law; `--linkage` may expose alternatives later.

**scipy lives inside `correlation.py`.** `squareform`, `linkage`, and `fcluster`
are imported by the single command module and used only in its clustering step;
`common` gains no scipy dependency (decision 7).

### 7. A single self-contained command module

`correlation` is **one module**, `src/e1f/correlation.py`, like every other e1f
command. It owns all of its own logic — `PairwiseOverlap`, `eur_return_series`,
`pairwise_overlap` (alignment + `MIN_OVERLAP` floor + zero-variance guard),
`pearson_correlation` (pure NumPy — `cov / √(var_x·var_y)`, ~5 lines, no
`scipy.stats.pearsonr`), the ±1 clamp / `numerical_error` finalization, flag logic,
EUR-value weighting, clustering (importing scipy's `squareform`/`linkage`/
`fcluster` directly), the report, and `--explain`.

From `common` it imports **only what is genuinely shared**: the EUR valuation core
(`fund_eur_value`, the price-series loader, `convert_to_eur` — ADR-0013 decision 8,
ADR-0010), the `Status` vocabulary (ADR-0013), and net-across-brokers holdings
(ADR-0011). Nothing new is added to `common`; `PairwiseOverlap` and the return math
are correlation-specific and stay in the command.

**CLI parameter validation (argparse-level, all bounds enforced even if not all are
exposed yet):**

```
--rho-flag      ∈ [-1, 1]   (default 0.90)
--cluster-rho   ∈ [-1, 1]   (default ≈ 0.80)
--weight-flag   ∈ [0, 1]    (default 0.20)
--min-overlap   integer ≥ 2 (default 60)   # ≥2 observations required to define a return correlation
```

Out-of-range values fail fast with a clear argparse error, not a downstream NumPy
warning.

**Layer placement is unchanged from ADR-0003.** `correlation` simply joins the
command siblings — no new layer, no `forbidden` contract, no package split. scipy
being confined to this one command is a natural consequence of it being the only
command that clusters, not a separately-enforced boundary. Updated `layers`
contract (one addition):

```toml
layers = [
    "e1f.cli",
    "… | e1f.overlap | e1f.correlation",   # correlation joins the command siblings
    "e1f.common",
]
```

### 8. The correlation universe, its construction order, and the report taxonomy

**Construction order (deterministic, no ambiguity):**

```
1. Held funds        = net-across-brokers holdings (ADR-0011)
2. Valued funds      = { f : fund_eur_value(f) is a positive EUR value }
                       (non-positive / None valuations dropped, disclosed)
3. Return-usable     = { f ∈ Valued : eur_return_series(f) has ≥ 1 observation }
                       (positive-valued funds with no usable return series dropped,
                        disclosed SEPARATELY — they had value but no history)
4. CORRELATION UNIVERSE = Return-usable.  Weights normalize over THIS set:
       w_f = value_f / Σ_{k ∈ universe} value_k
5. Clustering-eligible  = { f ∈ universe : ≥ 1 CALCULATED pair } (decision 6)
6. Clustering input     = the all-peer-valid subset of clustering-eligible (decision 6)
```

So the weight denominator is the **return-usable, positive-value** universe — a
positive fund excluded for lack of usable history is *not* in the denominator, and
is disclosed as its own category. Screening funds *outside* the portfolio and any
prescriptive re-weighting are out of scope for v1 (Deferred).

**Report taxonomy — three distinct populations, kept distinct in output:**

```
Correlation universe (positive value + usable return series)
 ├─ has ≥1 CALCULATED pair
 │    ├─ universal-vertex (all-peer-valid)      → clustering input
 │    └─ non-universal                          → [UNAVAILABLE for clustering]
 └─ no CALCULATED pair with anyone              → [UNAVAILABLE for correlation]

Outside the universe (disclosed separately, never silently dropped):
 ├─ non-positive / unvaluable                   → excluded (no EUR value)
 └─ positive value but no usable return series  → excluded (no history)
```

`[UNAVAILABLE for correlation]` (a universe member that never cleared `MIN_OVERLAP`
against anyone) and `[UNAVAILABLE for clustering]` (a fund with pairs but missing an
edge to some peer) are **different categories** and must not be collapsed.

Shape:

```
Return co-movement — ADR-0015
Window policy: pairwise overlap · min 60 return observations · returns in EUR

Redundant pairs (ρ ≥ 0.90, combined weight ≥ 20%):
  IE00B4L5Y983  ×  IE00BK5BQT80    ρ 0.97   combined 41%   [2019-06 … 2026-08, n=1810]
  IE00B4L5YC18  ×  IE00BFY0GT14    ρ 0.94   combined 23%   [2022-03 … 2026-08, n=610]

Clusters (cut at ρ ≈ 0.80):
  Cluster 1  — 58% of portfolio   IE00B4L5Y983, IE00BK5BQT80, IE00B4L5YC18
  (singletons omitted)

Unavailable for correlation (no pair cleared 60 observations)  [UNAVAILABLE]:
  IE00BNEW2024      (in universe; only 41 return observations, no CALCULATED pair)

Excluded from clustering (incomplete distances to all peers)   [UNAVAILABLE]:
  IE00BFY0GT14      (has CALCULATED pairs, but missing an edge to some peer)

Excluded from the universe (disclosed, not correlated):
  IE00BZERO0000     (no positive EUR value)
  IE00BFRESH000     (positive value, but no usable return series yet)
```

`--explain` **recomputes** the pair from current source data and displays the
reconstructed result; it never reads a persisted correlation result (there is none —
consistent with ADR-0012 decision 7's reconstruct-don't-log philosophy). Selection:
bare `--explain` reconstructs the flagged pairs (the reported results); naming two
held ISINs (`--explain ISIN_A ISIN_B`) reconstructs *that* pair on demand — at any
status, ignoring the flag thresholds — so a reader can interrogate a specific pair
(e.g. one merged inside a cluster) that the flags did not surface. A named ISIN
outside the correlation universe is disclosed as a blocker rather than correlated. To stay
readable it renders a **bounded preview** of the aligned vectors (first/last few
observations) plus a digest of the complete vectors so a reader can confirm two runs
saw the same sample. The digest's exact serialization is an **implementation detail**,
not fixed by this ADR (cross-implementation digest reproducibility is not a v1
requirement); the authoritative reproduction path is re-running `--explain`.

## Rationale

- **A second, independent axis.** Co-movement catches the redundancy look-through
  data cannot see; keeping it a separate command stops statistical evidence from
  being mistaken for structural evidence (decision 1).
- **Young funds are first-class.** Pairwise overlap is the only alignment policy
  that neither drops young funds nor lets one truncate every other pair
  (decision 2).
- **Sample length is provenance, made typed.** `PairwiseOverlap` carries `status`,
  `reason`, window, and `n`; the weak case is UNAVAILABLE by construction, not a
  deceptively precise number (decision 3).
- **Redundancy = co-movement × weight.** A 0.99 correlation between two 0.5%
  positions is noise; the weighting surfaces what actually moves the portfolio
  (decision 5).
- **Conservative beats fabricated.** The all-peer-valid subset can discard a valid
  triangle, but it guarantees every distance handed to `linkage` is real — the same
  "exclude-and-disclose, never invent" discipline as `overlap` (decision 6).
- **Diagnostic, not prescriptive.** The command *measures* redundancy; it does not
  recommend weights, avoiding an optimizer's PSD and forecasting burdens
  (decision 6, Deferred).
- **One self-contained command.** Like every other e1f command, `correlation` is a
  single module owning its own logic and importing only the shared valuation/FX/
  status core from `common` — no new layer or contract, and Pearson stays pure NumPy
  (decision 7).

## Consequences

- One new flat module `src/e1f/correlation.py`; wired into `cli.py` `COMMANDS` /
  `PARSER_FACTORIES`, the CLI epilog, autocomplete,
  `tests/test_contracts.py::test_cli_commands_surface`, and added to the
  import-linter `layers` command siblings (one line, no new layer or contract).
- **`common.py` is unchanged** — `correlation` reuses its existing valuation/FX/
  `Status`/holdings surface and defines its own `PairwiseOverlap` and return math.
  No new storage — `correlation` only *reads* prices, FX, and transactions.
- **New dependency: scipy**, added to `pyproject.toml` as a runtime dependency
  compatible with the project's supported Python versions; e1f was numpy/pandas-only
  until now. No version pin (matching the existing unpinned `numpy`/`pandas`
  convention). Imported only by `correlation.py` (the only command that clusters).
- The pure math (alignment → variance guard → pairwise ρ over its window → distance
  → all-peer-valid subset → linkage → flag thresholds) is tested in isolation; the
  `PairwiseOverlap` invariants and a young-fund fixture (one member below
  `MIN_OVERLAP`) and a sparse-fund fixture (the {A,B,C}+D case → `{A}`, no cluster)
  are regression tests. Coverage floor 90%.
- README gains a `correlation` behaviour description and CLAUDE.md's Layout tree
  gains the two modules **when the code lands** — not before (avoids doc drift).

## Deferred (not in this ADR)

- **Universe-wide screening & outside-portfolio simulation** — correlating held
  funds against candidates you do not yet own, to test a prospective buy's
  redundancy. Wanted by the user; needs a candidate-selection surface and universe
  price coverage. A future ADR.
- **Any allocator / optimizer** (HRP, MVO, CRISP, …) — this command is diagnostic
  only. A prescriptive `allocate` would need a coherent PSD covariance estimate,
  return forecasts, and their whole validation burden; explicitly out of scope so
  `correlation` cannot quietly grow into one.
- **`--window Ny` recency view** — the fixed-lookback alignment (decision 2) as an
  opt-in second lens once young funds have more history.
- **Maximal-clique clustering** — would rescue a valid triangle from one sparse
  peer (decision 6) but is NP-hard; the all-peer-valid subset is the v1 policy.
- **Effective-number-of-bets scalar** — needs a single coherent (PSD-repaired)
  matrix (decision 5); the pairwise base does not provide one.
- **Fusing correlation with `overlap` into a combined redundancy score** —
  rejected by decision 1; a non-goal recorded here so it is not re-proposed.
- **A maximum-gap / minimum-calendar-span constraint on a pairwise window** —
  because gaps are bridged (decision 4), `n` is a count of *observations*, not a
  measure of temporal coverage: a long stretch of missing closes collapses into one
  return spanning it, so `--min-overlap` guarantees a sample size, never a minimum
  span. Missing days are expected to be rare, so v1 accepts this; a future ADR could
  add a max-gap or min-span guard if sparse histories prove a problem in practice.
