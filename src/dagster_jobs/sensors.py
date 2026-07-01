# Copyright (c) 2026 Eric Grynspan. All rights reserved.
"""
Dagster self-healing sensors — denial-rate spike detection and auto-remediation.

The SelfHealingSensor closes the loop between Layer 4 outcome observation
and Layer 1/2 rule graph refresh. It runs on a configurable polling cadence
(Dagster sensor tick or standalone call) and applies tiered remediation:

  Diagnosis → Rule-caused spike (payer-specific + temporal correlation)
    Action:  PayerRuleGraph.reload() + emit to rules.control Kafka topic
    Why:     A rule change is recoverable — reload restores correct scoring.
             Kill-switch would halt the pipeline unnecessarily.

  Diagnosis → Drift-caused spike (spread across payers, no temporal correlation)
    Action:  KillSwitch.activate("denial_spike_undiagnosed")
    Why:     Unknown cause = unsafe to auto-correct. Human must investigate.
             Kill-switch bounds blast radius to FLAG routing only.

  Diagnosis → Ambiguous / insufficient data
    Action:  Emit SPIKE_ALERT structlog event only. No state change.
    Why:     False positives on kill-switch cost pipeline throughput.
             Alert first; sensor re-evaluates on next tick.

Interview frame:
  "Self-healing before alerting" — the sensor resolves known recoverable
  failure modes (stale rules) autonomously. Unknown failures immediately
  escalate to human review. The kill-switch is always the escape hatch.

Why this is "agentic, not just automated":
  A cron alert says "something is wrong." This sensor diagnoses the cause,
  attempts the appropriate targeted remediation, and only escalates if it
  cannot resolve the issue itself — same reasoning loop as the scoring agent.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import structlog

from src.action.kill_switch import KillSwitch
from src.config.settings import KafkaConfig
from src.feedback.outcome_store import AdjudicationOutcomeStore
from src.intelligence.rule_graph import PayerRuleGraph

log = structlog.get_logger(__name__)

# Minimum payer-specific outcome count before per-payer diagnosis is meaningful
MIN_PAYER_OUTCOMES = int(10)
# Denial rate that constitutes a "spike" vs this payer's rolling baseline
PAYER_SPIKE_THRESHOLD = 0.35
# Time window for "temporally correlated with rule ingest" check
RULE_INGEST_CORRELATION_MINUTES = 60


@dataclass
class SensorResult:
    triggered: bool
    diagnosis: str                   # "rule_caused" | "drift_caused" | "ambiguous" | "no_spike"
    remediation_taken: str           # "rule_reload" | "kill_switch" | "alert_only" | "none"
    payer_id: Optional[str]          # payer implicated in spike, if any
    spike_denial_rate: Optional[float]
    baseline_denial_rate: Optional[float]
    kill_switch_activated: bool
    rule_graph_reloaded: bool
    checked_at: str


class SelfHealingSensor:
    """
    Self-healing sensor for denial-rate spikes.

    Construct once and call check() on each Dagster sensor tick
    (or on a schedule via AdjudicationConsumer's drift check loop).

    The sensor does NOT replace DriftMonitor — DriftMonitor watches
    aggregate drift and is the primary kill-switch trigger. This sensor
    adds per-payer diagnosis and rule-reload remediation on top.
    """

    def __init__(
        self,
        outcome_store: AdjudicationOutcomeStore,
        kill_switch: KillSwitch,
        rule_graph: Optional[PayerRuleGraph] = None,
        kafka_producer=None,               # optional — injected in production
        spike_threshold: float = PAYER_SPIKE_THRESHOLD,
        min_payer_outcomes: int = MIN_PAYER_OUTCOMES,
    ) -> None:
        self._store = outcome_store
        self._kill_switch = kill_switch
        self._rule_graph = rule_graph
        self._kafka_producer = kafka_producer
        self._spike_threshold = spike_threshold
        self._min_payer_outcomes = min_payer_outcomes
        # Track last rule ingest timestamp for temporal correlation
        self._last_rule_ingest_at: Optional[datetime] = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def notify_rule_ingest(self) -> None:
        """Call after any PayerRuleGraph reload so temporal correlation works."""
        self._last_rule_ingest_at = datetime.now(timezone.utc)

    def check(self) -> SensorResult:
        """
        Run one sensor tick. Returns SensorResult describing what happened.
        Never raises — sensor failures log and return a no-op result.
        """
        try:
            return self._run_check()
        except Exception as e:
            log.error("sensor_exception", error=str(e), exc_info=True)
            return SensorResult(
                triggered=False, diagnosis="ambiguous",
                remediation_taken="none", payer_id=None,
                spike_denial_rate=None, baseline_denial_rate=None,
                kill_switch_activated=False, rule_graph_reloaded=False,
                checked_at=datetime.now(timezone.utc).isoformat(),
            )

    # ------------------------------------------------------------------
    # Core logic
    # ------------------------------------------------------------------

    def _run_check(self) -> SensorResult:
        checked_at = datetime.now(timezone.utc).isoformat()
        records = list(self._store._records)

        if len(records) < self._min_payer_outcomes * 2:
            log.debug("sensor.cold_start", n_records=len(records))
            return SensorResult(
                triggered=False, diagnosis="no_spike",
                remediation_taken="none", payer_id=None,
                spike_denial_rate=None, baseline_denial_rate=None,
                kill_switch_activated=False, rule_graph_reloaded=False,
                checked_at=checked_at,
            )

        # 1. Compute per-payer denial rates in rolling window (last N records)
        rolling_window = records[-max(50, self._min_payer_outcomes * 2):]
        payer_stats = self._compute_payer_stats(rolling_window)

        # 2. Find spiking payer (if any)
        spiking_payer, spike_rate, baseline_rate = self._find_spike(payer_stats, records)

        if spiking_payer is None:
            log.debug("sensor.no_spike_detected")
            return SensorResult(
                triggered=False, diagnosis="no_spike",
                remediation_taken="none", payer_id=None,
                spike_denial_rate=None, baseline_denial_rate=None,
                kill_switch_activated=False, rule_graph_reloaded=False,
                checked_at=checked_at,
            )

        log.warning(
            "sensor.spike_detected",
            payer_id=spiking_payer,
            spike_rate=round(spike_rate, 3),
            baseline_rate=round(baseline_rate, 3),
        )

        # 3. Diagnose: payer-specific + temporally correlated with rule ingest?
        diagnosis = self._diagnose(spiking_payer, payer_stats)

        # 4. Remediate
        if diagnosis == "rule_caused":
            self._remediate_rule_reload(spiking_payer)
            return SensorResult(
                triggered=True, diagnosis=diagnosis,
                remediation_taken="rule_reload", payer_id=spiking_payer,
                spike_denial_rate=spike_rate, baseline_denial_rate=baseline_rate,
                kill_switch_activated=False, rule_graph_reloaded=True,
                checked_at=checked_at,
            )

        if diagnosis == "drift_caused":
            self._remediate_kill_switch(spiking_payer, spike_rate, baseline_rate)
            return SensorResult(
                triggered=True, diagnosis=diagnosis,
                remediation_taken="kill_switch", payer_id=spiking_payer,
                spike_denial_rate=spike_rate, baseline_denial_rate=baseline_rate,
                kill_switch_activated=True, rule_graph_reloaded=False,
                checked_at=checked_at,
            )

        # Ambiguous — alert only, no state change
        log.warning(
            "SPIKE_ALERT",
            payer_id=spiking_payer,
            diagnosis=diagnosis,
            spike_rate=round(spike_rate, 3),
            note="Ambiguous spike cause — no automated remediation. Human review required.",
        )
        return SensorResult(
            triggered=True, diagnosis=diagnosis,
            remediation_taken="alert_only", payer_id=spiking_payer,
            spike_denial_rate=spike_rate, baseline_denial_rate=baseline_rate,
            kill_switch_activated=False, rule_graph_reloaded=False,
            checked_at=checked_at,
        )

    # ------------------------------------------------------------------
    # Diagnosis
    # ------------------------------------------------------------------

    def _diagnose(self, spiking_payer: str, payer_stats: dict) -> str:
        """
        Classify the spike cause.

        rule_caused:  spike is isolated to one payer AND temporally correlated
                      with a recent rule ingest (within RULE_INGEST_CORRELATION_MINUTES)
        drift_caused: spike is spread across multiple payers (no rule-change explanation)
        ambiguous:    payer-specific but no recent rule ingest, OR mixed signals
        """
        n_spiking_payers = sum(
            1 for stats in payer_stats.values()
            if stats["rolling_rate"] > self._spike_threshold
            and stats["n_rolling"] >= self._min_payer_outcomes
        )

        # Multiple payers spiking = not a rule change, more likely drift
        if n_spiking_payers > 2:
            return "drift_caused"

        # Single-payer spike — check temporal correlation with rule ingest
        if self._last_rule_ingest_at is not None:
            minutes_since_ingest = (
                datetime.now(timezone.utc) - self._last_rule_ingest_at
            ).total_seconds() / 60
            if minutes_since_ingest <= RULE_INGEST_CORRELATION_MINUTES:
                return "rule_caused"

        return "ambiguous"

    # ------------------------------------------------------------------
    # Remediation
    # ------------------------------------------------------------------

    def _remediate_rule_reload(self, payer_id: str) -> None:
        """Reload rule graph and emit to rules.control Kafka topic."""
        if self._rule_graph is not None:
            self._rule_graph.reload()
            log.info("sensor.rule_graph_reloaded", triggered_by_payer=payer_id)

        # Emit to rules.control — signals all consumers to reload their caches
        if self._kafka_producer is not None:
            try:
                import json
                self._kafka_producer.produce(
                    KafkaConfig.TOPIC_RULES_CONTROL,
                    value=json.dumps({
                        "event_type": "rule_reload",
                        "triggered_by": "self_healing_sensor",
                        "payer_id": payer_id,
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    }).encode(),
                )
                self._kafka_producer.flush()
            except Exception as e:
                log.warning("sensor.kafka_emit_failed", error=str(e))

        log.info(
            "sensor.remediation_rule_reload",
            payer_id=payer_id,
            rules_control_topic=KafkaConfig.TOPIC_RULES_CONTROL,
        )

    def _remediate_kill_switch(
        self, payer_id: str, spike_rate: float, baseline_rate: float
    ) -> None:
        """Activate kill-switch for undiagnosed drift-caused spike."""
        if not self._kill_switch.is_active:
            reason = f"denial_spike:{payer_id}:{spike_rate:.2%}_vs_{baseline_rate:.2%}_baseline"
            self._kill_switch.activate(reason)
            log.warning(
                "sensor.kill_switch_activated",
                payer_id=payer_id,
                spike_rate=round(spike_rate, 3),
                baseline_rate=round(baseline_rate, 3),
            )

    # ------------------------------------------------------------------
    # Stats helpers
    # ------------------------------------------------------------------

    def _compute_payer_stats(self, records) -> dict[str, dict]:
        """Per-payer denial counts in the provided window."""
        stats: dict[str, dict] = {}
        for r in records:
            payer = r.payer_id or "unknown"
            bucket = stats.setdefault(payer, {"n_rolling": 0, "n_denied": 0, "rolling_rate": 0.0})
            bucket["n_rolling"] += 1
            if r.is_denied:
                bucket["n_denied"] += 1
        for bucket in stats.values():
            n = bucket["n_rolling"]
            bucket["rolling_rate"] = bucket["n_denied"] / n if n > 0 else 0.0
        return stats

    def _find_spike(
        self, payer_stats: dict, all_records
    ) -> tuple[Optional[str], Optional[float], Optional[float]]:
        """
        Return (payer_id, spike_rate, baseline_rate) for the highest-spiking payer,
        or (None, None, None) if no payer exceeds the threshold.
        """
        # Baseline: all historical records (not just rolling window)
        baseline_by_payer: dict[str, dict] = {}
        for r in all_records:
            payer = r.payer_id or "unknown"
            b = baseline_by_payer.setdefault(payer, {"n": 0, "denied": 0})
            b["n"] += 1
            if r.is_denied:
                b["denied"] += 1

        worst_payer: Optional[str] = None
        worst_spike: float = 0.0
        worst_baseline: float = 0.0

        for payer, stats in payer_stats.items():
            if stats["n_rolling"] < self._min_payer_outcomes:
                continue
            rolling_rate = stats["rolling_rate"]
            if rolling_rate < self._spike_threshold:
                continue

            baseline = baseline_by_payer.get(payer, {})
            n_baseline = baseline.get("n", 0)
            baseline_rate = (baseline.get("denied", 0) / n_baseline) if n_baseline > 0 else 0.0

            relative_change = (
                (rolling_rate - baseline_rate) / baseline_rate
                if baseline_rate > 0 else 1.0
            )

            if relative_change > worst_spike:
                worst_spike = relative_change
                worst_payer = payer
                worst_baseline = baseline_rate

        if worst_payer is None:
            return None, None, None

        return worst_payer, payer_stats[worst_payer]["rolling_rate"], worst_baseline
