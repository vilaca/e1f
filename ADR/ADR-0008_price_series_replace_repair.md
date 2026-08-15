# ADR-0008 — Destructive series replace for price repair

**Scope:** `e1f fetch --replace` / `--allow-shrink`, the `prices` table

## Context

`fetch` is incremental by design: the default upsert keeps stored closes and only
adds missing dates, and `--force` overwrites matching dates but never deletes rows
absent from the new response (ADR-0001). Neither can remove rows that no longer
exist upstream — e.g. after a bad prior fetch wrote spurious or misdated closes,
or a share class was re-pinned (ADR-0002). Repairing such an ISIN previously meant
editing SQLite by hand.

## Decision

`e1f fetch <ISIN> --replace` repairs one series by deleting its stored rows and
re-inserting the freshly fetched range, in a single transaction, only after a
non-empty fetch. It requires an explicit ISIN and is mutually exclusive with
`--force` (upsert and delete-then-insert are different write strategies).

Because a truncated upstream response is indistinguishable from a legitimately
shorter series, `--replace` **refuses to drop any stored date**: the fetched
series may add or overwrite dates, but by default must be a superset of what is
stored. A missing stored date — whether the range is shorter, the window is
narrower, or there is an interior hole — aborts before the delete and leaves the
data untouched. `--allow-shrink` overrides this when the caller has confirmed the
shorter series is correct.

The requirement that `--replace` name a single ISIN is enforced in `fetch()`
itself, not only in the CLI, so a library caller cannot replace the whole
universe with a bare `fetch()`.

## Rationale

- **Delete-then-insert, not upsert** — only a full replace can drop stale rows;
  `--force` cannot.
- **Fail closed on shrink** — the failure mode that matters is silently wiping
  years of good history on a partial response. Refusing to shrink turns that
  silent data loss into a loud, recoverable error; the override keeps genuine
  shrinks possible.
- **Single ISIN only** — repair is targeted; a mistake touches one series, and the
  guard bounds the blast radius further.

## Consequences

- A legitimate shrink or re-dating (upstream really dropped a stored date) needs
  `--allow-shrink`.
- The transaction is per-ISIN; a crash mid-run leaves other ISINs untouched.
- Total fetch failure still raises `No data fetched` before any delete, so a dead
  upstream can never empty a stored series.
