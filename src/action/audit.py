# Copyright (c) 2026 Eric Grynspan. All rights reserved.
"""
Layer 3 — Immutable action audit log.

Every autonomous action is logged here before execution. The log is the
FCA defense: every auto-correct cites the governing rule; every escalation
captures the LLM's rationale; every kill-switch override is stamped.

Immutability contract:
  - Append-only. No updates, no deletes.
  - Each record is a point-in-time snapshot: what the system decided,
    why, what rule it cited, and whether the action is reversible.
  - Reversible=True on auto-corrections (modifier add can be removed).
  - Reversible=False on escalations (human has already reviewed).

v1: in-process list. Production upgrade: write to RAW.ACTION_LOG in
Snowflake via Snowpipe Streaming or batch insert.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Iterator

import structlog

log = structlog.get_logger(__name__)


@dataclass
class AuditRecord:
    action_id: str
    claim_id: str
    score_id: str | None
    action_taken: str          # pass | auto_correct | flag | hold | escalate
    action_timestamp: str      # ISO-8601
    confidence: float | None
    risk_score: int | None
    governing_rule_cited: str | None
    correction_applied: dict | None
    escalation_draft: str | None
    reversible: bool
    kill_switch_active: bool

    def to_snowflake_row(self) -> dict:
        return {
            "ACTION_ID": self.action_id,
            "CLAIM_ID": self.claim_id,
            "SCORE_ID": self.score_id,
            "ACTION_TAKEN": self.action_taken,
            "ACTION_TIMESTAMP": self.action_timestamp,
            "CONFIDENCE": self.confidence,
            "RISK_SCORE": self.risk_score,
            "GOVERNING_RULE_CITED": self.governing_rule_cited,
            "CORRECTION_APPLIED": json.dumps(self.correction_applied) if self.correction_applied else None,
            "ESCALATION_DRAFT": self.escalation_draft,
            "REVERSIBLE": self.reversible,
            "KILL_SWITCH_ACTIVE": self.kill_switch_active,
        }


class ImmutableAuditLog:
    """
    Append-only action audit log.

    Thread-safe for single-process use (no concurrent writes in v1 consumer).
    """

    def __init__(self) -> None:
        self._records: list[AuditRecord] = []

    def append(self, record: AuditRecord) -> None:
        """Append a record. Never modifies existing records."""
        self._records.append(record)
        log.info(
            "audit_record_appended",
            action_id=record.action_id,
            claim_id=record.claim_id,
            action_taken=record.action_taken,
            reversible=record.reversible,
            kill_switch_active=record.kill_switch_active,
        )

    def __iter__(self) -> Iterator[AuditRecord]:
        return iter(self._records)

    def __len__(self) -> int:
        return len(self._records)

    def records_for_claim(self, claim_id: str) -> list[AuditRecord]:
        return [r for r in self._records if r.claim_id == claim_id]

    def auto_correct_rate(self) -> float:
        if not self._records:
            return 0.0
        auto = sum(1 for r in self._records if r.action_taken == "auto_correct")
        return auto / len(self._records)

    def fallback_rate(self) -> float:
        """Fraction of records where kill_switch was active (forced flag)."""
        if not self._records:
            return 0.0
        forced = sum(1 for r in self._records if r.kill_switch_active)
        return forced / len(self._records)

    def to_snowflake_rows(self) -> list[dict]:
        return [r.to_snowflake_row() for r in self._records]
