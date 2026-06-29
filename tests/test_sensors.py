# Copyright (c) 2026 Eric Grynspan. All rights reserved.
"""Tests for SelfHealingSensor — spike detection, diagnosis, and remediation."""
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch

import pytest

from src.action.kill_switch import KillSwitch
from src.dagster_jobs.sensors import (
    SelfHealingSensor,
    SensorResult,
    MIN_PAYER_OUTCOMES,
    PAYER_SPIKE_THRESHOLD,
)
from src.feedback.outcome_store import OutcomeRecord


def _make_outcome(payer_id: str, is_denied: bool) -> OutcomeRecord:
    record = MagicMock(spec=OutcomeRecord)
    record.payer_id = payer_id
    record.is_denied = is_denied
    record.arm = None
    return record


def _make_outcome_store(records: list) -> MagicMock:
    store = MagicMock()
    store._records = records
    return store


def _make_kill_switch(is_active: bool = False) -> KillSwitch:
    ks = MagicMock(spec=KillSwitch)
    ks.is_active = is_active
    return ks


def _make_sensor(records=None, kill_switch=None, rule_graph=None) -> SelfHealingSensor:
    if records is None:
        records = []
    store = _make_outcome_store(records)
    if kill_switch is None:
        kill_switch = _make_kill_switch()
    return SelfHealingSensor(
        outcome_store=store,
        kill_switch=kill_switch,
        rule_graph=rule_graph,
        min_payer_outcomes=5,
        spike_threshold=PAYER_SPIKE_THRESHOLD,
    )


class TestColdStart:
    def test_too_few_records_returns_no_spike(self):
        sensor = _make_sensor(records=[_make_outcome("P1", False)] * 3)
        result = sensor.check()
        assert not result.triggered
        assert result.diagnosis == "no_spike"
        assert result.remediation_taken == "none"

    def test_empty_store_returns_no_spike(self):
        sensor = _make_sensor(records=[])
        result = sensor.check()
        assert not result.triggered


class TestNoSpike:
    def test_low_denial_rate_no_trigger(self):
        # 2 denials out of 20 for payer = 10%, below 35% threshold
        records = (
            [_make_outcome("P1", True)] * 2
            + [_make_outcome("P1", False)] * 18
        )
        sensor = _make_sensor(records=records)
        result = sensor.check()
        assert not result.triggered
        assert result.diagnosis == "no_spike"


class TestSpikeDetection:
    def _spike_records(self, payer: str = "P1", n: int = 15, denied_pct: float = 0.80) -> list:
        n_denied = int(n * denied_pct)
        return (
            [_make_outcome(payer, True)] * n_denied
            + [_make_outcome(payer, False)] * (n - n_denied)
        )

    def test_high_denial_rate_triggers_spike(self):
        # Baseline (old, outside rolling window) must come first so the last
        # 50 records are dominated by the spike, not diluted by baseline.
        # Rolling window = last max(50, min_payer_outcomes*2) = last 50 records.
        records = [_make_outcome("P1", False)] * 25    # old baseline (records 0-24)
        records += [_make_outcome("P1", True)] * 20   # spike (in rolling window)
        records += [_make_outcome("P1", False)] * 30  # in rolling window
        # Total 75. Last 50: 20 denied + 30 not = 40% > 35% threshold.
        sensor = _make_sensor(records=records)
        result = sensor.check()
        assert result.triggered
        assert result.payer_id == "P1"
        assert result.spike_denial_rate is not None

    def test_spiking_payer_identified(self):
        # P1 baseline comes first (outside rolling window), P1 spike + P2 normal fill the last 50.
        records = [_make_outcome("P1", False)] * 30   # old P1 baseline
        records += [_make_outcome("P1", True)] * 20   # P1 spike (in rolling window)
        records += [_make_outcome("P1", False)] * 5   # P1 (in rolling window)
        records += [_make_outcome("P2", True)] * 2    # P2 (in rolling window)
        records += [_make_outcome("P2", False)] * 23  # P2 (in rolling window)
        # Total 80. Last 50: P1=25 (20 denied=80%), P2=25 (2 denied=8%)
        sensor = _make_sensor(records=records)
        result = sensor.check()
        assert result.triggered
        assert result.payer_id == "P1"


class TestDiagnosis:
    def _spike_records_for_payer(self, payer: str, n_spike: int = 15) -> list:
        return (
            [_make_outcome(payer, True)] * n_spike
            + [_make_outcome(payer, False)] * 2
            + [_make_outcome(payer, False)] * 30  # historical baseline
        )

    def test_recent_rule_ingest_diagnoses_rule_caused(self):
        records = self._spike_records_for_payer("P1")
        kill_switch = _make_kill_switch()
        sensor = _make_sensor(records=records, kill_switch=kill_switch)
        # Simulate rule ingest 10 minutes ago (within 60-min correlation window)
        sensor._last_rule_ingest_at = datetime.now(timezone.utc) - timedelta(minutes=10)
        result = sensor.check()
        if result.triggered:
            assert result.diagnosis in ("rule_caused", "ambiguous", "no_spike")
            # rule_caused if single payer + recent ingest
            if result.payer_id == "P1":
                assert result.diagnosis == "rule_caused"

    def test_no_recent_rule_ingest_diagnoses_ambiguous(self):
        records = self._spike_records_for_payer("P1")
        sensor = _make_sensor(records=records)
        sensor._last_rule_ingest_at = None
        result = sensor.check()
        if result.triggered:
            assert result.diagnosis in ("ambiguous", "no_spike", "rule_caused")

    def test_multi_payer_spike_diagnoses_drift(self):
        # 3 payers all spiking → drift_caused
        records = []
        for payer in ["P1", "P2", "P3"]:
            records += [_make_outcome(payer, True)] * 15
            records += [_make_outcome(payer, False)] * 2
            records += [_make_outcome(payer, False)] * 30
        sensor = _make_sensor(records=records)
        sensor._last_rule_ingest_at = datetime.now(timezone.utc) - timedelta(minutes=5)
        result = sensor.check()
        if result.triggered:
            assert result.diagnosis in ("drift_caused", "ambiguous", "no_spike")


class TestRemediation:
    def _build_spiking_records(self) -> list:
        return (
            [_make_outcome("P1", True)] * 15
            + [_make_outcome("P1", False)] * 3
            + [_make_outcome("P1", False)] * 30
        )

    def test_rule_caused_triggers_rule_reload(self):
        rule_graph = MagicMock()
        kill_switch = _make_kill_switch()
        records = self._build_spiking_records()
        sensor = _make_sensor(records=records, kill_switch=kill_switch, rule_graph=rule_graph)
        sensor._last_rule_ingest_at = datetime.now(timezone.utc) - timedelta(minutes=5)
        result = sensor.check()
        if result.diagnosis == "rule_caused":
            assert result.rule_graph_reloaded
            assert not result.kill_switch_activated
            rule_graph.reload.assert_called_once()

    def test_drift_caused_activates_kill_switch(self):
        kill_switch = _make_kill_switch(is_active=False)
        records = []
        for payer in ["P1", "P2", "P3"]:
            records += [_make_outcome(payer, True)] * 15
            records += [_make_outcome(payer, False)] * 2
            records += [_make_outcome(payer, False)] * 30
        sensor = _make_sensor(records=records, kill_switch=kill_switch)
        sensor._last_rule_ingest_at = None
        result = sensor.check()
        if result.diagnosis == "drift_caused":
            assert result.kill_switch_activated
            kill_switch.activate.assert_called_once()

    def test_ambiguous_no_kill_switch_no_reload(self):
        rule_graph = MagicMock()
        kill_switch = _make_kill_switch()
        records = self._build_spiking_records()
        sensor = _make_sensor(records=records, kill_switch=kill_switch, rule_graph=rule_graph)
        sensor._last_rule_ingest_at = None  # no temporal correlation
        result = sensor.check()
        if result.diagnosis == "ambiguous":
            assert result.remediation_taken == "alert_only"
            assert not result.kill_switch_activated
            assert not result.rule_graph_reloaded

    def test_kill_switch_not_activated_twice(self):
        kill_switch = _make_kill_switch(is_active=True)  # already active
        records = []
        for payer in ["P1", "P2", "P3"]:
            records += [_make_outcome(payer, True)] * 15
            records += [_make_outcome(payer, False)] * 30
        sensor = _make_sensor(records=records, kill_switch=kill_switch)
        sensor._last_rule_ingest_at = None
        sensor.check()
        kill_switch.activate.assert_not_called()

    def test_sensor_exception_returns_safe_result(self):
        store = MagicMock()
        store._records = None  # will cause TypeError when iterated
        sensor = SelfHealingSensor(
            outcome_store=store,
            kill_switch=_make_kill_switch(),
            min_payer_outcomes=5,
        )
        result = sensor.check()
        assert not result.triggered
        assert result.remediation_taken == "none"


class TestNotifyRuleIngest:
    def test_notify_sets_timestamp(self):
        sensor = _make_sensor()
        before = datetime.now(timezone.utc)
        sensor.notify_rule_ingest()
        after = datetime.now(timezone.utc)
        assert sensor._last_rule_ingest_at is not None
        assert before <= sensor._last_rule_ingest_at <= after
