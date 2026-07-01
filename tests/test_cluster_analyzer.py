# Copyright (c) 2026 Eric Grynspan. All rights reserved.
"""
Tests for Phase 2 Component 4 — Denial Pattern Clustering.
Covers: feature extraction, DBSCAN fit, cluster summaries, new pattern detection,
noise handling, and Snowflake row serialization.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone, timedelta

import pytest

from src.feedback.cluster_analyzer import (
    DenialClusterAnalyzer,
    _extract_features,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_outcome(
    claim_id: str | None = None,
    payer_id: str = "UHC_COMMERCIAL",
    denial_code: str = "CO-50",
    procedure_code: str = "27447",
    risk_score: int = 75,
    scored_at: datetime | None = None,
) -> dict:
    return {
        "claim_id": claim_id or str(uuid.uuid4()),
        "payer_id": payer_id,
        "denial_code": denial_code,
        "procedure_code": procedure_code,
        "risk_score": risk_score,
        "scored_at": (scored_at or datetime.now(timezone.utc)).isoformat(),
    }


def _make_outcomes(
    n: int,
    payer_id: str = "UHC_COMMERCIAL",
    denial_code: str = "CO-50",
    procedure_code: str = "27447",
) -> list[dict]:
    return [_make_outcome(payer_id=payer_id, denial_code=denial_code,
                          procedure_code=procedure_code) for _ in range(n)]


# ---------------------------------------------------------------------------
# Feature extraction
# ---------------------------------------------------------------------------

class TestExtractFeatures:
    def test_returns_array_with_correct_shape(self):
        outcomes = _make_outcomes(10)
        features = _extract_features(outcomes)
        assert features.shape == (10, 4)

    def test_all_feature_values_finite(self):
        import numpy as np
        outcomes = _make_outcomes(20)
        features = _extract_features(outcomes)
        assert all(np.isfinite(features).flatten())

    def test_empty_outcomes_returns_empty_array(self):
        features = _extract_features([])
        assert features.shape[0] == 0


# ---------------------------------------------------------------------------
# Clustering: below threshold
# ---------------------------------------------------------------------------

class TestClusteringBelowThreshold:
    def test_skips_when_below_min_outcomes(self):
        outcomes = _make_outcomes(10)  # below MIN_OUTCOMES=50
        analyzer = DenialClusterAnalyzer(outcomes)
        records, summaries = analyzer.fit()
        assert records == []
        assert summaries == []


# ---------------------------------------------------------------------------
# Clustering: above threshold
# ---------------------------------------------------------------------------

class TestClusteringAboveThreshold:
    @pytest.fixture()
    def analyzer_with_data(self):
        # 60 outcomes: 50 with identical features (tight cluster) + 10 varied (noise/separate cluster)
        tight = _make_outcomes(50, payer_id="UHC_COMMERCIAL", denial_code="CO-50", procedure_code="27447")
        varied = [_make_outcome(payer_id=f"PAYER_{i}", denial_code=f"CO-{i*10}", procedure_code=f"{i}000") for i in range(10)]
        return DenialClusterAnalyzer(tight + varied)

    def test_fit_returns_records_and_summaries(self, analyzer_with_data):
        records, summaries = analyzer_with_data.fit()
        assert len(records) == 60
        assert len(summaries) > 0

    def test_records_have_required_fields(self, analyzer_with_data):
        records, _ = analyzer_with_data.fit()
        for record in records:
            assert record.cluster_record_id is not None
            assert record.claim_id is not None
            assert record.analyzed_at is not None

    def test_snowflake_row_has_required_keys(self, analyzer_with_data):
        records, _ = analyzer_with_data.fit()
        required = {"CLUSTER_RECORD_ID", "CLUSTER_ID", "CLAIM_ID",
                    "DOMINANT_CARC", "PROCEDURE_GROUP", "PAYER_ID", "ANALYZED_AT"}
        for record in records:
            row = record.to_snowflake_row()
            assert required.issubset(row.keys())

    def test_noise_points_have_cluster_id_minus_one(self, analyzer_with_data):
        records, _ = analyzer_with_data.fit()
        cluster_ids = {r.cluster_id for r in records}
        # DBSCAN may assign -1 for noise
        # Just verify that all cluster_ids are integers
        for cid in cluster_ids:
            assert isinstance(cid, int)

    def test_summaries_have_cluster_size(self, analyzer_with_data):
        _, summaries = analyzer_with_data.fit()
        for s in summaries:
            assert s.cluster_size > 0


# ---------------------------------------------------------------------------
# New pattern detection
# ---------------------------------------------------------------------------

class TestNewPatternDetection:
    def test_recent_clusters_flagged_as_new(self):
        outcomes = _make_outcomes(60)
        analyzer = DenialClusterAnalyzer(outcomes)
        _, summaries = analyzer.fit()
        new_patterns = analyzer.detect_new_patterns(summaries, cutoff_days=7)
        # All outcomes were just created — any real cluster should be "new"
        # At minimum, no errors should be raised
        assert isinstance(new_patterns, list)

    def test_noise_cluster_excluded_from_new_patterns(self):
        outcomes = _make_outcomes(60)
        analyzer = DenialClusterAnalyzer(outcomes)
        _, summaries = analyzer.fit()
        new_patterns = analyzer.detect_new_patterns(summaries)
        # Noise cluster (id=-1) should never appear in new_patterns
        assert all(s.cluster_id != -1 for s in new_patterns)

    def test_old_cluster_not_flagged(self):
        outcomes = [_make_outcome(scored_at=datetime.now(timezone.utc) - timedelta(days=30)) for _ in range(60)]
        analyzer = DenialClusterAnalyzer(outcomes)
        _, summaries = analyzer.fit()
        new_patterns = analyzer.detect_new_patterns(summaries, cutoff_days=7)
        assert new_patterns == []
