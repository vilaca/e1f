# ADR-0002 — Pinned ftgo resolution via currency_metadata.yaml sidecar

**Scope:** `fetch` command, ftgo ISIN-to-XID resolution

## Context

ftgo resolves an ISIN to a security via a search (`get_xid`). The search returns
multiple listings (different exchanges, currencies) and the ordering can change as
FT Markets updates its index. Without pinning, the same ISIN could resolve to a
different listing on different runs, producing prices in different currencies and
breaking any downstream analysis that assumes a stable series per ISIN.

## Decision

The first successful resolution of each ISIN is pinned to a YAML sidecar file
(`data/currency_metadata.yaml`). Subsequent fetches read the pinned `xid`,
`symbol`, and `currency` from the sidecar rather than re-querying ftgo. The
sidecar is committed so the resolution is shared across machines and is stable
across CI runs.

All readers and writers cross one typed `CurrencyMetadata` boundary. Fund pins
and the reserved `fx_pairs` map are separate collections in memory, even though
the backward-compatible YAML wire format remains flat. Unknown top-level keys
cannot be distinguished from legacy fund keys in that format, but malformed
fund/FX values fail closed instead of flowing into command-specific casts.
Writes use a fully flushed temporary file followed by atomic replacement.

Resolution logic prefers the listing quoted in the fund's own share-class currency
(parsed from the fund name, e.g. `"iShares … USD (Acc)"` → `USD`), falling back
to the first match when the currency cannot be determined.

## Rationale

- **Stability** — price series are consistent across time and machines; no silent
  currency switch mid-history.
- **Auditability** — the sidecar is a human-readable record of exactly which
  listing is being tracked for each ISIN.
- **Performance** — avoids a network round-trip on every incremental fetch once
  an ISIN is pinned.

## Consequences

- Adding a new ISIN triggers one `get_xid` call; all subsequent fetches are
  network-free for resolution.
- The sidecar must be kept in sync with the config: `e1f config remove` and
  `e1f config trim` both clean up the sidecar alongside the config YAML.
- To re-resolve an ISIN (e.g. if the chosen listing is wrong), delete its entry
  from `data/currency_metadata.yaml` and run `e1f fetch` again.
