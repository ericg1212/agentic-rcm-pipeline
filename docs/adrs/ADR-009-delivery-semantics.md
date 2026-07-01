# ADR-009: Consumer Delivery Semantics — At-Least-Once with Effect Deduplication

**Status:** Accepted
**Date:** 2026-07-01 (architecture review session)
**Decider:** Eric Grynspan

---

## Decision

- **Delivery model:** at-least-once, with effect deduplication — not exactly-once transactions
- **Offset lifecycle:** `enable.auto.offset.store=False`; offsets stored per message only after the claim is fully processed (routed + produced), committed in batches of `COMMIT_BATCH_SIZE` (default 100)
- **Commit ordering:** producer `flush()` **before** consumer `commit()` — an offset is only safe to commit once every downstream produce in that batch is acknowledged
- **Dedup lifecycle:** the claim_id dedup set is marked only **after** successful emit; checking and marking are separate operations
- **Poison messages:** dead-lettered with their raw bytes (base64) plus partition/offset, and their offset is stored — the DLQ owns the retry path, not offset replay
- **Failed deliveries:** every produce carries a delivery callback; a failed delivery dead-letters the claim. A DLQ delivery failure is log-only (terminal — no recursion)

## Why

**Bounded replay, not zero replay.** A crash replays at most one uncommitted batch (≤100 messages), absorbed by the dedup set and the idempotent producer. That bound is what the feedback loop and audit log actually require — a claim must not produce two *actions*, which effect-level dedup guarantees without transactional machinery.

**Flush-before-commit is the load-bearing ordering.** Committing an offset before the downstream produce is acknowledged converts the pipeline to at-most-once on a crash: the offset says "done," the claim never reached `claims.scored`. Flushing first makes the commit a true barrier.

**Mark-after-success keeps redelivery honest.** Marking a claim "seen" on first sight poisons legitimate redelivery of claims that failed mid-processing. A dead-lettered claim must remain redeliverable.

**Poison messages must carry their bytes.** A schema-violation DLQ record with an empty payload is a tombstone, not a dead letter — it can never be inspected or replayed. Storing the poison message's offset keeps one bad producer from wedging a partition on every restart.

## Rejected

| Alternative | Why rejected |
|---|---|
| **Kafka transactions (exactly-once)** | Correct but heavyweight: transactional producer + `send_offsets_to_transaction` + read-committed consumers across every downstream stage. The invariant that matters — one claim, one action — is already guaranteed by effect dedup at a fraction of the complexity. EOS protects the transport; the business risk lives at the effect layer. |
| **Auto-commit** | Commits on a timer, independent of processing success — messages can be committed before they are processed (at-most-once on crash). Unacceptable for claims. |
| **Per-message synchronous commit** | Correct but a round-trip to the group coordinator per message; batching behind the flush barrier gives the same safety with ~100× fewer commits. |
| **Global dedup (Redis SETNX / Snowflake MERGE)** | Right production upgrade, wrong v1 scope: the in-process LRU covers the actual replay window (one batch) once offsets commit correctly. Documented as the scaling path, not built speculatively. |
