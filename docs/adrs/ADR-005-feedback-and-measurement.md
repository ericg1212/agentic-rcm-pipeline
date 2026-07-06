# ADR-005: Feedback & Measurement — Drift Windows, Kill-Switch Threshold, Provider-Level Holdout

**Status:** Accepted
**Date:** 2026-06-17 (drift) · 2026-07-01 (holdout) · consolidated 2026-07-06
**Decider:** Eric Grynspan
**Consolidates:** ADR-005 (drift window sizing) + ADR-011 (holdout randomization unit)

---

## Decision

- **Drift:** check every 50 adjudication outcomes against a rolling baseline (default: last 100, env-configurable); **>20% relative drift fires the kill-switch**; cold start returns no action until minimum power is met. Lift snapshots every 500 outcomes.
- **Holdout:** the 10% control arm is randomized at the **provider (NPI) level** — every claim from a holdout provider is control, always. Assignment is deterministic rank-based sampling: NPIs ranked by SHA-256, lowest `round(fraction × N)` form the holdout set — stable across restarts, replays, and generator instances. Hash-threshold fallback for NPIs outside the roster; `HOLDOUT_UNIT=claim` retained for A/A testing.

## Why

**50-outcome windows balance signal against noise.** At a ~15% Medicare FFS denial rate that's ~7–8 denials per window — enough to detect a real shift without reacting to one bad batch. **>20% relative drift is operationally significant** (payer rule change, calibration failure, or distribution shift); below that is normal weekly variance. 500-outcome lift snapshots give stable per-arm estimates (`MIN_POWER_N=30` per arm).

**Claim-level randomization contaminates the control arm.** The intervention changes how a provider's staff submit *subsequent* claims, so splitting one provider across both arms lets treatment leak into control and biases lift toward zero. Cluster randomization at the interference unit — the provider — is the standard fix. Deterministic hash-rank assignment is a pure function of the NPI: no assignment state to store or sync. Cost owned: clustering inflates the lift confidence interval (design effect) — the provider, not the claim, is the independent unit.

## Rejected

| Alternative | Why rejected |
|---|---|
| **Continuous drift check (every outcome)** | High false-positive rate — every single outcome shifts a rolling mean |
| **Fixed 10% / 30% thresholds** | 10% fires on routine weekly fluctuation (alert fatigue); 30% permits material degradation before triggering |
| **Nightly batch reconciliation** | A model degrading at 9am isn't caught until midnight — pre-submission requires near-real-time feedback |
| **No kill-switch** | An autonomous system with no self-limiting mechanism is indefensible under FCA |
| **Claim-level Bernoulli holdout** | Within-provider interference biases measured lift toward zero |
| **Time-based or payer-level randomization** | Time confounds arm assignment with seasonal/staffing effects; payers are too few and too structurally different |
