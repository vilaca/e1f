# ADR-0034 — `glossary` command: a checked-in metric glossary the CLI can read

**Scope:** a new stable `glossary` command that reads `data/glossary.md` — a
human-readable, checked-in glossary of every metric e1f reports — and either lists
all terms (grouped) or prints the entries matching a query (`e1f glossary TWR`,
`e1f glossary P&L`). Documentation feature only; it computes nothing and touches no
DB.

## Context

The analytics surface grew large — `performance` (default table, `--metrics`,
`--diff`, `--series`, `--contrib`), `benchmark`, `deposits`, and `correlation` now
report ~40 distinct metrics. Their definitions were scattered across argparse
epilogs, ADRs, and column headers, and several are easy to confuse (P&Lctr vs Ctr%
— money-weighted P&L share vs time-weighted return contribution; TWR vs XIRR;
Out% vs RelStr). There was no single place a user could ask "what is this column
and what is it good for."

Two ways to answer it were considered: (a) a shareable web/artifact reference, and
(b) a checked-in Markdown file the CLI can query. We chose (b) — it keeps the answer
inside the tool the metrics come from, versions with the code, and needs no network
or external host. It also fits the project's "one home per fact" rule: the prose
lives in exactly one file, read the same way whether opened in an editor or queried
from the terminal.

## Decisions

1. **The content is a Markdown file, `data/glossary.md`, checked in** (like
   `etf_universe.yaml` / `currency_metadata.yaml`, not gitignored like the DB). It
   resolves via `DEFAULT_GLOSSARY` against the repo root (same `_ROOT` convention as
   the other default paths), so it works from any cwd; `--file` overrides it.

2. **The file is the single source — the command only parses and renders it.** No
   metric definition is duplicated in Python. `## ` headings are groups, `### `
   headings are terms, and each term's body is the Markdown until the next heading;
   text before the first term (title + intro) is ignored. This keeps the file
   readable on its own *and* machine-queryable, with no second copy to drift.

3. **Lookup is a case-insensitive substring match on the term name, with a
   group/body fallback.** `e1f glossary P&L` fans out to `P&L€`, `P&L%`, `P&Lctr`
   because "p&l" is a substring of each; a name match short-circuits (so `TWR`
   returns only the TWR entry, not every body that mentions TWR). When nothing
   matches a name, group and body text are searched so a topical query
   (`e1f glossary drawdown`, `risk`) still finds something. No arguments lists every
   term, grouped.

4. **Named `glossary`, not `explain` or `metrics`.** `--explain` already means
   provenance (ADR-0014) on `performance`/`portfolio`/`correlation`, so reusing
   "explain" would collide; `glossary` says plainly what it is.

5. **Scope is the stable analytics metrics.** The experimental commands
   (`concentration`, `overlap`, `backtest`, `seasonality`, ADR-0024) have their own
   metrics and their own churn; the glossary states they are out of scope rather
   than documenting a moving target.

## Consequences

A new stable command joins the `cli` router and the top-level help/autocomplete
listing; it satisfies the layer contract (ADR-0003: `cli → glossary → common`, using
only `DEFAULT_GLOSSARY`). Keeping metric definitions in a checked-in file means the
glossary is now a place that can drift from the code — the same audit surface the
`/doc-check` skill already covers for README/CLAUDE.md/ADRs. When a metric's meaning
changes or a metric is added (e.g. the ADR-0033 additions), `data/glossary.md` is the
one file to update. The command is a faithful reader: it renders whatever the file
says, so correctness is a documentation concern, not a code one.
