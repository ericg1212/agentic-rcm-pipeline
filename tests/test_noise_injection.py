"""
Tests for Layer 2 — noise injection eval harness.

Validates: dirty claim injection, gate false-negative guarantee,
lift calculation, and per-pattern reporting.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from src.consumer.ncci_gate import GateDecision, NCCIGate, Route
from src.eval.noise_injection import (
    LCD_RESTRICTED_PROCEDURES,
    LLM_RECOVERY_RISK_THRESHOLD,
    EvalResult,
    inject_wrong_diagnosis,
    run_noise_injection_eval,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def loaded_gate():
    gate = NCCIGate()
    gate.load()
    return gate


def _make_clean_claim(procedure_code: str = "27447", diagnosis_code: str = "M17.11") -> dict:
    return {
        "claim_id": f"eval-{procedure_code}",
        "event_time": 1718000000000,
        "service_date": "2026-06-17",
        "provider_npi": "1003000126",
        "payer_id": "MEDICARE_FFS",
        "claim_type": "professional",
        "place_of_service": "11" if procedure_code != "27447" else "24",
        "procedure_codes": [procedure_code],
        "diagnosis_codes": [diagnosis_code],
        "modifiers": [],
        "units": 1,
        "submitted_charge": "500.00",
        "ncci_edit_version": "2026Q3",
        "is_holdout": False,
    }


def _make_scoring_result(risk_score: int) -> MagicMock:
    result = MagicMock()
    result.risk_score = risk_score
    result.predicted_denial_code = "CO-50" if risk_score >= LLM_RECOVERY_RISK_THRESHOLD else None
    result.recommended_action = "hold" if risk_score >= LLM_RECOVERY_RISK_THRESHOLD else "flag"
    return result


# ---------------------------------------------------------------------------
# inject_wrong_diagnosis
# ---------------------------------------------------------------------------

def test_inject_wrong_diagnosis_replaces_codes():
    claim = _make_clean_claim("27447", "M17.11")
    dirty = inject_wrong_diagnosis(claim)

    assert dirty is not None
    assert dirty["diagnosis_codes"] != ["M17.11"]
    assert dirty.get("_injected_pattern") == "wrong_diagnosis"


def test_inject_wrong_diagnosis_does_not_mutate_original():
    claim = _make_clean_claim("27447", "M17.11")
    original_diag = list(claim["diagnosis_codes"])
    inject_wrong_diagnosis(claim)
    assert claim["diagnosis_codes"] == original_diag


def test_inject_wrong_diagnosis_returns_none_for_unrestricted_code():
    # 99213 has no LCD restrictions — injection not applicable
    claim = _make_clean_claim("99213", "I10")
    result = inject_wrong_diagnosis(claim)
    assert result is None


def test_inject_wrong_diagnosis_works_for_all_restricted_procedures():
    for proc_code in LCD_RESTRICTED_PROCEDURES:
        # Use a valid diagnosis for each; the injection replaces it
        claim = _make_clean_claim(proc_code, "M17.11")
        dirty = inject_wrong_diagnosis(claim)
        assert dirty is not None, f"Expected injection for {proc_code}"
        assert dirty["_injected_pattern"] == "wrong_diagnosis"


# ---------------------------------------------------------------------------
# Gate false-negative guarantee
# ---------------------------------------------------------------------------

def test_gate_passes_wrong_diagnosis_claim(loaded_gate):
    """Gate has no diagnosis check — wrong-diagnosis claim must pass."""
    claim = _make_clean_claim("27447", "M17.11")
    dirty = inject_wrong_diagnosis(claim)
    assert dirty is not None

    decision = loaded_gate.evaluate(dirty)
    assert decision.route == Route.PASS, (
        "NCCI gate should not flag a single-procedure claim with no NCCI violation. "
        f"Got: {decision.route} with violations: {decision.violations}"
    )


# ---------------------------------------------------------------------------
# run_noise_injection_eval
# ---------------------------------------------------------------------------

def test_eval_perfect_llm_recovery(loaded_gate):
    """LLM catches all gate false negatives → lift = 1.0."""
    claims = [_make_clean_claim("27447", "M17.11") for _ in range(20)]

    mock_scorer = MagicMock()
    mock_scorer.score.side_effect = lambda c, g: _make_scoring_result(85)

    result = run_noise_injection_eval(
        claims, loaded_gate, mock_scorer, dirty_fraction=1.0, seed=1
    )

    assert isinstance(result, EvalResult)
    assert result.gate_false_negatives > 0
    assert result.llm_recoveries == result.gate_false_negatives
    assert result.lift == pytest.approx(1.0)


def test_eval_zero_llm_recovery(loaded_gate):
    """LLM misses all gate false negatives → lift = 0.0."""
    claims = [_make_clean_claim("27447", "M17.11") for _ in range(20)]

    mock_scorer = MagicMock()
    mock_scorer.score.side_effect = lambda c, g: _make_scoring_result(10)

    result = run_noise_injection_eval(
        claims, loaded_gate, mock_scorer, dirty_fraction=1.0, seed=1
    )

    assert result.lift == pytest.approx(0.0)
    assert result.llm_recoveries == 0


def test_eval_partial_recovery(loaded_gate):
    """LLM catches 50% → lift ≈ 0.5."""
    claims = [_make_clean_claim("27447", "M17.11") for _ in range(40)]

    call_count = [0]

    def alternating_score(c, g):
        call_count[0] += 1
        score = 85 if call_count[0] % 2 == 1 else 10
        return _make_scoring_result(score)

    mock_scorer = MagicMock()
    mock_scorer.score.side_effect = alternating_score

    result = run_noise_injection_eval(
        claims, loaded_gate, mock_scorer, dirty_fraction=1.0, seed=1
    )

    assert 0.4 < result.lift < 0.6


def test_eval_no_restricted_procedures_yields_zero_dirty(loaded_gate):
    """Claims with no LCD-restricted procedures produce no dirty claims."""
    claims = [_make_clean_claim("99213", "I10") for _ in range(20)]

    mock_scorer = MagicMock()
    result = run_noise_injection_eval(
        claims, loaded_gate, mock_scorer, dirty_fraction=1.0, seed=1
    )

    assert result.n_dirty == 0
    assert result.gate_false_negatives == 0
    assert result.lift == pytest.approx(0.0)
    mock_scorer.score.assert_not_called()


def test_eval_result_summary_string(loaded_gate):
    claims = [_make_clean_claim("27447", "M17.11") for _ in range(10)]
    mock_scorer = MagicMock()
    mock_scorer.score.side_effect = lambda c, g: _make_scoring_result(80)

    result = run_noise_injection_eval(
        claims, loaded_gate, mock_scorer, dirty_fraction=1.0, seed=1
    )

    summary = result.summary()
    assert "Lift:" in summary
    assert "Gate FN:" in summary
