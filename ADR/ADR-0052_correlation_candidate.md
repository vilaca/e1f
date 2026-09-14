# ADR-0052 — `correlation --candidate`: screen an unheld fund against the book

**Scope:** let `correlation` take one or more `--candidate ISIN` values — funds
that are configured but **not held** — and report how each moves against the
current book, without requiring a scenario to be saved first. Candidates carry
zero weight and are reported in their own section, never mixed into the
weight-gated redundancy flags.

## Context

`correlation` reports the held book; `correlation --scenario` reports a
hypothetical one. Neither serves the question that precedes both: *should this
fund enter the portfolio at all?*

The gap has a concrete cost. Evaluating gold (`IE00B4ND3602`, unheld) against the
book required either saving a throwaway scenario or computing the pairwise ρ
outside the tool. The answer was decision-relevant — +0.05 to +0.27 against every
holding, against +0.75 for the regional equity sleeves under consideration — and
it is precisely the kind of screen the command already has every primitive for.
`funds --unheld` lists candidates by cost and standalone risk; nothing relates
them to what is already owned.

## Decisions

### 1. A candidate is a zero-weight member of the correlation universe

It is priced, it has a EUR return series, it enters every pairwise estimate — but
its weight is 0. This keeps one universe and one estimator rather than a parallel
code path.

### 2. Candidates are excluded from the weight-gated flags, and get their own section

ADR-0015's redundancy flag fires on `ρ ≥ rho-flag` **and** `combined weight ≥
weight-flag`. A zero-weight fund can never trip the weight gate, so a candidate
could never be flagged — and silently listing it among funds that were *eligible*
to be flagged would misrepresent why it wasn't. Candidates therefore render in a
dedicated `Candidates vs held book` section: per-holding ρ, the weighted-average ρ
to the book, and each pair's `n` and window.

### 3. The weighted-average ρ to the book is the headline, and it is weighted by held value

One number per candidate: `Σ wᵢ ρ(candidate, i)` over held funds, weights
normalized across the correlation universe as ADR-0015 decision 8 requires. It
answers "how much does this move with what I already own" directly. Per-pair rows
stay visible beneath it, because an average can hide a single high-ρ pair.

### 4. Candidates join clusters, but contribute zero cluster weight

A candidate appears in the dendrogram so its neighbourhood is visible — the
finding that a momentum fund clusters with the existing global-equity block is
exactly what a screen should surface. Its cluster's reported weight percentage is
unchanged by its presence, and the cluster line marks it, so a 0% contributor can
never inflate an apparent concentration.

### 5. `--candidate` and `--scenario` are mutually exclusive

`--scenario` already answers "what does the book look like after I buy this," with
real weights. Layering a zero-weight candidate onto a hypothetical book yields two
different notions of "not currently held" in one report, with no reading that is
obviously correct. One question per invocation.

### 6. A held ISIN passed as `--candidate` is an error

It would report a fund at zero weight that in fact carries weight. The error names
the holding and points at the default report.

## Implementation

`correlation.py`: `--candidate` (`action="append"`); `_build_universe` admits
zero-weight members; a `_render_candidates` section; validation against the held
set and against `--scenario`.

`data/glossary.md`: **Candidate ρ** and **Weighted-average ρ to book**, with their
`Where` contract.

## Invariance

Adding a candidate never changes any number in the held-book report: a pinned test
asserts the flags, clusters and cluster weights of `correlation` and
`correlation --candidate X` are identical, since a zero-weight member cannot enter
a weighted statistic. The weighted-average ρ of a candidate that *is* a held fund's
exact price series equals that fund's own weighted-average ρ to the rest of the
book, pinned as a degenerate case.
