# Copyright (c) 2026 Eric Grynspan. All rights reserved.
"""
Layer 2 — Noise injection eval harness.

Proves LLM lift over the deterministic NCCI gate on dirty claims.

THE KEY INSIGHT (own this cold):
The NCCI gate is a lookup: it checks procedure-procedure bundling (PTP) and unit
limits (MUE). It has NO visibility into diagnosis codes. A claim with a procedure
that requires a specific diagnosis for medical necessity will PASS the gate cleanly —
the gate has no false-negative detection for diagnosis-procedure mismatch.

The LLM DOES see diagnosis codes. It calls get_lcd_policy(hcpcs_code, diagnosis_code)
and reasons over the coverage note. For a claim where the injected diagnosis clearly
doesn't support the procedure, the LLM flags CO-11 (diagnosis inconsistent with
procedure) or CO-50 (not medically necessary).

Lift = LLM recovery rate on claims the gate passed with confidence.
This is the "why LLM" chart in Streamlit.

DIRTY PATTERNS INJECTED:
  wrong_diagnosis    — replace procedure-matched diagnoses with clearly unrelated codes
                       Gate result: PASS (no NCCI violation, gate is blind to diagnoses)
                       LLM result:  high risk, CO-11 or CO-50, action=hold/flag
"""
from __future__ import annotations

import copy
import random
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import structlog

from src.consumer.ncci_gate import GateDecision, NCCIGate, Route

if TYPE_CHECKING:
    from src.reasoning.scorer import ClaimScorer, ScoringResult

log = structlog.get_logger(__name__)

# Diagnoses that are clearly unrelated to most procedures in the seed pool.
# Chosen to be obviously wrong without being ambiguous:
#   S82.001A — closed displaced fracture of right patella (trauma, very specific)
#   F84.0    — childhood autism (psychiatric, non-medical/surgical)
#   Z41.1    — encounter for cosmetic surgery (cosmetic, not therapeutic)
#   G47.00   — insomnia, unspecified (sleep disorder, broadly unrelated to procedures)
UNRELATED_DIAGNOSES = ["S82.001A", "F84.0", "Z41.1", "G47.00"]

# Procedures that have strict diagnosis requirements (from LCD seed).
# These are the ones where wrong_diagnosis creates a clear false negative.
LCD_RESTRICTED_PROCEDURES = {
    "27447",  # total knee — requires M17/M16
    "29827",  # shoulder repair — requires M75.1/S40
    "43239",  # upper GI endoscopy — requires K21/K29/K92 etc.
    "93000",  # ECG — requires I-codes/R00/R07
    "11042",  # debridement — requires L89/E11.6
    "90837",  # psychotherapy — requires F-code
    "90834",  # psychotherapy — requires F-code
    "20610",  # joint injection — requires M17/M06
}

# Risk score floor for the LLM to "count" as a recovery on a wrong-diagnosis claim
LLM_RECOVERY_RISK_THRESHOLD = 50


@dataclass
class PatternResult:
    n_injected: int = 0
    gate_false_negatives: int = 0    # injected claims that passed the gate
    llm_recoveries: int = 0          # gate false negatives caught by LLM
    lift: float = 0.0


@dataclass
class EvalResult:
    n_clean: int
    n_dirty: int
    gate_false_negatives: int
    llm_recoveries: int
    lift: float
    by_pattern: dict[str, PatternResult] = field(default_factory=dict)

    def summary(self) -> str:
        return (
            f"Eval: {self.n_dirty} dirty / {self.n_clean} clean | "
            f"Gate FN: {self.gate_false_negatives} | "
            f"LLM recovered: {self.llm_recoveries} | "
            f"Lift: {self.lift:.1%}"
        )


def inject_wrong_diagnosis(claim: dict, rng: random.Random | None = None) -> dict | None:
    """
    Inject an unrelated diagnosis into a claim that has an LCD-restricted procedure.

    Returns the modified claim dict, or None if the claim has no LCD-restricted procedure
    (no injection possible for this pattern — skip it).
    """
    procs = claim.get("procedure_codes", [])
    if not any(p in LCD_RESTRICTED_PROCEDURES for p in procs):
        return None

    dirty = copy.deepcopy(claim)
    _rng = rng or random.Random()
    dirty["diagnosis_codes"] = [_rng.choice(UNRELATED_DIAGNOSES)]
    dirty["_injected_pattern"] = "wrong_diagnosis"
    return dirty


def run_noise_injection_eval(
    clean_claims: list[dict],
    ncci_gate: NCCIGate,
    scorer: "ClaimScorer",
    dirty_fraction: float = 0.30,
    seed: int = 42,
) -> EvalResult:
    """
    Inject dirt into a fraction of clean claims, run through gate + LLM, compute lift.

    Args:
        clean_claims:   List of claim dicts that pass the NCCI gate cleanly.
        ncci_gate:      Loaded NCCIGate instance.
        scorer:         Loaded ClaimScorer instance.
        dirty_fraction: Fraction of claims to inject with wrong_diagnosis.
        seed:           RNG seed for reproducibility.

    Returns:
        EvalResult with lift and per-pattern breakdown.
    """
    rng = random.Random(seed)
    pattern_result = PatternResult()
    gate_fn = 0
    llm_recovered = 0

    for claim in clean_claims:
        if rng.random() >= dirty_fraction:
            continue

        dirty = inject_wrong_diagnosis(claim, rng)
        if dirty is None:
            continue

        pattern_result.n_injected += 1

        gate_decision: GateDecision = ncci_gate.evaluate(dirty)
        if gate_decision.route != Route.PASS:
            # Gate caught it — not a false negative, not useful for the eval
            continue

        pattern_result.gate_false_negatives += 1
        gate_fn += 1

        score: ScoringResult = scorer.score(dirty, gate_decision)

        if score.risk_score >= LLM_RECOVERY_RISK_THRESHOLD:
            pattern_result.llm_recoveries += 1
            llm_recovered += 1
            log.debug(
                "eval_llm_recovery",
                claim_id=dirty.get("claim_id"),
                risk_score=score.risk_score,
                predicted_code=score.predicted_denial_code,
                action=score.recommended_action,
            )
        else:
            log.debug(
                "eval_llm_miss",
                claim_id=dirty.get("claim_id"),
                risk_score=score.risk_score,
                procedure_codes=dirty.get("procedure_codes"),
                bad_diagnoses=dirty.get("diagnosis_codes"),
            )

    pattern_result.lift = (
        pattern_result.llm_recoveries / pattern_result.gate_false_negatives
        if pattern_result.gate_false_negatives > 0 else 0.0
    )

    overall_lift = llm_recovered / gate_fn if gate_fn > 0 else 0.0

    result = EvalResult(
        n_clean=len(clean_claims),
        n_dirty=pattern_result.n_injected,
        gate_false_negatives=gate_fn,
        llm_recoveries=llm_recovered,
        lift=overall_lift,
        by_pattern={"wrong_diagnosis": pattern_result},
    )

    log.info("eval_complete", **{
        "summary": result.summary(),
        "lift": overall_lift,
    })
    return result
