# Copyright (c) 2026 Eric Grynspan. All rights reserved.
"""
Phase 2 Component 3 — Confidence Calibration Monitor.

Platt scaling: fits a logistic regression on labeled (confidence, is_denied)
pairs. Computes Expected Calibration Error (ECE) per N bins. Saves Platt
coefficients + ECE to RAW.CALIBRATION_CHECKPOINTS each nightly run.

FCA-adjacent risk trigger:
  ECE > 0.10 AND mean_confidence > 0.85 → CALIBRATION_ALERT structlog event.
  This combination signals systematic overconfidence — the model assigns high
  confidence to claims it gets wrong at a disproportionate rate. Under FCA,
  continuing to deploy a model in this state can constitute "reckless disregard"
  (scienter element). The alert requires ops acknowledgment before the next
  scoring run.

ADR-008: Platt chosen over isotonic regression (overfits at low N) and no-
calibration (FCA liability risk when scores are systematically miscalibrated).
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import numpy as np
import structlog
from sklearn.linear_model import LogisticRegression
from scipy.special import expit  # sigmoid

from src.config.settings import CalibrationConfig

log = structlog.get_logger(__name__)


@dataclass
class CalibrationCheckpoint:
    checkpoint_id: str
    computed_at: datetime
    platt_a: float
    platt_b: float
    ece: float
    n_labeled_outcomes: int
    fca_risk_flag: bool
    mean_confidence: float

    def to_snowflake_row(self) -> dict:
        return {
            "CHECKPOINT_ID": self.checkpoint_id,
            "COMPUTED_AT": self.computed_at.isoformat(),
            "PLATT_A": self.platt_a,
            "PLATT_B": self.platt_b,
            "ECE": self.ece,
            "N_LABELED_OUTCOMES": self.n_labeled_outcomes,
            "FCA_RISK_FLAG": self.fca_risk_flag,
            "MEAN_CONFIDENCE": self.mean_confidence,
        }


class CalibrationMonitor:
    """
    Nightly calibration job. Reads labeled (confidence, is_denied) pairs from
    the outcome store, fits Platt scaling, computes ECE, and saves a checkpoint.

    outcome_store: list of dicts with keys 'confidence' (float) and 'is_denied' (bool).
    In production this is loaded from a JOIN of RAW.LLM_SCORING_RESULTS and
    RAW.ADJUDICATION_OUTCOMES.
    """

    def __init__(self, outcome_store: list[dict]) -> None:
        self._outcomes = outcome_store

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run(self) -> Optional[CalibrationCheckpoint]:
        """
        Full calibration run. Returns None if insufficient labeled data.
        Emits CALIBRATION_ALERT when FCA risk conditions are met.
        """
        if len(self._outcomes) < CalibrationConfig.MIN_LABELED_OUTCOMES:
            log.info(
                "calibration.skipped",
                n_outcomes=len(self._outcomes),
                min_required=CalibrationConfig.MIN_LABELED_OUTCOMES,
            )
            return None

        confidences = np.array([o["confidence"] for o in self._outcomes], dtype=float)
        labels = np.array([int(o["is_denied"]) for o in self._outcomes], dtype=int)

        ece = self.compute_ece(confidences, labels)
        platt_a, platt_b = self.fit_platt(confidences, labels)
        mean_conf = float(np.mean(confidences))
        fca_risk = self.check_fca_risk(ece, mean_conf)

        if fca_risk:
            log.warning(
                "CALIBRATION_ALERT",
                ece=round(ece, 4),
                mean_confidence=round(mean_conf, 4),
                fca_alert_ece=CalibrationConfig.FCA_ALERT_ECE,
                fca_alert_confidence_floor=CalibrationConfig.FCA_ALERT_CONFIDENCE_FLOOR,
                note=(
                    "Model is systematically overconfident. "
                    "Continuing to deploy without recalibration may constitute "
                    "reckless disregard under FCA. Ops acknowledgment required."
                ),
            )
        elif ece > CalibrationConfig.ECE_THRESHOLD:
            log.info("calibration.recalibrating", ece=round(ece, 4))

        checkpoint = CalibrationCheckpoint(
            checkpoint_id=str(uuid.uuid4()),
            computed_at=datetime.now(timezone.utc),
            platt_a=platt_a,
            platt_b=platt_b,
            ece=ece,
            n_labeled_outcomes=len(self._outcomes),
            fca_risk_flag=fca_risk,
            mean_confidence=mean_conf,
        )
        log.info(
            "calibration.checkpoint_saved",
            ece=round(ece, 4),
            platt_a=round(platt_a, 4),
            platt_b=round(platt_b, 4),
            fca_risk=fca_risk,
        )
        return checkpoint

    def compute_ece(
        self,
        confidences: np.ndarray,
        labels: np.ndarray,
        n_bins: Optional[int] = None,
    ) -> float:
        """
        Expected Calibration Error: weighted mean absolute difference between
        bin-mean confidence and bin-actual denial rate.

        ECE = Σ_b (n_b / n_total) × |mean_confidence_b - actual_rate_b|
        """
        n_bins = n_bins or CalibrationConfig.N_BINS
        n_total = len(confidences)
        if n_total == 0:
            return 0.0

        bin_edges = np.linspace(0.0, 1.0, n_bins + 1)
        ece = 0.0

        for low, high in zip(bin_edges[:-1], bin_edges[1:]):
            in_bin = (confidences >= low) & (confidences < high)
            n_bin = int(np.sum(in_bin))
            if n_bin == 0:
                continue
            mean_conf = float(np.mean(confidences[in_bin]))
            actual_rate = float(np.mean(labels[in_bin]))
            ece += (n_bin / n_total) * abs(mean_conf - actual_rate)

        return round(ece, 6)

    def fit_platt(
        self,
        confidences: np.ndarray,
        labels: np.ndarray,
    ) -> tuple[float, float]:
        """
        Fit Platt scaling: logistic regression on (confidence → is_denied).
        Returns (a, b) coefficients for: P(deny) = sigmoid(a * confidence + b).
        """
        X = confidences.reshape(-1, 1)
        clf = LogisticRegression(max_iter=1000, solver="lbfgs")
        clf.fit(X, labels)
        a = float(clf.coef_[0][0])
        b = float(clf.intercept_[0])
        return a, b

    def calibrate_confidence(self, raw_confidence: float, a: float, b: float) -> float:
        """Apply Platt calibration to a single raw confidence score."""
        return float(expit(a * raw_confidence + b))

    def check_fca_risk(self, ece: float, mean_confidence: float) -> bool:
        """
        True when ECE > 0.10 AND mean confidence > 0.85.
        Indicates systematic overconfidence — FCA reckless disregard risk.
        """
        return (
            ece > CalibrationConfig.FCA_ALERT_ECE
            and mean_confidence > CalibrationConfig.FCA_ALERT_CONFIDENCE_FLOOR
        )
