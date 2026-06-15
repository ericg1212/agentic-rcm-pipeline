"""Tests for the NCCI gate — validates routing logic against seed PTP and MUE tables."""
import pytest

from src.consumer.ncci_gate import NCCIGate, Route, ViolationType


@pytest.fixture(scope="module")
def gate():
    g = NCCIGate(edit_version="2026Q3")
    g.load()
    return g


def _claim(procedure_codes, modifiers=None, units=1):
    return {
        "claim_id": "test-claim-001",
        "payer_id": "MEDICARE_FFS",
        "procedure_codes": procedure_codes,
        "modifiers": modifiers or [],
        "units": units,
        "submitted_charge": "150.00",
        "is_holdout": False,
    }


def test_clean_single_code_passes(gate):
    decision = gate.evaluate(_claim(["99215"]))
    assert decision.route == Route.PASS
    assert decision.risk_score == 0.0
    assert not decision.violations


def test_ptp_hard_fail_no_modifier(gate):
    """93000 + 93005 with modifier_indicator=0 — no bypass possible."""
    decision = gate.evaluate(_claim(["93000", "93005"]))
    assert decision.route == Route.HARD_FAIL
    assert decision.risk_score > 0.5
    assert any(v.violation_type == ViolationType.PTP_NO_BYPASS for v in decision.violations)
    assert decision.deterministic_carc == "CO-97"


def test_ptp_ambiguous_with_bypass_modifier(gate):
    """20610 + 99213 with modifier_indicator=1 and modifier 59 — ambiguous, must go to LLM."""
    decision = gate.evaluate(_claim(["20610", "99213"], modifiers=["59"]))
    assert decision.route == Route.AMBIGUOUS
    assert any(v.violation_type == ViolationType.PTP_BYPASS_UNVERIFIED for v in decision.violations)


def test_ptp_hard_fail_modifier_indicator_1_no_modifier(gate):
    """modifier_indicator=1 but no modifier present → hard fail."""
    decision = gate.evaluate(_claim(["20610", "99213"], modifiers=[]))
    assert decision.route == Route.HARD_FAIL


def test_mue_exceeded(gate):
    """99213 submitted with 3 units (MUE limit = 1) → hard fail."""
    decision = gate.evaluate(_claim(["99213"], units=3))
    assert decision.route == Route.HARD_FAIL
    mue_violations = [v for v in decision.violations if v.violation_type == ViolationType.MUE_EXCEEDED]
    assert mue_violations
    assert mue_violations[0].mue_limit == 1
    assert mue_violations[0].units_submitted == 3


def test_mue_within_limit_passes(gate):
    """36415 with 2 units (MUE limit = 3) → passes MUE check."""
    decision = gate.evaluate(_claim(["36415"], units=2))
    assert decision.route == Route.PASS


def test_combined_hard_fail_and_ambiguous_routes_ambiguous(gate):
    """If both a hard fail and an ambiguous violation exist, route = AMBIGUOUS."""
    # 99213+99211 (hard fail) + 20610+99213 with modifier 59 (ambiguous)
    # Combining: 20610, 99213, 99211 with modifier 59
    decision = gate.evaluate(_claim(["99213", "20610", "99211"], modifiers=["59"]))
    assert decision.route == Route.AMBIGUOUS
