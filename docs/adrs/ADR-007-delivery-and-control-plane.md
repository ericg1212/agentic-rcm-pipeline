# ADR-007: Delivery Semantics & Control Plane — At-Least-Once + Effect Dedup, Compacted Kill-Switch Topic

**Status:** Accepted
**Date:** 2026-07-01 · consolidated 2026-07-06
**Decider:** Eric Grynspan
**Consolidates:** ADR-009 (delivery semantics) + ADR-010 (kill-switch distribution)

---

## Decision

- **Delivery model: at-least-once with effect deduplication** — not exactly-once transactions. Offsets are stored per message only after the claim is fully processed, committed in batches (default 100) behind a producer `flush()` barrier. The dedup set is marked only **after** successful emit. Poison messages dead-letter with their raw bytes (base64) plus partition/offset; every produce carries a delivery callback, so a failed delivery dead-letters instead of dropping.
- **Kill-switch state distributes via a compacted Kafka control topic** (`control.kill-switch`, single key) — the same hot-swap pattern proven for NCCI editions on `rules.control`. Every `is_active` check applies the latest published state; each replica bootstraps from the compacted log with a unique group.id. Opt-in via `KILL_SWITCH_DISTRIBUTED=true`.

## Why

**Bounded replay, not zero replay.** A crash replays at most one uncommitted batch, absorbed by the dedup set and the idempotent producer. The invariant that matters — one claim, one *action* — is guaranteed by effect-level dedup at a fraction of transactional complexity. Flush-before-commit is the load-bearing ordering: committing first converts the pipeline to at-most-once on a crash. Mark-after-success keeps dead-lettered claims redeliverable.

**The FCA "single lever" guarantee must survive horizontal scaling.** With process-local state, activating the switch stops one replica and leaves the others auto-correcting — false at replica count two. Log compaction is exactly the right primitive for one current value under one key: it replays state to any new consumer and propagates changes within seconds. Pattern reuse is a design argument — one control-plane mechanism to reason about, test, and defend.

## Rejected

| Alternative | Why rejected |
|---|---|
| **Kafka transactions (exactly-once)** | EOS protects the transport; the business risk lives at the effect layer, already covered by dedup — heavyweight machinery for an invariant it doesn't add |
| **Auto-commit** | Commits on a timer independent of processing success — at-most-once on crash, unacceptable for claims |
| **Per-message synchronous commit** | Same safety as batched commits behind the flush barrier, ~100× more coordinator round-trips |
| **Global dedup (Redis / Snowflake MERGE)** | Right production upgrade, wrong v1 scope — the in-process set covers the actual replay window (one batch); documented as the scaling path |
| **Redis for kill-switch state** | An entire infrastructure component with its own availability story, for one boolean |
| **Snowflake control table + TTL cache** | A kill-switch that takes a cache-expiry to engage is not a kill-switch |
| **Keep process-local, document the caveat** | Leaves the flagship compliance claim false under the deployment shape the system is designed for |
