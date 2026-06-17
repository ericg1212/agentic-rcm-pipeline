# Copyright (c) 2026 Eric Grynspan. All rights reserved.
"""
Layer 4 — Denial rate drift monitor.

Compares denial rate in a rolling window against a baseline window.
When drift exceeds DRIFT_THRESHOLD (default 20% relative change),
activates the kill-switch to halt autonomous actions.

Why kill-switch on drift?
  If denial rate spikes well above baseline, the payer may have changed
  adjudication rules or the scoring model is miscalibrated. Continuing to
  auto-correct under these conditions risks FCA exposure. Kill-switch forces
  human review before autonomous actions resume.

Production upgrade: swap rolling list scan for a Great Expectations suite
running against a Snowflake RAW.ADJUDICATION_OUTCOMES slice — same logic,
payer-rule-aware expectations, alerting via PagerDuty webhook.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import structlog

from src.action.kill_switch import KillSwitch
from src.feedback.outcome_store import AdjudicationOutcomeStore

log = structlog.get_logger(__name__)


@dataclass
class DriftAlert:
    triggered: bool
    baseline_denial_rate: float
    rolling_denial_rate: float
    relative_change: float          # (rolling - baseline) / baseline
    threshold: float
    n_baseline: int
    n_rolling: int
    kill_switch_activated: bool
    checked_at: str                 # ISO-8601

    @property
    def message(self) -> str:
        if not self.triggered:
            return (
                f"No drift — rolling: {self.rolling_denial_rate:.1%}, "
                f"baseline: {self.baseline_denial_rate:.1%}"
            )
        return (
            f"DRIFT ALERT — rolling {self.rolling_denial_rate:.1%} vs "
            f"baseline {self.baseline_denial_rate:.1%} "
            f"({self.relative_change:+.1%} change, threshold={self.threshold:.0%})"
        )


class DriftMonitor:
    """
    Monitors denial rate drift and triggers the kill-switch on breach.

    Requires baseline_window + drift_window records before running.
    Returns None when data is insufficient — no false alarms on cold start.
    """

    def __init__(
        self,
        outcome_store: AdjudicationOutcomeStore,
        kill_switch: KillSwitch,
        baseline_window: int = 100,
        drift_window: int = 50,
        drift_threshold: float = 0.20,
    ) -> None:
        self._store = outcome_store
        self._kill_switch = kill_switch
        self._baseline_window = baseline_window
        self._drift_window = drift_window
        self._drift_threshold = drift_threshold

    def check_drift(self) -> Optional[DriftAlert]:
        """
        Returns DriftAlert if enough data exists; None on cold start.
        Activates kill-switch if drift exceeds threshold.
        """
        all_records = list(self._store._records)
        needed = self._baseline_window + self._drift_window

        if len(all_records) < needed:
            log.debug(
                "drift_check_skipped",
                n_records=len(all_records),
                needed=needed,
            )
            return None

        baseline = all_records[:self._baseline_window]
        rolling = all_records[-self._drift_window:]

        baseline_rate = sum(1 for r in baseline if r.is_denied) / len(baseline)
        rolling_rate = sum(1 for r in rolling if r.is_denied) / len(rolling)

        if baseline_rate == 0:
            return None  # can't compute relative change against zero baseline

        relative_change = (rolling_rate - baseline_rate) / baseline_rate
        triggered = abs(relative_change) > self._drift_threshold
        kill_switch_activated = False

        if triggered and not self._kill_switch.is_active:
            self._kill_switch.activate(f"denial_rate_drift:{relative_change:+.2%}")
            kill_switch_activated = True
            log.warning(
                "drift_kill_switch_activated",
                baseline_rate=f"{baseline_rate:.3f}",
                rolling_rate=f"{rolling_rate:.3f}",
                relative_change=f"{relative_change:+.3f}",
            )

        alert = DriftAlert(
            triggered=triggered,
            baseline_denial_rate=baseline_rate,
            rolling_denial_rate=rolling_rate,
            relative_change=relative_change,
            threshold=self._drift_threshold,
            n_baseline=len(baseline),
            n_rolling=len(rolling),
            kill_switch_activated=kill_switch_activated,
            checked_at=datetime.now(timezone.utc).isoformat(),
        )
        log.info(
            "drift_check_complete",
            triggered=triggered,
            relative_change=f"{relative_change:+.3f}",
        )
        return alert
