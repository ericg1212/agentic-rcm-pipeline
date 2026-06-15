# ADR-002: Data Strategy — Real Distributions vs Public Row-Level Claims

**Status:** Accepted  
**Date:** 2026-06-14 (Plan Mode architecture session)  
**Decider:** Eric Grynspan

---

## Decision

Generate synthetic claim events by sampling from real 2024 CMS Medicare Physician & Other Practitioners (Provider Utilization) distributions. Denial logic = real NCCI PTP/MUE edits (2026 Q3). Denial codes = real X12/WPC CARC/RARC enum. Denial-rate baseline = real CMS Transparency in Coverage PUF rates.

## Why

No public CMS dataset contains claim-level denial codes (CARC/RARC). Those live in the 835 remittance transaction, which is never released with beneficiary data. Obtaining real adjudicated claim rows requires a CMS Data Use Agreement (DUA) — weeks of lead time, institutional affiliation required, and any data obtained could never live in a public GitHub repo.

The resolution: **realness lives in the policy and distributions, not the rows**. Every denial in P4 traces to a real NCCI edit or Medicare coverage determination. Charge distributions, code frequencies, and NPI samples come from the actual CMS 2024 Provider Utilization file (public, downloadable, no DUA). Denial rates are calibrated to real Transparency in Coverage PUF issuer denial rates. The only synthetic atom is composing individual claim rows from these real aggregate distributions — the same approach CMS itself uses for synthetic research files.

## Rejected

| Alternative | Why rejected |
|---|---|
| **DE-SynPUF (2008–2010)** | Stale (ICD-9, not ICD-10-CM), statistically perturbed to prevent re-identification (distributions deliberately distorted), no NCCI/LCD coverage for that era's edit set. Not defensible as "real" in an interview |
| **2023 CMS Synthetic RIF** | Generated using Synthea — explicitly violates the no-Synthea constraint (P2 and P3 already used Synthea; P4's differentiator is breaking from synthetic data). Using the CMS Synthetic RIF erodes the entire P2→P3→P4 portfolio arc narrative |
| **Real DUA-gated claim file** | Right answer for a production system; not feasible for a public portfolio project (weeks of lead time, institutional affiliation required, data can't be committed to GitHub). Would also prevent sharing or demonstrating the project |

## Interview Framing

"P4 doesn't invent denial rules — it scores against NCCI edits and Medicare coverage determinations, the same rule sets that adjudicate real Medicare claims, calibrated to real Transparency-in-Coverage denial rates. Beneficiary-level claims aren't public anywhere, so claim volume is generated from real CMS distributions. But every denial traces to real policy. That's the honest framing — and it's actually the stronger story than 'I downloaded a CSV,' because it means the system works against the underlying rules, not a static dataset."
