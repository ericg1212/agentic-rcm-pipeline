# ADR-004: Confidence-Gated Autonomy — 3-Condition Gate + Platt Calibration

**Status:** Accepted
**Date:** 2026-06-17 (gate) · 2026-06-29 (calibration) · consolidated 2026-07-06
**Decider:** Eric Grynspan
**Consolidates:** ADR-004 (action routing thresholds) + ADR-008 (calibration algorithm)

---

## Decision

Auto-correct requires all three conditions simultaneously; any single failure routes the claim to `flag`:

1. `recommended_action == "auto_correct"` — the LLM must recommend the action, not just score risk
2. Calibrated `confidence >= 0.92`
3. `submitted_charge <= 500.00` — high-dollar corrections require human review

Confidence is calibrated with **Platt scaling** — a logistic regression fit on labeled `(confidence, is_denied)` outcome pairs — with quality measured by Expected Calibration Error across decile bins. ECE > 0.05 recalibrates and checkpoints `(a, b, ECE)` to `RAW.CALIBRATION_CHECKPOINTS`; ECE > 0.10 with mean confidence > 0.85 additionally emits `CALIBRATION_ALERT` and sets `fca_risk_flag=True`, requiring ops acknowledgment before the next scoring run.

## Why

The three conditions map one-to-one onto FCA liability elements: **falsity** is addressed by grounding every correction in a cited rule (`governing_rule_cited` required); **scienter** by the confidence floor and the human-review path for anything uncertain; **exposure** by the dollar ceiling.

Calibration is what makes the 0.92 floor meaningful — and a legal control in itself: continuing to deploy a model with documented systematic overconfidence is what satisfies the "reckless disregard" scienter standard. Platt fits the data regime: two interpretable parameters, low overfitting risk at hundreds of outcomes, coefficients that checkpoint cleanly for the audit trail.

## Rejected

| Alternative | Why rejected |
|---|---|
| **Confidence-only gate** | Doesn't protect high-dollar claims — a $10,000 claim at 0.95 confidence would auto-correct without review |
| **Action-only gate** | Ignores model uncertainty — a 0.51-confidence recommendation triggers the same action as a 0.99 one |
| **Charge-only threshold** | Protects on value but not accuracy — a low-confidence correction on a $499 claim still auto-corrects |
| **LLM-determined routing without a gate** | The LLM deciding its own action threshold is not a defensible FCA position |
| **Isotonic regression** | The step function overfits noise at hundreds of outcomes; the logistic form is the right regularizer and its two coefficients document cleanly |
| **No calibration** | Uncalibrated confidence cannot gate autonomous action — documented overconfidence is exactly the FCA scienter pattern |
