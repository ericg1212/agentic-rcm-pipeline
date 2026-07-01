# ADR-003: Latency Model and LLM Trigger Gate

**Status:** Accepted  
**Date:** 2026-06-14 (Plan Mode architecture session)  
**Decider:** Eric Grynspan

---

## Decision

Two-tier latency model: (1) deterministic NCCI gate — sub-millisecond per claim; (2) LLM call — ~300–600ms (Claude Sonnet, async). Claims are gated: only the ambiguous slice (estimated 30–40% of claims) touches the LLM. The gate produces a normalized risk score; LLM is called only for claims exceeding the threshold (default: 0.30). End-to-end target: p99 < 2 seconds from claim ingestion to action decision.

## Why

The pre-submission window is the intervention point — a claim sitting in a clearinghouse queue for 10 seconds is still actionable. Seconds/near-real-time is the right latency class: fast enough to intervene before submission, honest about the LLM API call being in the hot path. Micro-batch (minutes) loses the pre-submission window. Sub-second real-time (Redpanda + cached LLM) is over-engineered at this claim volume.

The deterministic gate is the cost and latency control: NCCI resolves the confident majority in under a millisecond, and only the ambiguous slice reaches the LLM. At 35% dirty-claim rate and 70% of violations being clear NCCI hard-fails, the actual LLM-touch rate is approximately 10–15% of total claim volume.

## Rejected

| Alternative | Why rejected |
|---|---|
| **Call LLM on every claim** | Defensible if cost is secondary; not defensible at scale ($0.003/claim × 1M claims/month = $3K/month just for LLM). If every claim reaches the LLM anyway, the deterministic layer adds no routing value and the cost control disappears |
| **Micro-batch (Dagster scheduled run)** | Loses the pre-submission intervention window. A claim in a batch run at T+5 minutes may have already been submitted and adjudicated |
| **Pre-score with XGBoost, skip NCCI gate** | Right call at Phase 3 production scale (100K+ claims/day). Premature for v1: adds ML training infrastructure before the outcome store has labels to train on. The NCCI gate is a deterministic first-principles check; XGBoost is a learned approximation. Both have a role — NCCI catches what's definitively wrong, XGBoost triage would optimize LLM call routing at scale |

