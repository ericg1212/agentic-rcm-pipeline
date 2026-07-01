# ADR-002: Data Strategy — Real Distributions vs Public Row-Level Claims

**Status:** Accepted  
**Date:** 2026-06-14 (Plan Mode architecture session)  
**Decider:** Eric Grynspan

---

## Decision

Generate synthetic claim events by sampling from real 2024 CMS Medicare Physician & Other Practitioners (Provider Utilization) distributions. Denial logic = real NCCI PTP/MUE edits (2026 Q3). Denial codes = real X12/WPC CARC/RARC enum. Denial-rate baseline = real CMS Transparency in Coverage PUF rates.

## Why

No public CMS dataset contains claim-level denial codes (CARC/RARC). Those live in the 835 remittance transaction, which is never released with beneficiary data. Obtaining real adjudicated claim rows requires a CMS Data Use Agreement (DUA) — weeks of lead time, institutional affiliation required, and any data obtained could never live in a public GitHub repo.

The resolution: **realness lives in the policy and distributions, not the rows**. Every denial in the pipeline traces to a real NCCI edit or Medicare coverage determination. Charge distributions, code frequencies, and NPI samples come from the actual CMS 2024 Provider Utilization file (public, downloadable, no DUA). Denial rates are calibrated to real Transparency in Coverage PUF issuer denial rates. The only synthetic atom is composing individual claim rows from these real aggregate distributions — the same approach CMS itself uses for synthetic research files.

## Rejected

| Alternative | Why rejected |
|---|---|
| **DE-SynPUF (2008–2010)** | Stale (ICD-9, not ICD-10-CM), statistically perturbed to prevent re-identification (distributions deliberately distorted), no NCCI/LCD coverage for that era's edit set — cannot be characterized as current, real-world utilization |
| **2023 CMS Synthetic RIF** | Generated with Synthea — synthetic patient trajectories rather than real utilization distributions, inheriting Synthea's known distributional artifacts. Conflicts with the core requirement that realness live in current policy and distributions |
| **Real DUA-gated claim file** | Right answer for a production system; not feasible for a public repository (weeks of lead time, institutional affiliation required, and the data could never be committed or demonstrated publicly) |

