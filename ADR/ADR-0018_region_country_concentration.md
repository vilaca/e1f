# ADR-0018 — portfolio-level region/country concentration (country HHI)

**Scope:** `concentration` command, cross-fund portfolio-level country/region exposure

## Context

`concentration` (ADR-0012) reports **within-fund** HHI only (security top-10,
sector, asset class); `overlap` (ADR-0013) gives cross-fund single-name **€
floors**. Neither answers "how concentrated is the *whole book* by country/region."
That dimension was deferred — not dropped — in ADR-0012/0013 with an explicit
mandate: *"Region / country concentration — deferred, not dropped, must never be
gated on."* It is the slot `concentration.py` currently reports as
`region_unavailable_v1` (the `REGION_CONTRACT`).

This ADR graduates that deferred item, exactly as `overlap` (ADR-0013) graduated
from its own deferral in ADR-0012.

**Governing invariant, carried unweakened (ADR-0012):**

> No analytical result may imply information that its provenance does not establish.

Region/country adds one honesty problem the family hasn't faced: the data source
covers only *some* funds, so coverage is partial at the **fund** level, not (as
with security top-10) at the *within-fund* level. The design makes that fund-level
gap the visible shape of the number, never a silent denominator.

### What exists today

- No portfolio-level HHI anywhere; `concentration` is within-fund only
  (`concentration.py`, title "Within-fund concentration").
- The region line is already an independent `UNAVAILABLE` row — the other
  dimensions are **not** gated on it. This design preserves that.
- Look-through snapshots (`holdings_snapshot` / `holding`, ADR-0012) store
  `security` / `sector` / `asset_class`. **No country row.**
- No free automated country source: yfinance `funds_data` has no country
  weighting; justETF's country chart is not in the `tl_etf-basics` table the
  scraper reads. Country data exists only via per-issuer holdings scrapes
  (Amundi/Xtrackers/SPDR) — infra deliberately deferred in concentration v1.

## Decision

### 1. Headline = portfolio-level country HHI

For covered funds `f` with EUR value `Vf` and a *complete* country weight vector
`w_{c,f}` (Σ_c w_{c,f} = 1 within a fund):

```
Exposure_c  = Σ_{f ∈ F_covered}  Vf · w_{c,f}       (EUR held in country c)
p_c         = Exposure_c / Σ_{f ∈ F_covered} Vf     (portfolio country weight)
HHI_country = Σ_c  p_c²                              (headline)
```

This is honest as an **exact** aggregate — *not* a floor — because issuer country
weights are complete per fund (they sum to 100%), unlike security top-10. The
number is exact *over the covered sub-portfolio*; the only qualifier is fund-level
coverage (decision 3). Reuses the EUR valuation core in `common` (`fund_eur_value`,
ADR-0013 decision 4). Also reported: top-N country exposures, effective number of
countries (`1/HHI`), and the region rollup (decision 5).

Status mapping (four-state `Status`):

- 100% fund coverage → **CALCULATED** exact portfolio country HHI.
- <100% fund coverage → **CALCULATED** over the covered set + disclosed coverage %
  (headline reads `… over N of M funds · country coverage X%`).
- No covered funds → **UNAVAILABLE** (the current state, until a source is wired).

### 2. Data source — manual pinned country sidecar first; issuer scrape later

Source is a reviewed, pinned per-fund country-weight table (ADR-0002-style sidecar,
`data/region_metadata.yaml`), human-entered/verified. Rationale:

- **Reviewed, not inferred** — same ethos as overlap's `canonical_key`; a human
  asserts the country vector, no fragile scraper writes identity.
- **Zero new scraping infra** — no per-issuer Akamai/JS walls; ships now.
- **Model-invariant upgrade path** — a future per-issuer auto-scraper (Amundi
  JSON, Xtrackers CSV, SPDR XLSX) swaps the *source* without touching the metric,
  exactly how `canonical_key` was designed to accept ISINs later. The sidecar then
  becomes the pin/override layer.

Rejected for v1: per-security top-10 domicile (only top-10, needs
name→ticker→country, coverage-bounded, meaningless for bond/multi-asset funds);
building per-issuer scrapers now (real value but multi-week + ongoing maintenance,
not required to ship an honest metric — its own follow-up).

### 3. Coverage model — fund-level partial, disclosed, never gated

```
fund has reviewed country vector? ──NO──▶ excluded + disclosed (not in HHI base)
     │
    YES ──▶ fund valued (Vf via fund_eur_value)? ──NO──▶ excluded + disclosed
             │
            YES ──▶ contributes Vf·w to numerator AND Vf to denominator
```

Two eligibility gates, single consistent basis (mirrors ADR-0013 decision 6):
`HHI_country` and every `p_c` share the **same covered-and-valued fund set**.
Uncovered/unvalued funds are named in the disclosure, never silently dropped into
the denominator and never blocking the other concentration dimensions (the "never
gated on" mandate stays structurally true).

### 4. Swap/synthetic funds — refused, not collateral-substituted

Synthetic/swap funds disclose *collateral*, not index exposure. Their country
vector is **never** inferred from collateral — already the recorded
`REGION_CONTRACT` refusal ("never inferred from swap collateral"). A swap fund with
no reviewed *index* country vector is simply uncovered (decision 3), not filled
from collateral.

### 5. Country → region rollup via a reviewed mapping

Country is the raw source dimension; region (North America / Developed Europe /
Developed Asia-Pacific / Emerging Markets / …) is a **derived rollup** through a
fixed, reviewed country→region table (asserted, not inferred — like
`canonical_key`). Report both a country HHI (headline) and a region rollup line.
**A fixed default country→region table ships with e1f**, overridable per entry in
the sidecar; disputed classifications (e.g. Korea DM/EM) are a recorded review
decision, not a heuristic.

### 6. Command home — `concentration` cross-fund section

Portfolio-level country HHI is **cross-fund**, so it is printed once as a new
portfolio-level section in `concentration`'s cross-fund mode, below the per-fund
blocks — not folded into `overlap`. Rationale: the metric is HHI-shaped
(concentration's vocabulary), not single-name-€-floor-shaped (overlap's). Keeps
one command for all concentration. `--explain` reconstructs the chain per country:
each covered fund's `Vf × w_{c,f}`, excluded funds with reasons, the covered
denominator, and both `as_of` dates (snapshot vs valuation), consistent with
ADR-0012/0013 `--explain`.

### 7. Provenance integration

Reuses the graduated `common` vocabulary (`Status`, `MetricContract`,
`_explain_metric`, ADR-0014). `REGION_CONTRACT` is upgraded from
`region_unavailable_v1` to `country_hhi_v1`:
`requires=("reviewed per-fund country weights",)`,
`does_not_require=("swap collateral (refused)", "security identity")`,
`supports=("portfolio country HHI", "region rollup")`,
`limitations=("covers only reviewed funds; weights are pinned snapshots, not live",)`.

### 8. Sidecar granularity & schema

The sidecar holds a **full country vector per fund** (region is derived via
decision 5, not stored) — richer HHI, single source of truth for both lines.
Sidecar-first means **no snapshot schema change** for v1: country vectors live in
`data/region_metadata.yaml`, read at report time. If/when the issuer scraper
lands, a `country` dimension is added to `holding` — the frozen 3-table design
already accepts a new `dimension` value with zero DDL.

## Rationale

- **Honest by construction** — fund-level coverage is the visible shape of the
  number (`over N of M funds`), never a silent denominator; upholds the ADR-0012
  governing invariant for a new kind of partiality.
- **Exact, not a floor** — complete per-fund country weights make the aggregate
  exact over the covered set, unlike overlap's top-10 floors.
- **Reviewed over inferred** — a human asserts the country vector, matching the
  `canonical_key` (ADR-0013) and pinned-resolution (ADR-0002) ethos.
- **Model-invariant source** — a later auto-scraper swaps the source beneath an
  unchanged metric and sidecar-as-override layer.
- **Never gated on** — the "must never be gated on" mandate from ADR-0012/0013 is
  preserved structurally: uncovered funds never block the other dimensions.

## Consequences

- **Not yet implemented** — `REGION_CONTRACT` is still `region_unavailable_v1`
  in `src/e1f/experimental/concentration.py`; `data/region_metadata.yaml` is
  not in the tree. The bullets below are the design when it lands.
- New reviewed sidecar `data/region_metadata.yaml` (country vectors + optional
  region-mapping overrides); a fixed default country→region table ships in code.
- `REGION_CONTRACT` graduates `region_unavailable_v1` → `country_hhi_v1`; the row
  stops being permanently `UNAVAILABLE` once any fund is covered.
- `concentration` gains a portfolio-level cross-fund section and its `--explain`
  chain; per-fund output is unchanged.
- No DDL: v1 reads the sidecar at report time; the `holding` `country` dimension
  waits for the scraper.

## Deferred (not in this ADR)

- **Per-issuer country auto-scraper** (Amundi/Xtrackers/SPDR) — the model-invariant
  source upgrade; its own follow-up.
- **`country` dimension in `holding`** — only when the scraper lands.
- **Per-security domicile look-through** — rejected (decision 2).
- **Historical country drift / trail** — snapshot-only, like the rest.
