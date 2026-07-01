# ADR-011: Holdout Randomization Unit — Provider-Level Cluster Assignment

**Status:** Accepted
**Date:** 2026-07-01 (architecture review session)
**Decider:** Eric Grynspan

---

## Decision

- The holdout control arm is randomized at the **provider (NPI) level**, not the claim level: every claim from a holdout provider is control, always
- Assignment is **deterministic rank-based sampling of the provider roster**: NPIs ranked by SHA-256, lowest `round(fraction × N)` form the holdout set — stable across restarts, replays, and generator instances
- NPIs outside the known roster fall back to a deterministic hash threshold
- Configurable via `HOLDOUT_UNIT` (`provider` default, `claim` legacy)

## Why

**Claim-level randomization contaminates the control arm.** The intervention operates on billing workflows: corrections and escalation drafts change how a provider's staff submit *subsequent* claims. Splitting one provider's claims across both arms lets the treatment leak into the control — the measured lift understates the true effect. Cluster randomization at the unit where interference happens (the provider) is the standard fix.

**Deterministic beats random-at-generation.** A per-claim RNG draw stamps assignment in-band at produce time, so a replayed or regenerated claim can flip arms. Hash-rank assignment is a pure function of the NPI — the same provider is in the same arm in every process, on every restart, with no assignment state to store or sync.

**Rank-based beats threshold-based on small rosters.** A raw `hash(NPI) < fraction` threshold realizes 25% at a 10% target on the 20-provider dev roster — hash buckets only converge to the target at population scale. Ranking the known roster and taking the lowest `round(fraction × N)` hits the fraction exactly at any roster size, which is also the realistic production framing: a provider org randomizes its roster, not an open NPI space.

**Cost acknowledged: clustering reduces effective sample size.** With few clusters, between-provider variance inflates the lift confidence interval (design effect) — lift inference must treat the provider, not the claim, as the independent unit, and the `MIN_POWER_N` guard remains necessary. This is the honest price of an uncontaminated control arm.

## Rejected

| Alternative | Why rejected |
|---|---|
| **Claim-level Bernoulli (previous behavior)** | Within-provider interference: intervention feedback changes the provider's future submissions in both arms. Biases lift toward zero. Kept available as `HOLDOUT_UNIT=claim` for A/A testing. |
| **Hash-threshold on open NPI space** | Fraction drift on small rosters (25% realized vs 10% target on 20 providers) — silently mis-sizes the control arm and the lift power calculation. Retained only as the fallback for NPIs outside the roster. |
| **Time-based alternation (holdout hours/days)** | Confounds arm assignment with temporal effects — payer rule changes, seasonal case mix, staffing patterns all vary with time. |
| **Payer-level randomization** | Too few clusters (single-digit payers) and payers differ structurally in denial behavior — arm imbalance would dominate the signal. |
