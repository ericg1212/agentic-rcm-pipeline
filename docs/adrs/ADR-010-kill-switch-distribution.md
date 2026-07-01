# ADR-010: Kill-Switch State Distribution — Compacted Control Topic

**Status:** Accepted  
**Date:** 2026-07-01 (architecture review session)  
**Decider:** Eric Grynspan

---

## Decision

- Kill-switch state is distributed via a **compacted Kafka control topic** (`control.kill-switch`, single key) — the same hot-swap pattern already proven for NCCI rule editions on `rules.control`
- Every state change (`activate`/`deactivate`) publishes the new state; every `is_active` check polls the topic non-blocking and applies the latest published state before answering
- Each replica consumes with a **unique group.id from earliest**, so a late-joining replica bootstraps the current state from the compacted log without coordinating with peers
- Opt-in via `KILL_SWITCH_DISTRIBUTED=true`; default remains process-local (tests, single-process dev)

## Why

**The FCA "single lever" guarantee must survive horizontal scaling.** With process-local state, activating the switch stops one replica and leaves the others auto-correcting — the guarantee the compliance story rests on is silently false at replica count two. Distribution is not an optimization; it is what makes the claim true.

**Compaction is exactly the right primitive.** The switch is one current value under one key. Log compaction reduces the topic to precisely that, replays it to any new consumer automatically, and propagates changes to running replicas within seconds — no polling budget, no cache invalidation, no new infrastructure.

**Pattern reuse is a design argument, not a convenience.** The pipeline already hot-swaps NCCI editions through a compacted control topic. Using the same mechanism for the kill-switch means one control-plane pattern to reason about, test, and defend.

## Rejected

| Alternative | Why rejected |
|---|---|
| **Redis (SETNX / pub-sub)** | The textbook answer, and the right one in a shop that already runs Redis — but here it adds an entire infrastructure component, its own availability story, and a new failure mode to guard the pipeline against, for one boolean. |
| **Snowflake control table with TTL cache** | Uses the analytical warehouse as a control plane. Per-claim polling adds query latency to the hot path; a TTL cache reintroduces staleness — a kill-switch that takes a cache-expiry to engage is not a kill-switch. |
| **File-backed shared state** | Works only for co-located processes on one host; silently breaks the moment replicas span machines. Reads as a workaround, not a design. |
| **Keep process-local, document the caveat** | Leaves the flagship compliance claim false under the deployment shape the system is designed for. The one gap not acceptable to paper over with documentation. |
