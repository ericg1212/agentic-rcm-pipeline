# ADR-006: Payer Rule Intelligence — Snowflake + In-Memory Cache, PA Pre-Check in the Tool Loop

**Status:** Accepted
**Date:** 2026-06-29 · consolidated 2026-07-06
**Decider:** Eric Grynspan
**Consolidates:** ADR-006 (rule graph storage) + ADR-007 (PA pre-check integration)

---

## Decision

- Coverage rules live in Snowflake `RAW.PAYER_RULES` (versioned upserts on RULE_ID — every policy version retained; Dagster ingestion: LCD daily per MAC, NCD weekly) and are served at scoring time from `PayerRuleGraph`, an in-memory nested dict loaded at startup and reloaded by a Dagster sensor — sub-10ms retrieval, no per-claim Snowflake hit. Offline and CI runs load from seed JSON.
- Prior authorization risk is evaluated **inside the existing scoring tool loop**, not by a second scorer: `ScoringResult` gains `pa_required` / `pa_approval_likelihood` / `pa_criteria_met`, `check_prior_auth_required` joins the `ToolRegistry`, and the router escalates PA-flagged claims below `PA_ESCALATE_THRESHOLD` (0.70) with `governing_rule="CMS-0057-F"`. Gold-carded providers bypass the check.

## Why

**Each job gets the right tool.** Snowflake provides durable, versioned, dbt-queryable storage — "which procedures changed coverage policy in the last 30 days?" is a mart query (`fct_coverage_policy_changes`), and the append-only history is the FCA audit trail. The in-memory dict serves the hot path: the procedure → MAC → rule cardinality is simple enough that a flat table plus a dict is fully equivalent to a graph store.

**PA criteria live in the same policy documents the loop already retrieves.** A separate PA scorer would make a second API round-trip to fetch the same data and hand the billing queue two results to reconcile. Extending the tool loop keeps reasoning atomic: one LLM call, one complete risk picture, one routing decision.

## Rejected

| Alternative | Why rejected |
|---|---|
| **Neo4j graph database** | New infrastructure dbt can't query; graph traversal buys nothing at this cardinality |
| **Static JSON files only** | No ingestion cadence, no delta tracking — "when did this rule change?" cannot be answered, failing the FCA documentation requirement |
| **Separate `pa_scorer.py` (second LLM call)** | Doubles API cost and latency to retrieve policy data already in scope; two risk assessments to reconcile for no additional information |
