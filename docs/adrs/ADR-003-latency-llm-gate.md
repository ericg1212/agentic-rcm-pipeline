# ADR-003: Latency Model and LLM Trigger Gate

**Status:** Accepted  
**Date:** 2026-06-14 (Plan Mode architecture session)  
**Decider:** Eric Grynspan

---

## Decision

Two-tier latency model: (1) deterministic NCCI gate — sub-millisecond per claim; (2) LLM call — ~300–600ms (Claude Sonnet, async). Claims are gated: only the ambiguous slice (estimated 30–40% of claims) touches the LLM. The gate produces a normalized risk score; LLM is called only for claims exceeding the threshold (default: 0.30). End-to-end target: p99 < 2 seconds from claim ingestion to action decision.

## Why

The pre-submission window is the intervention point — a claim sitting in a clearinghouse queue for 10 seconds is still actionable. Seconds/near-real-time is the right latency class: fast enough to intervene before submission, honest about the LLM API call being in the hot path. Micro-batch (minutes) loses the pre-submission window. Sub-second real-time (Redpanda + cached LLM) is over-engineered at P4 claim volume.

The deterministic gate is the cost and latency defense: "I don't call the LLM on every claim. NCCI catches the confident majority in under a millisecond. The LLM only sees the ambiguous slice — that's both the cost control and the ROI story." At 35% dirty-claim rate and 70% of violations being clear NCCI hard-fails, the actual LLM-touch rate is approximately 10–15% of total claim volume.

## Rejected

| Alternative | Why rejected |
|---|---|
| **Call LLM on every claim** | Defensible if cost is secondary; not defensible at scale ($0.003/claim × 1M claims/month = $3K/month just for LLM). Removes the gate as a cost-control story and weakens the "why not just rules" justification — if you're calling the LLM anyway, the gate has no value |
| **Micro-batch (Dagster scheduled run)** | Loses the pre-submission intervention window. A claim in a batch run at T+5 minutes may have already been submitted and adjudicated |
| **Pre-score with XGBoost, skip NCCI gate** | Right call at Phase 3 production scale (100K+ claims/day). Premature for MVP: adds ML training infrastructure without interview story clarity. The NCCI gate is a deterministic first-principles check; XGBoost is a learned approximation. Both have a role — NCCI catches what's definitively wrong, XGBoost triage would optimize LLM call routing at scale |

## Interview L1/L2/L3

**L1:** "Every claim runs through a deterministic NCCI check in under a millisecond. Only the ambiguous ones — maybe 15% of volume — ever call the LLM. That's the cost and latency defense."  
**L2:** "The gate produces three routes: PASS (clear, no action), HARD_FAIL (deterministic violation, no modifier bypass), and AMBIGUOUS (modifier present, modifier_indicator=1, LLM must verify clinical appropriateness). PASS and HARD_FAIL never hit the LLM API. Only AMBIGUOUS — and HARD_FAIL on high-dollar claims that need rationale — do."  
**L3:** "At production scale I'd replace the AMBIGUOUS routing with an XGBoost binary triage trained on historical gate+LLM outcomes. XGBoost handles the volume cheaply; LLM is called only on medium/high XGBoost-scored claims. That's Phase 3. For v1, the NCCI gate alone reduces LLM calls by 85% and is fully defensible."
