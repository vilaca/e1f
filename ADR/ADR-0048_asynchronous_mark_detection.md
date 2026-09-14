# ADR-0048 — Asynchronous-mark detection: a `validate` staleness axis and a BOUNDED correlation status

**Scope:** a new data-quality axis for **stale / asynchronous closing marks** — a
price series that is complete, spike-free and normally-volatile, yet whose *daily*
returns are too noisy to correlate. `validate` gains a staleness check (warning,
exit 0); `correlation` gains a disclosed `BOUNDED` status on every pair involving
a flagged fund. No change to stored prices, no auto-correction, no substitution of
one horizon's ρ for another's.

## Context

`validate`'s existing checks are all **coverage and level** checks: duplicate
keys, null/non-positive closes, weekend rows, missing-business-day gaps, interior
gaps, large price moves, short/sparse history, cash-like volatility. Every one of
them asks "is a close present, and is it plausible?" None asks "do consecutive
closes carry usable *return* information?"

They can all pass while the answer is no. Worked example from the current DB,
`IE0006WW1TQ4` (X MSCI WORLD EX USA 1C, an 11.8% holding):

- `validate`: clean. No gaps, no flagged moves, 630 observations from 2024-03-14.
- Annualized volatility 14.8% — normal for a developed-market equity fund, and
  sitting between MSCI Europe (13.7%) and its own peers.
- Yet its measured correlations are implausible for a developed-world-ex-US index
  fund:

| Peer | ρ daily | ρ 2-day | ρ weekly |
| --- | ---: | ---: | ---: |
| iShares Core MSCI Europe | +0.55 | +0.80 | **+0.87** |
| iShares Core MSCI World | +0.40 | +0.66 | +0.74 |
| Synthetic ex-US proxy (0.5 EU / 0.3 APAC / 0.2 EM) | +0.54 | — | +0.84 |

- Lead/lag against MSCI Europe spills into both neighbours (−1d: +0.13, 0d: +0.55,
  +1d: +0.22) instead of concentrating at zero.
- First-order autocorrelation of its own returns is **−0.17**, against −0.02 for
  MSCI Europe and −0.04 for Amundi Prime.

That signature — correlation rising sharply with horizon, return information
smeared across adjacent days, negative AR(1) — is a noisy or non-synchronous
closing mark, not genuine statistical independence. A truly independent asset
stays independent at every horizon; gold, for comparison, sits at +0.15/+0.18 to
the book at *both* daily and weekly sampling.

The consequence is not cosmetic. `correlation` currently reports this fund at
ρ +0.37 against the largest holding with no disclosure, which reads as the best
diversifier in the book. Acting on it means over-sizing a position on a
measurement artifact. ADR-0015's governing invariant already forbids this:

> **No analytical result may imply information that its provenance does not
> establish.**

For a correlation, ADR-0015 made the *sample window* structural. This ADR makes
the *mark quality* structural on the same grounds: a ρ computed from asynchronous
marks does not establish co-movement independence, and must not be presented as
though it does.

## Decisions

### 1. Staleness is a return-series property and gets its own check family

It is not a gap (every trading day is present), not a spike (no move exceeds the
limit), and not a cash-like-vol case (vol is normal). Folding it into any existing
check would misreport the cause and misdirect the repair — `fetch --force` fixes a
gap and does nothing here, because nothing is missing. New family, new message.

### 2. Two detectors, both computed from data already stored

- **AR(1)** — first-order autocorrelation of the fund's own gap-bridged EUR
  returns over the validation window. Flag at `≤ -0.10`. Negative serial
  correlation is the bid-ask-bounce / noisy-mark signature; clean funds in this
  DB sit at −0.02 to −0.04.
- **Horizon ratio** — `ρ_weekly / ρ_daily` against the fund's highest-ρ
  same-asset-class peer. Flag at `≥ 1.35`. A genuine low correlation is
  horizon-stable; an artifact is not.

Both thresholds are configurable (`--max-ar1`, `--max-horizon-ratio`) and both
must fire for a flag, because either alone has plausible innocent causes: a
volatile-but-clean fund can show mild negative AR(1), and a thin peer set can
inflate a single horizon ratio. Requiring both keeps the check conservative — this
is a warning that changes how a number is read, so a false positive is expensive.

### 3. The bias is one-sided and known, so the status is BOUNDED, not UNAVAILABLE

Asynchronous marks **attenuate** measured correlation and **understate** portfolio
volatility; they do not bias in an unknown direction. The measured ρ is therefore
a *lower bound* on the true ρ, which is exactly what ADR-0014's `BOUNDED` state
exists to express. Reporting `UNAVAILABLE` would discard a real, if conservative,
number; reporting bare `CALCULATED` would assert a precision the marks do not
support. `BOUNDED` says the honest thing: at least this correlated, plausibly more.

### 4. `validate` warns; it does not fail

Exit 0. The stored data is not wrong — a close that is genuinely the venue's
official close is correct data. What is wrong is *using it at daily frequency for
covariance*. The repair is not a re-fetch; it is reading the number differently.
The warning says so, and names the horizon at which the fund's correlations
stabilize.

### 5. `correlation` discloses; it never silently corrects

Every pair involving a flagged fund renders `BOUNDED` with the attenuation noted,
and any cluster containing one carries a footnote. The command does **not**
substitute the weekly ρ for the daily ρ, and does not drop the fund. Silent
substitution would break the ADR-0015 rule that each ρ is estimated over a stated
sample; a reader comparing two ρ values must know they were measured the same way.
Changing horizon is the user's explicit choice (ADR-0051), never an implicit fix.

### 6. The flag propagates to every consumer of pairwise ρ

`correlation`, and the scenario-risk and benchmark paths (ADR-0049, ADR-0050),
all inherit the `BOUNDED` status when a flagged fund carries weight — a portfolio
volatility built from attenuated covariances is itself understated, and a
single-command disclosure would leave that unsaid where it matters most.

## Implementation

`common` gains `mark_quality.py`: `ar1(returns) -> float`,
`horizon_ratio(returns, peer_returns) -> float`, and
`MarkQuality(isin, ar1, ratio, peer_isin, stable_horizon, flagged)`.

`validate.py` gains a `=== Mark Quality ===` section listing flagged ISINs with
both statistics, the peer used, and the horizon at which correlations stabilize.
Thresholds via `--max-ar1` (default −0.10) and `--max-horizon-ratio` (default 1.35).

`correlation.py` maps a `MarkQuality.flagged` fund onto `Status.BOUNDED` for every
pair it enters, adds the attenuation note to the notes block, and footnotes any
cluster containing one.

`data/glossary.md` gains **AR(1)**, **Horizon ratio**, and **Mark quality**, each
with its `Where` contract (`validate`, `correlation --explain`).

## Invariance

A fund whose returns are horizon-stable is never flagged: a pinned-date test
asserts `IE00B4K48X80` (AR(1) −0.02, ratio ≈ 1.05) stays `CALCULATED` while
`IE0006WW1TQ4` (AR(1) −0.17, ratio 1.58) is flagged `BOUNDED`, both computed from
pinned windows with hand-checked statistics. A flagged fund's *reported* ρ is
byte-identical to what the command reported before this ADR — only its status and
disclosure change, never its arithmetic. `validate` exit codes are unchanged for
every existing fixture.
