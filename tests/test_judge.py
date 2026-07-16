# Copyright (c) 2026 Eric Grynspan. All rights reserved.
"""
Tests for Phase 3 — LLM-as-Judge eval harness.

No real API calls: batch client is mocked. Tests validate rubric completeness,
judge-independence guarantees (no reasoning chain in the judge's context),
request construction, verdict parsing, metrics, and agreement computation.
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from src.eval.judge import (
    CRITERIA,
    JUDGE_MODEL,
    JUDGE_OUTPUT_SCHEMA,
    JUDGE_SYSTEM_PROMPT,
    RUBRIC_VERSION,
    JudgeHarness,
    JudgeVerdict,
    agreement_rate,
    build_batch_request,
    build_judge_user_message,
    build_rule_context,
    compute_metrics,
    parse_verdict,
    select_disagreements,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def case():
    return {
        "claim": {
            "claim_id": "claim-001",
            "payer_id": "MEDICARE_FFS",
            "procedure_codes": ["27447"],
            "diagnosis_codes": ["G47.00"],
            "modifiers": [],
            "units": 1,
            "submitted_charge": "1500.00",
        },
        "gate": {"route": "pass", "risk_score": 0.0, "violations": [], "deterministic_carc": None},
        "scoring": {
            "score_id": "score-001",
            "risk_score": 85,
            "confidence": 0.9,
            "predicted_denial_code": "CO-50",
            "driving_fields": ["diagnosis_codes"],
            "recommended_action": "hold",
            "rationale": "Attach documentation supporting medical necessity for 27447 with G47.00.",
            "used_fallback": False,
            "tool_calls": [{"tool": "get_lcd_policy", "input": {}, "result": {"secret": "REASONING_TRACE"}}],
        },
    }


def _passing_verdict_json() -> str:
    return json.dumps({
        "criteria": {
            name: {"passed": True, "reason": "ok"} for name in CRITERIA
        },
        "overall_pass": True,
    })


# ---------------------------------------------------------------------------
# Rubric completeness
# ---------------------------------------------------------------------------

def test_rubric_has_five_criteria():
    assert len(CRITERIA) == 5
    assert set(CRITERIA) == {
        "carc_plausible", "rule_applies", "action_consistent",
        "guidance_actionable", "no_fabrication",
    }


def test_schema_requires_every_criterion():
    crit_schema = JUDGE_OUTPUT_SCHEMA["properties"]["criteria"]
    assert set(crit_schema["required"]) == set(CRITERIA)
    assert crit_schema["additionalProperties"] is False
    for name in CRITERIA:
        leaf = crit_schema["properties"][name]
        assert leaf["required"] == ["passed", "reason"]
        assert leaf["additionalProperties"] is False


def test_system_prompt_names_every_criterion_and_version():
    for name in CRITERIA:
        assert name in JUDGE_SYSTEM_PROMPT
    assert RUBRIC_VERSION in JUDGE_SYSTEM_PROMPT


# ---------------------------------------------------------------------------
# Judge independence
# ---------------------------------------------------------------------------

def test_judge_message_excludes_reasoning_chain(case):
    msg = build_judge_user_message(case, rule_context={"lcd_policies": []})
    assert "REASONING_TRACE" not in msg
    assert "tool_calls" not in msg


def test_judge_message_includes_output_and_context(case):
    ctx = {"lcd_policies": [{"found": True, "coverage_note": "requires M17"}]}
    msg = build_judge_user_message(case, ctx)
    assert "CO-50" in msg
    assert "requires M17" in msg
    assert "GOVERNING RULE CONTEXT" in msg


def test_rule_context_is_deterministic(case):
    registry = MagicMock()
    registry.execute.return_value = {"found": False}
    ctx1 = build_rule_context(case["claim"], registry)
    ctx2 = build_rule_context(case["claim"], registry)
    assert ctx1 == ctx2
    # single procedure + single diagnosis: 1 LCD lookup + 1 payer rule, no NCCI pairs
    assert len(ctx1["lcd_policies"]) == 1
    assert len(ctx1["payer_rules"]) == 1
    assert ctx1["ncci_edits"] == []


def test_rule_context_pairs_ncci_for_multi_procedure(case):
    registry = MagicMock()
    registry.execute.return_value = {"found": False}
    claim = {**case["claim"], "procedure_codes": ["93000", "93005"]}
    ctx = build_rule_context(claim, registry)
    assert len(ctx["ncci_edits"]) == 2  # both orderings


# ---------------------------------------------------------------------------
# Request construction
# ---------------------------------------------------------------------------

def test_batch_request_shape(case):
    req = build_batch_request(case, rule_context={})
    assert req["custom_id"] == "score-001"
    assert req["params"]["model"] == JUDGE_MODEL
    fmt = req["params"]["output_config"]["format"]
    assert fmt["type"] == "json_schema"
    assert fmt["schema"] == JUDGE_OUTPUT_SCHEMA


# ---------------------------------------------------------------------------
# Verdict parsing
# ---------------------------------------------------------------------------

def test_parse_valid_verdict():
    v = parse_verdict(_passing_verdict_json(), "score-001", "claim-001")
    assert v.error is None
    assert v.overall_pass is True
    assert v.failed_criteria == []
    assert v.rubric_version == RUBRIC_VERSION


def test_parse_failed_criterion():
    payload = json.loads(_passing_verdict_json())
    payload["criteria"]["no_fabrication"] = {"passed": False, "reason": "invented CARC"}
    payload["overall_pass"] = False
    v = parse_verdict(json.dumps(payload), "score-002", "claim-002")
    assert v.overall_pass is False
    assert v.failed_criteria == ["no_fabrication"]


def test_parse_garbage_is_error_verdict():
    v = parse_verdict("not json {", "score-003", "claim-003")
    assert v.error is not None
    assert v.overall_pass is False


def test_parse_missing_criterion_is_error():
    payload = json.loads(_passing_verdict_json())
    del payload["criteria"]["rule_applies"]
    v = parse_verdict(json.dumps(payload), "score-004", "claim-004")
    assert v.error is not None


# ---------------------------------------------------------------------------
# Batch result collection (unordered, keyed by custom_id)
# ---------------------------------------------------------------------------

def _batch_result(custom_id: str, text: str, result_type: str = "succeeded"):
    r = MagicMock()
    r.custom_id = custom_id
    r.result.type = result_type
    block = MagicMock()
    block.type = "text"
    block.text = text
    r.result.message.content = [block]
    return r


def test_collect_keys_by_custom_id_not_position(case):
    case2 = json.loads(json.dumps(case))
    case2["scoring"]["score_id"] = "score-002"
    case2["claim"]["claim_id"] = "claim-002"
    cases = [case, case2]

    client = MagicMock()
    # Results arrive in REVERSE order
    client.messages.batches.results.return_value = iter([
        _batch_result("score-002", _passing_verdict_json()),
        _batch_result("score-001", _passing_verdict_json()),
    ])
    harness = JudgeHarness(client, MagicMock())
    verdicts = harness.collect("batch-1", cases)

    by_id = {v.score_id: v for v in verdicts}
    assert by_id["score-001"].claim_id == "claim-001"
    assert by_id["score-002"].claim_id == "claim-002"


def test_collect_errored_result_becomes_error_verdict(case):
    client = MagicMock()
    client.messages.batches.results.return_value = iter([
        _batch_result("score-001", "", result_type="errored"),
    ])
    harness = JudgeHarness(client, MagicMock())
    verdicts = harness.collect("batch-1", [case])
    assert verdicts[0].error == "errored"
    assert verdicts[0].overall_pass is False


# ---------------------------------------------------------------------------
# Metrics + agreement
# ---------------------------------------------------------------------------

def _verdict(score_id: str, overall: bool, failed: list[str] | None = None) -> JudgeVerdict:
    failed = failed or []
    return JudgeVerdict(
        score_id=score_id, claim_id=f"c-{score_id}", judge_model=JUDGE_MODEL,
        rubric_version=RUBRIC_VERSION,
        criteria={
            name: {"passed": name not in failed, "reason": "x"} for name in CRITERIA
        },
        overall_pass=overall,
    )


def test_compute_metrics():
    verdicts = [
        _verdict("s1", True),
        _verdict("s2", False, failed=["guidance_actionable"]),
        JudgeVerdict("s3", "c3", JUDGE_MODEL, RUBRIC_VERSION, {}, False, error="errored"),
    ]
    m = compute_metrics(verdicts)
    assert m.n_judged == 2
    assert m.n_errors == 1
    assert m.overall_pass_rate == 0.5
    assert m.per_criterion_pass_rate["guidance_actionable"] == 0.5
    assert m.per_criterion_pass_rate["no_fabrication"] == 1.0


def test_select_disagreements_excludes_errors():
    verdicts = [
        _verdict("s1", True),
        _verdict("s2", False, failed=["carc_plausible"]),
        JudgeVerdict("s3", "c3", JUDGE_MODEL, RUBRIC_VERSION, {}, False, error="errored"),
    ]
    picked = select_disagreements(verdicts)
    assert [v.score_id for v in picked] == ["s2"]


def test_agreement_rate_matches_by_score_id():
    haiku = [_verdict("s1", True), _verdict("s2", False, failed=["rule_applies"]), _verdict("s3", True)]
    sonnet = [_verdict("s3", True), _verdict("s1", True), _verdict("s2", True)]  # disagrees on s2
    assert agreement_rate(haiku, sonnet) == pytest.approx(2 / 3)


def test_agreement_rate_empty_is_zero():
    assert agreement_rate([], []) == 0.0
