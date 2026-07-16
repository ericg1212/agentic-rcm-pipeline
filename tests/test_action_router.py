# Copyright (c) 2026 Eric Grynspan. All rights reserved.
"""
Tests for Layer 3 — ActionRouter, KillSwitch, ImmutableAuditLog, corrections.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.action.audit import AuditRecord, ImmutableAuditLog
from src.action.corrections import attempt_modifier_correction
from src.action.kill_switch import KillSwitch
from src.action.router import ActionRouter
from src.config.settings import ActionConfig
from src.consumer.ncci_gate import GateDecision, NCCIGate, NCCIViolation, Route, ViolationType
from src.reasoning.scorer import ScoringResult


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def loaded_gate():
    gate = NCCIGate()
    gate.load()
    return gate


@pytest.fixture()
def router():
    return ActionRouter()


def _make_scoring_result(
    risk_score: int = 40,
    confidence: float = 0.85,
    action: str = "flag",
    denial_code: str | None = "CO-97",
    used_fallback: bool = False,
) -> ScoringResult:
    return ScoringResult(
        score_id="score-001",
        claim_id="claim-001",
        scored_at=datetime.now(timezone.utc),
        model_id="claude-sonnet-5",
        prompt_version="v1.0.0",
        ncci_edit_version="2026Q3",
        input_hash="abc123",
        risk_score=risk_score,
        risk_score_norm=risk_score / 100.0,
        confidence=confidence,
        predicted_denial_code=denial_code,
        driving_fields=["procedure_codes", "modifiers"],
        recommended_action=action,
        rationale="Test rationale for billing team.",
        tool_calls=[],
        latency_ms=120,
        used_fallback=used_fallback,
    )


def _make_claim(
    charge: float = 200.0,
    is_holdout: bool = False,
    modifiers: list | None = None,
    procedure_codes: list | None = None,
) -> dict:
    return {
        "claim_id": "claim-001",
        "service_date": "2026-06-17",
        "provider_npi": "1003000126",
        "payer_id": "MEDICARE_FFS",
        "claim_type": "professional",
        "place_of_service": "11",
        "procedure_codes": procedure_codes or ["93000", "93005"],
        "diagnosis_codes": ["I10"],
        "modifiers": modifiers or [],
        "units": 1,
        "submitted_charge": str(charge),
        "ncci_edit_version": "2026Q3",
        "is_holdout": is_holdout,
    }


def _make_pass_gate() -> GateDecision:
    return GateDecision(route=Route.PASS, risk_score=0.0)


def _make_ambiguous_gate() -> GateDecision:
    v = NCCIViolation(
        violation_type=ViolationType.PTP_BYPASS_UNVERIFIED,
        code="93000",
        col2_code="93005",
        modifier_indicator="1",
        carc_code="CO-4",
    )
    return GateDecision(route=Route.AMBIGUOUS, risk_score=0.65, violations=[v])


def _make_hard_fail_gate() -> GateDecision:
    v = NCCIViolation(
        violation_type=ViolationType.PTP_NO_BYPASS,
        code="93000",
        col2_code="93005",
        modifier_indicator="0",
        carc_code="CO-97",
    )
    return GateDecision(route=Route.HARD_FAIL, risk_score=0.90, violations=[v], deterministic_carc="CO-97")


# ---------------------------------------------------------------------------
# KillSwitch
# ---------------------------------------------------------------------------

def test_kill_switch_inactive_by_default():
    ks = KillSwitch()
    assert ks.is_active is False
    assert ks.reason is None


def test_kill_switch_activate():
    ks = KillSwitch()
    ks.activate("drift_breach")
    assert ks.is_active is True
    assert ks.reason == "drift_breach"
    assert ks.activation_count == 1


def test_kill_switch_deactivate():
    ks = KillSwitch()
    ks.activate("manual")
    ks.deactivate()
    assert ks.is_active is False
    assert ks.reason is None


def test_kill_switch_idempotent_activate():
    ks = KillSwitch()
    ks.activate("reason1")
    ks.activate("reason2")  # second call should not increment count
    assert ks.activation_count == 1


# ---------------------------------------------------------------------------
# ImmutableAuditLog
# ---------------------------------------------------------------------------

def test_audit_log_append_and_len():
    log = ImmutableAuditLog()
    record = AuditRecord(
        action_id="a1", claim_id="c1", score_id="s1",
        action_taken="flag", action_timestamp="2026-06-17T10:00:00",
        confidence=0.85, risk_score=40, governing_rule_cited=None,
        correction_applied=None, escalation_draft=None,
        reversible=True, kill_switch_active=False,
    )
    log.append(record)
    assert len(log) == 1


def test_audit_log_records_for_claim():
    log = ImmutableAuditLog()
    for i in range(3):
        log.append(AuditRecord(
            action_id=f"a{i}", claim_id="c1" if i < 2 else "c2",
            score_id=None, action_taken="flag",
            action_timestamp="2026-06-17T10:00:00",
            confidence=None, risk_score=None, governing_rule_cited=None,
            correction_applied=None, escalation_draft=None,
            reversible=True, kill_switch_active=False,
        ))
    assert len(log.records_for_claim("c1")) == 2
    assert len(log.records_for_claim("c2")) == 1


def test_audit_log_auto_correct_rate():
    log = ImmutableAuditLog()
    for action in ["auto_correct", "flag", "flag", "flag"]:
        log.append(AuditRecord(
            action_id=str(id(action)), claim_id="c1", score_id=None,
            action_taken=action, action_timestamp="2026-06-17T10:00:00",
            confidence=None, risk_score=None, governing_rule_cited=None,
            correction_applied=None, escalation_draft=None,
            reversible=True, kill_switch_active=False,
        ))
    assert log.auto_correct_rate() == pytest.approx(0.25)


# ---------------------------------------------------------------------------
# Corrections
# ---------------------------------------------------------------------------

def test_modifier_correction_adds_59():
    claim = _make_claim(modifiers=[])
    gate = _make_ambiguous_gate()
    result = attempt_modifier_correction(claim, gate)
    assert result is not None
    assert result.correction_type == "add_missing_modifier"
    assert "59" in result.after_value
    assert result.governing_rule_cited is not None
    assert "93000" in result.governing_rule_cited


def test_modifier_correction_blocked_by_hard_ban():
    claim = _make_claim()
    gate = _make_hard_fail_gate()  # modifier_indicator=0 → no bypass ever valid
    result = attempt_modifier_correction(claim, gate)
    assert result is None


def test_modifier_correction_skipped_when_modifier_present():
    claim = _make_claim(modifiers=["59"])
    gate = _make_ambiguous_gate()
    result = attempt_modifier_correction(claim, gate)
    assert result is None


def test_modifier_correction_does_not_mutate_original():
    claim = _make_claim(modifiers=[])
    gate = _make_ambiguous_gate()
    attempt_modifier_correction(claim, gate)
    assert claim["modifiers"] == []


def test_modifier_correction_corrected_claim_has_modifier():
    claim = _make_claim(modifiers=[])
    gate = _make_ambiguous_gate()
    result = attempt_modifier_correction(claim, gate)
    assert result is not None
    assert "59" in result.corrected_claim["modifiers"]


# ---------------------------------------------------------------------------
# ActionRouter — routing logic
# ---------------------------------------------------------------------------

def test_holdout_claim_passes_without_action(router):
    claim = _make_claim(is_holdout=True)
    score = _make_scoring_result(risk_score=80, action="flag")
    gate = _make_ambiguous_gate()
    decision = router.route(claim, score, gate)
    assert decision.action_taken == "pass"


def test_kill_switch_forces_flag(router):
    router.kill_switch.activate("test")
    claim = _make_claim()
    score = _make_scoring_result(risk_score=10, confidence=0.99, action="auto_correct")
    gate = _make_ambiguous_gate()
    decision = router.route(claim, score, gate)
    assert decision.action_taken == "flag"
    assert decision.kill_switch_active is True


def test_escalation_gate_overrides_auto_correct(router):
    threshold = int(ActionConfig.ESCALATE_RISK_MIN * 100)
    claim = _make_claim(charge=100.0)
    score = _make_scoring_result(
        risk_score=threshold + 1,
        confidence=0.99,
        action="auto_correct",
    )
    gate = _make_ambiguous_gate()
    decision = router.route(claim, score, gate)
    assert decision.action_taken == "escalate"


def test_auto_correct_fires_when_all_conditions_met(router):
    claim = _make_claim(charge=200.0, modifiers=[])
    score = _make_scoring_result(
        risk_score=50,
        confidence=ActionConfig.AUTO_CORRECT_CONFIDENCE_MIN,
        action="auto_correct",
    )
    gate = _make_ambiguous_gate()
    decision = router.route(claim, score, gate)
    assert decision.action_taken == "auto_correct"
    assert decision.governing_rule_cited is not None
    assert decision.reversible is True
    assert decision.corrected_claim is not None


def test_auto_correct_blocked_by_low_confidence(router):
    claim = _make_claim(charge=200.0)
    score = _make_scoring_result(
        risk_score=50,
        confidence=ActionConfig.AUTO_CORRECT_CONFIDENCE_MIN - 0.01,
        action="auto_correct",
    )
    gate = _make_ambiguous_gate()
    decision = router.route(claim, score, gate)
    assert decision.action_taken == "flag"


def test_auto_correct_blocked_by_high_charge(router):
    claim = _make_claim(charge=ActionConfig.AUTO_CORRECT_MAX_CHARGE + 1)
    score = _make_scoring_result(
        risk_score=50,
        confidence=ActionConfig.AUTO_CORRECT_CONFIDENCE_MIN,
        action="auto_correct",
    )
    gate = _make_ambiguous_gate()
    decision = router.route(claim, score, gate)
    assert decision.action_taken == "flag"


def test_flag_action_routes_to_flag(router):
    claim = _make_claim()
    score = _make_scoring_result(risk_score=60, action="flag")
    gate = _make_pass_gate()
    decision = router.route(claim, score, gate)
    assert decision.action_taken == "flag"


def test_escalate_builds_draft(router):
    threshold = int(ActionConfig.ESCALATE_RISK_MIN * 100)
    claim = _make_claim()
    score = _make_scoring_result(risk_score=threshold + 5, action="hold")
    gate = _make_pass_gate()
    decision = router.route(claim, score, gate)
    assert decision.action_taken == "escalate"
    assert decision.escalation_draft is not None
    assert "ESCALATION DRAFT" in decision.escalation_draft
    assert "BILLING GUIDANCE" in decision.escalation_draft
    assert score.rationale in decision.escalation_draft
    assert decision.reversible is False


def test_every_route_call_appends_to_audit_log(router):
    claims = [_make_claim() for _ in range(5)]
    for claim in claims:
        router.route(claim, _make_scoring_result(), _make_pass_gate())
    assert len(router.audit_log) == 5


def test_audit_record_has_required_fields(router):
    claim = _make_claim()
    score = _make_scoring_result()
    gate = _make_pass_gate()
    router.route(claim, score, gate)
    record = list(router.audit_log)[0]
    assert record.action_id is not None
    assert record.claim_id == "claim-001"
    assert record.score_id == "score-001"
    assert record.action_timestamp is not None
