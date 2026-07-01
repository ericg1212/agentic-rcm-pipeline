# ADR-007: PA Pre-Check Integration — Extend ScoringResult

**Status:** Accepted  
**Date:** 2026-06-29 (Phase 2 build — Prior Authorization Pre-Check Module)  
**Decider:** Eric Grynspan

---

## Decision

Prior authorization risk is evaluated inside the existing scoring tool loop, not by a second scorer. `ScoringResult` gains three fields (`pa_required`, `pa_approval_likelihood`, `pa_criteria_met`), `check_prior_auth_required` joins the `ToolRegistry`, and the router gains a PA escalation branch: PA required + approval likelihood below `PA_ESCALATE_THRESHOLD` (0.70) escalates with `governing_rule="CMS-0057-F"`.

## Why

PA denials (CARC CO-197, "precertification/authorization absent") are the second-largest denial category, and the criteria that decide them live in `RAW.PAYER_RULES` — the same policy documents the existing `get_payer_rules` tool already retrieves. A separate PA scorer would make a second API round-trip to fetch the same data. Extending the tool loop keeps the reasoning atomic: one LLM call, one complete risk picture, one routing decision, one coherent output for the billing queue.

| Criterion | Extend ScoringResult | Separate pa_scorer.py |
|---|---|---|
| API calls per claim | 1 | 2 |
| Policy data retrieval | Once | Twice (same data) |
| Billing staff output | One coherent result | Two results to reconcile |
| Code surface area | Minimal extension | New class + orchestration |

Downstream requirements this creates: `to_snowflake_row()` carries the three PA fields; `PAConfig` holds the threshold and CMS-0057-F turnaround times (72-hour urgent / 7-day standard, effective Jan 2027); gold-carded providers (`gold_card_exempt=True` per NPI) bypass the PA check entirely.

## Rejected

| Alternative | Why rejected |
|---|---|
| **Separate pa_scorer.py (second LLM call)** | Doubles API cost and latency to retrieve policy data already in scope; produces two risk assessments the billing queue has to reconcile; adds a new class and orchestration path for no additional information |
