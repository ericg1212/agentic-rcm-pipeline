# Copyright (c) 2026 Eric Grynspan. All rights reserved.
"""
Tests for Phase 2 Component 3 — Confidence Calibration Monitor.
Covers: ECE computation, Platt fitting, FCA risk detection, checkpoint structure.
"""
from __future__ import annotations

import numpy as np

from src.feedback.calibration import CalibrationMonitor, CalibrationCheckpoint


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_outcomes(
    n: int = 50,
    mean_confidence: float = 0.70,
    denial_rate: float = 0.30,
) -> list[dict]:
    """Synthetic labeled outcomes with deterministic pattern."""
    rng = np.random.default_rng(42)
    confidences = np.clip(rng.normal(mean_confidence, 0.1, n), 0.01, 0.99)
    labels = rng.binomial(1, denial_rate, n).astype(bool)
    return [{"confidence": float(c), "is_denied": bool(d)} for c, d in zip(confidences, labels)]


# ---------------------------------------------------------------------------
# ECE computation
# ---------------------------------------------------------------------------

class TestComputeECE:
    def test_perfect_calibration_ece_near_zero(self):
        """When predicted confidence matches actual rate in every bin, ECE ≈ 0."""
        # Force each confidence exactly equal to its label (trivial calibration)
        outcomes = [{"confidence": 1.0, "is_denied": True}] * 25 + \
                   [{"confidence": 0.0, "is_denied": False}] * 25
        monitor = CalibrationMonitor(outcomes)
        conf = np.array([o["confidence"] for o in outcomes])
        labels = np.array([int(o["is_denied"]) for o in outcomes])
        ece = monitor.compute_ece(conf, labels, n_bins=10)
        assert ece < 0.10

    def test_ece_returns_float(self):
        outcomes = _make_outcomes(50)
        monitor = CalibrationMonitor(outcomes)
        conf = np.array([o["confidence"] for o in outcomes])
        labels = np.array([int(o["is_denied"]) for o in outcomes])
        ece = monitor.compute_ece(conf, labels)
        assert isinstance(ece, float)

    def test_ece_bounded_0_to_1(self):
        outcomes = _make_outcomes(50)
        monitor = CalibrationMonitor(outcomes)
        conf = np.array([o["confidence"] for o in outcomes])
        labels = np.array([int(o["is_denied"]) for o in outcomes])
        ece = monitor.compute_ece(conf, labels)
        assert 0.0 <= ece <= 1.0

    def test_overconfident_model_has_positive_ece(self):
        """Overconfident model (all high confidence, mixed outcomes) has ECE > 0."""
        outcomes = [{"confidence": 0.95, "is_denied": True}] * 15 + \
                   [{"confidence": 0.95, "is_denied": False}] * 35
        monitor = CalibrationMonitor(outcomes)
        conf = np.full(50, 0.95)
        labels = np.array([1] * 15 + [0] * 35)
        ece = monitor.compute_ece(conf, labels)
        assert ece > 0.0


# ---------------------------------------------------------------------------
# Platt scaling
# ---------------------------------------------------------------------------

class TestFitPlatt:
    def test_fit_returns_two_floats(self):
        outcomes = _make_outcomes(50)
        monitor = CalibrationMonitor(outcomes)
        conf = np.array([o["confidence"] for o in outcomes])
        labels = np.array([int(o["is_denied"]) for o in outcomes])
        a, b = monitor.fit_platt(conf, labels)
        assert isinstance(a, float)
        assert isinstance(b, float)

    def test_calibrate_confidence_bounded(self):
        outcomes = _make_outcomes(50)
        monitor = CalibrationMonitor(outcomes)
        conf = np.array([o["confidence"] for o in outcomes])
        labels = np.array([int(o["is_denied"]) for o in outcomes])
        a, b = monitor.fit_platt(conf, labels)
        for raw in [0.0, 0.5, 0.9, 1.0]:
            cal = monitor.calibrate_confidence(raw, a, b)
            assert 0.0 <= cal <= 1.0


# ---------------------------------------------------------------------------
# FCA risk detection
# ---------------------------------------------------------------------------

class TestCheckFCARisk:
    def test_triggers_when_ece_high_and_confidence_high(self):
        monitor = CalibrationMonitor([])
        # ECE=0.12 > 0.10 and mean_conf=0.90 > 0.85
        assert monitor.check_fca_risk(ece=0.12, mean_confidence=0.90) is True

    def test_no_trigger_when_ece_below_threshold(self):
        monitor = CalibrationMonitor([])
        assert monitor.check_fca_risk(ece=0.08, mean_confidence=0.90) is False

    def test_no_trigger_when_confidence_below_floor(self):
        monitor = CalibrationMonitor([])
        assert monitor.check_fca_risk(ece=0.15, mean_confidence=0.80) is False

    def test_both_conditions_required(self):
        monitor = CalibrationMonitor([])
        assert monitor.check_fca_risk(ece=0.03, mean_confidence=0.90) is False
        assert monitor.check_fca_risk(ece=0.12, mean_confidence=0.70) is False
        assert monitor.check_fca_risk(ece=0.12, mean_confidence=0.90) is True


# ---------------------------------------------------------------------------
# Full run
# ---------------------------------------------------------------------------

class TestCalibrationRun:
    def test_skips_below_min_outcomes(self):
        outcomes = _make_outcomes(10)  # below MIN_LABELED_OUTCOMES=30
        monitor = CalibrationMonitor(outcomes)
        checkpoint = monitor.run()
        assert checkpoint is None

    def test_returns_checkpoint_above_min_outcomes(self):
        outcomes = _make_outcomes(60)
        monitor = CalibrationMonitor(outcomes)
        checkpoint = monitor.run()
        assert checkpoint is not None
        assert isinstance(checkpoint, CalibrationCheckpoint)

    def test_checkpoint_has_required_fields(self):
        outcomes = _make_outcomes(60)
        monitor = CalibrationMonitor(outcomes)
        checkpoint = monitor.run()
        assert checkpoint is not None
        assert checkpoint.checkpoint_id is not None
        assert 0.0 <= checkpoint.ece <= 1.0
        assert checkpoint.n_labeled_outcomes == 60

    def test_checkpoint_snowflake_row_serializable(self):
        outcomes = _make_outcomes(60)
        monitor = CalibrationMonitor(outcomes)
        checkpoint = monitor.run()
        assert checkpoint is not None
        row = checkpoint.to_snowflake_row()
        required = {"CHECKPOINT_ID", "COMPUTED_AT", "PLATT_A", "PLATT_B",
                    "ECE", "N_LABELED_OUTCOMES", "FCA_RISK_FLAG", "MEAN_CONFIDENCE"}
        assert required.issubset(row.keys())

    def test_fca_risk_flag_true_when_conditions_met(self):
        # Construct outcomes that produce high mean confidence + high ECE
        # All high confidence, half denied — ECE will be significant
        outcomes = (
            [{"confidence": 0.92, "is_denied": True}] * 20 +
            [{"confidence": 0.92, "is_denied": False}] * 30
        )
        monitor = CalibrationMonitor(outcomes)
        checkpoint = monitor.run()
        assert checkpoint is not None
        # ECE ~ |0.92 - 0.40| = 0.52 > 0.10, mean_conf = 0.92 > 0.85
        assert checkpoint.fca_risk_flag is True
