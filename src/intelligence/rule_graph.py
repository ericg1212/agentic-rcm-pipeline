# Copyright (c) 2026 Eric Grynspan. All rights reserved.
"""
Payer Rule Intelligence Graph — Phase 2, Component 1.

In-memory rule cache over RAW.PAYER_RULES (Snowflake). Loaded at startup
from the Snowflake table; falls back to seed JSON for offline/test use.
Dagster daily sensor triggers reload via reload().

Lookup chain: payer_id → state_codes (payer_registry.json)
              → contractor_id (mac_jurisdiction.json)
              → LCD rule (contractor-specific)
              → NCD fallback (contractor_id=null, always applicable)

NCD/LCD hierarchy: LCDs cannot contradict NCDs; they may add restrictions.
When NCD and LCD conflict, the more restrictive rule wins (FCA-safe).
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Optional

import structlog

from src.config.settings import DataConfig

log = structlog.get_logger(__name__)

_DATA_DIR = DataConfig.CARC_FILE.parent.parent


@dataclass
class RuleEntry:
    rule_id: str
    source_type: str                   # "ncd" | "lcd"
    contractor_id: Optional[str]       # None for NCDs (national scope)
    hcpcs_code: str
    icd10_prefixes: list[str]
    coverage_status: str               # "covered" | "covered_with_restrictions" | "not_covered"
    requires_prior_auth: bool
    pa_criteria: Optional[str]
    gold_card_eligible: bool
    typical_denial_code: Optional[str]
    effective_date: str
    expiration_date: Optional[str]

    def covers_diagnosis(self, icd10_code: str) -> bool:
        """True if this rule's covered prefixes include the given ICD-10 code."""
        if not self.icd10_prefixes:
            return True  # broadly covered — no diagnosis restriction
        return any(icd10_code.startswith(p) for p in self.icd10_prefixes)


class PayerRuleGraph:
    """
    In-memory rule cache. Keyed {hcpcs_code: {contractor_id: list[RuleEntry]}}.

    NCD entries are stored under contractor_id=None so they are always
    accessible as a fallback regardless of jurisdiction.

    Thread-safety: single-threaded consumer model — reload() is called by
    the Dagster daily sensor between processing windows, not mid-stream.
    """

    def __init__(self) -> None:
        self._rules: dict[str, dict[Optional[str], list[RuleEntry]]] = {}
        # state_code (2-letter) → contractor_id
        self._mac_jurisdiction: dict[str, str] = {}
        # payer_id → list of state_codes where payer operates
        self._payer_registry: dict[str, list[str]] = {}

    # ------------------------------------------------------------------
    # Load / reload
    # ------------------------------------------------------------------

    def load_from_seed(self) -> None:
        """Load rules from seed JSON files. Used in offline/test mode."""
        lcd_path = _DATA_DIR / "lcd" / "seed_lcd.json"
        mac_path = _DATA_DIR / "mac_jurisdiction.json"
        payer_path = _DATA_DIR / "payer_registry.json"

        with open(lcd_path, encoding="utf-8") as f:
            lcd_data = json.load(f)

        if mac_path.exists():
            with open(mac_path, encoding="utf-8") as f:
                raw = json.load(f)
            # Seed file wraps the mapping under "state_to_contractor"
            self._mac_jurisdiction = raw.get("state_to_contractor", raw)

        if payer_path.exists():
            with open(payer_path, encoding="utf-8") as f:
                raw = json.load(f)
            # Seed file wraps the mapping under "payer_states"
            self._payer_registry = raw.get("payer_states", raw)

        self._rules.clear()
        for hcpcs, policy in lcd_data.get("policies", {}).items():
            entry = RuleEntry(
                rule_id=f"seed-{hcpcs}",
                source_type=policy.get("source_type", "lcd"),
                contractor_id=policy.get("contractor_id"),
                hcpcs_code=hcpcs,
                icd10_prefixes=policy.get("covered_icd10_prefixes", []),
                coverage_status=policy.get("coverage_status", "covered"),
                requires_prior_auth=policy.get("requires_prior_auth", False),
                pa_criteria=policy.get("pa_criteria"),
                gold_card_eligible=policy.get("gold_card_eligible", False),
                typical_denial_code=policy.get("typical_denial_code"),
                effective_date=policy.get("effective_date", "2024-01-01"),
                expiration_date=policy.get("expiration_date"),
            )
            bucket = self._rules.setdefault(hcpcs, {})
            bucket.setdefault(entry.contractor_id, []).append(entry)

        total = sum(
            len(entries)
            for code_bucket in self._rules.values()
            for entries in code_bucket.values()
        )
        log.info("rule_graph.loaded_from_seed", hcpcs_codes=len(self._rules), total_rules=total)

    def reload(self) -> None:
        """Hot-reload from seed (production: from Snowflake). Called by Dagster daily sensor."""
        self.load_from_seed()
        log.info("rule_graph.reloaded")

    # ------------------------------------------------------------------
    # Jurisdiction resolution
    # ------------------------------------------------------------------

    def resolve_contractor(self, payer_id: str) -> Optional[str]:
        """
        Map payer_id → contractor_id via state → MAC jurisdiction lookup.

        If a payer operates in multiple MAC jurisdictions, returns the
        contractor_id appearing most frequently across its states. This
        approximates "home jurisdiction" without requiring per-claim state data.

        Returns None when payer is not in registry — caller should fall back
        to NCD (national policy, always applicable).
        """
        states = self._payer_registry.get(payer_id, [])
        if not states:
            return None

        counts: dict[str, int] = {}
        for state in states:
            cid = self._mac_jurisdiction.get(state)
            if cid:
                counts[cid] = counts.get(cid, 0) + 1

        if not counts:
            return None

        # Most-common MAC across payer's operating states
        return max(counts, key=lambda k: counts[k])

    # ------------------------------------------------------------------
    # Lookup
    # ------------------------------------------------------------------

    def lookup(
        self,
        hcpcs_code: str,
        payer_id: Optional[str],
        icd10_code: Optional[str] = None,
    ) -> dict:
        """
        Return the most applicable rule for this (procedure, payer, diagnosis) triple.

        Resolution order:
          1. LCD for resolved contractor_id (MAC-specific, jurisdiction-dependent)
          2. NCD (contractor_id=None, national, always applicable)
          3. Not found (procedure not in seed)

        NCD/LCD conflict: if LCD adds a diagnosis restriction the claim doesn't meet,
        returns conflict_type='lcd_adds_restriction'. More restrictive rule wins.
        NCD/LCD conflicts always escalate — they are never auto-corrected.
        """
        contractor_id = self.resolve_contractor(payer_id) if payer_id else None
        bucket = self._rules.get(hcpcs_code, {})

        if not bucket:
            return {"found": False, "hcpcs_code": hcpcs_code, "note": "Procedure not in rule graph."}

        # Prefer MAC-specific LCD; fall back to NCD
        rules: list[RuleEntry] = bucket.get(contractor_id, []) or bucket.get(None, [])

        if not rules:
            return {"found": False, "hcpcs_code": hcpcs_code, "note": "No rule for this jurisdiction."}

        best = rules[0]

        # Diagnosis coverage check
        diagnosis_covered: Optional[bool] = None
        conflict_type: Optional[str] = None

        if icd10_code is not None:
            diagnosis_covered = best.covers_diagnosis(icd10_code)
            if not diagnosis_covered and best.icd10_prefixes:
                conflict_type = "lcd_adds_restriction"

        return {
            "found": True,
            "rule_id": best.rule_id,
            "source_type": best.source_type,
            "contractor_id": best.contractor_id,
            "hcpcs_code": hcpcs_code,
            "coverage_status": best.coverage_status,
            "diagnosis_covered": diagnosis_covered,
            "requires_prior_auth": best.requires_prior_auth,
            "pa_criteria": best.pa_criteria,
            "gold_card_eligible": best.gold_card_eligible,
            "typical_denial_code": best.typical_denial_code,
            "effective_date": best.effective_date,
            "conflict_type": conflict_type,
        }

    def get_rule_for_tool(
        self,
        payer_id: str,
        procedure_code: str,
        diagnosis_code: Optional[str] = None,
    ) -> dict:
        """Tool-facing wrapper called by ToolRegistry.get_payer_rules()."""
        return self.lookup(procedure_code, payer_id, diagnosis_code)
