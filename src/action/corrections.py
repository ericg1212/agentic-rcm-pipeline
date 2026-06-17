"""
Layer 3 — Auto-correction handlers.

INTERVIEW-CRITICAL: own this cold.

Only modifier additions are auto-correctable in v1. The decision criterion:
  - Adding a missing bypass modifier is a paperwork fix with zero financial impact.
    It does not change the billed amount — it only clarifies that two procedures
    are distinct services, which they already are (per the LLM's verification).
  - Unit reductions DO change the billed amount. That requires human sign-off
    for compliance — auto-correcting units creates FCA exposure.

The governing_rule_cited field on every correction is the FCA defense:
"The system only acts where it can cite the exact rule it is applying."

v1 correction types: add_missing_modifier only.
v2 candidates (require separate Critic Protocol session): unit normalization,
  place-of-service fix, duplicate service detection.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass

import structlog

from src.consumer.ncci_gate import GateDecision, ViolationType

log = structlog.get_logger(__name__)

# The only modifier added by auto-correct in v1.
# 59 (Distinct Procedural Service) is the canonical bypass — XE/XS/XP/XU are
# more specific alternatives; v1 uses 59 universally and notes the specificity
# upgrade as a Phase 2 refinement.
AUTO_CORRECT_MODIFIER = "59"


@dataclass
class CorrectionResult:
    claim_id: str
    correction_type: str           # "add_missing_modifier" | future types
    field_corrected: str           # claim field that was modified
    before_value: object           # original value (for audit + reversibility)
    after_value: object            # corrected value
    governing_rule_cited: str      # exact NCCI/LCD rule applied
    corrected_claim: dict          # the modified claim dict


def attempt_modifier_correction(
    claim: dict,
    gate_decision: GateDecision,
) -> CorrectionResult | None:
    """
    Attempt to add a missing NCCI bypass modifier.

    Safe when:
      - Gate found exactly a PTP_BYPASS_UNVERIFIED violation (modifier_indicator=1,
        no bypass modifier present)
      - No modifier_indicator=0 (hard ban) violations coexist
      - Claim does not already have a bypass modifier (should be impossible given
        gate routing logic, but verified defensively)

    Returns None if the correction is not applicable or not safe.
    """
    if not gate_decision.violations:
        return None

    # Reject if any hard-ban violation exists (modifier_indicator=0 → never correctable)
    hard_bans = [
        v for v in gate_decision.violations
        if v.violation_type == ViolationType.PTP_NO_BYPASS
        and v.modifier_indicator == "0"
    ]
    if hard_bans:
        log.debug(
            "correction_blocked_hard_ban",
            claim_id=claim.get("claim_id"),
            codes=[(v.code, v.col2_code) for v in hard_bans],
        )
        return None

    # Find the unverified bypass violations (modifier_indicator=1, no modifier present)
    unverified = [
        v for v in gate_decision.violations
        if v.violation_type == ViolationType.PTP_BYPASS_UNVERIFIED
    ]
    if not unverified:
        return None

    # Sanity check: claim shouldn't already have a bypass modifier if gate said AMBIGUOUS
    existing_modifiers: list[str] = list(claim.get("modifiers", []))
    bypass_set = {"59", "XE", "XS", "XP", "XU"}
    if bypass_set & set(existing_modifiers):
        log.debug("correction_skipped_modifier_present", claim_id=claim.get("claim_id"))
        return None

    # Build governing rule citation from the first unverified violation
    v = unverified[0]
    governing_rule = (
        f"NCCI PTP edit {v.code}-{v.col2_code}, modifier_indicator=1: "
        f"separate billing is valid with bypass modifier when services are clinically distinct. "
        f"LLM confirmed procedures are distinct services. Adding modifier {AUTO_CORRECT_MODIFIER}."
    )

    corrected = copy.deepcopy(claim)
    new_modifiers = existing_modifiers + [AUTO_CORRECT_MODIFIER]
    corrected["modifiers"] = new_modifiers

    log.info(
        "correction_applied",
        claim_id=claim.get("claim_id"),
        correction_type="add_missing_modifier",
        modifier_added=AUTO_CORRECT_MODIFIER,
        violations_addressed=[f"{v.code}-{v.col2_code}" for v in unverified],
    )

    return CorrectionResult(
        claim_id=claim.get("claim_id", ""),
        correction_type="add_missing_modifier",
        field_corrected="modifiers",
        before_value=existing_modifiers,
        after_value=new_modifiers,
        governing_rule_cited=governing_rule,
        corrected_claim=corrected,
    )
