# ADR-0050 — `benchmark --scenario`: relative risk for a proposed book

**Scope:** extend `--scenario` to `benchmark`, so beta, R², tracking error,
information ratio, RelStr and Out% can be computed for the book a scenario
implies, against the same benchmark ETFs. No new statistics — the existing
estimators, applied to the ADR-0049 static-weight series.

## Context

`--scenario` reached `rebalance` (ADR-0016/0017) and `correlation` (ADR-0015), and
ADR-0049 brings it to `performance --metrics`. `benchmark` is the remaining
scenario-blind command, and it owns the statistics that answer the question users
actually ask about a tilt: *will this underperform the global market?*

That question is not answerable from absolute risk. A book can be less volatile
and still lag; what distinguishes a deliberate tilt from a closet tracker is the
pair (tracking error, beta). Worked from the current DB over ~2.1 years of weekly
returns against MSCI ACWI:

| Book | Beta | TE | Excess/yr |
| --- | ---: | ---: | ---: |
| held portfolio | 0.83 | 4.20% | −1.91% |
| `regional-tilt` | 0.77 | 5.66% | −0.78% |
| `factor-tilt` | 0.87 | 4.27% | −0.44% |
| a US-growth variant | 0.94 | 3.16% | −0.35% |

The diagnostic only appears when the rows sit together: the variants close the gap
to ACWI mostly by raising beta toward 1.0 and *lowering* tracking error — i.e. by
converging on the benchmark. A scenario at beta 0.94 / TE 3.2% is a multi-fund
reconstruction of an index the user can buy in one line at 0.07%, and no
absolute-risk report reveals that. Reaching it currently requires hand-rolled
regression scripts outside the tool.

## Decisions

### 1. One subject definition, imported from ADR-0049

The scenario book, its static-weight series, its common window and its binding
fund all come from `common/scenario_book.py` and `common/static_book.py`. This
command adds no second notion of "the book after a plan."

### 2. The estimators are unchanged; only the subject moves

Beta, R², TE, IR, RelStr and Out% are computed exactly as ADR-0041/0044/0045
define them, over each pair's shared window. A scenario run must be numerically
comparable to a held-book run, which it can only be if the estimator is identical.

### 3. The Book line names the scenario and its limits

`Book (scenario 'NAME', static weights, in-sample)` with the window and binding
fund. The `Out%` and `RelStr` columns keep their held-book meaning — table TWR
minus overlap book TWR — but that TWR is now the static-weight series', not a
realized return, and the header says so.

### 4. Status is BOUNDED, inherited

Same grounds as ADR-0049: static weights, in-sample window, shortest-constituent
bound, plus any ADR-0048 attenuation. A tracking error built from attenuated
covariances is itself understated — material here, because TE is the number that
distinguishes a real tilt from a closet tracker, and understating it makes a
scenario look *more* like an active bet than it is.

### 5. `--scenario` composes with `--against`, `--all` and `--chart`

The chart draws the scenario's cumulative-return line against each benchmark's,
which is the visual form of the same comparison.

## Implementation

`benchmark.py`: `--scenario` / `--scenarios-file`; the portfolio-series builder
branches on subject; the Book line and `--explain` block carry the scenario label,
window, binding fund, and `Status.BOUNDED`.

`data/glossary.md`: the `Where` contract on **Beta**, **R²**, **Tracking Error**,
**Information Ratio**, **RelStr** and **Out%** extends to `benchmark --scenario`.

## Invariance

A scenario reproducing today's weights gives beta/R²/TE/IR equal to `benchmark` on
the held book, to full precision — the same-fixture reconciliation test required
of a shared primitive, and the counterpart to ADR-0049's. Benchmarking a scenario
against a benchmark that is itself one of its constituents yields beta 1.00,
R² 1.00, TE 0.00% when that fund is the scenario's only weighted holding; pinned
as a degenerate-case test.
