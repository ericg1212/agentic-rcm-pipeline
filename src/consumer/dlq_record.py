# Copyright (c) 2026 Eric Grynspan. All rights reserved.
"""
DLQ envelope — typed failure record for the claims.dlq topic.

Every message on claims.dlq is a DLQRecord. The failure_type drives
retry vs. quarantine decisions in the DLQ consumer.

Failure type taxonomy:
  SCHEMA_VIOLATION — Avro/JSON deserialization failed; payload is corrupt.
                     Non-retryable: re-sending a malformed record won't fix it.
  PROCESSING_ERROR — Exception raised during gate evaluation or routing emit.
                     Retryable: likely a transient infra issue.
  ROUTING_ERROR    — Gate logic raised an unexpected exception (bad seed data,
                     missing config). Retryable after gate reload.
  SCORING_TIMEOUT  — LLM call exceeded latency budget. Retryable.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


class FailureType(str, Enum):
    SCHEMA_VIOLATION = "SCHEMA_VIOLATION"
    PROCESSING_ERROR = "PROCESSING_ERROR"
    ROUTING_ERROR = "ROUTING_ERROR"
    SCORING_TIMEOUT = "SCORING_TIMEOUT"

    def is_retryable(self) -> bool:
        return self != FailureType.SCHEMA_VIOLATION


@dataclass
class DLQRecord:
    failure_type: FailureType
    original_topic: str
    error_message: str
    payload: dict[str, Any]
    claim_id: str = ""
    retry_count: int = 0
    failed_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def is_retryable(self) -> bool:
        return self.failure_type.is_retryable()

    def next_retry(self) -> "DLQRecord":
        """Return a copy with retry_count incremented and failed_at refreshed."""
        return DLQRecord(
            failure_type=self.failure_type,
            original_topic=self.original_topic,
            error_message=self.error_message,
            payload=self.payload,
            claim_id=self.claim_id,
            retry_count=self.retry_count + 1,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "failure_type": self.failure_type.value,
            "original_topic": self.original_topic,
            "error_message": self.error_message,
            "payload": self.payload,
            "claim_id": self.claim_id,
            "retry_count": self.retry_count,
            "failed_at": self.failed_at,
        }

    def to_bytes(self) -> bytes:
        return json.dumps(self.to_dict()).encode("utf-8")

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DLQRecord":
        return cls(
            failure_type=FailureType(data["failure_type"]),
            original_topic=data["original_topic"],
            error_message=data["error_message"],
            payload=data.get("payload", {}),
            claim_id=data.get("claim_id", ""),
            retry_count=data.get("retry_count", 0),
            failed_at=data.get("failed_at", ""),
        )

    @classmethod
    def from_bytes(cls, raw: bytes) -> "DLQRecord":
        return cls.from_dict(json.loads(raw.decode("utf-8")))
