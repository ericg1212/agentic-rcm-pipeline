# ADR-006: Rule Graph Storage — Snowflake + In-Memory Cache

**Status:** Accepted  
**Date:** 2026-06-29 (Phase 2 build — Payer Rule Intelligence Layer)  
**Decider:** Eric Grynspan

---

## Decision

Coverage rules live in Snowflake `RAW.PAYER_RULES` (written by Dagster ingestion ops; append-friendly upsert on RULE_ID so every policy version is retained) and are served at scoring time from an in-memory cache: `PayerRuleGraph` loads all active rules into a nested dict `{hcpcs_code: {contractor_id: list[RuleEntry]}}` at startup. A Dagster sensor triggers `reload()` on ingestion — no per-claim Snowflake hit.

## Why

The layer must serve payer-specific NCD/LCD rules in <10ms per claim while ingesting CMS Coverage API updates on two cadences (NCD weekly, LCD daily per MAC) and versioning every change for the audit trail. The split does each job with the right tool: Snowflake provides durable, versioned, dbt-queryable storage (`stg_payer_rules` + `fct_coverage_policy_changes` track first-seen, last-changed, and distinct rule states per procedure/contractor pair — "which procedures changed coverage policy in the last 30 days?" is a mart query); the in-memory dict provides sub-millisecond lookups on the hot path. The relationship cardinality (procedure → MAC → rule) is simple enough that a flat table plus a joining dict is fully equivalent to a graph store. Offline and CI runs load from seed JSON via `load_from_seed()` — no Snowflake connection required.

| Criterion | Snowflake + cache | Neo4j | Static JSON |
|---|---|---|---|
| Lookup latency | Sub-ms (in-memory) | ~5ms network round-trip | Sub-ms |
| Stack consistency | Already in stack | New infra, new ops | No warehouse layer |
| dbt support | Full (stg + mart models) | None | None |
| Ingestion cadence | Dagster op per source type | Custom connector needed | Manual file update |
| Versioning / audit trail | Native (append-only table) | Native but isolated | None |

## Rejected

| Alternative | Why rejected |
|---|---|
| **Neo4j graph database** | New infrastructure with no operational precedent in this stack; dbt cannot query it; and the procedure → MAC → rule cardinality is simple enough that a flat table with an in-memory dict is equivalent — graph traversal buys nothing here |
| **Static JSON files only** | No ingestion cadence, no delta tracking, no audit trail — "when did this rule change?" cannot be answered, which fails the FCA documentation requirement |
