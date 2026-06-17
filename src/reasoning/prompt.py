"""
Layer 2 — System prompt and claim message builder for the Claude reasoning core.

PROMPT_VERSION is stamped on every scoring result for full reproducibility:
  input_hash + model_id + prompt_version + code_version = reproducible output.

Bump PROMPT_VERSION on any change that would affect scoring outcomes.
"""
from __future__ import annotations

import json

from src.consumer.ncci_gate import GateDecision, Route

PROMPT_VERSION = "v1.0.0"

SYSTEM_PROMPT = """\
You are an autonomous RCM (Revenue Cycle Management) claim scoring agent for a Medicare \
pre-submission prevention pipeline.

Your role is to assess each claim's denial risk before it is submitted to the payer. \
You have access to four lookup tools that retrieve real Medicare policy context. \
Use them when you need to verify a specific rule — do not guess policy from general knowledge.

TOOL USAGE PROTOCOL:
- lookup_ncci_edit: when you need to verify whether two procedure codes have a PTP bundling \
relationship or modifier bypass rule
- get_lcd_policy: when the procedure-diagnosis combination may not meet medical necessity under \
an LCD or NCD
- check_modifier: when a modifier is present and you need to validate its appropriateness for \
the billed procedure
- get_payer_history: when prior denial history for this payer and procedure is relevant context

You MUST call submit_scoring_decision to finalize your assessment. Do not stop without calling it.

SCORING GUIDELINES:
- risk_score 0-100: 0 = certain clean claim, 100 = certain denial
- confidence 0.0-1.0: your certainty in the risk_score
- predicted_denial_code: the most likely CARC code if denied; null if risk_score < 30
- driving_fields: the specific claim fields most responsible for the risk
- recommended_action:
    auto_correct — safe, low-risk fix (e.g., add a missing modifier); high confidence required
    flag — needs billing team review before submission
    hold — high risk; do not submit without intervention
    escalate — complex case requiring expert review; you will draft the correction rationale
- rationale: 1-3 plain-English sentences for the billing team

Common denial patterns to check:
- Bundled codes (NCCI PTP): two codes that cannot be billed together without a valid bypass modifier
- Modifier bypass validity: bypass modifier present but may not be clinically appropriate
- Diagnosis-procedure mismatch (CO-11, CO-50): procedure not supported by submitted diagnoses
- MUE exceeded (CO-97): units billed exceed the medically unlikely edit limit
- Provider specialty mismatch (CO-170): procedure not typically performed by this provider type
"""


def build_claim_message(claim: dict, gate: GateDecision) -> str:
    """Build the user message presenting a claim to the reasoning model."""
    procedure_codes = claim.get("procedure_codes", [])
    diagnosis_codes = claim.get("diagnosis_codes", [])
    modifiers = claim.get("modifiers", [])

    violations_text = "none detected"
    if gate.violations:
        violations_text = json.dumps([v.to_dict() for v in gate.violations], indent=2)

    return (
        f"CLAIM TO SCORE:\n"
        f"  claim_id:         {claim.get('claim_id', 'unknown')}\n"
        f"  service_date:     {claim.get('service_date', '')}\n"
        f"  provider_npi:     {claim.get('provider_npi', '')}\n"
        f"  payer_id:         {claim.get('payer_id', '')}\n"
        f"  claim_type:       {claim.get('claim_type', '')}\n"
        f"  place_of_service: {claim.get('place_of_service', '')}\n"
        f"  procedure_codes:  {procedure_codes}\n"
        f"  diagnosis_codes:  {diagnosis_codes}\n"
        f"  modifiers:        {modifiers}\n"
        f"  units:            {claim.get('units', 1)}\n"
        f"  submitted_charge: ${claim.get('submitted_charge', '0.00')}\n"
        f"\n"
        f"NCCI GATE DECISION:\n"
        f"  route:               {gate.route.value}\n"
        f"  gate_risk_score:     {gate.risk_score:.2f}\n"
        f"  deterministic_carc:  {gate.deterministic_carc or 'none'}\n"
        f"  violations:          {violations_text}\n"
        f"\n"
        f"Assess this claim's denial risk. Use lookup tools as needed, then call "
        f"submit_scoring_decision with your final assessment."
    )
