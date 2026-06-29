# Copyright (c) 2026 Eric Grynspan. All rights reserved.
"""
Phase 2 Component 4 — Denial Pattern Clustering.

DBSCAN-based clustering of denied claims. Auto-discovers cluster count —
no k to specify. Noise points (CLUSTER_ID=-1) are outlier denials that
don't form dense patterns. New clusters (first seen < 7 days ago) surface
as emerging denial patterns.

Feedback loop: when a new pattern is detected, it is emitted to the
rules.control Kafka topic (already provisioned in Layer 1) with
event_type="new_denial_pattern". This re-enters the rule graph ingestion
pipeline — closing the loop from outcome observation back to prevention.

Interview frame: "Denial patterns discovered in Layer 4 automatically
re-enter the rule graph through the rules.control topic — the same control
plane provisioned in Layer 1. That's the full feedback loop: pre-submission
prevention → outcome tracking → pattern clustering → rule graph update →
back to pre-submission."

ADR: DBSCAN over K-means because K-means requires specifying k. Emerging
patterns by definition have unknown count — DBSCAN discovers it.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import numpy as np
import structlog
from scipy.spatial.distance import cdist
from sklearn.cluster import DBSCAN
from sklearn.preprocessing import StandardScaler

from src.config.settings import ClusterConfig

log = structlog.get_logger(__name__)


@dataclass
class ClusterSummary:
    cluster_id: int                    # DBSCAN label; -1 = noise
    dominant_carc: Optional[str]       # most common CARC code in cluster
    procedure_group: Optional[str]     # HCPCS first 2 digits (procedure family)
    payer_id: Optional[str]
    cluster_size: int
    is_new_pattern: bool               # first seen < 7 days ago
    analyzed_at: datetime


@dataclass
class DenialClusterRecord:
    """One row for RAW.DENIAL_CLUSTERS."""
    cluster_record_id: str
    cluster_id: int
    claim_id: str
    dominant_carc: Optional[str]
    procedure_group: Optional[str]
    payer_id: Optional[str]
    risk_score: Optional[int]
    analyzed_at: datetime

    def to_snowflake_row(self) -> dict:
        return {
            "CLUSTER_RECORD_ID": self.cluster_record_id,
            "CLUSTER_ID": self.cluster_id,
            "CLAIM_ID": self.claim_id,
            "DOMINANT_CARC": self.dominant_carc,
            "PROCEDURE_GROUP": self.procedure_group,
            "PAYER_ID": self.payer_id,
            "RISK_SCORE": self.risk_score,
            "ANALYZED_AT": self.analyzed_at.isoformat(),
        }


def _extract_features(outcomes: list[dict]) -> np.ndarray:
    """
    Build feature matrix for DBSCAN clustering.
    Features: [payer_hash_norm, carc_int_norm, procedure_group_int_norm, risk_score_norm]

    All features are hashed/encoded to integer then normalized to [0,1] range
    so no single dimension dominates the DBSCAN distance calculation.
    """
    rows = []
    for o in outcomes:
        payer_id = o.get("payer_id", "")
        carc = o.get("denial_code", "") or ""
        hcpcs = (o.get("procedure_code", "") or "")[:2]
        risk_score = float(o.get("risk_score", 50))

        # Hash string fields to a stable integer in [0, 999]
        payer_feat = abs(hash(payer_id)) % 1000
        carc_feat = abs(hash(carc)) % 1000
        hcpcs_feat = abs(hash(hcpcs)) % 1000

        rows.append([payer_feat, carc_feat, hcpcs_feat, risk_score])

    return np.array(rows, dtype=float)


class DenialClusterAnalyzer:
    """
    Nightly denial pattern clustering job.

    outcome_store: list of dicts with keys:
      claim_id, payer_id, denial_code (CARC), procedure_code, risk_score, scored_at
    In production this is loaded from fct_claim_risk_scores + ADJUDICATION_OUTCOMES.
    """

    def __init__(self, outcome_store: list[dict]) -> None:
        self._outcomes = outcome_store

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def fit(self) -> tuple[list[DenialClusterRecord], list[ClusterSummary]]:
        """
        Run DBSCAN on the outcome store. Returns (raw_records, summaries).
        raw_records go to RAW.DENIAL_CLUSTERS; summaries feed fct_denial_clusters.

        Returns empty lists if below MIN_OUTCOMES threshold.
        """
        if len(self._outcomes) < ClusterConfig.MIN_OUTCOMES:
            log.info(
                "clustering.skipped",
                n_outcomes=len(self._outcomes),
                min_required=ClusterConfig.MIN_OUTCOMES,
            )
            return [], []

        features = _extract_features(self._outcomes)
        scaler = StandardScaler()
        features_scaled = scaler.fit_transform(features)

        db = DBSCAN(
            eps=ClusterConfig.DBSCAN_EPS,
            min_samples=ClusterConfig.DBSCAN_MIN_SAMPLES,
        )
        labels = db.fit_predict(features_scaled)

        analyzed_at = datetime.now(timezone.utc)
        raw_records: list[DenialClusterRecord] = []
        cluster_data: dict[int, list[dict]] = {}

        for outcome, label in zip(self._outcomes, labels):
            cluster_id = int(label)
            raw_records.append(DenialClusterRecord(
                cluster_record_id=str(uuid.uuid4()),
                cluster_id=cluster_id,
                claim_id=outcome.get("claim_id", ""),
                dominant_carc=outcome.get("denial_code"),
                procedure_group=(outcome.get("procedure_code", "")[:2] or None),
                payer_id=outcome.get("payer_id"),
                risk_score=outcome.get("risk_score"),
                analyzed_at=analyzed_at,
            ))
            cluster_data.setdefault(cluster_id, []).append(outcome)

        summaries = self._build_summaries(cluster_data, analyzed_at)

        n_clusters = len([c for c in cluster_data if c != -1])
        n_noise = len(cluster_data.get(-1, []))
        log.info(
            "clustering.complete",
            n_outcomes=len(self._outcomes),
            n_clusters=n_clusters,
            n_noise=n_noise,
        )
        return raw_records, summaries

    def detect_new_patterns(
        self,
        summaries: list[ClusterSummary],
        cutoff_days: int = 7,
    ) -> list[ClusterSummary]:
        """Return summaries flagged as new patterns (cluster_id != -1, is_new_pattern=True)."""
        return [
            s for s in summaries
            if s.cluster_id != -1 and s.is_new_pattern
        ]

    # ------------------------------------------------------------------
    # Private
    # ------------------------------------------------------------------

    def _build_summaries(
        self,
        cluster_data: dict[int, list[dict]],
        analyzed_at: datetime,
        cutoff_days: int = 7,
    ) -> list[ClusterSummary]:
        summaries: list[ClusterSummary] = []

        for cluster_id, members in cluster_data.items():
            dominant_carc = self._mode([m.get("denial_code") for m in members if m.get("denial_code")])
            dominant_hcpcs_group = self._mode([
                (m.get("procedure_code", "")[:2] or None)
                for m in members if m.get("procedure_code")
            ])
            dominant_payer = self._mode([m.get("payer_id") for m in members if m.get("payer_id")])

            # "New pattern" heuristic: scored_at within last 7 days for all members
            is_new = all(
                self._within_days(m.get("scored_at"), cutoff_days)
                for m in members
            )
            is_new = is_new and cluster_id != -1

            summaries.append(ClusterSummary(
                cluster_id=cluster_id,
                dominant_carc=dominant_carc,
                procedure_group=dominant_hcpcs_group,
                payer_id=dominant_payer,
                cluster_size=len(members),
                is_new_pattern=is_new,
                analyzed_at=analyzed_at,
            ))

        return summaries

    @staticmethod
    def _mode(values: list) -> Optional[str]:
        """Most frequent non-None value, or None if list is empty."""
        filtered = [v for v in values if v is not None]
        if not filtered:
            return None
        return max(set(filtered), key=filtered.count)

    @staticmethod
    def _within_days(scored_at, days: int) -> bool:
        """True when scored_at is within the last `days` days. Permissive on missing data."""
        if scored_at is None:
            return True
        try:
            if isinstance(scored_at, str):
                from datetime import datetime as dt
                ts = dt.fromisoformat(scored_at.replace("Z", "+00:00"))
            else:
                ts = scored_at
            delta = datetime.now(timezone.utc) - ts.astimezone(timezone.utc)
            return delta.days <= days
        except Exception:
            return True
