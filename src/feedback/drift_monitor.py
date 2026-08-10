# Copyright (c) 2026 Eric Grynspan. All rights reserved.
"""
Layer 4 — Denial rate drift monitor.

Compares denial rate in a rolling window against a baseline window and
activates the kill-switch when the difference is BOTH statistically real
and operationally material.

Why kill-switch on drift?
  If denial rate spikes well above baseline, the payer may have changed
  adjudication rules or the scoring model is miscalibrated. Continuing to
  auto-correct under these conditions risks FCA exposure. Kill-switch forces
  human review before autonomous actions resume.

Two-gate trigger (significance AND materiality)
-----------------------------------------------
A raw "relative change > 20%" threshold is not a drift detector — it is a
coin flip. At the shipped window sizes (baseline 100, rolling 50) and a
realistic 12% denial rate, the standard error of the DIFFERENCE between the
two windows is ~5.6 points while a 20% relative move is only ~2.4 points.
The noise floor sat at more than twice the trigger, so roughly two checks in
three would have fired on sampling noise alone — and since activate() is
idempotent and recovery is manual, the switch would latch early and stay
latched. That is a defect, not a scoping decision.

The fix keeps the 20% number but demotes it from "the trigger" to "the
materiality floor", and adds a two-proportion z-test as the evidence gate:

    triggered = (p_value < DRIFT_ALPHA) AND (|relative_change| > threshold)

Significance answers "is this difference real given how much data I have?";
materiality answers "is it big enough to be worth halting autonomy over?".
Both are required. The z-test is self-regulating with respect to sample
size — small windows widen the standard error, so only large differences
reach significance and no separate minimum-n guard is needed.

DRIFT_ALPHA defaults to 0.01 rather than the conventional 0.05 because this
check runs every 50 outcomes, so the multiple-comparison surface is large:
at 0.05 a latching switch would trip on noise roughly every 20 checks. 0.01
is a deliberate trade of detection sensitivity for false-positive cost.
Known remaining limitation: the alpha is not corrected for the number of
checks performed, so false-positive risk still accumulates over a long
enough run.

Known limitation — the baseline is anchored, not rolling: it is the first
`baseline_window` outcomes ever recorded and never advances. That is correct
for detecting movement away from a known-good reference period, and wrong
once legitimate seasonal movement accumulates. The production design
re-anchors on a known event (payer rule version change, model or prompt
version bump) rather than on a timer, so the reference period always
corresponds to a defined system state. Not built — see ADR-005.

Production upgrade: swap rolling list scan for a Great Expectations suite
running against a Snowflake RAW.ADJUDICATION_OUTCOMES slice — same logic,
payer-rule-aware expectations, alerting via PagerDuty webhook.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import structlog

from src.action.kill_switch import KillSwitch
from src.feedback.outcome_store import AdjudicationOutcomeStore

log = structlog.get_logger(__name__)


def two_proportion_test(
    x_baseline: int, n_baseline: int, x_rolling: int, n_rolling: int
) -> tuple[float, float]:
    """
    Pooled two-proportion z-test. Returns (z_statistic, two-tailed p_value).

    Stdlib only — the two-tailed p-value of a standard normal is exactly
    erfc(|z| / sqrt(2)), so no scipy dependency is pulled into Layer 4.

    Returns (0.0, 1.0) — "no evidence of a difference" — when the pooled rate
    is degenerate (every outcome denied, or none were), because the standard
    error is zero and the z-statistic is undefined.
    """
    if n_baseline == 0 or n_rolling == 0:
        return 0.0, 1.0

    pooled = (x_baseline + x_rolling) / (n_baseline + n_rolling)
    if pooled <= 0.0 or pooled >= 1.0:
        return 0.0, 1.0

    se = math.sqrt(pooled * (1.0 - pooled) * (1.0 / n_baseline + 1.0 / n_rolling))
    if se == 0.0:
        return 0.0, 1.0

    z = ((x_rolling / n_rolling) - (x_baseline / n_baseline)) / se
    p_value = math.erfc(abs(z) / math.sqrt(2.0))
    return z, p_value


@dataclass
class DriftAlert:
    triggered: bool
    baseline_denial_rate: float
    rolling_denial_rate: float
    relative_change: float          # (rolling - baseline) / baseline
    threshold: float                # materiality floor on |relative_change|
    n_baseline: int
    n_rolling: int
    kill_switch_activated: bool
    checked_at: str                 # ISO-8601
    z_statistic: float = 0.0
    p_value: float = 1.0
    alpha: float = 0.01
    is_significant: bool = False    # p_value < alpha
    is_material: bool = False       # |relative_change| > threshold

    @property
    def noise_floor(self) -> float:
        """
        Standard error of the difference between the two windows, in absolute
        rate points. The smallest difference this check can distinguish from
        sampling noise — report it alongside the observed difference so an
        underpowered check is visible rather than silent.
        """
        n_b, n_r = self.n_baseline, self.n_rolling
        if n_b == 0 or n_r == 0:
            return 0.0
        pooled = (
            self.baseline_denial_rate * n_b + self.rolling_denial_rate * n_r
        ) / (n_b + n_r)
        if pooled <= 0.0 or pooled >= 1.0:
            return 0.0
        return math.sqrt(pooled * (1.0 - pooled) * (1.0 / n_b + 1.0 / n_r))

    @property
    def message(self) -> str:
        observed = abs(self.rolling_denial_rate - self.baseline_denial_rate)
        stats = (
            f"z={self.z_statistic:+.2f}, p={self.p_value:.4f}, "
            f"observed diff {observed:.1%} vs noise floor {self.noise_floor:.1%}"
        )
        if self.triggered:
            return (
                f"DRIFT ALERT — rolling {self.rolling_denial_rate:.1%} vs "
                f"baseline {self.baseline_denial_rate:.1%} "
                f"({self.relative_change:+.1%} change, "
                f"materiality floor={self.threshold:.0%}); {stats}"
            )
        if self.is_material and not self.is_significant:
            return (
                f"No drift — {self.relative_change:+.1%} change clears the "
                f"{self.threshold:.0%} materiality floor but is within sampling "
                f"noise; {stats}"
            )
        if self.is_significant and not self.is_material:
            return (
                f"No drift — {self.relative_change:+.1%} change is statistically "
                f"real but below the {self.threshold:.0%} materiality floor; {stats}"
            )
        return (
            f"No drift — rolling: {self.rolling_denial_rate:.1%}, "
            f"baseline: {self.baseline_denial_rate:.1%}; {stats}"
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
        alpha: float = 0.01,
    ) -> None:
        self._store = outcome_store
        self._kill_switch = kill_switch
        self._baseline_window = baseline_window
        self._drift_window = drift_window
        # Materiality floor on |relative_change| — NOT the trigger on its own.
        self._drift_threshold = drift_threshold
        # Significance level for the two-proportion z-test evidence gate.
        self._alpha = alpha

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

        x_baseline = sum(1 for r in baseline if r.is_denied)
        x_rolling = sum(1 for r in rolling if r.is_denied)
        baseline_rate = x_baseline / len(baseline)
        rolling_rate = x_rolling / len(rolling)

        if baseline_rate == 0:
            return None  # can't compute relative change against zero baseline

        relative_change = (rolling_rate - baseline_rate) / baseline_rate

        # Evidence gate — is the difference distinguishable from sampling noise?
        z_statistic, p_value = two_proportion_test(
            x_baseline, len(baseline), x_rolling, len(rolling)
        )
        is_significant = p_value < self._alpha
        # Materiality gate — is it big enough to be worth halting autonomy over?
        is_material = abs(relative_change) > self._drift_threshold

        triggered = is_significant and is_material
        kill_switch_activated = False

        if triggered and not self._kill_switch.is_active:
            self._kill_switch.activate(
                f"denial_rate_drift:{relative_change:+.2%}:p={p_value:.4f}"
            )
            kill_switch_activated = True
            log.warning(
                "drift_kill_switch_activated",
                baseline_rate=f"{baseline_rate:.3f}",
                rolling_rate=f"{rolling_rate:.3f}",
                relative_change=f"{relative_change:+.3f}",
                z_statistic=f"{z_statistic:+.3f}",
                p_value=f"{p_value:.5f}",
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
            z_statistic=z_statistic,
            p_value=p_value,
            alpha=self._alpha,
            is_significant=is_significant,
            is_material=is_material,
        )
        log.info(
            "drift_check_complete",
            triggered=triggered,
            relative_change=f"{relative_change:+.3f}",
            p_value=f"{p_value:.5f}",
            is_significant=is_significant,
            is_material=is_material,
        )
        return alert
