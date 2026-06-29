# Copyright (c) 2026 Eric Grynspan. All rights reserved.
"""Tests for ScoringResultValidator — inline and batch validation paths."""
import pytest

from src.validation.scoring_suite import (
    ScoringResultValidator,
    ValidationResult,
    VALID_ACTIONS,
)


def _valid_row(**overrides) -> dict:
    base = {
        "CLAIM_ID": "CLM-001",
        "SCORE_ID": "SCR-001",
        "RISK_SCORE": 0.72,
        "CONFIDENCE": 0.85,
        "RECOMMENDED_ACTION": "flag",
        "PREDICTED_DENIAL_CODE": None,
    }
    base.update(overrides)
    return base


class TestValidateRowPass:
    def test_valid_row_succeeds(self):
        v = ScoringResultValidator(carc_codes=frozenset())
        result = v.validate_row(_valid_row())
        assert result.success
        assert result.failures == []

    def test_all_valid_actions_accepted(self):
        v = ScoringResultValidator(carc_codes=frozenset())
        for action in VALID_ACTIONS:
            result = v.validate_row(_valid_row(RECOMMENDED_ACTION=action))
            assert result.success, f"Expected success for action={action}"

    def test_boundary_risk_score_zero(self):
        v = ScoringResultValidator(carc_codes=frozenset())
        assert v.validate_row(_valid_row(RISK_SCORE=0.0)).success

    def test_boundary_risk_score_one(self):
        v = ScoringResultValidator(carc_codes=frozenset())
        assert v.validate_row(_valid_row(RISK_SCORE=1.0)).success

    def test_boundary_confidence_zero(self):
        v = ScoringResultValidator(carc_codes=frozenset())
        assert v.validate_row(_valid_row(CONFIDENCE=0.0)).success

    def test_null_carc_allowed(self):
        v = ScoringResultValidator(carc_codes=frozenset({"4", "97"}))
        result = v.validate_row(_valid_row(PREDICTED_DENIAL_CODE=None))
        assert result.success

    def test_valid_carc_accepted_when_codes_loaded(self):
        v = ScoringResultValidator(carc_codes=frozenset({"4", "97"}))
        result = v.validate_row(_valid_row(PREDICTED_DENIAL_CODE="4"))
        assert result.success


class TestValidateRowFail:
    def test_risk_score_above_one_fails(self):
        v = ScoringResultValidator(carc_codes=frozenset())
        result = v.validate_row(_valid_row(RISK_SCORE=1.01))
        assert not result.success
        columns = [f.column for f in result.failures]
        assert "RISK_SCORE" in columns

    def test_risk_score_below_zero_fails(self):
        v = ScoringResultValidator(carc_codes=frozenset())
        result = v.validate_row(_valid_row(RISK_SCORE=-0.01))
        assert not result.success

    def test_confidence_above_one_fails(self):
        v = ScoringResultValidator(carc_codes=frozenset())
        result = v.validate_row(_valid_row(CONFIDENCE=1.5))
        assert not result.success
        assert "CONFIDENCE" in [f.column for f in result.failures]

    def test_invalid_action_fails(self):
        v = ScoringResultValidator(carc_codes=frozenset())
        result = v.validate_row(_valid_row(RECOMMENDED_ACTION="approve"))
        assert not result.success
        assert "RECOMMENDED_ACTION" in [f.column for f in result.failures]

    def test_null_claim_id_fails(self):
        v = ScoringResultValidator(carc_codes=frozenset())
        result = v.validate_row(_valid_row(CLAIM_ID=None))
        assert not result.success
        assert "CLAIM_ID" in [f.column for f in result.failures]

    def test_empty_claim_id_fails(self):
        v = ScoringResultValidator(carc_codes=frozenset())
        result = v.validate_row(_valid_row(CLAIM_ID=""))
        assert not result.success

    def test_invalid_carc_fails_when_codes_loaded(self):
        v = ScoringResultValidator(carc_codes=frozenset({"4", "97"}))
        result = v.validate_row(_valid_row(PREDICTED_DENIAL_CODE="BOGUS"))
        assert not result.success
        assert "PREDICTED_DENIAL_CODE" in [f.column for f in result.failures]

    def test_non_numeric_risk_score_fails(self):
        v = ScoringResultValidator(carc_codes=frozenset())
        result = v.validate_row(_valid_row(RISK_SCORE="high"))
        assert not result.success

    def test_failure_messages_populated(self):
        v = ScoringResultValidator(carc_codes=frozenset())
        result = v.validate_row(_valid_row(RISK_SCORE=2.0, CLAIM_ID=None))
        msgs = result.failure_messages
        assert len(msgs) == 2
        assert any("RISK_SCORE" in m for m in msgs)
        assert any("CLAIM_ID" in m for m in msgs)


class TestInlineBatchValidate:
    def test_all_valid_rows_succeeds(self):
        v = ScoringResultValidator(carc_codes=frozenset())
        rows = [_valid_row(CLAIM_ID=f"CLM-{i}", SCORE_ID=f"SCR-{i}") for i in range(5)]
        result = v._inline_batch_validate(rows)
        assert result["success"]
        assert result["n_rows"] == 5
        assert result["n_failed"] == 0

    def test_one_bad_row_fails_batch(self):
        v = ScoringResultValidator(carc_codes=frozenset())
        rows = [
            _valid_row(CLAIM_ID="CLM-0", SCORE_ID="SCR-0"),
            _valid_row(CLAIM_ID=None, SCORE_ID="SCR-1"),  # bad
        ]
        result = v._inline_batch_validate(rows)
        assert not result["success"]
        assert result["n_failed"] == 1

    def test_empty_batch_succeeds(self):
        v = ScoringResultValidator(carc_codes=frozenset())
        result = v._inline_batch_validate([])
        assert result["success"]
        assert result["n_rows"] == 0
