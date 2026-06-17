"""
Tests for Layer 2 — ClaimScorer.

All tests mock the Anthropic client — no real API calls in CI.
Tests validate: validation logic, fallback routing, CARC enum enforcement,
and the full score() path with a canned LLM response.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from src.consumer.ncci_gate import GateDecision, NCCIGate, Route
from src.reasoning.scorer import ClaimScorer, ScoringResult, _compute_input_hash


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def loaded_gate(tmp_path):
    """Return an NCCIGate loaded from the seed files."""
    gate = NCCIGate()
    gate.load()
    return gate


@pytest.fixture()
def scorer(loaded_gate):
    """Return a ClaimScorer with a mocked Anthropic client."""
    with patch("src.reasoning.scorer.anthropic.Anthropic"):
        s = ClaimScorer(loaded_gate)
    return s


@pytest.fixture()
def clean_claim():
    return {
        "claim_id": "test-001",
        "event_time": 1718000000000,
        "service_date": "2026-06-17",
        "provider_npi": "1003000126",
        "payer_id": "MEDICARE_FFS",
        "claim_type": "professional",
        "place_of_service": "11",
        "procedure_codes": ["99213"],
        "diagnosis_codes": ["I10"],
        "modifiers": [],
        "units": 1,
        "submitted_charge": "142.00",
        "ncci_edit_version": "2026Q3",
        "is_holdout": False,
    }


@pytest.fixture()
def pass_gate():
    return GateDecision(route=Route.PASS, risk_score=0.0)


@pytest.fixture()
def hard_fail_gate():
    from src.consumer.ncci_gate import NCCIViolation, ViolationType
    v = NCCIViolation(
        violation_type=ViolationType.PTP_NO_BYPASS,
        code="93000",
        col2_code="93005",
        modifier_indicator="0",
        carc_code="CO-97",
    )
    return GateDecision(route=Route.HARD_FAIL, risk_score=0.90, violations=[v], deterministic_carc="CO-97")


# ---------------------------------------------------------------------------
# Validation tests
# ---------------------------------------------------------------------------

def test_validate_valid_inputs(scorer):
    inputs = {
        "risk_score": 75,
        "confidence": 0.88,
        "predicted_denial_code": "CO-97",
        "driving_fields": ["procedure_codes"],
        "recommended_action": "flag",
        "rationale": "Bundled procedure codes without valid bypass modifier.",
    }
    assert scorer._validate(inputs) is True


def test_validate_risk_score_out_of_range(scorer):
    inputs = {
        "risk_score": 150,
        "confidence": 0.9,
        "predicted_denial_code": None,
        "driving_fields": [],
        "recommended_action": "flag",
        "rationale": "Some rationale here.",
    }
    assert scorer._validate(inputs) is False


def test_validate_invalid_carc_code(scorer):
    inputs = {
        "risk_score": 70,
        "confidence": 0.80,
        "predicted_denial_code": "HALLUCINATED-999",
        "driving_fields": ["procedure_codes"],
        "recommended_action": "flag",
        "rationale": "Some rationale here.",
    }
    assert scorer._validate(inputs) is False


def test_validate_invalid_action(scorer):
    inputs = {
        "risk_score": 50,
        "confidence": 0.70,
        "predicted_denial_code": None,
        "driving_fields": [],
        "recommended_action": "submit",   # not a valid action
        "rationale": "Some rationale here.",
    }
    assert scorer._validate(inputs) is False


def test_validate_empty_rationale(scorer):
    inputs = {
        "risk_score": 50,
        "confidence": 0.70,
        "predicted_denial_code": None,
        "driving_fields": [],
        "recommended_action": "flag",
        "rationale": "  ",   # whitespace only
    }
    assert scorer._validate(inputs) is False


def test_validate_null_denial_code_is_valid(scorer):
    inputs = {
        "risk_score": 10,
        "confidence": 0.95,
        "predicted_denial_code": None,
        "driving_fields": [],
        "recommended_action": "flag",
        "rationale": "Low risk, no specific denial predicted.",
    }
    assert scorer._validate(inputs) is True


# ---------------------------------------------------------------------------
# Fallback tests
# ---------------------------------------------------------------------------

def test_fallback_pass_route(scorer, clean_claim, pass_gate):
    result = scorer._fallback(clean_claim, pass_gate, "test_reason", "deadbeef", 100)
    assert result.used_fallback is True
    assert result.fallback_reason == "test_reason"
    assert result.risk_score == 5
    assert result.recommended_action == "flag"
    assert result.predicted_denial_code is None


def test_fallback_hard_fail_route(scorer, clean_claim, hard_fail_gate):
    result = scorer._fallback(clean_claim, hard_fail_gate, "validation_failed", "deadbeef", 50)
    assert result.used_fallback is True
    assert result.risk_score == 85
    assert result.predicted_denial_code == "CO-97"
    assert result.recommended_action == "flag"


def test_fallback_returns_scoring_result(scorer, clean_claim, pass_gate):
    result = scorer._fallback(clean_claim, pass_gate, "test", "abc", 0)
    assert isinstance(result, ScoringResult)
    assert result.claim_id == "test-001"
    assert result.model_id is not None
    assert result.prompt_version is not None


# ---------------------------------------------------------------------------
# Full score() path — canned LLM response
# ---------------------------------------------------------------------------

def _make_tool_use_block(name: str, tool_input: dict, block_id: str = "toolu_01"):
    block = MagicMock()
    block.type = "tool_use"
    block.name = name
    block.input = tool_input
    block.id = block_id
    return block


def _make_api_response(content: list, stop_reason: str = "tool_use"):
    resp = MagicMock()
    resp.content = content
    resp.stop_reason = stop_reason
    return resp


def test_score_valid_llm_response(scorer, clean_claim, pass_gate):
    submit_block = _make_tool_use_block(
        "submit_scoring_decision",
        {
            "risk_score": 10,
            "confidence": 0.92,
            "predicted_denial_code": None,
            "driving_fields": ["procedure_codes"],
            "recommended_action": "flag",
            "rationale": "Low-risk office visit. No bundling violations detected.",
        },
    )
    scorer._client.messages.create.return_value = _make_api_response([submit_block])

    result = scorer.score(clean_claim, pass_gate)

    assert result.used_fallback is False
    assert result.risk_score == 10
    assert result.confidence == 0.92
    assert result.recommended_action == "flag"
    assert result.predicted_denial_code is None
    assert result.claim_id == "test-001"
    assert result.latency_ms >= 0


def test_score_with_lookup_then_submit(scorer, clean_claim, pass_gate):
    lookup_block = _make_tool_use_block(
        "lookup_ncci_edit",
        {"col1_code": "99213", "col2_code": "99211"},
        block_id="toolu_01",
    )
    submit_block = _make_tool_use_block(
        "submit_scoring_decision",
        {
            "risk_score": 60,
            "confidence": 0.80,
            "predicted_denial_code": "CO-97",
            "driving_fields": ["procedure_codes"],
            "recommended_action": "flag",
            "rationale": "Bundled E&M codes. CO-97 likely.",
        },
        block_id="toolu_02",
    )
    scorer._client.messages.create.side_effect = [
        _make_api_response([lookup_block], stop_reason="tool_use"),
        _make_api_response([submit_block], stop_reason="tool_use"),
    ]

    result = scorer.score(clean_claim, pass_gate)

    assert result.used_fallback is False
    assert result.risk_score == 60
    assert result.predicted_denial_code == "CO-97"
    assert len(result.tool_calls) == 1
    assert result.tool_calls[0]["tool"] == "lookup_ncci_edit"
    assert scorer._client.messages.create.call_count == 2


def test_score_invalid_carc_triggers_fallback(scorer, clean_claim, pass_gate):
    submit_block = _make_tool_use_block(
        "submit_scoring_decision",
        {
            "risk_score": 80,
            "confidence": 0.75,
            "predicted_denial_code": "NOT-REAL-CODE",
            "driving_fields": ["procedure_codes"],
            "recommended_action": "flag",
            "rationale": "Some denial reason.",
        },
    )
    scorer._client.messages.create.return_value = _make_api_response([submit_block])

    result = scorer.score(clean_claim, pass_gate)

    assert result.used_fallback is True
    assert result.fallback_reason == "validation_failed"


def test_score_api_exception_triggers_fallback(scorer, clean_claim, pass_gate):
    scorer._client.messages.create.side_effect = RuntimeError("Connection error")

    result = scorer.score(clean_claim, pass_gate)

    assert result.used_fallback is True
    assert "exception" in result.fallback_reason


# ---------------------------------------------------------------------------
# Input hash
# ---------------------------------------------------------------------------

def test_input_hash_deterministic(clean_claim, pass_gate):
    h1 = _compute_input_hash(clean_claim, pass_gate)
    h2 = _compute_input_hash(clean_claim, pass_gate)
    assert h1 == h2
    assert len(h1) == 64  # SHA-256 hex


def test_input_hash_changes_with_claim(clean_claim, pass_gate):
    h1 = _compute_input_hash(clean_claim, pass_gate)
    modified = {**clean_claim, "submitted_charge": "999.00"}
    h2 = _compute_input_hash(modified, pass_gate)
    assert h1 != h2
