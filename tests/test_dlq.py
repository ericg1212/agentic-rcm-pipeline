# Copyright (c) 2026 Eric Grynspan. All rights reserved.
"""
Tests for the DLQ layer — DLQRecord and DLQConsumer.

DLQRecord: failure type taxonomy, is_retryable, serialization round-trips,
           next_retry immutability.
DLQConsumer: quarantine vs. retry routing, retry success/failure, resolve_target
             routing table, clearinghouse short-circuit, unparseable raw handling.
"""
from __future__ import annotations

from unittest.mock import MagicMock


from src.config.settings import DLQConfig, KafkaConfig
from src.consumer.dlq_consumer import DLQConsumer
from src.consumer.dlq_record import DLQRecord, FailureType
from src.consumer.ncci_gate import GateDecision, Route


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_record(
    failure_type: FailureType = FailureType.PROCESSING_ERROR,
    retry_count: int = 0,
    claim_id: str = "c-001",
) -> DLQRecord:
    return DLQRecord(
        failure_type=failure_type,
        original_topic=KafkaConfig.TOPIC_CLAIMS_RAW,
        error_message="test error",
        payload={
            "claim_id": claim_id,
            "payer_id": "MEDICARE_FFS",
            "submitted_charge": "300.00",
            "procedure_codes": ["99215"],
            "modifiers": [],
            "units": 1,
            "is_holdout": False,
        },
        claim_id=claim_id,
        retry_count=retry_count,
    )


def _make_consumer(gate: MagicMock | None = None) -> tuple[DLQConsumer, MagicMock]:
    mock_gate = gate or MagicMock()
    mock_consumer = MagicMock()
    mock_producer = MagicMock()
    return DLQConsumer(gate=mock_gate, consumer=mock_consumer, producer=mock_producer), mock_producer


def _gate_decision(route: Route = Route.AMBIGUOUS) -> GateDecision:
    return GateDecision(route=route, risk_score=0.6, violations=[], deterministic_carc=None)


# ---------------------------------------------------------------------------
# DLQRecord — failure type taxonomy
# ---------------------------------------------------------------------------

def test_schema_violation_not_retryable():
    assert not FailureType.SCHEMA_VIOLATION.is_retryable()


def test_processing_error_retryable():
    assert FailureType.PROCESSING_ERROR.is_retryable()


def test_routing_error_retryable():
    assert FailureType.ROUTING_ERROR.is_retryable()


def test_scoring_timeout_retryable():
    assert FailureType.SCORING_TIMEOUT.is_retryable()


def test_record_is_retryable_delegates_to_failure_type():
    assert _make_record(FailureType.PROCESSING_ERROR).is_retryable()
    assert not _make_record(FailureType.SCHEMA_VIOLATION).is_retryable()


# ---------------------------------------------------------------------------
# DLQRecord — serialization
# ---------------------------------------------------------------------------

def test_record_dict_round_trip():
    r = _make_record(retry_count=2)
    restored = DLQRecord.from_dict(r.to_dict())
    assert restored.failure_type == r.failure_type
    assert restored.original_topic == r.original_topic
    assert restored.error_message == r.error_message
    assert restored.payload == r.payload
    assert restored.claim_id == r.claim_id
    assert restored.retry_count == r.retry_count


def test_record_bytes_round_trip():
    r = _make_record(retry_count=1)
    assert DLQRecord.from_bytes(r.to_bytes()).retry_count == 1


def test_record_failure_type_preserved_as_enum_after_round_trip():
    r = _make_record(FailureType.ROUTING_ERROR)
    assert DLQRecord.from_dict(r.to_dict()).failure_type is FailureType.ROUTING_ERROR


# ---------------------------------------------------------------------------
# DLQRecord — next_retry
# ---------------------------------------------------------------------------

def test_next_retry_increments_count():
    r = _make_record(retry_count=0)
    assert r.next_retry().retry_count == 1


def test_next_retry_chained():
    r = _make_record(retry_count=0)
    assert r.next_retry().next_retry().retry_count == 2


def test_next_retry_does_not_mutate_original():
    r = _make_record(retry_count=0)
    _ = r.next_retry()
    assert r.retry_count == 0


def test_next_retry_preserves_payload():
    r = _make_record(retry_count=0)
    assert r.next_retry().payload == r.payload


# ---------------------------------------------------------------------------
# DLQConsumer._process — routing decisions
# ---------------------------------------------------------------------------

def test_process_schema_violation_quarantines_immediately():
    consumer, _ = _make_consumer()
    consumer._quarantine = MagicMock()
    consumer._retry = MagicMock()

    record = _make_record(FailureType.SCHEMA_VIOLATION)
    consumer._process(record.to_bytes())

    consumer._quarantine.assert_called_once()
    quarantine_kwargs = consumer._quarantine.call_args
    assert quarantine_kwargs[1]["reason"] == "non_retryable"
    consumer._retry.assert_not_called()


def test_process_retries_exhausted_quarantines():
    consumer, _ = _make_consumer()
    consumer._quarantine = MagicMock()
    consumer._retry = MagicMock()

    record = _make_record(FailureType.PROCESSING_ERROR, retry_count=DLQConfig.MAX_RETRIES)
    consumer._process(record.to_bytes())

    consumer._quarantine.assert_called_once()
    assert consumer._quarantine.call_args[1]["reason"] == "retries_exhausted"
    consumer._retry.assert_not_called()


def test_process_retryable_under_limit_calls_retry():
    consumer, _ = _make_consumer()
    consumer._quarantine = MagicMock()
    consumer._retry = MagicMock()

    record = _make_record(FailureType.PROCESSING_ERROR, retry_count=0)
    consumer._process(record.to_bytes())

    consumer._retry.assert_called_once()
    consumer._quarantine.assert_not_called()


def test_process_retry_count_at_limit_minus_one_still_retries():
    consumer, _ = _make_consumer()
    consumer._quarantine = MagicMock()
    consumer._retry = MagicMock()

    record = _make_record(retry_count=DLQConfig.MAX_RETRIES - 1)
    consumer._process(record.to_bytes())

    consumer._retry.assert_called_once()
    consumer._quarantine.assert_not_called()


def test_process_unparseable_raw_does_not_crash():
    consumer, _ = _make_consumer()
    consumer._quarantine = MagicMock()
    consumer._retry = MagicMock()

    consumer._process(b"not valid json {{{")

    consumer._quarantine.assert_not_called()
    consumer._retry.assert_not_called()


# ---------------------------------------------------------------------------
# DLQConsumer._retry — success / failure paths
# ---------------------------------------------------------------------------

def test_retry_success_produces_to_downstream():
    gate = MagicMock()
    gate.evaluate.return_value = _gate_decision(Route.AMBIGUOUS)
    consumer, _ = _make_consumer(gate)
    consumer._produce = MagicMock()
    consumer._produce_to_dlq = MagicMock()

    consumer._retry(_make_record(retry_count=0))

    consumer._produce.assert_called_once()
    assert consumer._produce.call_args[0][0] == KafkaConfig.TOPIC_CLAIMS_SCORED
    consumer._produce_to_dlq.assert_not_called()


def test_retry_failure_reenqueues_with_incremented_count():
    gate = MagicMock()
    gate.evaluate.side_effect = RuntimeError("gate exploded")
    consumer, _ = _make_consumer(gate)
    consumer._produce = MagicMock()
    consumer._produce_to_dlq = MagicMock()

    consumer._retry(_make_record(retry_count=1))

    consumer._produce_to_dlq.assert_called_once()
    re_enqueued: DLQRecord = consumer._produce_to_dlq.call_args[0][0]
    assert re_enqueued.retry_count == 2
    consumer._produce.assert_not_called()


def test_retry_pass_route_does_not_produce_to_kafka():
    gate = MagicMock()
    gate.evaluate.return_value = _gate_decision(Route.PASS)
    consumer, mock_producer = _make_consumer(gate)

    consumer._retry(_make_record(retry_count=0))

    mock_producer.produce.assert_not_called()


# ---------------------------------------------------------------------------
# DLQConsumer._resolve_target — routing table
# ---------------------------------------------------------------------------

def test_resolve_target_pass_returns_clearinghouse():
    consumer, _ = _make_consumer()
    decision = _gate_decision(Route.PASS)
    assert consumer._resolve_target({}, decision) == "clearinghouse"


def test_resolve_target_ambiguous_returns_claims_scored():
    consumer, _ = _make_consumer()
    decision = _gate_decision(Route.AMBIGUOUS)
    assert consumer._resolve_target({}, decision) == KafkaConfig.TOPIC_CLAIMS_SCORED


def test_resolve_target_hard_fail_high_value_returns_claims_scored():
    consumer, _ = _make_consumer()
    decision = _gate_decision(Route.HARD_FAIL)
    claim = {"submitted_charge": "1000.00"}
    assert consumer._resolve_target(claim, decision) == KafkaConfig.TOPIC_CLAIMS_SCORED


def test_resolve_target_hard_fail_low_value_returns_claims_actions():
    consumer, _ = _make_consumer()
    decision = _gate_decision(Route.HARD_FAIL)
    claim = {"submitted_charge": "50.00"}
    assert consumer._resolve_target(claim, decision) == KafkaConfig.TOPIC_CLAIMS_ACTIONS


def test_resolve_target_missing_charge_defaults_to_actions():
    consumer, _ = _make_consumer()
    decision = _gate_decision(Route.HARD_FAIL)
    assert consumer._resolve_target({}, decision) == KafkaConfig.TOPIC_CLAIMS_ACTIONS


# ---------------------------------------------------------------------------
# DLQConsumer._produce — clearinghouse short-circuit
# ---------------------------------------------------------------------------

def test_produce_clearinghouse_does_not_call_kafka():
    consumer, mock_producer = _make_consumer()
    consumer._produce("clearinghouse", {"claim_id": "c1"}, "MEDICARE_FFS")
    mock_producer.produce.assert_not_called()


def test_produce_scored_topic_calls_kafka():
    consumer, mock_producer = _make_consumer()
    consumer._produce(KafkaConfig.TOPIC_CLAIMS_SCORED, {"claim_id": "c1"}, "MEDICARE_FFS")
    mock_producer.produce.assert_called_once()
    assert mock_producer.produce.call_args[1]["topic"] == KafkaConfig.TOPIC_CLAIMS_SCORED
