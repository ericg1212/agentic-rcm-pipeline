# ADR-008: Confidence Calibration Algorithm — Platt Scaling

**Status:** Accepted  
**Date:** 2026-06-29  
**Decider:** Eric Grynspan  
**Component:** Phase 2 — Confidence Calibration Curve  

---

## Context

The LLM-assigned `confidence` scores in `RAW.LLM_SCORING_RESULTS` may be systematically over- or under-confident relative to actual denial rates. This matters beyond accuracy: under FCA, a model that reports confidence=0.92 on claims it gets wrong at a high rate exhibits potential "reckless disregard" — the scienter element. Calibration is a legal risk mitigation measure, not only a quality metric.

Three approaches were evaluated.

---

## Options

### Option A: Platt Scaling (Chosen)

Fit a logistic regression on labeled (confidence, is_denied) pairs from the outcome store:
```
P(deny | raw_score) = sigmoid(a × raw_score + b)
```
Two parameters (a, b). Interpretable. Appropriate for hundreds to low thousands of labeled outcomes.

Expected Calibration Error (ECE) is computed across N bins:
```
ECE = Σ (n_bin / n_total) × |mean_confidence_bin - actual_denial_rate_bin|
```

### Option B: Isotonic Regression

Non-parametric step-function calibration. Monotonically increasing. More flexible than Platt.

### Option C: No Calibration

Trust raw LLM confidence scores as-is. Accept that calibration is a future concern.

---

## Decision

**Option A: Platt Scaling.**

---

## Rationale

| Criterion | Platt Scaling | Isotonic Regression | No Calibration |
|---|---|---|---|
| Labeled data requirement | Low (30+ samples) | High (100+ for stability) | None |
| Overfitting risk | Low (2 params) | Medium (can overfit at low N) | N/A |
| Interpretability | High (2 params, sigmoid curve) | Low (opaque step function) | N/A |
| FCA audit trail | Checkpoint table (a, b, ECE) | Harder to document | None |
| Implementation complexity | Low (scipy.special.expit) | Low | None |

Isotonic regression rejected: more flexible, but at the data volumes expected in Phase 2 (hundreds of outcomes, not millions), isotonic regression can overfit to noise in individual bins. Platt scaling's regularization via logistic form is appropriate.

No calibration rejected: a model with ECE > 0.10 and mean confidence > 0.85 is a litigation liability. The FCA scienter element ("knowingly or in reckless disregard") is satisfied when the organization continues deploying a model with documented systematic overconfidence. The `check_fca_risk()` gate emits a `CALIBRATION_ALERT` structured log event rather than silently logging — it must surface to operations.

---

## Calibration Triggers

| ECE Threshold | Action |
|---|---|
| ECE > 0.05 | Recalibrate Platt coefficients; save checkpoint to `RAW.CALIBRATION_CHECKPOINTS` |
| ECE > 0.10 AND mean_confidence > 0.85 | Emit `CALIBRATION_ALERT` structlog event; set `fca_risk_flag=True` in checkpoint; requires ops acknowledgment before next scoring run |

---

## Consequences

- `CalibrationMonitor.fit_platt()` uses `sklearn.linear_model.LogisticRegression` with labels `{0: not denied, 1: denied}` and features `[[confidence_score]]`.
- Minimum labeled outcomes: 30 (same guard as `LiftCalculator`). Below this threshold, calibration is skipped and logged.
- Platt (a, b) coefficients are versioned in `RAW.CALIBRATION_CHECKPOINTS` — each nightly run produces a new checkpoint row, never overwriting historical data.
- `fct_calibration_curve` dbt mart computes actual vs predicted denial rate per decile — directly renderable as a reliability diagram in Power BI.
