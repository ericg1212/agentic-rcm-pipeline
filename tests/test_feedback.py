"""
Tests for Layer 4 — AdjudicationOutcomeStore, LiftCalculator, DriftMonitor,
AdjudicationConsumer._parse.
"""
from __future__ import annotations

import json

import pytest

from src.action.kill_switch import KillSwitch
from src.feedback.adjudication_consumer import AdjudicationConsumer
from src.feedback.drift_monitor import DriftMonitor
from src.feedback.lift_calculator import LiftCalculator, MIN_POWER_N
from src.feedback.outcome_store import AdjudicationOutcomeStore, OutcomeRecord


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _record(
    claim_id: str = "c1",
    outcome: str = "PAID",
    arm: str = "intervention",
) -> OutcomeRecord:
    return OutcomeRecord(
        claim_id=claim_id,
        payer_id="MEDICARE_FFS",
        adjudication_timestamp="2026-06-17T10:00:00+00:00",
        outcome=outcome,
        denial_code="CO-97" if outcome == "DENIED" else None,
        paid_amount=200.0 if outcome == "PAID" else None,
        adjustment_amount=None,
        arm=arm,
    )


def _fill_store(
    store: AdjudicationOutcomeStore,
    n: int,
    arm: str = "intervention",
    denial_rate: float = 0.0,
    seed: int = 42,
) -> None:
    import random
    rng = random.Random(seed)
    for i in range(n):
        outcome = "DENIED" if rng.random() < denial_rate else "PAID"
        store.record_outcome(_record(claim_id=f"{arm}-{i}", outcome=outcome, arm=arm))


# ---------------------------------------------------------------------------
# AdjudicationOutcomeStore
# ---------------------------------------------------------------------------

def test_store_record_and_retrieve():
    store = AdjudicationOutcomeStore()
    r = _record("c1", "PAID")
    store.record_outcome(r)
    assert store.get_outcome("c1") is r
    assert len(store) == 1


def test_store_invalid_outcome_raises():
    store = AdjudicationOutcomeStore()
    with pytest.raises(ValueError, match="Invalid outcome"):
        store.record_outcome(_record(outcome="APPROVED"))


def test_store_get_missing_returns_none():
    store = AdjudicationOutcomeStore()
    assert store.get_outcome("nonexistent") is None


def test_store_denial_rate_all_paid():
    store = AdjudicationOutcomeStore()
    for i in range(5):
        store.record_outcome(_record(f"c{i}", "PAID"))
    assert store.denial_rate() == 0.0


def test_store_denial_rate_all_denied():
    store = AdjudicationOutcomeStore()
    for i in range(4):
        store.record_outcome(_record(f"c{i}", "DENIED"))
    assert store.denial_rate() == pytest.approx(1.0)


def test_store_denial_rate_by_arm():
    store = AdjudicationOutcomeStore()
    store.record_outcome(_record("h1", "DENIED", arm="holdout"))
    store.record_outcome(_record("h2", "DENIED", arm="holdout"))
    store.record_outcome(_record("i1", "PAID", arm="intervention"))
    store.record_outcome(_record("i2", "PAID", arm="intervention"))

    assert store.denial_rate("holdout") == pytest.approx(1.0)
    assert store.denial_rate("intervention") == pytest.approx(0.0)


def test_store_denial_rate_empty_returns_zero():
    store = AdjudicationOutcomeStore()
    assert store.denial_rate() == 0.0
    assert store.denial_rate("holdout") == 0.0


def test_store_outcome_count_by_arm():
    store = AdjudicationOutcomeStore()
    for i in range(3):
        store.record_outcome(_record(f"h{i}", arm="holdout"))
    for i in range(7):
        store.record_outcome(_record(f"i{i}", arm="intervention"))
    assert store.outcome_count("holdout") == 3
    assert store.outcome_count("intervention") == 7
    assert store.outcome_count() == 10


def test_store_snowflake_rows():
    store = AdjudicationOutcomeStore()
    store.record_outcome(_record("c1"))
    rows = store.to_snowflake_rows()
    assert len(rows) == 1
    assert rows[0]["CLAIM_ID"] == "c1"
    assert "ARM" in rows[0]


# ---------------------------------------------------------------------------
# LiftCalculator
# ---------------------------------------------------------------------------

def test_lift_positive_when_intervention_lower():
    store = AdjudicationOutcomeStore()
    _fill_store(store, MIN_POWER_N, arm="intervention", denial_rate=0.05)
    _fill_store(store, MIN_POWER_N, arm="holdout", denial_rate=0.25)
    result = LiftCalculator(store).calculate()
    assert result.sufficient_power is True
    assert result.absolute_lift > 0


def test_lift_negative_when_intervention_higher():
    store = AdjudicationOutcomeStore()
    _fill_store(store, MIN_POWER_N, arm="intervention", denial_rate=0.30)
    _fill_store(store, MIN_POWER_N, arm="holdout", denial_rate=0.10)
    result = LiftCalculator(store).calculate()
    assert result.absolute_lift < 0


def test_lift_insufficient_power_below_min_n():
    store = AdjudicationOutcomeStore()
    _fill_store(store, MIN_POWER_N - 1, arm="intervention")
    _fill_store(store, MIN_POWER_N - 1, arm="holdout")
    result = LiftCalculator(store).calculate()
    assert result.sufficient_power is False


def test_lift_zero_holdout_rate_no_division_error():
    store = AdjudicationOutcomeStore()
    _fill_store(store, MIN_POWER_N, arm="intervention", denial_rate=0.0)
    _fill_store(store, MIN_POWER_N, arm="holdout", denial_rate=0.0)
    result = LiftCalculator(store).calculate()
    assert result.relative_lift == pytest.approx(0.0)


def test_lift_summary_insufficient_power():
    store = AdjudicationOutcomeStore()
    result = LiftCalculator(store).calculate()
    assert "INSUFFICIENT POWER" in result.summary()


def test_lift_summary_positive():
    store = AdjudicationOutcomeStore()
    _fill_store(store, MIN_POWER_N, arm="intervention", denial_rate=0.05, seed=1)
    _fill_store(store, MIN_POWER_N, arm="holdout", denial_rate=0.30, seed=2)
    result = LiftCalculator(store).calculate()
    assert "POSITIVE" in result.summary()


# ---------------------------------------------------------------------------
# DriftMonitor
# ---------------------------------------------------------------------------

def _monitor(baseline_window: int = 10, drift_window: int = 5, threshold: float = 0.20):
    store = AdjudicationOutcomeStore()
    ks = KillSwitch()
    monitor = DriftMonitor(
        outcome_store=store,
        kill_switch=ks,
        baseline_window=baseline_window,
        drift_window=drift_window,
        drift_threshold=threshold,
    )
    return store, ks, monitor


def test_drift_returns_none_insufficient_data():
    store, ks, monitor = _monitor()
    _fill_store(store, 5, denial_rate=0.10)  # less than baseline+drift
    assert monitor.check_drift() is None


def test_drift_no_alert_within_threshold():
    store, ks, monitor = _monitor(baseline_window=10, drift_window=5, threshold=0.20)
    # Baseline: ~20% denial rate
    for i in range(10):
        store.record_outcome(_record(f"b{i}", outcome="DENIED" if i < 2 else "PAID"))
    # Rolling: ~20% denial rate (no drift)
    for i in range(5):
        store.record_outcome(_record(f"r{i}", outcome="DENIED" if i < 1 else "PAID"))
    alert = monitor.check_drift()
    assert alert is not None
    assert alert.triggered is False
    assert ks.is_active is False


def test_drift_alert_triggers_kill_switch():
    store, ks, monitor = _monitor(baseline_window=10, drift_window=5, threshold=0.20)
    # Baseline: 10% denial rate
    for i in range(10):
        store.record_outcome(_record(f"b{i}", outcome="DENIED" if i == 0 else "PAID"))
    # Rolling: 80% denial rate — massive drift
    for i in range(5):
        store.record_outcome(_record(f"r{i}", outcome="DENIED" if i < 4 else "PAID"))
    alert = monitor.check_drift()
    assert alert is not None
    assert alert.triggered is True
    assert alert.kill_switch_activated is True
    assert ks.is_active is True


def test_drift_does_not_double_activate_kill_switch():
    store, ks, monitor = _monitor(baseline_window=10, drift_window=5, threshold=0.20)
    ks.activate("pre_existing")
    initial_count = ks.activation_count

    # Fill with drifting data
    for i in range(10):
        store.record_outcome(_record(f"b{i}", outcome="DENIED" if i == 0 else "PAID"))
    for i in range(5):
        store.record_outcome(_record(f"r{i}", outcome="DENIED"))

    monitor.check_drift()
    assert ks.activation_count == initial_count  # no additional activation


def test_drift_zero_baseline_returns_none():
    store, ks, monitor = _monitor(baseline_window=10, drift_window=5, threshold=0.20)
    # Baseline: 0% denial (can't compute relative change)
    for i in range(15):
        store.record_outcome(_record(f"c{i}", outcome="PAID"))
    assert monitor.check_drift() is None


# ---------------------------------------------------------------------------
# AdjudicationConsumer._parse
# ---------------------------------------------------------------------------

def test_consumer_parse_valid_message():
    consumer = AdjudicationConsumer.__new__(AdjudicationConsumer)
    consumer._store = AdjudicationOutcomeStore()
    consumer._kill_switch = KillSwitch()

    payload = json.dumps({
        "claim_id": "C-001",
        "payer_id": "MEDICARE_FFS",
        "adjudication_timestamp": "2026-06-17T10:00:00Z",
        "outcome": "PAID",
        "paid_amount": 250.0,
        "arm": "intervention",
    }).encode()

    record = consumer._parse(payload)
    assert record is not None
    assert record.claim_id == "C-001"
    assert record.outcome == "PAID"
    assert record.arm == "intervention"


def test_consumer_parse_missing_claim_id_returns_none():
    consumer = AdjudicationConsumer.__new__(AdjudicationConsumer)
    payload = json.dumps({"outcome": "PAID"}).encode()
    assert consumer._parse(payload) is None


def test_consumer_parse_invalid_json_returns_none():
    consumer = AdjudicationConsumer.__new__(AdjudicationConsumer)
    assert consumer._parse(b"not json") is None


def test_consumer_parse_defaults_arm_to_intervention():
    consumer = AdjudicationConsumer.__new__(AdjudicationConsumer)
    payload = json.dumps({"claim_id": "C-002", "outcome": "DENIED"}).encode()
    record = consumer._parse(payload)
    assert record is not None
    assert record.arm == "intervention"
