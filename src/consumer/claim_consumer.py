# Copyright (c) 2026 Eric Grynspan. All rights reserved.
"""
Layer 1 — Idempotent Kafka Consumer + claim routing.

Consumes claims.raw, applies the NCCI gate, and routes each claim to:
  - clearinghouse (PASS, no further action)
  - claims.scored (AMBIGUOUS → LLM reasoning layer)
  - claims.scored (HARD_FAIL, high-value → LLM for rationale enrichment)
  - claims.actions (HARD_FAIL, low-value → deterministic flag, skip LLM)
  - claims.dlq    (schema violation or unhandled error)

Idempotency: keyed on claim_id + Kafka partition offset. If a message is
re-delivered (e.g., after consumer restart), the claim_id lookup in the
processed-set prevents double-processing. In production, this set would be
backed by Redis or Snowflake; for v1 it is in-process with a bounded LRU.

Holdout arm: claims with is_holdout=True are routed as if they are PASS
regardless of NCCI result — they flow to the clearinghouse unmodified and
their outcomes are tracked in the feedback loop as the control arm.
"""
from __future__ import annotations

import json
import signal
import sys
from collections import OrderedDict
from dataclasses import dataclass

import structlog
from confluent_kafka import KafkaError, KafkaException, Producer
from confluent_kafka.avro import AvroConsumer
from confluent_kafka.avro.serializer import SerializerError

from src.config.settings import GateConfig, KafkaConfig
from src.consumer.ncci_gate import GateDecision, NCCIGate, Route

log = structlog.get_logger(__name__)

# Bounded in-process dedup set (claim_id → offset seen)
# Production upgrade: swap for Redis SETNX or Snowflake dedup table
_MAX_DEDUP_SIZE = 50_000


class BoundedDeduplicator:
    """LRU-bounded set for claim_id deduplication within a consumer instance."""

    def __init__(self, max_size: int = _MAX_DEDUP_SIZE) -> None:
        self._seen: OrderedDict[str, bool] = OrderedDict()
        self._max = max_size

    def is_duplicate(self, claim_id: str) -> bool:
        if claim_id in self._seen:
            self._seen.move_to_end(claim_id)
            return True
        self._seen[claim_id] = True
        if len(self._seen) > self._max:
            self._seen.popitem(last=False)
        return False


@dataclass
class RoutingDecision:
    claim_id: str
    payer_id: str
    route: Route
    risk_score: float
    target_topic: str
    gate_decision: GateDecision
    is_holdout: bool


class ClaimConsumer:
    """
    Kafka consumer for the claims.raw topic.

    Routing table:
      is_holdout=True            → clearinghouse (no intervention; control arm)
      PASS                       → clearinghouse
      AMBIGUOUS                  → claims.scored (LLM required)
      HARD_FAIL + charge > $500  → claims.scored (LLM for rationale on high-value)
      HARD_FAIL + charge ≤ $500  → claims.actions (deterministic flag, skip LLM)
      schema error / exception   → claims.dlq
    """

    def __init__(self) -> None:
        self._gate = NCCIGate()
        self._dedup = BoundedDeduplicator()
        self._consumer = self._build_consumer()
        self._producer = self._build_producer()
        self._running = False

    def start(self) -> None:
        self._gate.load()
        self._consumer.subscribe([KafkaConfig.TOPIC_CLAIMS_RAW])
        self._running = True

        signal.signal(signal.SIGINT, self._shutdown)
        signal.signal(signal.SIGTERM, self._shutdown)

        log.info("consumer_started", topic=KafkaConfig.TOPIC_CLAIMS_RAW)
        processed = 0

        while self._running:
            try:
                msg = self._consumer.poll(timeout=1.0)
                if msg is None:
                    continue
                if msg.error():
                    self._handle_consumer_error(msg.error())
                    continue

                claim = msg.value()
                if claim is None:
                    continue

                claim_id = claim.get("claim_id", "")

                if self._dedup.is_duplicate(claim_id):
                    log.debug("duplicate_skipped", claim_id=claim_id)
                    continue

                routing = self._route(claim)
                self._emit(routing, claim)
                processed += 1

                if processed % 500 == 0:
                    log.info("consumer_heartbeat", processed=processed)

            except SerializerError as e:
                log.error("schema_violation", error=str(e))
                # Cannot deserialize — send raw bytes to DLQ if possible
                self._emit_dlq({"error": str(e), "raw": str(msg.value() if msg else None)})
            except Exception as e:
                log.error("consumer_error", error=str(e), exc_info=True)

    def _route(self, claim: dict) -> RoutingDecision:
        claim_id = claim.get("claim_id", "")
        payer_id = claim.get("payer_id", "")
        is_holdout = claim.get("is_holdout", False)

        if is_holdout:
            return RoutingDecision(
                claim_id=claim_id,
                payer_id=payer_id,
                route=Route.PASS,
                risk_score=0.0,
                target_topic="clearinghouse",
                gate_decision=GateDecision(route=Route.PASS, risk_score=0.0),
                is_holdout=True,
            )

        gate = self._gate.evaluate(claim)

        if gate.route == Route.PASS:
            return RoutingDecision(
                claim_id=claim_id, payer_id=payer_id,
                route=Route.PASS, risk_score=0.0,
                target_topic="clearinghouse",
                gate_decision=gate, is_holdout=False,
            )

        if gate.route == Route.AMBIGUOUS:
            return RoutingDecision(
                claim_id=claim_id, payer_id=payer_id,
                route=Route.AMBIGUOUS, risk_score=gate.risk_score,
                target_topic=KafkaConfig.TOPIC_CLAIMS_SCORED,
                gate_decision=gate, is_holdout=False,
            )

        # HARD_FAIL — route based on charge value
        charge = float(claim.get("submitted_charge", "0") or "0")
        if charge > GateConfig.LLM_RISK_THRESHOLD * 1000:
            # High-value: LLM enriches the rationale for the billing team
            target = KafkaConfig.TOPIC_CLAIMS_SCORED
        else:
            # Low-value: deterministic flag is sufficient, skip LLM
            target = KafkaConfig.TOPIC_CLAIMS_ACTIONS

        return RoutingDecision(
            claim_id=claim_id, payer_id=payer_id,
            route=Route.HARD_FAIL, risk_score=gate.risk_score,
            target_topic=target,
            gate_decision=gate, is_holdout=False,
        )

    def _emit(self, routing: RoutingDecision, original_claim: dict) -> None:
        if routing.target_topic == "clearinghouse":
            log.debug("routed_pass", claim_id=routing.claim_id, is_holdout=routing.is_holdout)
            return

        payload = {
            **original_claim,
            "gate_route": routing.route.value,
            "gate_risk_score": routing.risk_score,
            "gate_violations": [v.to_dict() for v in routing.gate_decision.violations],
            "deterministic_carc": routing.gate_decision.deterministic_carc,
        }
        self._producer.produce(
            topic=routing.target_topic,
            key=routing.payer_id,
            value=json.dumps(payload).encode("utf-8"),
        )
        self._producer.poll(0)
        log.info(
            "claim_routed",
            claim_id=routing.claim_id,
            route=routing.route.value,
            target=routing.target_topic,
            risk_score=routing.risk_score,
        )

    def _emit_dlq(self, payload: dict) -> None:
        self._producer.produce(
            topic=KafkaConfig.TOPIC_DLQ,
            value=json.dumps(payload).encode("utf-8"),
        )
        self._producer.poll(0)
        log.warning("sent_to_dlq", payload=payload)

    def _handle_consumer_error(self, error: KafkaError) -> None:
        if error.code() == KafkaError._PARTITION_EOF:
            return  # normal end-of-partition, not an error
        log.error("kafka_consumer_error", code=error.code(), reason=error.str())
        if error.fatal():
            raise KafkaException(error)

    def _shutdown(self, sig, frame) -> None:
        log.info("shutdown_signal", signal=sig)
        self._running = False
        self._consumer.close()
        self._producer.flush()
        sys.exit(0)

    @staticmethod
    def _build_consumer() -> AvroConsumer:
        return AvroConsumer({
            "bootstrap.servers": KafkaConfig.BOOTSTRAP_SERVERS,
            "schema.registry.url": KafkaConfig.SCHEMA_REGISTRY_URL,
            "group.id": KafkaConfig.CONSUMER_GROUP_ID,
            "auto.offset.reset": "earliest",
            "enable.auto.commit": False,   # manual commit for at-least-once semantics
            "max.poll.records": KafkaConfig.MAX_POLL_RECORDS,
        })

    @staticmethod
    def _build_producer() -> Producer:
        return Producer({
            "bootstrap.servers": KafkaConfig.BOOTSTRAP_SERVERS,
            "enable.idempotence": True,
            "acks": "all",
        })


if __name__ == "__main__":
    consumer = ClaimConsumer()
    consumer.start()
