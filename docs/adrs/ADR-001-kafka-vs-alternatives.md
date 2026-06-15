# ADR-001: Streaming Technology — Kafka vs Alternatives

**Status:** Accepted  
**Date:** 2026-06-14 (Plan Mode architecture session)  
**Decider:** Eric Grynspan

---

## Decision

Apache Kafka (Bitnami KRaft, no ZooKeeper) via Docker for local dev; MSK Serverless for the production credential spike.

## Why

Pre-submission claim interception is latency-sensitive (seconds window, not hours). Kafka provides genuine event-time streaming with per-payer ordering guarantees via partition key assignment. The claims.raw partition key = payer_id ensures all claims for a given payer are processed in sequence by a single consumer — critical for applying payer-specific rule versions consistently. The compacted rules.control topic enables zero-downtime NCCI quarterly hot-swaps. Kafka is the industry standard for healthcare claims streaming (Waystar, Cotiviti, Change Healthcare all run Kafka); it carries maximum resume signal.

## Rejected

| Alternative | Why rejected |
|---|---|
| **Amazon Kinesis** | Equivalent latency profile but lower interview signal; no compacted-topic primitive for rule hot-swap; 7-day retention default is short for the feedback loop's 30–90 day adjudication window |
| **Dagster sensors / polling** | Not streaming — sensors poll on a schedule (minutes to hours), defeating the pre-submission window. Architecturally dishonest: the narrative requires genuine real-time, not micro-batch dressed as streaming |
| **Spark Structured Streaming** | Micro-batch by default; true streaming mode (continuous processing) is experimental. Adds JVM complexity with no latency benefit at P4's claim volume; Spark is the right call at 100M+ events/day, not 10K |
| **Redpanda** | Kafka-compatible and Rust-based (lower latency overhead), but smaller ecosystem, fewer operators know it, weaker interview signal. Right call if latency were measured in microseconds; P4 is bounded by the LLM API call (~300ms), making Redpanda's edge irrelevant |

## Interview L1/L2/L3

**L1:** "Kafka is the standard for real-time claims streaming, and I needed per-payer ordering and zero-downtime rule updates — both require primitives Kafka has natively."  
**L2:** "The partition key = payer_id guarantees that all in-flight claims for a given payer land in the same partition and are processed by one consumer thread. That matters because I hot-swap NCCI quarterly editions via a compacted control topic — if two claims from the same payer straddle a rule update on different partitions, one would be scored against stale rules."  
**L3:** "In production I'd add a hash salt to the payer key to prevent hot partitions on large payers like UHC (which can represent 30%+ of Medicare Advantage volume). For MVP with 6 partitions and 6 payers, the distribution is clean."
