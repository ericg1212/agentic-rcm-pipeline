# Copyright (c) 2026 Eric Grynspan. All rights reserved.
"""
Tests for Layer 2 — ClaimScorer.

All tests mock the Anthropic client — no real API calls in CI.
Tests validate: validation logic, fallback routing, CARC enum enforcement,
and the full score() path with a canned LLM response.
"""
from __future__ import annotations

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


def _make_api_response(
    content: list,
    stop_reason: str = "tool_use",
    input_tokens: int = 120,
    output_tokens: int = 60,
    cache_creation_tokens: int = 0,
    cache_read_tokens: int = 0,
):
    resp = MagicMock()
    resp.content = content
    resp.stop_reason = stop_reason
    resp.usage.input_tokens = input_tokens
    resp.usage.output_tokens = output_tokens
    resp.usage.cache_creation_input_tokens = cache_creation_tokens
    resp.usage.cache_read_input_tokens = cache_read_tokens
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


# ---------------------------------------------------------------------------
# Token usage + cost-per-claim
# ---------------------------------------------------------------------------

def test_score_captures_token_usage(scorer, clean_claim, pass_gate):
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
    scorer._client.messages.create.return_value = _make_api_response(
        [submit_block], input_tokens=200, output_tokens=80
    )

    result = scorer.score(clean_claim, pass_gate)

    assert result.input_tokens == 200
    assert result.output_tokens == 80
    assert result.cost_usd > 0.0
    # cost = (200 * 2.0 + 80 * 10.0) / 1_000_000 = 0.0012 (sonnet-5 intro pricing)
    assert abs(result.cost_usd - 0.0012) < 1e-9


def test_score_accumulates_tokens_across_iterations(scorer, clean_claim, pass_gate):
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
        _make_api_response([lookup_block], stop_reason="tool_use", input_tokens=150, output_tokens=40),
        _make_api_response([submit_block], stop_reason="tool_use", input_tokens=200, output_tokens=70),
    ]

    result = scorer.score(clean_claim, pass_gate)

    assert result.input_tokens == 350   # 150 + 200
    assert result.output_tokens == 110  # 40 + 70
    # cost = (350 * 2.0 + 110 * 10.0) / 1_000_000 = 0.001800 (sonnet-5 intro pricing)
    assert abs(result.cost_usd - 0.001800) < 1e-9


def test_score_cache_tokens_priced_correctly(scorer, clean_claim, pass_gate):
    """Cached tokens are excluded from usage.input_tokens and priced at their
    own multipliers: writes 1.25x, reads 0.10x the input rate."""
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
    scorer._client.messages.create.return_value = _make_api_response(
        [submit_block], input_tokens=100, output_tokens=50,
        cache_creation_tokens=2000, cache_read_tokens=4000,
    )

    result = scorer.score(clean_claim, pass_gate)

    assert result.cache_creation_input_tokens == 2000
    assert result.cache_read_input_tokens == 4000
    # cost = (100*2.0 + 2000*2.0*1.25 + 4000*2.0*0.10 + 50*10.0) / 1e6 = 0.0065
    assert abs(result.cost_usd - 0.0065) < 1e-9


def test_fallback_cost_attributed_when_loop_ran(scorer, clean_claim, pass_gate):
    """Tokens consumed before validation failure should still be attributed."""
    submit_block = _make_tool_use_block(
        "submit_scoring_decision",
        {
            "risk_score": 80,
            "confidence": 0.75,
            "predicted_denial_code": "NOT-REAL-CODE",  # invalid → validation_failed fallback
            "driving_fields": ["procedure_codes"],
            "recommended_action": "flag",
            "rationale": "Some denial reason.",
        },
    )
    scorer._client.messages.create.return_value = _make_api_response(
        [submit_block], input_tokens=180, output_tokens=50
    )

    result = scorer.score(clean_claim, pass_gate)

    assert result.used_fallback is True
    assert result.input_tokens == 180
    assert result.output_tokens == 50
    assert result.cost_usd > 0.0
