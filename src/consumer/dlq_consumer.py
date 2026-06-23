# Copyright (c) 2026 Eric Grynspan. All rights reserved.
"""
DLQ consumer — reads claims.dlq, retries retryable failures, quarantines the rest.

Retry policy:
  SCHEMA_VIOLATION          → quarantine immediately (corrupt payload; retry won't help)
  PROCESSING/ROUTING/TIMEOUT, retry_count < MAX_RETRIES
                            → re-apply gate + routing in-process; on success, produce
                              to the correct downstream topic; on failure, re-enqueue
                              to claims.dlq with retry_count + 1
  PROCESSING/ROUTING/TIMEOUT, retry_count >= MAX_RETRIES
                            → quarantine

Quarantine: structured CRITICAL log. Production upgrade: write to
DLQConfig.QUARANTINE_TABLE in Snowflake for durable audit trail.

In-process retry (vs. re-producing to claims.raw) avoids Avro round-trip
complexity and keeps retry logic co-located with failure handling — one
code path to maintain instead of two.
"""
from __future__ import annotations

import json
import signal
import sys
from typing import Optional

import structlog
from confluent_kafka import Consumer, KafkaError, KafkaException, Producer

from src.config.settings import DLQConfig, GateConfig, KafkaConfig
from src.consumer.dlq_record import DLQRecord
from src.consumer.ncci_gate import GateDecision, NCCIGate, Route

log = structlog.get_logger(__name__)


class DLQConsumer:
    """
    Reads claims.dlq and drives the retry/quarantine decision for each failure.

    gate, consumer, and producer are injectable for testing without Kafka running.
    """

    def __init__(
        self,
        gate: Optional[NCCIGate] = None,
        consumer: Optional[Consumer] = None,
        producer: Optional[Producer] = None,
    ) -> None:
        self._gate = gate or NCCIGate()
        self._consumer = consumer or self._build_consumer()
        self._producer = producer or self._build_producer()
        self._running = False

    def start(self) -> None:
        self._gate.load()
        self._consumer.subscribe([KafkaConfig.TOPIC_DLQ])
        self._running = True

        signal.signal(signal.SIGINT, self._shutdown)
        signal.signal(signal.SIGTERM, self._shutdown)

        log.info("dlq_consumer_started", topic=KafkaConfig.TOPIC_DLQ)

        while self._running:
            try:
                msg = self._consumer.poll(timeout=1.0)
                if msg is None:
                    continue
                if msg.error():
                    self._handle_kafka_error(msg.error())
                    continue

                raw = msg.value()
                if raw is None:
                    continue

                self._process(raw)

            except Exception as e:
                log.error("dlq_consumer_unhandled_error", error=str(e), exc_info=True)

    # ------------------------------------------------------------------
    # Core processing
    # ------------------------------------------------------------------

    def _process(self, raw: bytes) -> None:
        try:
            record = DLQRecord.from_bytes(raw)
        except Exception as e:
            log.critical(
                "dlq_record_unparseable",
                error=str(e),
                raw_preview=raw[:200].decode("utf-8", errors="replace"),
            )
            return

        if not record.is_retryable():
            self._quarantine(record, reason="non_retryable")
            return

        if record.retry_count >= DLQConfig.MAX_RETRIES:
            self._quarantine(record, reason="retries_exhausted")
            return

        self._retry(record)

    def _retry(self, record: DLQRecord) -> None:
        claim = record.payload
        claim_id = record.claim_id or claim.get("claim_id", "unknown")
        attempt = record.retry_count + 1

        log.info(
            "dlq_retry_attempt",
            claim_id=claim_id,
            failure_type=record.failure_type.value,
            attempt=attempt,
            max_retries=DLQConfig.MAX_RETRIES,
        )

        try:
            gate_decision = self._gate.evaluate(claim)
            target = self._resolve_target(claim, gate_decision)
            self._produce(target, claim, claim.get("payer_id", ""))
            log.info(
                "dlq_retry_succeeded",
                claim_id=claim_id,
                attempt=attempt,
                routed_to=target,
            )
        except Exception as e:
            next_record = record.next_retry()
            log.warning(
                "dlq_retry_failed",
                claim_id=claim_id,
                attempt=attempt,
                error=str(e),
                next_retry_count=next_record.retry_count,
            )
            self._produce_to_dlq(next_record)

    def _resolve_target(self, claim: dict, gate_decision: GateDecision) -> str:
        if gate_decision.route == Route.PASS:
            return "clearinghouse"
        if gate_decision.route == Route.AMBIGUOUS:
            return KafkaConfig.TOPIC_CLAIMS_SCORED
        # HARD_FAIL: high-value claims get LLM rationale; low-value get deterministic flag
        charge = float(claim.get("submitted_charge", "0") or "0")
        if charge > GateConfig.LLM_RISK_THRESHOLD * 1000:
            return KafkaConfig.TOPIC_CLAIMS_SCORED
        return KafkaConfig.TOPIC_CLAIMS_ACTIONS

    def _quarantine(self, record: DLQRecord, reason: str) -> None:
        # TODO: write to Snowflake DLQConfig.QUARANTINE_TABLE for durable audit trail
        log.critical(
            "claim_quarantined",
            claim_id=record.claim_id,
            failure_type=record.failure_type.value,
            reason=reason,
            retry_count=record.retry_count,
            original_topic=record.original_topic,
            error_message=record.error_message,
        )

    # ------------------------------------------------------------------
    # Kafka I/O
    # ------------------------------------------------------------------

    def _produce(self, topic: str, payload: dict, payer_id: str = "") -> None:
        if topic == "clearinghouse":
            return
        self._producer.produce(
            topic=topic,
            key=payer_id.encode("utf-8") if payer_id else None,
            value=json.dumps(payload).encode("utf-8"),
        )
        self._producer.poll(0)

    def _produce_to_dlq(self, record: DLQRecord) -> None:
        self._producer.produce(
            topic=KafkaConfig.TOPIC_DLQ,
            value=record.to_bytes(),
        )
        self._producer.poll(0)

    def _handle_kafka_error(self, error: KafkaError) -> None:
        if error.code() == KafkaError._PARTITION_EOF:
            return
        log.error("dlq_kafka_error", code=error.code(), reason=error.str())
        if error.fatal():
            raise KafkaException(error)

    def _shutdown(self, sig, frame) -> None:
        log.info("dlq_consumer_shutdown")
        self._running = False
        self._consumer.close()
        self._producer.flush()
        sys.exit(0)

    @staticmethod
    def _build_consumer() -> Consumer:
        return Consumer({
            "bootstrap.servers": KafkaConfig.BOOTSTRAP_SERVERS,
            "group.id": f"{KafkaConfig.CONSUMER_GROUP_ID}-dlq",
            "auto.offset.reset": "earliest",
            "enable.auto.commit": False,
        })

    @staticmethod
    def _build_producer() -> Producer:
        return Producer({
            "bootstrap.servers": KafkaConfig.BOOTSTRAP_SERVERS,
            "enable.idempotence": True,
            "acks": "all",
        })


if __name__ == "__main__":
    consumer = DLQConsumer()
    consumer.start()
