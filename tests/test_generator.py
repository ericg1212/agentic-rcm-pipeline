# Copyright (c) 2026 Eric Grynspan. All rights reserved.
"""Tests for the live claim generator — validates statistical properties and schema compliance."""
import pytest

from src.generator.claim_generator import ClaimGenerator, ClaimEvent, NCCI_VIOLATION_PAIRS
from src.generator.distributions import SEED_PROCEDURE_WEIGHTS


@pytest.fixture
def generator():
    return ClaimGenerator(holdout_fraction=0.10, dirty_fraction=0.35, seed=42)


def test_clean_claim_structure(generator):
    claim = generator._generate_clean_claim(is_holdout=False)
    assert isinstance(claim, ClaimEvent)
    assert len(claim.claim_id) == 36  # UUID format
    assert len(claim.procedure_codes) == 1
    assert claim.units >= 1
    assert float(claim.submitted_charge) > 0
    assert claim.is_holdout is False


def test_dirty_claim_has_violation_pair(generator):
    claim = generator._generate_dirty_claim(is_holdout=False)
    all_pairs = [(a, b) for a, b in NCCI_VIOLATION_PAIRS] + [(b, a) for a, b in NCCI_VIOLATION_PAIRS]
    codes = claim.procedure_codes
    assert len(codes) == 2
    assert (codes[0], codes[1]) in all_pairs or (codes[1], codes[0]) in all_pairs


def test_holdout_flag_propagates(generator):
    claim = generator._generate_clean_claim(is_holdout=True)
    assert claim.is_holdout is True


def test_generate_batch_dirty_fraction(generator):
    n = 500
    claims = [generator.generate_one() for _ in range(n)]
    dirty = [c for c in claims if len(c.procedure_codes) == 2]
    ratio = len(dirty) / n
    # Allow +/- 10% variance from 35% dirty_fraction target
    assert 0.25 <= ratio <= 0.45, f"Dirty fraction out of expected range: {ratio:.2%}"


def test_holdout_fraction(generator):
    n = 1000
    claims = [generator.generate_one() for _ in range(n)]
    holdout_ratio = sum(1 for c in claims if c.is_holdout) / n
    # Allow +/- 5% variance from 10% holdout target
    assert 0.05 <= holdout_ratio <= 0.15, f"Holdout fraction out of range: {holdout_ratio:.2%}"


def test_procedure_codes_in_known_set(generator):
    known_codes = {w.hcpcs_code for w in SEED_PROCEDURE_WEIGHTS}
    for _ in range(50):
        claim = generator._generate_clean_claim(is_holdout=False)
        assert claim.procedure_codes[0] in known_codes


def test_submitted_charge_is_decimal_string(generator):
    claim = generator._generate_clean_claim(is_holdout=False)
    # Must be parseable as float and have 2 decimal places
    parts = claim.submitted_charge.split(".")
    assert len(parts) == 2
    assert len(parts[1]) == 2
    assert float(claim.submitted_charge) > 0
