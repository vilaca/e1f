# ADR-0051 — `--horizon`: explicit return-sampling frequency

**Scope:** a `--horizon daily|weekly|monthly` option on `correlation`,
`benchmark`, and the scenario-risk path (ADR-0049/0050), selecting the frequency
at which EUR returns are sampled before any statistic is computed. Default stays
`daily` — no existing output changes. Resampling is non-overlapping and anchored
at the window end.

## Context

Every return statistic in e1f is computed from **daily** EUR returns. That was an
implicit decision, never a recorded one, and ADR-0048 shows it is load-bearing:
horizon-dependence is the diagnostic that separates a genuinely uncorrelated asset
from a measurement artifact.

It also changes conclusions, not just diagnostics. Two scenario books compared in
this session ranked one way on daily returns and the other way on weekly, because
the daily figures understated the co-movement of a fund one of them tripled:

| Book | Vol (daily) | Vol (weekly) |
| --- | ---: | ---: |
| ex-USA at 18% | 11.18% | 12.79% |
| ex-USA at 12% | 11.15% | **12.44%** |

On daily returns the two are indistinguishable; on weekly the second is clearly
lower-risk. Recommending the first would have been an artifact of the sampling
frequency alone.

ADR-0048 deliberately refuses to *silently* substitute a weekly ρ for a daily one.
That refusal only holds up if changing horizon is available as an explicit,
disclosed user choice — otherwise the tool detects a problem it gives no way to
work around. This ADR is the other half of that decision.

## Decisions

### 1. Non-overlapping resampling, anchored at the window end

Weekly and monthly returns are taken between period-end closes drawn from the
gap-bridged EUR series, walking **backwards** from the window's last trading day
so the most recent observation is always a full period. Overlapping windows were
rejected: they inflate sample size without inflating information and bias every
standard error, which would quietly weaken the ADR-0015 rule that a ρ's `n` is
evidence of its strength.

### 2. Periods come from the price calendar, not the wall calendar

A "week" is five trading observations in the gap-bridged series, a "month" is 21.
Using calendar boundaries would reintroduce the holiday and venue-alignment
problems ADR-0015's gap-bridging exists to solve, and would make the period count
depend on which venues a pair happens to share.

### 3. Minimum-overlap floors scale with horizon

`--min-overlap` (60 daily observations, ADR-0015) becomes horizon-relative: 60
daily, 26 weekly, 12 monthly. A fixed floor would either make weekly correlations
unobtainable for young funds or make monthly ones meaningless. Below the floor the
pair is `UNAVAILABLE` exactly as today.

### 4. Annualization follows the horizon

√252, √52, √12 for volatility and tracking error; the wealth-index CAGR is
unchanged (it is period-count-invariant by construction). A mismatched
annualization factor is the classic silent error here, so the factor is asserted
per horizon in tests rather than inferred at the call site.

### 5. The horizon is printed, always

The window-policy line becomes `Window policy: pairwise overlap · min N returns ·
returns in EUR · horizon=weekly`. Two ρ values measured at different horizons are
not comparable, and a reader must never have to infer which they are looking at.
This is why the horizon is disclosed even at the `daily` default.

### 6. The default does not move

`daily` stays the default everywhere. Weekly sampling is more robust for
covariance, but changing the default would silently restate every number the tool
has ever reported, and would mask rather than surface the ADR-0048 problem — a
flagged fund would simply stop looking anomalous.

## Implementation

`common/returns.py` gains `Horizon` (an enum carrying its step, min-overlap floor
and annualization factor) and `resample(series, horizon) -> list[float]`.

`correlation.py`, `benchmark.py` and the ADR-0049 static-weight path take
`--horizon` and thread the enum into every estimator; ADR-0048's horizon-ratio
detector reuses `resample` rather than carrying its own.

`data/glossary.md`: **Horizon**, plus a `Where` line on every metric whose value
depends on it (ρ, Vol, TE, Beta, R², IR).

## Invariance

`--horizon daily` reproduces every pre-ADR number byte-for-byte across the full
existing fixture set — the regression that makes this change safe. A pinned-date
test asserts non-overlapping period boundaries land on the expected closes walking
back from a fixed window end, and that a synthetic series with known weekly
correlation recovers it at `--horizon weekly` and is attenuated at `--horizon
daily` by the expected factor.
