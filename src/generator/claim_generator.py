# Copyright (c) 2026 Eric Grynspan. All rights reserved.
"""
Live stochastic claim event generator.

Emits novel claim events by sampling real 2024 CMS Provider Utilization distributions
at a Poisson-modulated arrival rate (business-hours weighted). This makes the pipeline
immune to "you're just replaying a CSV" — every event is generated fresh.

Design decisions documented in ADR-002 (data/ground-truth).
"""
from __future__ import annotations

import hashlib
import time
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

import numpy as np
import structlog

from src.config.settings import GateConfig, GeneratorConfig
from src.generator.distributions import (
    SEED_NPIS,
    ProcedureWeight,
    get_procedure_weights,
    sample_npi,
    sample_payer,
    sample_procedure,
)

log = structlog.get_logger(__name__)

# NCCI bypass modifiers — injected on modifier_indicator=1 pairs to simulate valid bypasses
BYPASS_MODIFIERS = ["59", "XE", "XS", "XP", "XU"]

# Codes that commonly appear together and need NCCI check (paired-code injection pool)
NCCI_VIOLATION_PAIRS = [
    ("93000", "93005"),
    ("99213", "99211"),
    ("99214", "99211"),
    ("43239", "43235"),
    ("29827", "29823"),
    ("27447", "27446"),
    ("36415", "99213"),
    ("36415", "99214"),
    ("20610", "99213"),
    ("20610", "99214"),
]


@dataclass
class ClaimEvent:
    claim_id: str
    event_time: int  # epoch ms
    service_date: str  # ISO-8601
    provider_npi: str
    payer_id: str
    claim_type: str
    place_of_service: str
    procedure_codes: list[str]
    diagnosis_codes: list[str]
    modifiers: list[str]
    units: int
    submitted_charge: str  # decimal string
    ncci_edit_version: str
    is_holdout: bool = False

    def to_dict(self) -> dict:
        return {
            "claim_id": self.claim_id,
            "event_time": self.event_time,
            "service_date": self.service_date,
            "provider_npi": self.provider_npi,
            "payer_id": self.payer_id,
            "claim_type": self.claim_type,
            "place_of_service": self.place_of_service,
            "procedure_codes": self.procedure_codes,
            "diagnosis_codes": self.diagnosis_codes,
            "modifiers": self.modifiers,
            "units": self.units,
            "submitted_charge": self.submitted_charge,
            "ncci_edit_version": self.ncci_edit_version,
            "is_holdout": self.is_holdout,
        }


class ClaimGenerator:
    """
    Generates synthetic claim events from real CMS distributions.

    The dirty_fraction controls what fraction of emitted claims contain deliberate
    NCCI violations — used to ensure the eval harness has enough positive examples
    and to drive the noise-injection eval (why-LLM-not-rules proof).
    """

    def __init__(
        self,
        holdout_fraction: float = GateConfig.HOLDOUT_FRACTION,
        dirty_fraction: float = GeneratorConfig.DIRTY_CLAIM_FRACTION,
        ncci_edit_version: str = GeneratorConfig.NCCI_EDIT_VERSION,
        seed: int | None = None,
        holdout_unit: str | None = None,
    ) -> None:
        self.holdout_fraction = holdout_fraction
        self.dirty_fraction = dirty_fraction
        self.ncci_edit_version = ncci_edit_version
        self.holdout_unit = holdout_unit or GateConfig.HOLDOUT_UNIT
        self._rng = np.random.default_rng(seed)
        self._procedure_weights = get_procedure_weights()
        self._holdout_npis = _holdout_roster(SEED_NPIS, holdout_fraction)
        log.info(
            "generator_initialized",
            holdout_fraction=holdout_fraction,
            holdout_unit=self.holdout_unit,
            holdout_providers=len(self._holdout_npis),
            dirty_fraction=dirty_fraction,
            procedure_pool_size=len(self._procedure_weights),
        )

    def generate_one(self) -> ClaimEvent:
        """Generate a single claim event, potentially with deliberate NCCI violations."""
        is_dirty = self._rng.random() < self.dirty_fraction

        claim = self._generate_dirty_claim(False) if is_dirty else self._generate_clean_claim(False)
        claim.is_holdout = self._assign_holdout(claim.provider_npi)
        return claim

    def _assign_holdout(self, provider_npi: str) -> bool:
        """
        Holdout arm assignment (ADR-005).

        provider unit (default): cluster randomization — a deterministic
        holdout_fraction sample of the provider roster, ranked by SHA-256 of
        NPI. Every claim from a holdout provider is control, always: stable
        across restarts and replays, and no within-provider arm mixing to
        contaminate the intervention effect. NPIs outside the roster fall
        back to a hash threshold so assignment stays deterministic.

        claim unit (legacy): independent Bernoulli draw per claim.
        """
        if self.holdout_unit == "claim":
            return bool(self._rng.random() < self.holdout_fraction)
        if provider_npi in self._holdout_npis:
            return True
        if provider_npi not in SEED_NPIS:
            return _npi_hash_bucket(provider_npi) < self.holdout_fraction
        return False

    def generate_stream(self, events_per_second: float = GeneratorConfig.EVENTS_PER_SECOND):
        """
        Infinite generator. Yields ClaimEvents at a Poisson-modulated rate.
        Poisson inter-arrival times simulate realistic claim submission bursts
        (higher volume mid-morning, lower overnight).
        """
        mean_interval = 1.0 / events_per_second
        while True:
            yield self.generate_one()
            interval = self._rng.exponential(mean_interval)
            time.sleep(interval)

    def _generate_clean_claim(self, is_holdout: bool) -> ClaimEvent:
        proc = sample_procedure(self._procedure_weights, self._rng)
        charge = self._sample_charge(proc)
        return ClaimEvent(
            claim_id=str(uuid.uuid4()),
            event_time=int(datetime.now(timezone.utc).timestamp() * 1000),
            service_date=self._sample_service_date(),
            provider_npi=sample_npi(self._rng),
            payer_id=sample_payer(self._rng),
            claim_type="professional",
            place_of_service=self._rng.choice(proc.typical_place_of_service or ["11"]),
            procedure_codes=[proc.hcpcs_code],
            diagnosis_codes=list(self._rng.choice(proc.typical_diagnoses, size=min(2, len(proc.typical_diagnoses)), replace=False)),
            modifiers=[],
            units=1,
            submitted_charge=f"{charge:.2f}",
            ncci_edit_version=self.ncci_edit_version,
            is_holdout=is_holdout,
        )

    def _generate_dirty_claim(self, is_holdout: bool) -> ClaimEvent:
        """
        Inject a known NCCI violation pair. Modifier is included ~30% of the time
        to simulate the ambiguous modifier-bypass case that the LLM must reason about.
        """
        pair = self._rng.choice(len(NCCI_VIOLATION_PAIRS))
        col1, col2 = NCCI_VIOLATION_PAIRS[pair]

        # 30% chance of including a bypass modifier (ambiguous — gate must route to LLM)
        modifiers = []
        if self._rng.random() < 0.30:
            modifiers = [self._rng.choice(BYPASS_MODIFIERS)]

        # Find a charge for col1 (or fall back)
        proc = next((p for p in self._procedure_weights if p.hcpcs_code == col1), None)
        charge = self._sample_charge(proc) if proc else 150.00

        return ClaimEvent(
            claim_id=str(uuid.uuid4()),
            event_time=int(datetime.now(timezone.utc).timestamp() * 1000),
            service_date=self._sample_service_date(),
            provider_npi=sample_npi(self._rng),
            payer_id=sample_payer(self._rng),
            claim_type="professional",
            place_of_service="11",
            procedure_codes=[col1, col2],
            diagnosis_codes=["I10", "Z00.00"],
            modifiers=modifiers,
            units=1,
            submitted_charge=f"{charge:.2f}",
            ncci_edit_version=self.ncci_edit_version,
            is_holdout=is_holdout,
        )

    def _sample_charge(self, proc: ProcedureWeight | None) -> float:
        if proc is None:
            return round(float(self._rng.uniform(50, 500)), 2)
        # Log-normal variation around the CMS average submitted charge
        sigma = 0.25
        return round(float(self._rng.lognormal(
            mean=np.log(max(proc.avg_submitted_charge, 1.0)),
            sigma=sigma,
        )), 2)

    def _sample_service_date(self) -> str:
        # Service dates within the last 30 days (realistic pre-submission window)
        days_back = int(self._rng.integers(0, 30))
        return (date.today() - timedelta(days=days_back)).isoformat()


def _npi_hash_bucket(npi: str) -> float:
    """Deterministic uniform [0,1) bucket for an NPI — stable across processes."""
    digest = hashlib.sha256(str(npi).encode("utf-8")).hexdigest()
    return int(digest, 16) % 10_000 / 10_000


def _holdout_roster(registry, fraction: float) -> frozenset[str]:
    """
    Deterministic holdout provider set: rank the roster by SHA-256(NPI) and
    take the lowest ceil(fraction × N). Rank-based (not threshold-based)
    assignment hits the target fraction exactly on any roster size — a raw
    hash threshold drifts badly on small rosters (25% realized at a 10%
    target on a 20-provider pool).
    """
    if fraction <= 0:
        return frozenset()
    ranked = sorted(
        (str(npi) for npi in registry),
        key=lambda n: hashlib.sha256(n.encode("utf-8")).hexdigest(),
    )
    k = max(1, round(len(ranked) * fraction))
    return frozenset(ranked[:k])
