# Copyright (c) 2026 Eric Grynspan. All rights reserved.
"""
Tests for Phase 2 Component 2 — PA Pre-Check Module.

Covers:
- ToolRegistry._check_prior_auth_required output contract
- ScoringResult PA field defaults and post_init
- Router PA escalation branch (pa_required=True + low likelihood → escalate)
- Router PA bypass (pa_required=False → no escalation from PA gate)
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from src.reasoning.scorer import ScoringResult
from src.reasoning.tools import ToolRegistry
from src.action.router import ActionRouter
from src.consumer.ncci_gate import GateDecision, Route


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_scoring_result(**overrides) -> ScoringResult:
    defaults = dict(
        score_id=str(uuid.uuid4()),
        claim_id="CLAIM-001",
        scored_at=datetime.now(timezone.utc),
        model_id="claude-sonnet-4-6",
        prompt_version="v1",
        ncci_edit_version="2024Q1",
        input_hash="abc123",
        risk_score=30,
        risk_score_norm=0.30,
        confidence=0.75,
        predicted_denial_code=None,
        driving_fields=[],
        recommended_action="flag",
        rationale="Test rationale for scoring result",
        tool_calls=[],
        latency_ms=100,
    )
    defaults.update(overrides)
    return ScoringResult(**defaults)


def _make_gate(route: Route = Route.PASS) -> GateDecision:
    gate = MagicMock(spec=GateDecision)
    gate.route = route
    gate.violations = []
    gate.deterministic_carc = None
    gate.to_dict.return_value = {}
    return gate


def _make_claim(claim_id: str = "CLAIM-001", charge: float = 200.0) -> dict:
    return {
        "claim_id": claim_id,
        "submitted_charge": charge,
        "procedure_codes": ["27447"],
        "payer_id": "UHC_COMMERCIAL",
        "diagnosis_code": "M17.11",
        "is_holdout": False,
    }


# ---------------------------------------------------------------------------
# ToolRegistry: _check_prior_auth_required
# ---------------------------------------------------------------------------

class TestCheckPriorAuthTool:
    """Uses the real PayerRuleGraph loaded from seed — no mock."""

    @pytest.fixture()
    def registry(self):
        gate = MagicMock()
        gate.lookup_ptp.return_value = None
        return ToolRegistry(gate)

    def test_pa_required_for_surgical_procedure(self, registry):
        result = registry.execute("check_prior_auth_required", {
            "payer_id": "UHC_COMMERCIAL",
            "hcpcs_code": "27447",
            "diagnosis_code": "M17.11",
        })
        assert result["pa_required"] is True

    def test_approval_likelihood_between_0_and_1(self, registry):
        result = registry.execute("check_prior_auth_required", {
            "payer_id": "UHC_COMMERCIAL",
            "hcpcs_code": "27447",
            "diagnosis_code": "M17.11",
        })
        assert 0.0 <= result["pa_approval_likelihood"] <= 1.0

    def test_diagnosis_covered_increases_likelihood(self, registry):
        covered = registry.execute("check_prior_auth_required", {
            "payer_id": "UHC_COMMERCIAL",
            "hcpcs_code": "27447",
            "diagnosis_code": "M17.11",  # matches M17 prefix
        })
        not_covered = registry.execute("check_prior_auth_required", {
            "payer_id": "UHC_COMMERCIAL",
            "hcpcs_code": "27447",
            "diagnosis_code": "Z99.99",  # does not match
        })
        assert covered["pa_approval_likelihood"] > not_covered["pa_approval_likelihood"]

    def test_denial_code_co197_when_pa_required(self, registry):
        result = registry.execute("check_prior_auth_required", {
            "payer_id": "UHC_COMMERCIAL",
            "hcpcs_code": "27447",
        })
        if result["pa_required"]:
            assert result["denial_code_if_denied"] == "CO-197"

    def test_unknown_procedure_no_pa(self, registry):
        result = registry.execute("check_prior_auth_required", {
            "payer_id": "UHC_COMMERCIAL",
            "hcpcs_code": "ZZZZZ",
        })
        assert result["pa_required"] is False
        assert result["pa_approval_likelihood"] == 1.0

    def test_result_has_required_keys(self, registry):
        result = registry.execute("check_prior_auth_required", {
            "payer_id": "UHC_COMMERCIAL",
            "hcpcs_code": "27447",
        })
        required_keys = {
            "pa_required", "pa_approval_likelihood", "pa_criteria_met",
            "pa_criteria_unmet", "denial_code_if_denied", "gold_card_exempt",
        }
        assert required_keys.issubset(result.keys())


# ---------------------------------------------------------------------------
# ScoringResult PA fields
# ---------------------------------------------------------------------------

class TestScoringResultPAFields:
    def test_pa_fields_default_to_none(self):
        result = _make_scoring_result()
        assert result.pa_required is None
        assert result.pa_approval_likelihood is None
        assert result.pa_criteria_met == []

    def test_pa_fields_can_be_set(self):
        result = _make_scoring_result(
            pa_required=True,
            pa_approval_likelihood=0.55,
            pa_criteria_met=["diagnosis_supported"],
        )
        assert result.pa_required is True
        assert result.pa_approval_likelihood == 0.55
        assert "diagnosis_supported" in result.pa_criteria_met

    def test_to_snowflake_row_includes_pa_fields(self):
        result = _make_scoring_result(
            pa_required=True,
            pa_approval_likelihood=0.65,
            pa_criteria_met=["diagnosis_supported"],
        )
        row = result.to_snowflake_row()
        assert row["PA_REQUIRED"] is True
        assert row["PA_APPROVAL_LIKELIHOOD"] == 0.65
        assert "PA_CRITERIA_MET" in row


# ---------------------------------------------------------------------------
# Router: PA escalation branch
# ---------------------------------------------------------------------------

class TestRouterPAEscalation:
    def test_escalates_when_pa_required_and_low_likelihood(self):
        router = ActionRouter()
        claim = _make_claim()
        score = _make_scoring_result(
            pa_required=True,
            pa_approval_likelihood=0.40,  # below 0.70 threshold
            recommended_action="flag",
        )
        gate = _make_gate(Route.PASS)
        decision = router.route(claim, score, gate)
        assert decision.action_taken == "escalate"

    def test_escalation_cites_cms_0057_f(self):
        router = ActionRouter()
        claim = _make_claim()
        score = _make_scoring_result(
            pa_required=True,
            pa_approval_likelihood=0.40,
            recommended_action="flag",
        )
        gate = _make_gate(Route.PASS)
        decision = router.route(claim, score, gate)
        assert decision.governing_rule_cited == "CMS-0057-F"

    def test_no_pa_escalation_when_likelihood_above_threshold(self):
        router = ActionRouter()
        claim = _make_claim()
        score = _make_scoring_result(
            pa_required=True,
            pa_approval_likelihood=0.85,  # above 0.70 threshold
            recommended_action="flag",
        )
        gate = _make_gate(Route.PASS)
        decision = router.route(claim, score, gate)
        # Should NOT escalate from PA gate; falls through to normal flag
        assert decision.action_taken == "flag"

    def test_no_pa_escalation_when_pa_not_required(self):
        router = ActionRouter()
        claim = _make_claim()
        score = _make_scoring_result(
            pa_required=False,
            pa_approval_likelihood=0.30,  # low, but pa_required=False
            recommended_action="flag",
        )
        gate = _make_gate(Route.PASS)
        decision = router.route(claim, score, gate)
        assert decision.action_taken == "flag"

    def test_no_pa_escalation_when_pa_fields_none(self):
        """PA fields not set (tool not called) should not trigger PA escalation."""
        router = ActionRouter()
        claim = _make_claim()
        score = _make_scoring_result(
            pa_required=None,
            pa_approval_likelihood=None,
            recommended_action="flag",
        )
        gate = _make_gate(Route.PASS)
        decision = router.route(claim, score, gate)
        assert decision.action_taken == "flag"

    def test_kill_switch_takes_precedence_over_pa(self):
        from src.action.kill_switch import KillSwitch
        ks = KillSwitch()
        ks.activate("test")
        router = ActionRouter(kill_switch=ks)
        claim = _make_claim()
        score = _make_scoring_result(
            pa_required=True,
            pa_approval_likelihood=0.20,
            recommended_action="flag",
        )
        gate = _make_gate(Route.PASS)
        decision = router.route(claim, score, gate)
        assert decision.action_taken == "flag"
        assert decision.kill_switch_active is True
