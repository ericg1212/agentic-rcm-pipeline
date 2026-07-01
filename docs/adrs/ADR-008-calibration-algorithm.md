# ADR-008: Confidence Calibration Algorithm — Platt Scaling

**Status:** Accepted  
**Date:** 2026-06-29 (Phase 2 build — Confidence Calibration Curve)  
**Decider:** Eric Grynspan

---

## Decision

LLM confidence scores are calibrated with Platt scaling — a logistic regression fit on labeled `(confidence, is_denied)` pairs from the outcome store:

```
P(deny | raw_score) = sigmoid(a × raw_score + b)
```

Calibration quality is measured by Expected Calibration Error across decile bins:

```
ECE = Σ (n_bin / n_total) × |mean_confidence_bin − actual_denial_rate_bin|
```

Triggers: ECE > 0.05 recalibrates and checkpoints `(a, b, ECE)` to `RAW.CALIBRATION_CHECKPOINTS`; ECE > 0.10 with mean confidence > 0.85 additionally emits a `CALIBRATION_ALERT` event and sets `fca_risk_flag=True`, requiring ops acknowledgment before the next scoring run.

## Why

Calibration is a legal control, not only a quality metric. Under the FCA, a model that reports confidence 0.92 on claims it gets wrong at a high rate exhibits the "reckless disregard" scienter element — and *continuing to deploy* a model with documented systematic overconfidence is what satisfies it. The two-threshold design separates routine maintenance (recalibrate quietly at ECE > 0.05) from the compliance event (alert loudly and require acknowledgment at ECE > 0.10 + high confidence).

Platt fits the data regime: two parameters, low overfitting risk at the hundreds-of-outcomes volume the feedback loop produces, interpretable coefficients that checkpoint cleanly for the audit trail. Implementation is `sklearn` LogisticRegression on `[[confidence]]` features; the 30-outcome minimum guard matches `LiftCalculator`. Checkpoints append — never overwrite — so calibration history is reconstructible, and the `fct_calibration_curve` dbt mart renders actual-vs-predicted denial rate per decile as a reliability diagram.

| Criterion | Platt scaling | Isotonic regression | No calibration |
|---|---|---|---|
| Labeled data needed | Low (30+) | High (100+ for stability) | None |
| Overfitting risk | Low (2 params) | Medium at low N | N/A |
| Interpretability | High (sigmoid, 2 params) | Low (step function) | N/A |
| FCA audit trail | Checkpointed (a, b, ECE) | Harder to document | None |

## Rejected

| Alternative | Why rejected |
|---|---|
| **Isotonic regression** | More flexible, but at hundreds of outcomes (not millions) the step function overfits noise in individual bins; the logistic form is the right regularizer for this data volume, and its two coefficients document cleanly |
| **No calibration** | A model with ECE > 0.10 and mean confidence > 0.85 left in production is exactly the documented-overconfidence pattern that satisfies FCA scienter — uncalibrated confidence cannot gate autonomous action |
