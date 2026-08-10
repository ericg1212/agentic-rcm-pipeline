# ADR-005: Feedback & Measurement — Drift Windows, Kill-Switch Threshold, Provider-Level Holdout

**Status:** Accepted
**Date:** 2026-06-17 (drift) · 2026-07-01 (holdout) · consolidated 2026-07-06
**Decider:** Eric Grynspan
**Consolidates:** ADR-005 (drift window sizing) + ADR-011 (holdout randomization unit)

---

## Decision

- **Drift:** check every 50 adjudication outcomes (rolling window) against an **anchored** baseline — the *first* 100 outcomes recorded, env-configurable, not a sliding window. The kill-switch fires only when the difference clears **both** gates: statistically significant by a two-proportion z-test (`DRIFT_ALPHA`, default 0.01) **and** material (`DRIFT_THRESHOLD`, default >20% relative change). Cold start returns no action until both windows are filled. Lift snapshots every 500 outcomes.
- **Holdout:** the 10% control arm is randomized at the **provider (NPI) level** — every claim from a holdout provider is control, always. Assignment is deterministic rank-based sampling: NPIs ranked by SHA-256, lowest `round(fraction × N)` form the holdout set — stable across restarts, replays, and generator instances. Hash-threshold fallback for NPIs outside the roster; `HOLDOUT_UNIT=claim` retained for A/A testing.

## Why

**Significance and materiality answer different questions, and both are required.** A relative-change threshold alone asks only "is this difference big?" — never "given how much data I have, is it real?" Those are separable, and conflating them is what made the original design unsound (see the measured-fix section below). The z-test is the evidence gate; the 20% floor is the operational one. Keeping both means window sizes stop being magic numbers: the test widens its own standard error when the sample is small, so an underpowered check simply cannot reach significance and no separate minimum-n guard is needed.

**`DRIFT_ALPHA` is 0.01, not the conventional 0.05.** The check runs every 50 outcomes, so the multiple-comparison surface is large, and the kill-switch latches until a human clears it. At 0.05 a false trip lands roughly every 20 checks. Cost owned: the alpha is not corrected for the number of checks performed, so false-positive risk still accumulates over a long enough run — a sequential test (e.g. SPRT) is the correct answer at that horizon.

**The baseline is anchored, not rolling.** It is the first `baseline_window` outcomes and never advances, which is right for measuring movement away from a known-good reference period and wrong once legitimate seasonal movement accumulates. A continuously rolling baseline is *not* the fix — it drifts along with slow degradation and never fires (the boiling-frog failure). The production design re-anchors on a known event — payer rule version change, model or prompt version bump — so the reference period always corresponds to a defined system state. Not built.

**500-outcome lift snapshots** give stable per-arm estimates (`MIN_POWER_N=30` per arm).

## Measured fix — the threshold-only detector was unsound (2026-08-10)

The original trigger was `abs(relative_change) > 0.20` with no significance test. Arithmetic at the shipped window sizes, against a realistic 12% Medicare FFS denial rate:

| Quantity | Value |
|---|---|
| Standard error, baseline (n=100) | ±3.3 points |
| Standard error, rolling (n=50) | ±4.6 points |
| **Standard error of the difference** | **±5.6 points** |
| **Trigger (20% relative on a 12% base)** | **2.4 points** |

The noise floor sat at more than twice the trigger. On a check where nothing had changed, the probability of exceeding the threshold was roughly **two in three** — and because `activate()` is idempotent and recovery is manual, the switch would have latched on one of the first checks and stayed latched. This was a defect in the detector's core arithmetic, not a scope decision; it does not scale away, and the "v1 single-process by scope" defense that covers the process-local kill-switch and dedup set does not apply to it.

Fix: the 20% number is retained but demoted from *the trigger* to *the materiality floor*, and a pooled two-proportion z-test was added as the evidence gate. `DriftAlert` now carries `z_statistic`, `p_value`, `is_significant`, `is_material`, and a `noise_floor` property (the standard error of the difference, in rate points) so an underpowered check is visible in the log rather than silent. Implemented with `math.erfc` — the two-tailed normal p-value is exactly `erfc(|z|/√2)` — so no scipy dependency enters Layer 4.

Verification: 275 → 284 tests, zero regressions. All five pre-existing drift tests pass unchanged. The regression test (`test_drift_material_but_within_noise_does_not_fire`) pins the exact case the old logic got wrong — a 12% baseline against an 18% rolling rate is a +50% relative change that clears the materiality floor, but at p≈0.32 sits well inside sampling noise, and the observed 6-point gap is smaller than the check's own ~6-point noise floor.

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
