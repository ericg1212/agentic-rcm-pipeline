# ADR-006: Rule Graph Storage — Snowflake + In-Memory Cache

**Status:** Accepted  
**Date:** 2026-06-29  
**Decider:** Eric Grynspan  
**Component:** Phase 2 — Payer Rule Intelligence Layer  

---

## Context

The Payer Rule Intelligence Layer needs to store and serve NCD/LCD coverage policies at claim-scoring time. The system must:

- Retrieve payer-specific coverage rules in <10ms per claim (sub-second scoring is the SLA)
- Ingest CMS Coverage API updates on different cadences (NCD: weekly; LCD: daily per MAC)
- Version rule changes for audit trail and FCA defensibility
- Be queryable by dbt for coverage policy change analytics

Three options were evaluated.

---

## Options

### Option A: Snowflake RAW.PAYER_RULES + In-Memory Cache (Chosen)

Rules are written to `RAW.PAYER_RULES` by Dagster ingestion ops. At startup, `PayerRuleGraph.load_from_seed()` reads all active rules into a nested in-memory dict `{hcpcs_code: {contractor_id: list[RuleEntry]}}`. Lookups are sub-millisecond. Dagster daily sensor triggers `reload()` to refresh the cache.

**dbt coverage:** `stg_payer_rules` + `fct_coverage_policy_changes` track first-seen, last-changed, and distinct rule states per (procedure, contractor) pair.

### Option B: Neo4j Graph Database

Store rules as a property graph (Procedure nodes → Rule edges → MAC nodes). Native graph traversal for NCD/LCD hierarchy.

### Option C: Static JSON Files Only

Extend the existing seed JSON files. No Snowflake table, no ingestion cadence.

---

## Decision

**Option A: Snowflake + In-Memory Cache.**

---

## Rationale

| Criterion | Snowflake + Cache | Neo4j | Static JSON |
|---|---|---|---|
| Lookup latency | Sub-ms (in-memory) | Network round-trip ~5ms | Sub-ms |
| Stack consistency | Already in stack | New infra, new ops | No Snowflake |
| dbt support | Full (stg + mart models) | None | None |
| Ingestion cadence | Dagster op per source type | Custom connector needed | Manual file update |
| Rule versioning / audit trail | Native (append-only table) | Native but isolated | None |
| Operational risk | Low | Medium (new infra, oncall) | Low but no audit trail |

Neo4j rejected: introduces new infrastructure with no operational precedent in this stack, and the relationship cardinality (procedure → MAC → rule) is simple enough that a flat table with a joining dict is equivalent. dbt cannot query Neo4j.

Static JSON rejected: no ingestion cadence, no delta tracking, no audit trail for FCA defensibility. The "rule changed on date X" question cannot be answered.

---

## Consequences

- `RAW.PAYER_RULES` schema is append-friendly (upsert on RULE_ID) — every ingested policy version is retained for audit.
- Cache reload is triggered by Dagster sensor, not by scoring requests — no per-claim Snowflake hit.
- Offline / CI tests use `load_from_seed()` with the seed JSON — no Snowflake connection required.
- `fct_coverage_policy_changes` mart answers: "which procedures changed coverage policy in the last 30 days?" — directly usable in clinical informatics reporting.
