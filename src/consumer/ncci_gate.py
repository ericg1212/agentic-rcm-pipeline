"""
Layer 1 — Deterministic NCCI Gate.

Applies NCCI PTP (Procedure-to-Procedure) and MUE (Medically Unlikely Edit) checks
against a loaded ruleset before the LLM is ever called.

Gate routing logic (the interview-critical design decision):
  PASS      → claim clears all NCCI checks; no LLM needed; route to clearinghouse
  HARD_FAIL → clear deterministic violation with no valid bypass modifier; flag + CARC;
              LLM may still be called for rationale enrichment on high-value claims
  AMBIGUOUS → modifier present on a modifier_indicator=1 pair (bypass may be valid);
              or multi-code claim with complex interaction; must route to LLM

The gate handles the confident majority deterministically, routing only the ambiguous
slice to the LLM. This is the cost/latency defense: "X% of claims never touch the LLM."

NCCI edit rules:
  PTP modifier_indicator=0: no bypass modifier is ever valid → HARD_FAIL
  PTP modifier_indicator=1: bypass is valid with appropriate modifier (59/XE/XS/XP/XU)
    - modifier present → AMBIGUOUS (LLM must verify the bypass is clinically appropriate)
    - modifier absent  → HARD_FAIL
  MUE: units submitted > MUE limit → HARD_FAIL (no modifier bypass exists for MUE)
"""
from __future__ import annotations

import csv
import enum
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import NamedTuple

import structlog

from src.config.settings import DataConfig, GateConfig

log = structlog.get_logger(__name__)

# NCCI bypass modifier codes — presence on a modifier_indicator=1 pair triggers AMBIGUOUS
BYPASS_MODIFIERS: frozenset[str] = frozenset(["59", "XE", "XS", "XP", "XU"])


class Route(str, enum.Enum):
    PASS = "pass"
    HARD_FAIL = "hard_fail"
    AMBIGUOUS = "ambiguous"


class ViolationType(str, enum.Enum):
    MUE_EXCEEDED = "mue_exceeded"
    PTP_NO_BYPASS = "ptp_no_bypass"
    PTP_BYPASS_UNVERIFIED = "ptp_bypass_unverified"


@dataclass
class NCCIViolation:
    violation_type: ViolationType
    code: str
    col2_code: str | None = None
    units_submitted: int | None = None
    mue_limit: int | None = None
    modifier_indicator: str | None = None
    carc_code: str = "CO-97"

    def to_dict(self) -> dict:
        return {
            "violation_type": self.violation_type.value,
            "code": self.code,
            "col2_code": self.col2_code,
            "units_submitted": self.units_submitted,
            "mue_limit": self.mue_limit,
            "modifier_indicator": self.modifier_indicator,
            "carc_code": self.carc_code,
        }


@dataclass
class GateDecision:
    route: Route
    risk_score: float  # 0.0–1.0; used downstream to prioritize LLM calls
    violations: list[NCCIViolation] = field(default_factory=list)
    deterministic_carc: str | None = None  # set on HARD_FAIL for immediate coding

    def to_dict(self) -> dict:
        return {
            "route": self.route.value,
            "risk_score": self.risk_score,
            "violations": [v.to_dict() for v in self.violations],
            "deterministic_carc": self.deterministic_carc,
        }


class PTPEdit(NamedTuple):
    col1: str
    col2: str
    modifier_indicator: str  # "0" or "1"


class MUEEdit(NamedTuple):
    hcpcs_code: str
    mue_limit: int
    mue_adjudication_indicator: str  # "1"=line, "2"=DOS, "3"=claim


class NCCIGate:
    """
    Loads NCCI PTP and MUE tables and evaluates incoming claims.

    In production, the active edit version is hot-swapped via the rules.control
    compacted Kafka topic (zero-downtime quarterly update). For v1, the version
    is loaded at startup from the local data directory.
    """

    def __init__(self, edit_version: str = "2026Q3") -> None:
        self.edit_version = edit_version
        # PTP index: (col1, col2) → PTPEdit; also stored as (col2, col1) for symmetric lookup
        self._ptp: dict[tuple[str, str], PTPEdit] = {}
        # MUE index: hcpcs_code → MUEEdit
        self._mue: dict[str, MUEEdit] = {}
        self._loaded = False

    def load(self) -> None:
        """Load PTP and MUE tables from the data directory."""
        ptp_path = _resolve_ncci_file("ptp")
        mue_path = _resolve_ncci_file("mue")
        self._ptp = _load_ptp(ptp_path)
        self._mue = _load_mue(mue_path)
        self._loaded = True
        log.info(
            "ncci_gate_loaded",
            edit_version=self.edit_version,
            ptp_pairs=len(self._ptp) // 2,
            mue_codes=len(self._mue),
        )

    def evaluate(self, claim: dict) -> GateDecision:
        """
        Evaluate a claim dict (from Avro-deserialized ClaimEvent) against NCCI rules.

        Returns a GateDecision with route and any violations found.
        The calling consumer uses decision.route to determine whether to:
          - Route to clearinghouse (PASS)
          - Flag immediately with deterministic CARC (HARD_FAIL, low-value claims)
          - Call the LLM for reasoning (AMBIGUOUS, or HARD_FAIL on high-value claims)
        """
        if not self._loaded:
            raise RuntimeError("NCCIGate.load() must be called before evaluate()")

        procedure_codes: list[str] = claim.get("procedure_codes", [])
        modifiers: list[str] = claim.get("modifiers", [])
        units: int = claim.get("units", 1)

        violations: list[NCCIViolation] = []

        # --- MUE check ---
        for code in procedure_codes:
            mue = self._mue.get(code)
            if mue and units > mue.mue_limit:
                violations.append(NCCIViolation(
                    violation_type=ViolationType.MUE_EXCEEDED,
                    code=code,
                    units_submitted=units,
                    mue_limit=mue.mue_limit,
                    carc_code="CO-97",
                ))

        # --- PTP check ---
        has_bypass_modifier = bool(BYPASS_MODIFIERS & set(modifiers))
        ambiguous_ptp: list[NCCIViolation] = []

        for i, col1 in enumerate(procedure_codes):
            for col2 in procedure_codes[i + 1:]:
                edit = self._ptp.get((col1, col2)) or self._ptp.get((col2, col1))
                if edit is None:
                    continue

                if edit.modifier_indicator == "0":
                    # No bypass possible — hard NCCI violation
                    violations.append(NCCIViolation(
                        violation_type=ViolationType.PTP_NO_BYPASS,
                        code=col1,
                        col2_code=col2,
                        modifier_indicator="0",
                        carc_code="CO-97",
                    ))
                elif edit.modifier_indicator == "1":
                    if has_bypass_modifier:
                        # Bypass modifier present — clinically valid? LLM must verify
                        ambiguous_ptp.append(NCCIViolation(
                            violation_type=ViolationType.PTP_BYPASS_UNVERIFIED,
                            code=col1,
                            col2_code=col2,
                            modifier_indicator="1",
                            carc_code="CO-4",
                        ))
                    else:
                        # modifier_indicator=1 but no bypass modifier → hard fail
                        violations.append(NCCIViolation(
                            violation_type=ViolationType.PTP_NO_BYPASS,
                            code=col1,
                            col2_code=col2,
                            modifier_indicator="1",
                            carc_code="CO-4",
                        ))

        # --- Routing decision ---
        if not violations and not ambiguous_ptp:
            return GateDecision(route=Route.PASS, risk_score=0.0)

        if ambiguous_ptp:
            # At least one unverified bypass — must go to LLM regardless of hard fails
            return GateDecision(
                route=Route.AMBIGUOUS,
                risk_score=0.65,
                violations=violations + ambiguous_ptp,
            )

        # Hard fails only, no ambiguous modifiers
        return GateDecision(
            route=Route.HARD_FAIL,
            risk_score=0.90,
            violations=violations,
            deterministic_carc=violations[0].carc_code if violations else None,
        )


# --- File loaders ---

def _resolve_ncci_file(edit_type: str) -> Path:
    """
    Prefer the real quarterly CMS file if present; fall back to seed file.
    Real file naming convention: ptp_<year>q<quarter>.csv or mue_<year>q<quarter>.csv
    """
    ncci_dir = DataConfig.NCCI_DIR
    # Look for real quarterly file first
    real_files = sorted(ncci_dir.glob(f"{edit_type}_*.csv"), reverse=True)
    if real_files:
        log.info("ncci_loading_real_file", edit_type=edit_type, path=str(real_files[0]))
        return real_files[0]
    # Fall back to seed
    seed_path = ncci_dir / f"seed_{edit_type}.csv"
    log.warning("ncci_using_seed_file", edit_type=edit_type, path=str(seed_path))
    return seed_path


def _load_ptp(path: Path) -> dict[tuple[str, str], PTPEdit]:
    """
    Load PTP edits. Skips comment lines (starting with #).
    Stores both (col1, col2) and (col2, col1) for O(1) symmetric lookup.
    """
    edits: dict[tuple[str, str], PTPEdit] = {}
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            col1 = (row.get("col1_code") or "").strip()
            col2 = (row.get("col2_code") or "").strip()
            mod = (row.get("modifier_indicator") or "0").strip()
            if not col1 or not col2 or col1.startswith("#"):
                continue
            edit = PTPEdit(col1=col1, col2=col2, modifier_indicator=mod)
            edits[(col1, col2)] = edit
            edits[(col2, col1)] = edit  # symmetric lookup
    return edits


def _load_mue(path: Path) -> dict[str, MUEEdit]:
    """Load MUE edits. Skips comment lines."""
    edits: dict[str, MUEEdit] = {}
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            code = (row.get("hcpcs_code") or "").strip()
            if not code or code.startswith("#"):
                continue
            try:
                limit = int(row.get("mue_value") or 0)
            except ValueError:
                continue
            edits[code] = MUEEdit(
                hcpcs_code=code,
                mue_limit=limit,
                mue_adjudication_indicator=row.get("mue_adjudication_indicator", "2").strip(),
            )
    return edits
