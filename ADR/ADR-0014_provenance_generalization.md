# ADR-0014 — provenance generalization across commands

**Scope:** retrofit `performance` and `portfolio` to disclose provenance through
the shared four-state `Status`, `MetricContract`, and `--explain` rendering helpers
that ADR-0013 decision 8 graduated into `common` as a *mechanism* without
generalizing its *use*. No new analytics: every number these commands already
compute is unchanged; this ADR only makes each figure's provenance sayable in the
one vocabulary `concentration` and `overlap` already speak.

Fulfils the forward reference ADR-0012 decision 7 and ADR-0013 decision 8 both
name — "a future ADR (0014+)" — and retires the ADR-0014 placeholder.

## Context

`concentration` and `overlap` disclose *why a figure is trustworthy* through one
vocabulary: a `Status` (CALCULATED / BOUNDED / UNAVAILABLE / UNRESOLVED), a
`MetricContract` (what would tighten a figure, what would not, what it supports,
what caveats travel with it), and `--explain` blocks that reconstruct each metric's
chain from source. `performance` and `portfolio` predate that vocabulary and speak
their own dialect: ad-hoc `~` / `*` / `n/a` markers and free-text footnotes.

That dialect is not *wrong* — it already discloses staleness, extrapolation, and
unavailability — but it is a second home for the same idea. The governing invariant
(ADR-0012) is unchanged and still satisfied by both commands today:

> No analytical result may imply information that its provenance does not establish.

This ADR unifies *how* that disclosure is spoken, not *what* is disclosed.

## Decision

### 1. Provenance is opt-in on these two commands; the default table is untouched

Unlike `concentration` / `overlap`, where status is intrinsic to what the command
reports (an HHI *bound*, a `≥` *floor*), `performance` and `portfolio` headline a
clean point number that most runs want unadorned. So provenance disclosure is
**opt-in**, in two tiers:

- **`--show-status`** — adds one lightweight `Status` column to the table.
- **`--explain`** — the verbose per-metric block (Status + Result + Inputs + Method
  + Limited-by), and **implies status visibility** (`--explain` alone turns on the
  column; you never need both flags).

The **default output is byte-for-byte unchanged**: the existing `~` (stale close),
`*` (short-history extrapolation), `n/a`, and `⚠ excluded` markers and their
footnotes stay exactly as they are. Nothing disclosed today is hidden, and no
existing test output moves. `concentration` / `overlap` are **not** gated — their
status stays always-on, because it *is* the number's type there.

### 2. The `Status` column reports the row's load-bearing gate, not a per-cell tag

A `performance` row carries six metrics; a per-cell status tag would drown the
grid. The single column therefore reports the **valuation status** — the gate every
other figure on the row depends on:

- **CALCULATED** — the holding has a EUR market value (priced on, or carried
  forward to, the as-of date).
- **UNAVAILABLE** — no close/FX on or before the as-of date; the row shows `n/a`
  and is excluded from `TOTAL` (today's `⚠` case).

`BOUNDED` and `UNRESOLVED` never arise here — `performance` emits point values or
`n/a`, and has no cross-fund identity problem. The shared enum does not require a
command to use all four states. A stale-but-present value is **CALCULATED with a
limitation** (carried forward), not a distinct status — the `~` marker and the
contract's limitation line carry that caveat, matching the enum's meaning (a point
value exists). Likewise a short-history annualized figure is CALCULATED-with-`*`,
never a separate state.

`portfolio` holdings are derived exactly from stored transactions, so every row is
**CALCULATED**; the column is uniformly so by design. Its value is not
discrimination between rows but *consistency* — portfolio now states, in the same
word as every other command, that its numbers are complete, and `--explain` names
the one thing they are *not* built from (market data), which is exactly what
distinguishes portfolio's cost-basis `weight` from `performance`'s market value.

### 3. Metric contracts stay in the command modules; families, not cells

Per ADR-0013 decision 8, the *mechanism* lives in `common`; the contract
*instances* stay with the command that owns them. `performance` defines two, along
the two provenance families its metrics fall into:

- **`VALUATION_CONTRACT`** — MktVal, P&L, P&L%, P&L-share. Requires a close on/before
  the as-of date and (for a foreign-priced fund) an FX rate to EUR; does not require
  look-through or identity; limitation: a stale close is carried forward and flagged.
- **`RETURN_CONTRACT`** — XIRR, TWR, CAGR, Vol, MaxDD. Requires a dated contribution
  series and a terminal EUR value; limitations: annualized figures under a year of
  history are extrapolated (`*`), and XIRR/TWR are `n/a` without a sign change or
  ≥2 valuation points.

`portfolio` defines one — **`HOLDINGS_CONTRACT`** (average-cost accounting; requires
nothing beyond the stored transactions; explicitly does *not* require price/FX/
look-through; limitations: average-cost not FIFO/LIFO, weight is a share of cost
basis not market value, fund metadata shown only where the config carries it).

### 4. `--explain` reconstructs from the row, never from a log; granularity follows the data

Consistent with ADR-0012 decision 7, `--explain` recomputes each chain from the
in-memory row — it is always what the code did, never a persisted audit trail.

Granularity differs between the two commands *because their data does*:

- **`performance` explains per holding** (plus a `TOTAL` block). Holdings have
  genuinely heterogeneous provenance — one is estimated, another short-history,
  another unvaluable — so a block per holding carries real per-row information, the
  same way `concentration` explains per fund.
- **`portfolio` explains once.** Every holding shares one identical contract and
  status, so repeating the block per row would be noise; the single block states the
  holdings contract and reports metadata completeness across the set (e.g. "N of M
  holdings missing config metadata"). The per-holding numbers already sit in the
  table above.

## Rationale

- **One home for the disclosure idiom.** After this ADR every command speaks status
  and contract the same way; a reader learns the vocabulary once. This is the "one
  home per fact" convention applied to *how provenance is said*.
- **Opt-in where the headline is a clean number.** Widening the default table for
  everyone to serve the minority of runs that want the trust column is the wrong
  trade; `concentration`/`overlap` stay always-on because there the status is the
  number's type, not an annotation on it.
- **No new numbers, no moved output.** The retrofit is disclosure-only; the default
  tables are unchanged to the byte, so no behaviour and no existing test shifts.
- **Granularity honest to the data.** Per-holding where provenance varies row to row,
  once where it does not — the explain output never implies more variation than
  exists.

## Consequences

- `performance` gains `--show-status` and `--explain` (the latter implies the
  former); two `MetricContract` instances; a `Status` column and per-holding +
  `TOTAL` explain blocks. Default output byte-unchanged.
- `portfolio` gains `--show-status` and `--explain`; one `MetricContract` instance; a
  uniformly-CALCULATED `Status` column and a single explain block with metadata-
  completeness reporting. Default output byte-unchanged.
- Both import `Status`, `MetricContract`, and the `_explain_metric` / `_limited_by`
  helpers from `common` (within the `cli → command → common` layer contract,
  ADR-0003); no `common` change is needed — the mechanism already graduated in 0013.
- README command table and per-command `--help` epilogs gain the two flags; the
  coverage floor (90%) is held with tests for the column, the explain blocks, and
  the status/contract mapping.

## Deferred (not in this ADR)

- **`concentration` / `overlap` re-gating.** Their always-on status is deliberate
  (decision 1); no flag is added to them.
- **`validate`'s error/warning contract** (ADR-0009) is a different disclosure axis
  (config/DB integrity, not per-metric provenance) and is not folded in here.
- **`--explain --json`** and a persisted historical-claims trail — unchanged from
  ADR-0012/0013's Deferred sections; `--explain` stays reconstructed, human-readable.
- **A per-metric column** (one status tag per cell) — rejected in decision 2 as grid
  noise; the single valuation-gate column plus `--explain` covers the need.
