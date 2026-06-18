# ADR-005: Feedback Loop — Drift Window Sizing and Kill-Switch Threshold

**Status:** Accepted  
**Date:** 2026-06-17 (Layer 4 build session)  
**Decider:** Eric Grynspan

---

## Decision

- **Drift check cadence:** every 50 adjudication outcomes
- **Lift snapshot cadence:** every 500 adjudication outcomes
- **Kill-switch threshold:** >20% relative drift vs. rolling baseline denial rate
- **Rolling window:** configurable via `DRIFT_ROLLING_WINDOW` env var (default: last 100 outcomes)
- **Cold start:** drift monitor returns `None` (no action) until minimum power threshold is met

## Why

**50-outcome drift check:** Balances responsiveness against noise. At a realistic Medicare FFS denial rate of ~15%, a 50-outcome window contains ~7–8 denials — enough signal to detect a meaningful shift without reacting to a single bad batch. Checking every outcome would trigger false positives on normal variance; checking every 500 would miss a bad model update for hours.

**500-outcome lift snapshot:** Lift calculation (holdout vs. intervention denial rate) requires statistical power. With a 10% holdout, 500 outcomes = 50 holdout records and 450 intervention records. MIN_POWER_N=30 per arm provides a reasonable lower bound; 500 total provides stable estimates. Lift is a strategic metric (is the system working?), not an operational alert — it doesn't need 50-outcome cadence.

**20% relative drift threshold:** Derived from the noise injection eval baseline. A 20% relative increase (e.g., 15% → 18% denial rate) is operationally significant — it suggests either a payer rule change, a model calibration failure, or a data distribution shift. Below 20%, variance is consistent with normal weekly fluctuation. Above 20%, the cost of continued autonomous action exceeds the cost of falling back to flag-only mode.

**Configurable via env vars:** `DRIFT_BASELINE`, `DRIFT_ROLLING_WINDOW`, `DRIFT_THRESHOLD` are all injectable at runtime. Payers with higher baseline denial rates need different thresholds. The default values are calibrated for Medicare FFS, not commercial payers.

## Rejected

| Alternative | Why rejected |
|---|---|
| **Continuous drift check (every outcome)** | Excessive computation overhead; high false-positive rate on normal variance. Every individual outcome can shift a rolling mean — not a meaningful signal. |
| **10-outcome drift window** | Too noisy. With ~15% denial rate, 10 outcomes ≈ 1–2 denials. Single-outcome fluctuation would trigger false kills. |
| **Fixed 10% threshold** | Too sensitive for Medicare FFS variance. Produces alert fatigue; kill-switch becomes unreliable if it fires on routine weekly fluctuation. |
| **Fixed 30% threshold** | Too permissive. A model consistently miscorrecting claims in one direction could cause significant financial harm before triggering. 30% relative drift at 15% baseline = 19.5% denial rate — that is a material degradation. |
| **Nightly batch reconciliation** | Too slow. If the model starts degrading at 9am, nightly batch doesn't catch it until midnight. Pre-submission pipeline requires near-real-time feedback, not next-day reporting. |
| **No kill-switch** | Not viable for FCA compliance. An autonomous system with no self-limiting mechanism is indefensible if a model failure causes systematic incorrect corrections. The kill-switch is the compliance circuit breaker. |
