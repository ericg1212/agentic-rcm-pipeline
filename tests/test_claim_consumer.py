# Copyright (c) 2026 Eric Grynspan. All rights reserved.
"""
Layer 1 consumer tests — offset lifecycle, dedup semantics, delivery
callbacks, poison-message DLQ capture, and charge-based routing.

These cover the delivery-semantics guarantees the module docstring claims:
  - offsets stored only after successful processing, committed in batches
  - dedup marked only after successful emit (failed claims stay redeliverable)
  - poison messages dead-lettered with raw bytes and offset-stored
  - failed deliveries are dead-lettered, never silent
"""
from __future__ import annotations

import base64
import json

import pytest
from confluent_kafka.serialization import SerializationError

from src.config.settings import GateConfig, KafkaConfig
from src.consumer.claim_consumer import BoundedDeduplicator, ClaimConsumer
from src.consumer.dlq_record import DLQRecord, FailureType
from src.consumer.ncci_gate import GateDecision, Route


# ------------------------------------------------------------------
# Fakes
# ------------------------------------------------------------------

class FakeMessage:
    def __init__(self, value: bytes | None, topic: str = "claims.raw",
                 partition: int = 0, offset: int = 0):
        self._value = value
        self._topic = topic
        self._partition = partition
        self._offset = offset

    def value(self):
        return self._value

    def error(self):
        return None

    def topic(self):
        return self._topic

    def partition(self):
        return self._partition

    def offset(self):
        return self._offset


class FakeConsumer:
    def __init__(self):
        self.stored_offsets: list[FakeMessage] = []
        self.commits = 0

    def store_offsets(self, message):
        self.stored_offsets.append(message)

    def commit(self, asynchronous=False):
        self.commits += 1

    def subscribe(self, topics):
        pass

    def close(self):
        pass


class FakeProducer:
    def __init__(self, fail_topics: set[str] | None = None):
        self.produced: list[dict] = []
        self.flushes = 0
        self._fail_topics = fail_topics or set()

    def produce(self, topic, value, key=None, on_delivery=None):
        if topic in self._fail_topics:
            raise BufferError("queue full")
        self.produced.append({
            "topic": topic, "key": key, "value": value,
            "on_delivery": on_delivery,
        })

    def poll(self, timeout):
        return 0

    def flush(self, timeout=None):
        self.flushes += 1
        return 0


class StubGate:
    """Gate stub returning a fixed decision."""

    def __init__(self, decision: GateDecision):
        self._decision = decision

    def evaluate(self, claim):
        return self._decision

    def load(self):
        pass


def _passthrough_deserializer(raw, ctx):
    return json.loads(raw.decode("utf-8"))


def _make_consumer(gate_decision: GateDecision | None = None,
                   fail_topics: set[str] | None = None) -> ClaimConsumer:
    c = ClaimConsumer(
        consumer=FakeConsumer(),
        producer=FakeProducer(fail_topics=fail_topics),
        deserializer=_passthrough_deserializer,
    )
    if gate_decision is not None:
        c._gate = StubGate(gate_decision)
    return c


def _claim_msg(claim: dict, offset: int = 0) -> FakeMessage:
    return FakeMessage(json.dumps(claim).encode("utf-8"), offset=offset)


AMBIGUOUS = GateDecision(route=Route.AMBIGUOUS, risk_score=0.5)
HARD_FAIL = GateDecision(route=Route.HARD_FAIL, risk_score=0.9,
                         deterministic_carc="97")
PASS = GateDecision(route=Route.PASS, risk_score=0.0)


# ------------------------------------------------------------------
# BoundedDeduplicator — check/mark are separate on purpose
# ------------------------------------------------------------------

class TestBoundedDeduplicator:
    def test_contains_does_not_insert(self):
        d = BoundedDeduplicator()
        assert d.contains("c1") is False
        assert d.contains("c1") is False  # still unseen — no insert on check

    def test_mark_then_contains(self):
        d = BoundedDeduplicator()
        d.mark("c1")
        assert d.contains("c1") is True

    def test_lru_bound_evicts_oldest(self):
        d = BoundedDeduplicator(max_size=2)
        d.mark("c1")
        d.mark("c2")
        d.mark("c3")
        assert d.contains("c1") is False
        assert d.contains("c2") is True
        assert d.contains("c3") is True


# ------------------------------------------------------------------
# Offset lifecycle
# ------------------------------------------------------------------

class TestOffsetLifecycle:
    def test_offset_stored_after_successful_processing(self):
        c = _make_consumer(AMBIGUOUS)
        msg = _claim_msg({"claim_id": "c1", "payer_id": "p1"})
        c.process_message(msg)
        assert c._consumer.stored_offsets == [msg]

    def test_commit_fires_at_batch_size(self, monkeypatch):
        monkeypatch.setattr(KafkaConfig, "COMMIT_BATCH_SIZE", 3)
        c = _make_consumer(PASS)
        for i in range(3):
            c.process_message(_claim_msg({"claim_id": f"c{i}", "payer_id": "p1"}, offset=i))
        assert c._consumer.commits == 1
        assert c._uncommitted == 0

    def test_no_commit_below_batch_size(self, monkeypatch):
        monkeypatch.setattr(KafkaConfig, "COMMIT_BATCH_SIZE", 100)
        c = _make_consumer(PASS)
        c.process_message(_claim_msg({"claim_id": "c1", "payer_id": "p1"}))
        assert c._consumer.commits == 0
        assert c._uncommitted == 1

    def test_producer_flushed_before_commit(self, monkeypatch):
        monkeypatch.setattr(KafkaConfig, "COMMIT_BATCH_SIZE", 1)
        c = _make_consumer(AMBIGUOUS)
        c.process_message(_claim_msg({"claim_id": "c1", "payer_id": "p1"}))
        assert c._producer.flushes == 1
        assert c._consumer.commits == 1

    def test_idle_barrier_drains_pending(self, monkeypatch):
        monkeypatch.setattr(KafkaConfig, "COMMIT_BATCH_SIZE", 100)
        c = _make_consumer(PASS)
        c.process_message(_claim_msg({"claim_id": "c1", "payer_id": "p1"}))
        c._commit_barrier()
        assert c._consumer.commits == 1
        assert c._uncommitted == 0

    def test_empty_barrier_is_noop(self):
        c = _make_consumer(PASS)
        c._commit_barrier()
        assert c._consumer.commits == 0
        assert c._producer.flushes == 0


# ------------------------------------------------------------------
# Dedup marked only after success
# ------------------------------------------------------------------

class TestDedupSemantics:
    def test_duplicate_skipped_but_offset_stored(self):
        c = _make_consumer(AMBIGUOUS)
        m1 = _claim_msg({"claim_id": "c1", "payer_id": "p1"}, offset=1)
        m2 = _claim_msg({"claim_id": "c1", "payer_id": "p1"}, offset=2)
        c.process_message(m1)
        c.process_message(m2)
        # one produce (the duplicate skipped), both offsets stored
        assert len(c._producer.produced) == 1
        assert c._consumer.stored_offsets == [m1, m2]

    def test_failed_emit_does_not_mark_dedup(self):
        # Producing to claims.scored raises → claim dead-letters,
        # and a later redelivery must NOT be treated as a duplicate
        c = _make_consumer(AMBIGUOUS, fail_topics={KafkaConfig.TOPIC_CLAIMS_SCORED})
        c.process_message(_claim_msg({"claim_id": "c1", "payer_id": "p1"}))
        assert c._dedup.contains("c1") is False
        dlq = [p for p in c._producer.produced if p["topic"] == KafkaConfig.TOPIC_DLQ]
        assert len(dlq) == 1
        record = DLQRecord.from_bytes(dlq[0]["value"])
        assert record.failure_type == FailureType.PROCESSING_ERROR
        assert record.claim_id == "c1"
        assert record.payload["claim_id"] == "c1"


# ------------------------------------------------------------------
# Poison messages
# ------------------------------------------------------------------

class TestPoisonMessage:
    def test_deserialization_failure_dead_letters_raw_bytes(self):
        def exploding_deserializer(raw, ctx):
            raise SerializationError("bad magic byte")

        c = ClaimConsumer(
            consumer=FakeConsumer(),
            producer=FakeProducer(),
            deserializer=exploding_deserializer,
        )
        raw = b"\x00\x01\x02 not avro"
        msg = FakeMessage(raw, partition=3, offset=42)
        c.process_message(msg)

        dlq = [p for p in c._producer.produced if p["topic"] == KafkaConfig.TOPIC_DLQ]
        assert len(dlq) == 1
        record = DLQRecord.from_bytes(dlq[0]["value"])
        assert record.failure_type == FailureType.SCHEMA_VIOLATION
        assert record.is_retryable() is False
        # Raw bytes recoverable for inspection/replay
        assert base64.b64decode(record.payload["raw_value_b64"]) == raw
        assert record.payload["partition"] == 3
        assert record.payload["offset"] == 42
        # Offset stored so the poison message doesn't wedge the partition
        assert c._consumer.stored_offsets == [msg]

    def test_none_value_finalized_without_dlq(self):
        c = _make_consumer(PASS)
        msg = FakeMessage(None)
        c.process_message(msg)
        assert c._producer.produced == []
        assert c._consumer.stored_offsets == [msg]


# ------------------------------------------------------------------
# Delivery callback — failed produce is never silent
# ------------------------------------------------------------------

class TestDeliveryCallback:
    def test_delivery_failure_dead_letters(self):
        c = _make_consumer(AMBIGUOUS)
        c.process_message(_claim_msg({"claim_id": "c1", "payer_id": "p1"}))
        produced = c._producer.produced[0]
        assert produced["on_delivery"] is not None

        class FakeErr:
            def str(self):
                return "broker unreachable"

        failed_msg = FakeMessage(produced["value"], topic=produced["topic"])
        produced["on_delivery"](FakeErr(), failed_msg)

        dlq = [p for p in c._producer.produced if p["topic"] == KafkaConfig.TOPIC_DLQ]
        assert len(dlq) == 1
        record = DLQRecord.from_bytes(dlq[0]["value"])
        assert "delivery_failed" in record.error_message
        assert record.payload["claim_id"] == "c1"

    def test_dlq_delivery_failure_does_not_recurse(self):
        c = _make_consumer(AMBIGUOUS)

        class FakeErr:
            def str(self):
                return "broker unreachable"

        before = len(c._producer.produced)
        c._on_dlq_delivery(FakeErr(), FakeMessage(b"{}", topic=KafkaConfig.TOPIC_DLQ))
        assert len(c._producer.produced) == before  # log-only, no re-emit


# ------------------------------------------------------------------
# Routing — charge threshold uses explicit config
# ------------------------------------------------------------------

class TestChargeRouting:
    @pytest.mark.parametrize("charge,expected_topic", [
        (GateConfig.HIGH_VALUE_CHARGE_USD + 1, KafkaConfig.TOPIC_CLAIMS_SCORED),
        (GateConfig.HIGH_VALUE_CHARGE_USD, KafkaConfig.TOPIC_CLAIMS_ACTIONS),
        (0.0, KafkaConfig.TOPIC_CLAIMS_ACTIONS),
    ])
    def test_hard_fail_routes_by_charge(self, charge, expected_topic):
        c = _make_consumer(HARD_FAIL)
        routing = c._route({
            "claim_id": "c1", "payer_id": "p1",
            "submitted_charge": str(charge),
        })
        assert routing.target_topic == expected_topic

    def test_holdout_bypasses_gate(self):
        c = _make_consumer(HARD_FAIL)  # gate would HARD_FAIL, holdout wins
        routing = c._route({
            "claim_id": "c1", "payer_id": "p1", "is_holdout": True,
        })
        assert routing.target_topic == "clearinghouse"
        assert routing.is_holdout is True
