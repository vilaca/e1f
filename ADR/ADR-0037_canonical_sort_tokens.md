# ADR-0037 — Canonical `--sort` tokens across table commands

**Scope:** the `--sort` / `--reverse` flags on every stable command that prints
a reorderable row table (`portfolio`, `performance`, `deposits`, `benchmark`,
`rebalance`, `transactions list`, `config list`). Does not change any metric
definition, valuation, or default row order except where a previous `--sort`
token is renamed to the canonical one.

## Context

Three commands already took `--sort`, but the tokens disagreed for the same
quantity (`portfolio --sort total` vs `performance --sort cost` vs
`deposits --sort amount` for cost basis; `deposits --sort gain` vs
`performance --sort pnl` for unrealized P&L) and several displayed columns
were not sortable at all (`portfolio` `Value€`, `performance` TWR/Vol/MaxDD/CAGR,
`deposits` `%P&L`). Table commands added later (`benchmark`) and ones whose
sort was deferred (`rebalance`, ADR-0016) had no flag. Completing `--sort
value` on `performance` and then guessing `total` or `gain` on the next
command was the prompt for a single vocabulary.

## Decisions

**One token per quantity, shared wherever that quantity appears.** Tokens are
CLI identifiers (lowercase, no `€`/`%`/`Δ`), not copies of the display header.
A command exposes only the columns it has; overlapping columns use the same
token. The contract is pinned by `tests/test_contracts.py`, not a shared
`SORT_FIELDS` constant — each command's argparse stays local (ADR-0003).

| Token | Quantity | Headers it sorts |
|---|---|---|
| `isin` | ISIN | ISIN / Symbol (transactions) |
| `name` | Fund name | Name / Fund / Benchmark |
| `date` | Calendar date | Date / Datetime |
| `broker` | Broker | Brkr / Broker |
| `value` | EUR market value | MktVal€ / Value€ / Current€ |
| `cost` | EUR cost basis / contribution | Cost€ / Total / Amount€ |
| `pnl` | Unrealized P&L | P&L€ / Gain€ |
| `pnl_pct` | P&L as % of cost | P&L% / Ret% |
| `pnl_ctr` | Share of book P&L | P&Lctr / %P&L |
| `weight` | Row weight | Weight / Cur% |
| `twr` / `xirr` / `cagr` / `vol` / `maxdd` | Return/risk | TWR / XIRR / CAGR / Vol / MaxDD |
| `ctr` | Cariño return contribution | Ctr% (`--contrib` only) |
| `ter` / `fee_yr` | Fee | TER / Fee/yr / Fee€/yr |
| `units` | Share count | Units / Shares |
| `class` | Asset class | Class / Asset class |

Command-specific tokens (`avg`, `last_px`, `ccy`, `dist`, `tgt`, `buy`,
`final`, `n`, `beta`, `r2`, `te`, `ir`, `relstr`, `out`, `side`, `price`,
`fee`, `tax`, `ticker`, `exchange`) stay local — they name a column that
command alone prints.

**Renames (no aliases).** `portfolio total` → `cost`; `deposits amount` →
`cost`; `deposits gain` → `pnl`; `deposits ret` → `pnl_pct`. Old tokens are
removed so argparse `choices` is the whole vocabulary.

**Every displayed data column is sortable** on commands that already had
`--sort`, plus `--contrib` gains `twr` / `weight` / `ctr`. Missing numerics
sort as −∞ (bottom under `--reverse`, matching `performance` today).
`--series` stays date-ordered and `--sort` stays inert there (ADR-0030).

**`--diff` sorts identity + money deltas** (`isin`, `name`, `value`, `cost`,
`pnl`). Other tokens accepted by the shared `performance` choices fall back
to `value` (the previous behaviour for `xirr`).

**New `--sort` on the remaining reorderable tables.** Defaults preserve
today's order: `benchmark` and `rebalance` default to *no* column sort
(listed-order / binder-first per ADR-0016) and `--sort` is opt-in;
`transactions list` defaults to `date`; `config list` defaults to `isin`
(`ConfigManager.list` already isin-sorts). `--reverse` without `--sort`
reverses that default order.

**Not in scope.** `correlation` (order is the cluster/flag ranking);
experimental commands; `scenario show` (tiny CRUD table); `glossary`.
Transactions `price`/`fee` are not `last_px`/`fee_yr` — trade price and
trade fee are not last close or annual TER bill. `ctr` is not `pnl_ctr`
(Cariño TWR share vs euro P&L share; the glossary already keeps them
apart). `weight` is the same token for cost-basis `portfolio` Weight and
market-value `--contrib` / rebalance Cur% — the header is Weight/Cur%, the
definition difference already lives in the glossary.

## Rationale

A user who sorts `performance` by market value should type `--sort value` on
`portfolio` and `deposits` too. Matching display glyphs (`MktVal€`, `P&L€`)
would make tokens untypeable; keeping per-command nicknames (`total`,
`gain`, `amount`) was the bug this ADR removes. Aliases would leave two
names for one column, which is what we are unifying. A shared constant in
`common` would pull argparse vocabulary into the primitives package for no
runtime sharing — a test that the overlapping tokens agree is the cheaper
contract.

Rebalance's binder-first default is load-bearing (ADR-0016 decision 9), so
it stays until the caller asks for a column. Correlation's sort *is* the
analysis; exposing `--sort` there would fight the ranking.

## Consequences

`--sort total` / `--sort gain` / `--sort amount` / `--sort ret` stop working.
Completions and examples move to the canonical tokens. `benchmark`,
`rebalance`, `transactions list`, and `config list` gain `--sort` /
`--reverse`. ADR-0016's " `--sort` deferred" note is superseded for
rebalance; the binder-first default is unchanged.
