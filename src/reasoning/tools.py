"""
Layer 2 — LLM Tool Registry.

Implements the four lookup tools the reasoning model calls during claim scoring.
All lookups are in-memory (no network calls in the hot path).

get_payer_history uses a session-scoped rolling window. In production this
would query the mart layer's fct_prediction_vs_adjudication rolling aggregate.
"""
from __future__ import annotations

import json

import structlog

from src.config.settings import DataConfig
from src.consumer.ncci_gate import NCCIGate

log = structlog.get_logger(__name__)

# Recognized CMS modifier codes (not exhaustive — covers common denial-relevant modifiers)
RECOGNIZED_MODIFIERS: frozenset[str] = frozenset([
    "25", "26", "32", "33", "50", "51", "52", "53", "57", "58", "59",
    "62", "66", "73", "74", "76", "77", "78", "79", "80", "81", "82",
    "90", "91", "95", "99",
    "GQ", "GT", "GY", "GZ",
    "TC",
    "XE", "XP", "XS", "XU",
])

# Bypass modifiers valid for NCCI PTP modifier_indicator=1 pairs
NCCI_BYPASS_MODIFIERS: frozenset[str] = frozenset(["59", "XE", "XS", "XP", "XU"])

# E&M code prefixes (99xxx range used in Medicare professional billing)
EM_CODE_PREFIXES: frozenset[str] = frozenset(["992", "993", "994", "995", "996", "997", "998"])


class ToolRegistry:
    """
    Executes tool calls from the Claude reasoning model.

    Injected into ClaimScorer. Holds a reference to the already-loaded
    NCCIGate so lookup_ncci_edit uses the same in-memory PTP/MUE tables.
    """

    def __init__(self, ncci_gate: NCCIGate) -> None:
        self._gate = ncci_gate
        self._lcd: dict = self._load_lcd()
        # Rolling denial counts: {payer_id: {procedure_code: {"seen": int, "denied": int}}}
        self._payer_history: dict[str, dict[str, dict[str, int]]] = {}

    # ------------------------------------------------------------------
    # Public dispatcher
    # ------------------------------------------------------------------

    def execute(self, tool_name: str, tool_input: dict) -> dict:
        """Dispatch a tool call by name. Returns a JSON-serializable result dict."""
        dispatch = {
            "lookup_ncci_edit": self._lookup_ncci_edit,
            "get_lcd_policy": self._get_lcd_policy,
            "check_modifier": self._check_modifier,
            "get_payer_history": self._get_payer_history,
        }
        fn = dispatch.get(tool_name)
        if fn is None:
            return {"error": f"Unknown tool: {tool_name}"}
        try:
            return fn(**tool_input)
        except Exception as e:
            log.warning("tool_execution_error", tool=tool_name, error=str(e))
            return {"error": str(e)}

    def record_outcome(self, payer_id: str, procedure_code: str, denied: bool) -> None:
        """Update the in-process denial history window after an adjudication."""
        bucket = self._payer_history.setdefault(payer_id, {}).setdefault(
            procedure_code, {"seen": 0, "denied": 0}
        )
        bucket["seen"] += 1
        if denied:
            bucket["denied"] += 1

    # ------------------------------------------------------------------
    # Tool implementations
    # ------------------------------------------------------------------

    def _lookup_ncci_edit(self, col1_code: str, col2_code: str) -> dict:
        edit = self._gate.lookup_ptp(col1_code, col2_code)
        if edit is None:
            return {
                "found": False,
                "col1_code": col1_code,
                "col2_code": col2_code,
                "note": "No NCCI PTP edit found for this procedure pair.",
            }
        return {
            "found": True,
            "col1_code": edit.col1,
            "col2_code": edit.col2,
            "modifier_indicator": edit.modifier_indicator,
            "note": (
                "modifier_indicator=0: no bypass modifier is ever valid — hard NCCI violation."
                if edit.modifier_indicator == "0"
                else "modifier_indicator=1: a valid bypass modifier (59/XE/XS/XP/XU) may allow "
                     "separate billing if services are clinically distinct."
            ),
        }

    def _get_lcd_policy(self, hcpcs_code: str, diagnosis_code: str | None = None) -> dict:
        policy = self._lcd.get("policies", {}).get(hcpcs_code)
        if policy is None:
            return {
                "found": False,
                "hcpcs_code": hcpcs_code,
                "note": "No LCD policy on record for this procedure code. Broadly covered.",
            }

        covered_prefixes: list[str] = policy.get("covered_icd10_prefixes", [])
        result = {
            "found": True,
            "hcpcs_code": hcpcs_code,
            "procedure_name": policy.get("procedure_name", ""),
            "coverage_note": policy.get("coverage_note", ""),
            "typical_denial_code": policy.get("typical_denial_code"),
        }

        if diagnosis_code and covered_prefixes:
            matches = any(diagnosis_code.startswith(pfx) for pfx in covered_prefixes)
            result["diagnosis_code"] = diagnosis_code
            result["diagnosis_covered"] = matches
            if not matches:
                result["diagnosis_coverage_note"] = (
                    f"Diagnosis {diagnosis_code} does not match covered prefixes "
                    f"({', '.join(covered_prefixes)}). Medical necessity may not be supported."
                )
        elif not covered_prefixes:
            result["diagnosis_covered"] = True
            result["diagnosis_coverage_note"] = "No diagnosis restrictions — broadly covered."

        return result

    def _check_modifier(self, procedure_code: str, modifier_code: str) -> dict:
        is_em = procedure_code[:3] in EM_CODE_PREFIXES
        is_bypass = modifier_code in NCCI_BYPASS_MODIFIERS
        is_recognized = modifier_code in RECOGNIZED_MODIFIERS

        if not is_recognized:
            return {
                "valid": False,
                "procedure_code": procedure_code,
                "modifier_code": modifier_code,
                "note": f"Modifier {modifier_code!r} is not a recognized CMS modifier code.",
                "suggested_action": "Remove unrecognized modifier and recode.",
            }

        if is_bypass and is_em and procedure_code[:5] not in {"99211", "99212", "99213", "99214", "99215"}:
            return {
                "valid": True,
                "procedure_code": procedure_code,
                "modifier_code": modifier_code,
                "note": (
                    f"Modifier {modifier_code} (NCCI bypass) on E&M code {procedure_code}. "
                    "Valid only if billed alongside a same-day procedure and the E&M is a "
                    "separately identifiable, significant service beyond the pre-op assessment. "
                    "Consider whether modifier 25 is more appropriate."
                ),
                "suggested_action": "Verify clinical documentation supports separate E&M service.",
            }

        if modifier_code == "25" and not is_em:
            return {
                "valid": False,
                "procedure_code": procedure_code,
                "modifier_code": "25",
                "note": (
                    "Modifier 25 (significant, separately identifiable E&M service) is only "
                    f"valid on E&M codes. {procedure_code} is not an E&M code."
                ),
                "suggested_action": "Remove modifier 25 or replace with the appropriate bypass modifier.",
            }

        return {
            "valid": True,
            "procedure_code": procedure_code,
            "modifier_code": modifier_code,
            "note": f"Modifier {modifier_code} is valid for procedure {procedure_code}.",
        }

    def _get_payer_history(self, payer_id: str, procedure_code: str) -> dict:
        bucket = self._payer_history.get(payer_id, {}).get(procedure_code)
        if bucket is None or bucket["seen"] == 0:
            return {
                "payer_id": payer_id,
                "procedure_code": procedure_code,
                "history_available": False,
                "note": (
                    "No denial history in current session window. "
                    "In production, this queries the mart layer's rolling 90-day denial rate."
                ),
            }
        denial_rate = bucket["denied"] / bucket["seen"]
        return {
            "payer_id": payer_id,
            "procedure_code": procedure_code,
            "history_available": True,
            "claims_seen": bucket["seen"],
            "claims_denied": bucket["denied"],
            "denial_rate": round(denial_rate, 4),
            "note": (
                f"Denial rate {denial_rate:.1%} over {bucket['seen']} recent claims "
                f"for {payer_id} + {procedure_code}."
            ),
        }

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _load_lcd() -> dict:
        try:
            with open(DataConfig.CARC_FILE.parent.parent / "lcd" / "seed_lcd.json", encoding="utf-8") as f:
                return json.load(f)
        except FileNotFoundError:
            log.warning("lcd_seed_not_found")
            return {"policies": {}}
