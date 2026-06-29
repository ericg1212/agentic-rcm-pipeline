# ADR-007: PA Pre-Check Integration — Extend ScoringResult

**Status:** Accepted  
**Date:** 2026-06-29  
**Decider:** Eric Grynspan  
**Component:** Phase 2 — Prior Authorization Pre-Check Module  

---

## Context

The system needs to surface prior authorization (PA) risk before a claim is adjudicated. PA denials (CARC CO-197) are the second-highest denial category in the RCM pipeline. Two integration approaches were evaluated.

---

## Options

### Option A: Extend ScoringResult + Same Claude Tool Loop (Chosen)

Add three fields to `ScoringResult`:
```python
pa_required: bool | None
pa_approval_likelihood: float | None
pa_criteria_met: list[str]
```

Add `check_prior_auth_required` as a new tool in `ToolRegistry`. Claude calls this tool in the same inference pass that calls `get_payer_rules`, `get_carc_codes`, etc. — one API call returns the complete risk picture including PA status.

Add a PA-triggered escalation branch in `router.py`:
```python
if scoring_result.pa_required and scoring_result.pa_approval_likelihood < PA_ESCALATE_THRESHOLD:
    return self._escalate(claim, scoring_result, gate_decision,
                          reason="PA_APPROVAL_RISK",
                          governing_rule="CMS-0057-F")
```

### Option B: Separate pa_scorer.py

A second Claude API call dedicated to PA evaluation, running in parallel or serial with the primary scoring call.

---

## Decision

**Option A: Extend ScoringResult in the same tool loop.**

---

## Rationale

PA criteria live in `RAW.PAYER_RULES` (populated from `seed_lcd.json`) — the same policy documents the existing `get_payer_rules` tool already retrieves. A separate PA scorer would make a second round-trip to retrieve the same policy data.

| Criterion | Extend ScoringResult | Separate pa_scorer.py |
|---|---|---|
| API calls per claim | 1 | 2 |
| Policy data retrieval | Once | Twice (same data) |
| Interview frame | "Unified intelligence call" | "Two scorers per claim" |
| Billing staff output | One coherent result | Two results to reconcile |
| Code surface area | Minimal extension | New class + orchestration |

The PA approval likelihood is derived from the same policy context already in scope — no new information source is needed. Extending the ScoringResult keeps the reasoning atomic: one Claude call, one risk picture, one routing decision.

---

## Consequences

- `ScoringResult.to_snowflake_row()` must include the 3 new PA fields.
- `PAConfig` defines `PA_ESCALATE_THRESHOLD = 0.70` and CMS-0057-F turnaround times (72hr urgent, 7-day standard, effective Jan 2027).
- Gold-carding: `gold_card_eligible` flag per procedure in seed data. When `gold_card_exempt=True` for a provider NPI, the PA check is bypassed — escalation threshold does not apply.
- CO-197 is the canonical denial code for "precertification/authorization absent." The routing escalation reason `PA_APPROVAL_RISK` maps to this in the billing queue.
