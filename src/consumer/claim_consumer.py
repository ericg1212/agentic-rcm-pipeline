# Copyright (c) 2026 Eric Grynspan. All rights reserved.
"""
Layer 1 — Idempotent Kafka Consumer + claim routing.

Consumes claims.raw, applies the NCCI gate, and routes each claim to:
  - clearinghouse (PASS, no further action)
  - claims.scored (AMBIGUOUS → LLM reasoning layer)
  - claims.scored (HARD_FAIL, high-value → LLM for rationale enrichment)
  - claims.actions (HARD_FAIL, low-value → deterministic flag, skip LLM)
  - claims.dlq    (schema violation or unhandled error)

Delivery semantics — at-least-once, effect-deduplicated:
  - enable.auto.offset.store=False: offsets are stored per-message only AFTER
    the claim has been fully processed (routed + produced), then committed in
    batches behind a producer flush barrier. A crash replays at most one
    uncommitted batch — never the whole topic.
  - The producer is idempotent (acks=all) and every produce carries a delivery
    callback: a failed delivery is logged and dead-lettered, never silent.
  - The dedup set is marked only after successful processing, so a claim that
    dead-letters mid-flight is NOT poisoned against legitimate redelivery.
  - Poison messages (deserialization failures) are dead-lettered WITH their
    raw bytes (base64) so they can be inspected and replayed, and their
    offsets are stored so they don't wedge the partition on restart.

Idempotency scope: the dedup set is in-process with a bounded LRU. It absorbs
redelivery within the committed-offset replay window (one batch). Cross-restart
global dedup is a production upgrade (Redis SETNX or Snowflake MERGE on
claim_id) — documented tradeoff, not an accident.

Holdout arm: claims with is_holdout=True are routed as if they are PASS
regardless of NCCI result — they flow to the clearinghouse unmodified and
their outcomes are tracked in the feedback loop as the control arm.
"""
from __future__ import annotations

import base64
import json
import signal
import sys
from collections import OrderedDict
from dataclasses import dataclass
from typing import Callable

import structlog
from confluent_kafka import Consumer, KafkaError, KafkaException, Message, Producer
from confluent_kafka.schema_registry import SchemaRegistryClient
from confluent_kafka.schema_registry.avro import AvroDeserializer
from confluent_kafka.serialization import (
    MessageField,
    SerializationContext,
    SerializationError,
)

from src.config.settings import GateConfig, KafkaConfig
from src.consumer.dlq_record import DLQRecord, FailureType
from src.consumer.ncci_gate import GateDecision, NCCIGate, Route

log = structlog.get_logger(__name__)

# Bounded in-process dedup set (claim_id seen within this consumer instance)
# Production upgrade: swap for Redis SETNX or Snowflake dedup table
_MAX_DEDUP_SIZE = 50_000


class BoundedDeduplicator:
    """
    LRU-bounded set for claim_id deduplication within a consumer instance.

    contains() and mark() are deliberately separate: a claim is marked seen
    only after it has been successfully processed. Marking on first sight
    would poison redelivery of claims that failed mid-processing.
    """

    def __init__(self, max_size: int = _MAX_DEDUP_SIZE) -> None:
        self._seen: OrderedDict[str, bool] = OrderedDict()
        self._max = max_size

    def contains(self, claim_id: str) -> bool:
        if claim_id in self._seen:
            self._seen.move_to_end(claim_id)
            return True
        return False

    def mark(self, claim_id: str) -> None:
        self._seen[claim_id] = True
        self._seen.move_to_end(claim_id)
        if len(self._seen) > self._max:
            self._seen.popitem(last=False)


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
      is_holdout=True                              → clearinghouse (control arm)
      PASS                                         → clearinghouse
      AMBIGUOUS                                    → claims.scored (LLM required)
      HARD_FAIL + charge > HIGH_VALUE_CHARGE_USD   → claims.scored (LLM rationale)
      HARD_FAIL + charge ≤ HIGH_VALUE_CHARGE_USD   → claims.actions (deterministic)
      schema error / exception                     → claims.dlq

    Dependencies are injectable for testing; defaults build real Kafka clients.
    """

    def __init__(
        self,
        consumer: Consumer | None = None,
        producer: Producer | None = None,
        deserializer: Callable | None = None,
    ) -> None:
        self._gate = NCCIGate()
        self._dedup = BoundedDeduplicator()
        self._consumer = consumer or self._build_consumer()
        self._producer = producer or self._build_producer()
        self._deserializer = deserializer or self._build_deserializer()
        self._running = False
        self._uncommitted = 0
        self._processed = 0

    def start(self) -> None:
        self._gate.load()
        self._consumer.subscribe([KafkaConfig.TOPIC_CLAIMS_RAW])
        self._running = True

        signal.signal(signal.SIGINT, self._shutdown)
        signal.signal(signal.SIGTERM, self._shutdown)

        log.info("consumer_started", topic=KafkaConfig.TOPIC_CLAIMS_RAW)

        while self._running:
            msg = self._consumer.poll(timeout=1.0)
            if msg is None:
                # Idle: opportunistically drain the commit barrier so offsets
                # don't sit uncommitted across a quiet period
                self._commit_barrier()
                continue
            if msg.error():
                self._handle_consumer_error(msg.error())
                continue

            self.process_message(msg)

    def process_message(self, msg: Message) -> None:
        """
        Process a single claims.raw message end-to-end.

        Offset is stored (not committed) for every terminal outcome — routed,
        duplicate, or dead-lettered — so poison messages never wedge the
        partition. Commit happens in batches behind a producer flush barrier.
        """
        claim: dict | None = None
        try:
            claim = self._deserialize(msg)
            if claim is None:
                # Poison message: already dead-lettered with raw bytes
                self._finalize(msg)
                return

            claim_id = claim.get("claim_id", "")

            if self._dedup.contains(claim_id):
                log.debug("duplicate_skipped", claim_id=claim_id)
                self._finalize(msg)
                return

            routing = self._route(claim)
            self._emit(routing, claim)

            # Mark seen + store offset only after successful emit
            self._dedup.mark(claim_id)
            self._finalize(msg)

            self._processed += 1
            if self._processed % 500 == 0:
                log.info("consumer_heartbeat", processed=self._processed)

        except Exception as e:
            log.error("consumer_error", error=str(e), exc_info=True)
            self._emit_dlq(DLQRecord(
                failure_type=FailureType.PROCESSING_ERROR,
                original_topic=KafkaConfig.TOPIC_CLAIMS_RAW,
                error_message=str(e),
                payload=claim or {},
                claim_id=(claim or {}).get("claim_id", ""),
            ))
            # Store the offset: the DLQ owns the retry path (dlq_consumer),
            # not offset replay. The claim is NOT marked in the dedup set.
            self._finalize(msg)

    def _deserialize(self, msg: Message) -> dict | None:
        """Deserialize Avro payload; dead-letter poison messages with raw bytes."""
        raw = msg.value()
        if raw is None:
            return None
        try:
            return self._deserializer(
                raw, SerializationContext(msg.topic(), MessageField.VALUE)
            )
        except SerializationError as e:
            log.error("schema_violation", error=str(e), offset=msg.offset())
            self._emit_dlq(DLQRecord(
                failure_type=FailureType.SCHEMA_VIOLATION,
                original_topic=KafkaConfig.TOPIC_CLAIMS_RAW,
                error_message=str(e),
                payload={
                    "raw_value_b64": base64.b64encode(raw).decode("ascii"),
                    "partition": msg.partition(),
                    "offset": msg.offset(),
                },
                claim_id="",
            ))
            return None

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
        if charge > GateConfig.HIGH_VALUE_CHARGE_USD:
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
            on_delivery=self._on_delivery,
        )
        self._producer.poll(0)
        log.info(
            "claim_routed",
            claim_id=routing.claim_id,
            route=routing.route.value,
            target=routing.target_topic,
            risk_score=routing.risk_score,
        )

    def _emit_dlq(self, record: DLQRecord) -> None:
        self._producer.produce(
            topic=KafkaConfig.TOPIC_DLQ,
            value=record.to_bytes(),
            on_delivery=self._on_dlq_delivery,
        )
        self._producer.poll(0)
        log.warning("sent_to_dlq", claim_id=record.claim_id, failure_type=record.failure_type.value)

    # ------------------------------------------------------------------
    # Delivery callbacks — a failed produce is never silent
    # ------------------------------------------------------------------

    def _on_delivery(self, err: KafkaError | None, msg: Message) -> None:
        if err is None:
            return
        log.error(
            "delivery_failed",
            topic=msg.topic(),
            error=err.str(),
        )
        raw = msg.value()
        self._emit_dlq(DLQRecord(
            failure_type=FailureType.PROCESSING_ERROR,
            original_topic=msg.topic(),
            error_message=f"delivery_failed: {err.str()}",
            payload=self._payload_from_bytes(raw),
        ))

    def _on_dlq_delivery(self, err: KafkaError | None, msg: Message) -> None:
        # Terminal: a DLQ delivery failure only logs — no re-emit, no recursion
        if err is not None:
            log.critical("dlq_delivery_failed", error=err.str())

    @staticmethod
    def _payload_from_bytes(raw: bytes | None) -> dict:
        if raw is None:
            return {}
        try:
            return json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return {"raw_value_b64": base64.b64encode(raw).decode("ascii")}

    # ------------------------------------------------------------------
    # Offset lifecycle — store per message, commit in batches
    # ------------------------------------------------------------------

    def _finalize(self, msg: Message) -> None:
        """Store the offset for a fully handled message; commit at batch size."""
        self._consumer.store_offsets(message=msg)
        self._uncommitted += 1
        if self._uncommitted >= KafkaConfig.COMMIT_BATCH_SIZE:
            self._commit_barrier()

    def _commit_barrier(self) -> None:
        """
        Flush the producer, then commit stored offsets. Ordering matters:
        an offset is only safe to commit once every downstream produce for
        that batch has been acknowledged — otherwise a crash between commit
        and delivery drops claims (at-most-once by accident).
        """
        if self._uncommitted == 0:
            return
        self._producer.flush(KafkaConfig.PRODUCER_FLUSH_TIMEOUT_S)
        try:
            self._consumer.commit(asynchronous=False)
        except KafkaException as e:
            if e.args and e.args[0].code() == KafkaError._NO_OFFSET:
                pass  # nothing stored since last commit — benign
            else:
                raise
        log.debug("offsets_committed", batch=self._uncommitted)
        self._uncommitted = 0

    def _handle_consumer_error(self, error: KafkaError) -> None:
        if error.code() == KafkaError._PARTITION_EOF:
            return  # normal end-of-partition, not an error
        log.error("kafka_consumer_error", code=error.code(), reason=error.str())
        if error.fatal():
            raise KafkaException(error)

    def _shutdown(self, sig, frame) -> None:
        log.info("shutdown_signal", signal=sig)
        self._running = False
        self._commit_barrier()
        self._producer.flush()
        self._consumer.close()
        sys.exit(0)

    @staticmethod
    def _build_consumer() -> Consumer:
        return Consumer({
            "bootstrap.servers": KafkaConfig.BOOTSTRAP_SERVERS,
            "group.id": KafkaConfig.CONSUMER_GROUP_ID,
            "auto.offset.reset": "earliest",
            # Manual offset lifecycle: store after successful processing,
            # commit in batches behind the producer flush barrier
            "enable.auto.commit": False,
            "enable.auto.offset.store": False,
        })

    @staticmethod
    def _build_producer() -> Producer:
        return Producer({
            "bootstrap.servers": KafkaConfig.BOOTSTRAP_SERVERS,
            "enable.idempotence": True,
            "acks": "all",
        })

    @staticmethod
    def _build_deserializer() -> AvroDeserializer:
        registry = SchemaRegistryClient({"url": KafkaConfig.SCHEMA_REGISTRY_URL})
        return AvroDeserializer(registry)


if __name__ == "__main__":
    consumer = ClaimConsumer()
    consumer.start()
