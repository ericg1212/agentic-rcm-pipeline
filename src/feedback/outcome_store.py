# Copyright (c) 2026 Eric Grynspan. All rights reserved.
"""
Layer 4 — Adjudication outcome store.

Receives delayed payer responses (days/weeks after pre-submission action)
and tracks them by claim_id. This closes the feedback loop:
  - Intervention arm: claims that received a pre-submission action
  - Holdout arm: claims that passed through unmodified (control)

Denial rate comparison between arms is the business proof that intervention
actually prevented denials — not just that the system fired.

v1: in-process list. Production upgrade: write to RAW.ADJUDICATION_OUTCOMES
via Snowpipe Streaming and query via dbt fct_adjudication_outcomes.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import structlog

log = structlog.get_logger(__name__)

VALID_OUTCOMES = frozenset(["PAID", "DENIED", "PARTIAL_PAYMENT", "PENDING"])
VALID_ARMS = frozenset(["holdout", "intervention", "deterministic"])


@dataclass
class OutcomeRecord:
    claim_id: str
    payer_id: str
    adjudication_timestamp: str      # ISO-8601
    outcome: str                      # PAID | DENIED | PARTIAL_PAYMENT | PENDING
    denial_code: Optional[str]        # CARC if denied, else None
    paid_amount: Optional[float]
    adjustment_amount: Optional[float]
    arm: str                          # holdout | intervention | deterministic

    @property
    def is_denied(self) -> bool:
        return self.outcome == "DENIED"

    def to_snowflake_row(self) -> dict:
        return {
            "CLAIM_ID": self.claim_id,
            "PAYER_ID": self.payer_id,
            "ADJUDICATION_TIMESTAMP": self.adjudication_timestamp,
            "OUTCOME": self.outcome,
            "DENIAL_CODE": self.denial_code,
            "PAID_AMOUNT": self.paid_amount,
            "ADJUSTMENT_AMOUNT": self.adjustment_amount,
            "ARM": self.arm,
        }


class AdjudicationOutcomeStore:
    """Append-only store for payer adjudication outcomes."""

    def __init__(self) -> None:
        self._records: list[OutcomeRecord] = []
        self._index: dict[str, OutcomeRecord] = {}  # claim_id → latest record

    def record_outcome(self, record: OutcomeRecord) -> None:
        if record.outcome not in VALID_OUTCOMES:
            raise ValueError(
                f"Invalid outcome {record.outcome!r}. Must be one of {VALID_OUTCOMES}"
            )
        self._records.append(record)
        self._index[record.claim_id] = record
        log.info(
            "outcome_recorded",
            claim_id=record.claim_id,
            outcome=record.outcome,
            arm=record.arm,
            denial_code=record.denial_code,
        )

    def get_outcome(self, claim_id: str) -> Optional[OutcomeRecord]:
        return self._index.get(claim_id)

    def denial_rate(self, arm: Optional[str] = None) -> float:
        records = self._records_for_arm(arm)
        if not records:
            return 0.0
        return sum(1 for r in records if r.is_denied) / len(records)

    def outcome_count(self, arm: Optional[str] = None) -> int:
        return len(self._records_for_arm(arm))

    def _records_for_arm(self, arm: Optional[str]) -> list[OutcomeRecord]:
        if arm is None:
            return self._records
        return [r for r in self._records if r.arm == arm]

    def __len__(self) -> int:
        return len(self._records)

    def to_snowflake_rows(self) -> list[dict]:
        return [r.to_snowflake_row() for r in self._records]
