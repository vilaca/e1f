# ADR-0014 — provenance generalization across commands

**Status: PLACEHOLDER — not yet decided.**

Reserves the 0014 slot (CLAUDE.md: one ADR per decision, no gaps in numbering) for
the work ADR-0012 decision 7 and ADR-0013 decision 8 forward-reference as "a
future ADR (0014+)": retrofitting the existing commands to speak the provenance
vocabulary — the four-state `Status`, `MetricContract`, and the `--explain`
rendering helpers — that ADR-0013 graduated into `common` as a *mechanism* without
generalizing its *use*.

## Intended scope (to be filled in when this ADR is written)

- Retrofit `performance` and `portfolio` to report metrics through the `Status` /
  `MetricContract` / `--explain` model, so every command discloses provenance the
  same way `concentration` and `overlap` already do.
- Decide whether `--show-status` is opt-in or default per command.

Until then this file is a stub; no decision here is binding.
