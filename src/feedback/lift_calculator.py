# Copyright (c) 2026 Eric Grynspan. All rights reserved.
"""
Layer 4 — Holdout lift calculator.

Compares denial rates between:
  - Intervention arm: claims that received a pre-submission action (auto-correct/flag)
  - Holdout arm: 10% control — no intervention, routed straight to clearinghouse

Lift = holdout_denial_rate - intervention_denial_rate

Positive lift = intervention arm was denied less often than control.
This is the business proof that prevention is working.

Power threshold (MIN_POWER_N=30): don't report lift until both arms have
enough records to be statistically meaningful. Returns sufficient_power=False
with the counts so the caller knows to wait.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

import structlog

from src.feedback.outcome_store import AdjudicationOutcomeStore

log = structlog.get_logger(__name__)

MIN_POWER_N = 30


@dataclass
class LiftResult:
    intervention_denial_rate: float
    holdout_denial_rate: float
    absolute_lift: float        # holdout_rate - intervention_rate (positive = good)
    relative_lift: float        # absolute_lift / holdout_rate
    n_intervention: int
    n_holdout: int
    sufficient_power: bool
    computed_at: str            # ISO-8601

    def summary(self) -> str:
        if not self.sufficient_power:
            return (
                f"INSUFFICIENT POWER — intervention n={self.n_intervention}, "
                f"holdout n={self.n_holdout} (need {MIN_POWER_N} per arm)"
            )
        direction = "POSITIVE" if self.absolute_lift > 0 else "NEGATIVE"
        return (
            f"Lift: {direction} | "
            f"Intervention denial rate: {self.intervention_denial_rate:.1%} | "
            f"Holdout denial rate: {self.holdout_denial_rate:.1%} | "
            f"Absolute lift: {self.absolute_lift:+.1%} | "
            f"Relative lift: {self.relative_lift:+.1%}"
        )


class LiftCalculator:
    """Calculates holdout lift from the adjudication outcome store."""

    def __init__(self, outcome_store: AdjudicationOutcomeStore) -> None:
        self._store = outcome_store

    def calculate(self) -> LiftResult:
        n_intervention = self._store.outcome_count("intervention")
        n_holdout = self._store.outcome_count("holdout")

        intervention_rate = self._store.denial_rate("intervention")
        holdout_rate = self._store.denial_rate("holdout")

        absolute_lift = holdout_rate - intervention_rate
        relative_lift = (absolute_lift / holdout_rate) if holdout_rate > 0 else 0.0

        result = LiftResult(
            intervention_denial_rate=intervention_rate,
            holdout_denial_rate=holdout_rate,
            absolute_lift=absolute_lift,
            relative_lift=relative_lift,
            n_intervention=n_intervention,
            n_holdout=n_holdout,
            sufficient_power=(n_intervention >= MIN_POWER_N and n_holdout >= MIN_POWER_N),
            computed_at=datetime.now(timezone.utc).isoformat(),
        )
        log.info(
            "lift_calculated",
            absolute_lift=f"{absolute_lift:+.3f}",
            n_intervention=n_intervention,
            n_holdout=n_holdout,
            sufficient_power=result.sufficient_power,
        )
        return result
