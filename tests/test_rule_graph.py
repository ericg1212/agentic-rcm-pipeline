# Copyright (c) 2026 Eric Grynspan. All rights reserved.
"""
Tests for Phase 2 Component 1 — Payer Rule Intelligence Graph.
Covers: load_from_seed, resolve_contractor, lookup, tool interface, ingestion.
"""
from __future__ import annotations

import pytest

from src.intelligence.rule_graph import PayerRuleGraph, RuleEntry
from src.intelligence.ingestion import ingest_from_seed, ingest_lcd_rules


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def graph() -> PayerRuleGraph:
    g = PayerRuleGraph()
    g.load_from_seed()
    return g


# ---------------------------------------------------------------------------
# RuleEntry unit tests
# ---------------------------------------------------------------------------

class TestRuleEntry:
    def test_covers_diagnosis_matching_prefix(self):
        entry = RuleEntry(
            rule_id="t1", source_type="lcd", contractor_id="13101",
            hcpcs_code="27447", icd10_prefixes=["M17", "M16"],
            coverage_status="covered_with_restrictions",
            requires_prior_auth=True, pa_criteria="MN required",
            gold_card_eligible=False, typical_denial_code="CO-50",
            effective_date="2024-01-01", expiration_date=None,
        )
        assert entry.covers_diagnosis("M17.11") is True

    def test_covers_diagnosis_non_matching_prefix(self):
        entry = RuleEntry(
            rule_id="t2", source_type="lcd", contractor_id="13101",
            hcpcs_code="27447", icd10_prefixes=["M17"],
            coverage_status="covered_with_restrictions",
            requires_prior_auth=True, pa_criteria=None,
            gold_card_eligible=False, typical_denial_code=None,
            effective_date="2024-01-01", expiration_date=None,
        )
        assert entry.covers_diagnosis("Z99.9") is False

    def test_covers_diagnosis_empty_prefixes_is_broad(self):
        entry = RuleEntry(
            rule_id="t3", source_type="ncd", contractor_id=None,
            hcpcs_code="99213", icd10_prefixes=[],
            coverage_status="covered", requires_prior_auth=False,
            pa_criteria=None, gold_card_eligible=False,
            typical_denial_code=None, effective_date="2024-01-01",
            expiration_date=None,
        )
        assert entry.covers_diagnosis("Z00.00") is True


# ---------------------------------------------------------------------------
# Load from seed
# ---------------------------------------------------------------------------

class TestLoadFromSeed:
    def test_loads_without_error(self, graph):
        assert len(graph._rules) > 0

    def test_known_procedure_present(self, graph):
        assert "27447" in graph._rules

    def test_mac_jurisdiction_loaded(self, graph):
        assert len(graph._mac_jurisdiction) > 0

    def test_payer_registry_loaded(self, graph):
        assert len(graph._payer_registry) > 0

    def test_reload_is_idempotent(self, graph):
        count_before = len(graph._rules)
        graph.reload()
        assert len(graph._rules) == count_before


# ---------------------------------------------------------------------------
# Contractor resolution
# ---------------------------------------------------------------------------

class TestResolveContractor:
    def test_known_payer_resolves(self, graph):
        cid = graph.resolve_contractor("BCBS_NY")
        assert cid is not None

    def test_medicare_ffs_returns_none_ncd_fallback(self, graph):
        # MEDICARE_FFS has empty state list → NCD fallback → contractor_id=None
        cid = graph.resolve_contractor("MEDICARE_FFS")
        assert cid is None

    def test_unknown_payer_returns_none(self, graph):
        cid = graph.resolve_contractor("UNKNOWN_PAYER_XYZ")
        assert cid is None

    def test_multi_state_payer_returns_dominant_mac(self, graph):
        # UHC_COMMERCIAL spans multiple states; should return a valid contractor_id
        cid = graph.resolve_contractor("UHC_COMMERCIAL")
        assert cid is not None


# ---------------------------------------------------------------------------
# Lookup
# ---------------------------------------------------------------------------

class TestLookup:
    def test_known_procedure_found(self, graph):
        result = graph.lookup("27447", None)
        assert result["found"] is True
        assert result["hcpcs_code"] == "27447"

    def test_unknown_procedure_not_found(self, graph):
        result = graph.lookup("XXXXX", None)
        assert result["found"] is False

    def test_requires_prior_auth_on_surgical(self, graph):
        result = graph.lookup("27447", None)
        assert result["requires_prior_auth"] is True

    def test_pa_criteria_populated_for_pa_required(self, graph):
        result = graph.lookup("27447", None)
        assert result["pa_criteria"] is not None and len(result["pa_criteria"]) > 0

    def test_matched_diagnosis_sets_covered(self, graph):
        result = graph.lookup("27447", None, icd10_code="M17.11")
        assert result["diagnosis_covered"] is True
        assert result["conflict_type"] is None

    def test_unmatched_diagnosis_sets_conflict_type(self, graph):
        result = graph.lookup("27447", None, icd10_code="Z99.99")
        assert result["diagnosis_covered"] is False
        assert result["conflict_type"] == "lcd_adds_restriction"

    def test_no_diagnosis_sets_diagnosis_covered_none(self, graph):
        result = graph.lookup("27447", None)
        assert result["diagnosis_covered"] is None
        assert result["conflict_type"] is None

    def test_medicare_ffs_resolves_via_ncd(self, graph):
        # MEDICARE_FFS → contractor_id=None → NCD lookup
        result = graph.lookup("27447", "MEDICARE_FFS")
        assert result["found"] is True

    def test_commercial_payer_resolves_rule(self, graph):
        result = graph.lookup("27447", "BCBS_NY")
        assert result["found"] is True


# ---------------------------------------------------------------------------
# Tool interface
# ---------------------------------------------------------------------------

class TestToolInterface:
    def test_get_rule_for_tool_returns_dict(self, graph):
        result = graph.get_rule_for_tool("UHC_COMMERCIAL", "27447", "M17.11")
        assert isinstance(result, dict)
        assert result["found"] is True

    def test_get_rule_for_tool_unknown_procedure(self, graph):
        result = graph.get_rule_for_tool("UHC_COMMERCIAL", "ZZZZZ", None)
        assert result["found"] is False


# ---------------------------------------------------------------------------
# Ingestion
# ---------------------------------------------------------------------------

class TestIngestion:
    def test_ingest_from_seed_returns_rows(self):
        rows = ingest_from_seed()
        assert len(rows) > 0

    def test_ingest_seed_rows_have_required_fields(self):
        rows = ingest_from_seed()
        required = {"RULE_ID", "SOURCE_TYPE", "HCPCS_CODE", "COVERAGE_STATUS",
                    "REQUIRES_PRIOR_AUTH", "EFFECTIVE_DATE", "INGESTED_AT"}
        for row in rows:
            assert required.issubset(row.keys()), f"Missing fields in row: {row}"

    def test_ingest_lcd_rules_stamps_contractor_id(self):
        rows = ingest_lcd_rules("13101")
        lcd_rows = [r for r in rows if r["SOURCE_TYPE"] == "lcd"]
        assert all(r["CONTRACTOR_ID"] == "13101" for r in lcd_rows)
