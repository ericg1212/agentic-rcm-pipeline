# Copyright (c) 2026 Eric Grynspan. All rights reserved.
"""
Layer 3 — Autonomous Action Router.

INTERVIEW-CRITICAL: own this cold.

Tiered confidence-gated autonomy (the routing decision tree):

  1. Holdout bypass    — holdout claims skip action entirely (control arm integrity)
  2. Kill-switch       — when active, all claims FLAG; no auto-corrections fire
  3. Escalation gate   — risk_score >= ESCALATE_RISK_MIN → escalate regardless of action
  4. Auto-correct gate — ALL three conditions required:
                           a) LLM recommended "auto_correct"
                           b) confidence >= AUTO_CORRECT_CONFIDENCE_MIN (0.92)
                           c) submitted_charge <= AUTO_CORRECT_MAX_CHARGE ($500)
                         + correction must be structurally possible (modifier add)
  5. Flag / Hold       — LLM recommended flag or hold
  6. Pass              — low risk, route to clearinghouse

The three-condition auto-correct gate is the FCA defense:
  - Condition (a): model must explicitly recommend correction, not just flag
  - Condition (b): high confidence floor — uncertainty routes to human
  - Condition (c): dollar ceiling — high-value claims need human sign-off
All three failing safely: a claim never gets auto-corrected when any condition
is uncertain. The floor and ceiling are configurable via ActionConfig.

GOVERNING RULE REQUIREMENT (FCA compliance):
  Every auto-correct cites the exact NCCI/LCD rule applied. This is the same
  standard as a human biller annotating a correction in the portal — the
  difference is the system does it consistently and immutably.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from textwrap import dedent

import structlog

from src.action.audit import AuditRecord, ImmutableAuditLog
from src.action.corrections import attempt_modifier_correction
from src.action.kill_switch import KillSwitch
from src.config.settings import ActionConfig
from src.consumer.ncci_gate import GateDecision
from src.reasoning.scorer import ScoringResult

log = structlog.get_logger(__name__)

VALID_ACTIONS = frozenset(["pass", "auto_correct", "flag", "hold", "escalate"])


@dataclass
class ActionDecision:
    action_id: str
    claim_id: str
    score_id: str | None
    action_taken: str
    action_timestamp: datetime
    confidence: float | None
    risk_score: int | None
    governing_rule_cited: str | None
    correction_applied: dict | None
    escalation_draft: str | None
    reversible: bool
    kill_switch_active: bool
    corrected_claim: dict | None  # the modified claim if auto_correct, else None

    def to_audit_record(self) -> AuditRecord:
        return AuditRecord(
            action_id=self.action_id,
            claim_id=self.claim_id,
            score_id=self.score_id,
            action_taken=self.action_taken,
            action_timestamp=self.action_timestamp.isoformat(),
            confidence=self.confidence,
            risk_score=self.risk_score,
            governing_rule_cited=self.governing_rule_cited,
            correction_applied=self.correction_applied,
            escalation_draft=self.escalation_draft,
            reversible=self.reversible,
            kill_switch_active=self.kill_switch_active,
        )


class ActionRouter:
    """
    Routes each scored claim to the appropriate autonomous action.

    Construct once per process. Pass the shared KillSwitch and ImmutableAuditLog
    instances from the consumer so state is shared across all claims in a session.
    """

    def __init__(
        self,
        kill_switch: KillSwitch | None = None,
        audit_log: ImmutableAuditLog | None = None,
    ) -> None:
        self._kill_switch = kill_switch or KillSwitch()
        self._audit_log = audit_log or ImmutableAuditLog()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def route(
        self,
        claim: dict,
        scoring_result: ScoringResult,
        gate_decision: GateDecision,
    ) -> ActionDecision:
        """
        Route a scored claim to its action. Always appends to the audit log.
        Never raises — routing failures default to FLAG.
        """
        try:
            decision = self._decide(claim, scoring_result, gate_decision)
        except Exception as e:
            log.error("router_exception", claim_id=claim.get("claim_id"), error=str(e), exc_info=True)
            decision = self._flag_decision(
                claim, scoring_result,
                reason=f"router_exception:{type(e).__name__}",
                kill_switch_active=self._kill_switch.is_active,
            )

        self._audit_log.append(decision.to_audit_record())
        log.info(
            "action_routed",
            claim_id=decision.claim_id,
            action=decision.action_taken,
            risk_score=decision.risk_score,
            confidence=decision.confidence,
            kill_switch=decision.kill_switch_active,
        )
        return decision

    @property
    def kill_switch(self) -> KillSwitch:
        return self._kill_switch

    @property
    def audit_log(self) -> ImmutableAuditLog:
        return self._audit_log

    # ------------------------------------------------------------------
    # Decision logic
    # ------------------------------------------------------------------

    def _decide(
        self,
        claim: dict,
        score: ScoringResult,
        gate: GateDecision,
    ) -> ActionDecision:
        claim_id = claim.get("claim_id", "")

        # 1. Holdout — bypass action entirely (control arm integrity)
        if claim.get("is_holdout"):
            return self._pass_decision(claim, score, reason="holdout_bypass")

        # 2. Kill-switch — force FLAG on everything
        if self._kill_switch.is_active:
            return self._flag_decision(
                claim, score,
                reason=f"kill_switch:{self._kill_switch.reason}",
                kill_switch_active=True,
            )

        # 3. Escalation gate — risk_score >= threshold → escalate regardless of action
        escalate_threshold = int(ActionConfig.ESCALATE_RISK_MIN * 100)
        if score.risk_score >= escalate_threshold:
            return self._escalate_decision(claim, score)

        # 4. Auto-correct gate — all three conditions required
        if (
            score.recommended_action == "auto_correct"
            and score.confidence >= ActionConfig.AUTO_CORRECT_CONFIDENCE_MIN
            and float(claim.get("submitted_charge", 0)) <= ActionConfig.AUTO_CORRECT_MAX_CHARGE
        ):
            correction = attempt_modifier_correction(claim, gate)
            if correction is not None:
                return self._auto_correct_decision(claim, score, correction)
            # Correction not structurally possible — fall through to flag
            log.debug("auto_correct_not_applicable", claim_id=claim_id)

        # 5. Flag / Hold
        if score.recommended_action in {"flag", "hold", "auto_correct"}:
            return self._flag_decision(claim, score, reason="llm_flag")

        # 6. Pass — low risk
        return self._pass_decision(claim, score, reason="low_risk")

    # ------------------------------------------------------------------
    # Decision builders
    # ------------------------------------------------------------------

    def _pass_decision(
        self, claim: dict, score: ScoringResult, reason: str
    ) -> ActionDecision:
        return ActionDecision(
            action_id=str(uuid.uuid4()),
            claim_id=claim.get("claim_id", ""),
            score_id=score.score_id,
            action_taken="pass",
            action_timestamp=datetime.now(timezone.utc),
            confidence=score.confidence,
            risk_score=score.risk_score,
            governing_rule_cited=None,
            correction_applied=None,
            escalation_draft=None,
            reversible=True,
            kill_switch_active=False,
            corrected_claim=None,
        )

    def _flag_decision(
        self,
        claim: dict,
        score: ScoringResult,
        reason: str,
        kill_switch_active: bool = False,
    ) -> ActionDecision:
        return ActionDecision(
            action_id=str(uuid.uuid4()),
            claim_id=claim.get("claim_id", ""),
            score_id=score.score_id,
            action_taken="flag",
            action_timestamp=datetime.now(timezone.utc),
            confidence=score.confidence,
            risk_score=score.risk_score,
            governing_rule_cited=score.predicted_denial_code,
            correction_applied=None,
            escalation_draft=None,
            reversible=True,
            kill_switch_active=kill_switch_active,
            corrected_claim=None,
        )

    def _auto_correct_decision(
        self, claim: dict, score: ScoringResult, correction
    ) -> ActionDecision:
        return ActionDecision(
            action_id=str(uuid.uuid4()),
            claim_id=claim.get("claim_id", ""),
            score_id=score.score_id,
            action_taken="auto_correct",
            action_timestamp=datetime.now(timezone.utc),
            confidence=score.confidence,
            risk_score=score.risk_score,
            governing_rule_cited=correction.governing_rule_cited,
            correction_applied={
                "correction_type": correction.correction_type,
                "field": correction.field_corrected,
                "before": correction.before_value,
                "after": correction.after_value,
            },
            escalation_draft=None,
            reversible=True,
            kill_switch_active=False,
            corrected_claim=correction.corrected_claim,
        )

    def _escalate_decision(self, claim: dict, score: ScoringResult) -> ActionDecision:
        draft = _build_escalation_draft(claim, score)
        return ActionDecision(
            action_id=str(uuid.uuid4()),
            claim_id=claim.get("claim_id", ""),
            score_id=score.score_id,
            action_taken="escalate",
            action_timestamp=datetime.now(timezone.utc),
            confidence=score.confidence,
            risk_score=score.risk_score,
            governing_rule_cited=score.predicted_denial_code,
            correction_applied=None,
            escalation_draft=draft,
            reversible=False,
            kill_switch_active=False,
            corrected_claim=None,
        )


# ------------------------------------------------------------------
# Escalation draft builder
# ------------------------------------------------------------------

def _build_escalation_draft(claim: dict, score: ScoringResult) -> str:
    """
    Build a plain-text escalation draft for the human review queue.
    The agent drafts the correction and cites the exact policy — the human just approves.
    """
    driving = ", ".join(score.driving_fields) if score.driving_fields else "unspecified"
    denial_code = score.predicted_denial_code or "unknown"

    return dedent(f"""\
        ESCALATION DRAFT — Human Review Required
        ─────────────────────────────────────────
        Claim ID:        {claim.get("claim_id", "unknown")}
        Service Date:    {claim.get("service_date", "unknown")}
        Provider NPI:    {claim.get("provider_npi", "unknown")}
        Payer:           {claim.get("payer_id", "unknown")}
        Procedures:      {claim.get("procedure_codes", [])}
        Diagnoses:       {claim.get("diagnosis_codes", [])}
        Submitted:       ${claim.get("submitted_charge", "0.00")}

        RISK ASSESSMENT
        Risk Score:      {score.risk_score}/100
        Confidence:      {score.confidence:.0%}
        Predicted Code:  {denial_code}
        Driving Fields:  {driving}

        AGENT RATIONALE
        {score.rationale}

        RECOMMENDED ACTION
        Review the above fields before submission. Approve or reject this claim
        based on clinical and billing documentation. This draft was generated by
        the autonomous scoring agent and requires human sign-off before any
        submission or correction is applied.
        ─────────────────────────────────────────
        Model: {score.model_id} | Prompt: {score.prompt_version} | Hash: {score.input_hash[:16]}…
    """)
