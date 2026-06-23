# Copyright (c) 2026 Eric Grynspan. All rights reserved.
"""
Layer 2 — Claude API reasoning core.

INTERVIEW-CRITICAL: own this cold.

Agentic tool-use pattern (the "why not just rules?" answer):
  1. Build claim + gate context message
  2. Claude calls lookup tools to retrieve policy context (optional)
  3. We execute each tool call and return results
  4. Claude calls submit_scoring_decision (forced structured output via tool schema)
  5. We validate the output: risk_score bounds, CARC enum, action enum
  6. On any validation failure: deterministic fallback from gate decision + dead-letter

Why temperature=0: same claim + same inputs = same score. Reproducibility is an audit
requirement. The input_hash + model_id + prompt_version stamps every result for replay.

Why tool-use for structured output: the submit_scoring_decision schema enforces types at
the API boundary. risk_score is bounded [0,100], denial code is constrained to the CARC
enum. This is more reliable than parsing free-form JSON from a text response.

Why tool-use for retrieval: the model decides which tools it needs. A simple pass-claim
may call submit directly. An ambiguous modifier claim calls check_modifier first, then
reasons over the result. The model's tool-calling trace is the audit log of its reasoning.
"""
from __future__ import annotations

import hashlib
import json
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

import anthropic
import structlog

from src.config.settings import DataConfig, LLMConfig
from src.consumer.ncci_gate import GateDecision, Route
from src.reasoning.prompt import PROMPT_VERSION, SYSTEM_PROMPT, build_claim_message
from src.reasoning.tools import ToolRegistry

log = structlog.get_logger(__name__)

MAX_TOOL_ITERATIONS = 5
VALID_ACTIONS = frozenset(["auto_correct", "flag", "hold", "escalate"])


@dataclass
class ScoringResult:
    score_id: str
    claim_id: str
    scored_at: datetime
    model_id: str
    prompt_version: str
    ncci_edit_version: str
    input_hash: str
    risk_score: int          # 0-100 (LLM scale)
    risk_score_norm: float   # 0.0-1.0 (Snowflake NUMBER(5,4))
    confidence: float
    predicted_denial_code: str | None
    driving_fields: list[str]
    recommended_action: str
    rationale: str
    tool_calls: list[dict]
    latency_ms: int
    used_fallback: bool = False
    fallback_reason: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0

    def to_snowflake_row(self) -> dict:
        """Serialize for insertion into RAW.LLM_SCORING_RESULTS."""
        return {
            "SCORE_ID": self.score_id,
            "CLAIM_ID": self.claim_id,
            "SCORED_AT": self.scored_at.isoformat(),
            "MODEL_ID": self.model_id,
            "PROMPT_VERSION": self.prompt_version,
            "NCCI_EDIT_VERSION": self.ncci_edit_version,
            "INPUT_HASH": self.input_hash,
            "RISK_SCORE": self.risk_score_norm,
            "CONFIDENCE": self.confidence,
            "PREDICTED_DENIAL_CODE": self.predicted_denial_code,
            "DRIVING_FIELDS": json.dumps(self.driving_fields),
            "RECOMMENDED_ACTION": self.recommended_action,
            "RATIONALE": self.rationale,
            "FULL_RESPONSE": json.dumps({"tool_calls": self.tool_calls, "fallback": self.used_fallback}),
            "LATENCY_MS": self.latency_ms,
            "INPUT_TOKENS": self.input_tokens,
            "OUTPUT_TOKENS": self.output_tokens,
            "COST_USD": self.cost_usd,
        }


def _load_carc_codes() -> frozenset[str]:
    with open(DataConfig.CARC_FILE, encoding="utf-8") as f:
        data = json.load(f)
    return frozenset(data.get("carc", {}).keys())


def _compute_input_hash(claim: dict, gate: GateDecision) -> str:
    payload = json.dumps(
        {"claim": claim, "gate": gate.to_dict()},
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


class ClaimScorer:
    """
    Scores a single claim using the Claude API agentic tool-use pattern.

    Construct once per process (client + CARC enum loaded at init).
    Pass the already-loaded NCCIGate instance from the consumer.
    """

    def __init__(self, ncci_gate) -> None:
        self._client = anthropic.Anthropic(api_key=LLMConfig.API_KEY)
        self._tool_registry = ToolRegistry(ncci_gate)
        self._carc_codes = _load_carc_codes()
        self._submit_tool = self._build_submit_tool()
        self._all_tools = self._build_lookup_tools() + [self._submit_tool]

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def score(self, claim: dict, gate_decision: GateDecision) -> ScoringResult:
        """Score a claim. Always returns a ScoringResult — never raises."""
        start_ns = time.monotonic_ns()
        claim_id = claim.get("claim_id", "")
        input_hash = _compute_input_hash(claim, gate_decision)

        try:
            claim_message = build_claim_message(claim, gate_decision)
            messages: list[dict] = [{"role": "user", "content": claim_message}]

            submit_inputs, tool_trace, input_tokens, output_tokens = self._run_tool_loop(messages)
            cost_usd = (
                input_tokens * LLMConfig.INPUT_COST_PER_MTOK
                + output_tokens * LLMConfig.OUTPUT_COST_PER_MTOK
            ) / 1_000_000

            if submit_inputs is None:
                r = self._fallback(
                    claim, gate_decision, "max_iterations_exceeded",
                    input_hash, _elapsed_ms(start_ns),
                )
                r.input_tokens, r.output_tokens, r.cost_usd = input_tokens, output_tokens, cost_usd
                return r

            if not self._validate(submit_inputs):
                r = self._fallback(
                    claim, gate_decision, "validation_failed",
                    input_hash, _elapsed_ms(start_ns),
                )
                r.input_tokens, r.output_tokens, r.cost_usd = input_tokens, output_tokens, cost_usd
                return r

            risk_score = int(submit_inputs["risk_score"])
            result = ScoringResult(
                score_id=str(uuid.uuid4()),
                claim_id=claim_id,
                scored_at=datetime.now(timezone.utc),
                model_id=LLMConfig.MODEL_VERSION_TAG,
                prompt_version=PROMPT_VERSION,
                ncci_edit_version=claim.get("ncci_edit_version", ""),
                input_hash=input_hash,
                risk_score=risk_score,
                risk_score_norm=risk_score / 100.0,
                confidence=float(submit_inputs["confidence"]),
                predicted_denial_code=submit_inputs.get("predicted_denial_code"),
                driving_fields=list(submit_inputs.get("driving_fields", [])),
                recommended_action=submit_inputs["recommended_action"],
                rationale=submit_inputs["rationale"],
                tool_calls=tool_trace,
                latency_ms=_elapsed_ms(start_ns),
                used_fallback=False,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cost_usd=cost_usd,
            )
            log.info(
                "claim_scored",
                claim_id=claim_id,
                risk_score=risk_score,
                action=result.recommended_action,
                tool_calls=len(tool_trace),
                latency_ms=result.latency_ms,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cost_usd=round(cost_usd, 6),
            )
            return result

        except Exception as e:
            log.error("scorer_exception", claim_id=claim_id, error=str(e), exc_info=True)
            return self._fallback(
                claim, gate_decision, f"exception:{type(e).__name__}",
                input_hash, _elapsed_ms(start_ns),
            )

    # ------------------------------------------------------------------
    # Tool loop
    # ------------------------------------------------------------------

    def _run_tool_loop(
        self, messages: list[dict]
    ) -> tuple[dict | None, list[dict], int, int]:
        """
        Run the agentic tool loop. Returns (submit_inputs, tool_trace, input_tokens, output_tokens).

        On the final iteration, tools are restricted to submit_scoring_decision
        and tool_choice is forced to "any" — the model MUST submit.
        Token counts are accumulated across all iterations for cost-per-claim attribution.
        """
        tool_trace: list[dict] = []
        total_input_tokens = 0
        total_output_tokens = 0

        for iteration in range(MAX_TOOL_ITERATIONS):
            is_last = iteration == MAX_TOOL_ITERATIONS - 1
            tools = [self._submit_tool] if is_last else self._all_tools
            tool_choice: dict = {"type": "any"} if is_last else {"type": "auto"}

            response = self._client.messages.create(
                model=LLMConfig.MODEL_VERSION_TAG,
                max_tokens=LLMConfig.MAX_TOKENS,
                temperature=LLMConfig.TEMPERATURE,
                system=SYSTEM_PROMPT,
                tools=tools,
                tool_choice=tool_choice,
                messages=messages,
            )

            total_input_tokens += response.usage.input_tokens
            total_output_tokens += response.usage.output_tokens
            messages.append({"role": "assistant", "content": response.content})

            if response.stop_reason == "max_tokens":
                log.warning("scorer_max_tokens_hit", iteration=iteration)
                return None, tool_trace, total_input_tokens, total_output_tokens

            submit_inputs: dict | None = None
            tool_results: list[dict] = []

            for block in response.content:
                if block.type != "tool_use":
                    continue

                if block.name == "submit_scoring_decision":
                    submit_inputs = dict(block.input)
                else:
                    result = self._tool_registry.execute(block.name, dict(block.input))
                    tool_trace.append({
                        "tool": block.name,
                        "input": dict(block.input),
                        "result": result,
                    })
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": json.dumps(result),
                    })

            if submit_inputs is not None:
                return submit_inputs, tool_trace, total_input_tokens, total_output_tokens

            if tool_results:
                messages.append({"role": "user", "content": tool_results})
            # else: stop_reason=end_turn with no tools → loop will force submit next iteration

        return None, tool_trace, total_input_tokens, total_output_tokens

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    def _validate(self, inputs: dict) -> bool:
        """Validate the submit_scoring_decision inputs. Returns False on any violation."""
        try:
            risk_score = int(inputs["risk_score"])
            if not 0 <= risk_score <= 100:
                log.warning("validation_risk_out_of_range", risk_score=risk_score)
                return False

            confidence = float(inputs["confidence"])
            if not 0.0 <= confidence <= 1.0:
                log.warning("validation_confidence_out_of_range", confidence=confidence)
                return False

            denial_code = inputs.get("predicted_denial_code")
            if denial_code is not None and denial_code not in self._carc_codes:
                log.warning("validation_invalid_carc", code=denial_code)
                return False

            action = inputs.get("recommended_action")
            if action not in VALID_ACTIONS:
                log.warning("validation_invalid_action", action=action)
                return False

            rationale = inputs.get("rationale", "")
            if not rationale or len(rationale.strip()) < 10:
                log.warning("validation_empty_rationale")
                return False

            return True

        except (KeyError, ValueError, TypeError) as e:
            log.warning("validation_schema_error", error=str(e))
            return False

    # ------------------------------------------------------------------
    # Fallback
    # ------------------------------------------------------------------

    def _fallback(
        self,
        claim: dict,
        gate: GateDecision,
        reason: str,
        input_hash: str,
        latency_ms: int,
    ) -> ScoringResult:
        """
        Deterministic fallback when LLM scoring fails.

        Uses the gate decision as a floor: a claim never gets a lower
        risk signal than what the deterministic NCCI gate already found.
        The claim is still written to RAW.LLM_SCORING_RESULTS with
        used_fallback=True so the fallback rate is visible in the drift monitor.
        """
        route = gate.route
        if route == Route.PASS:
            risk_score, confidence, action = 5, 0.90, "flag"
            denial_code = None
        elif route == Route.HARD_FAIL:
            risk_score, confidence, action = 85, 0.85, "flag"
            denial_code = gate.deterministic_carc
        else:
            risk_score, confidence, action = 60, 0.50, "flag"
            denial_code = None

        log.warning("scorer_fallback", claim_id=claim.get("claim_id"), reason=reason)

        driving = [v.code for v in gate.violations] if gate.violations else []

        return ScoringResult(
            score_id=str(uuid.uuid4()),
            claim_id=claim.get("claim_id", ""),
            scored_at=datetime.now(timezone.utc),
            model_id=LLMConfig.MODEL_VERSION_TAG,
            prompt_version=PROMPT_VERSION,
            ncci_edit_version=claim.get("ncci_edit_version", ""),
            input_hash=input_hash,
            risk_score=risk_score,
            risk_score_norm=risk_score / 100.0,
            confidence=confidence,
            predicted_denial_code=denial_code,
            driving_fields=driving,
            recommended_action=action,
            rationale=f"Deterministic fallback ({reason}). Gate route: {route.value}.",
            tool_calls=[],
            latency_ms=latency_ms,
            used_fallback=True,
            fallback_reason=reason,
        )

    # ------------------------------------------------------------------
    # Tool schema builders
    # ------------------------------------------------------------------

    def _build_lookup_tools(self) -> list[dict]:
        return [
            {
                "name": "lookup_ncci_edit",
                "description": "Look up NCCI PTP bundling edit for a procedure code pair.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "col1_code": {"type": "string", "description": "First procedure code (HCPCS/CPT)"},
                        "col2_code": {"type": "string", "description": "Second procedure code (HCPCS/CPT)"},
                    },
                    "required": ["col1_code", "col2_code"],
                },
            },
            {
                "name": "get_lcd_policy",
                "description": (
                    "Retrieve LCD/NCD coverage policy for a procedure, optionally checking "
                    "whether a specific diagnosis supports medical necessity."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "hcpcs_code": {"type": "string", "description": "HCPCS/CPT procedure code"},
                        "diagnosis_code": {
                            "type": "string",
                            "description": "ICD-10-CM code to check against coverage criteria (optional)",
                        },
                    },
                    "required": ["hcpcs_code"],
                },
            },
            {
                "name": "check_modifier",
                "description": "Validate whether a modifier is appropriate for a procedure code.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "procedure_code": {"type": "string"},
                        "modifier_code": {"type": "string"},
                    },
                    "required": ["procedure_code", "modifier_code"],
                },
            },
            {
                "name": "get_payer_history",
                "description": (
                    "Retrieve recent denial rate for a payer and procedure combination "
                    "from the session feedback window."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "payer_id": {"type": "string"},
                        "procedure_code": {"type": "string"},
                    },
                    "required": ["payer_id", "procedure_code"],
                },
            },
        ]

    def _build_submit_tool(self) -> dict:
        carc_enum = sorted(self._carc_codes)
        return {
            "name": "submit_scoring_decision",
            "description": (
                "Submit the final claim risk scoring decision. "
                "MUST be called to finalize the assessment."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "risk_score": {
                        "type": "integer",
                        "minimum": 0,
                        "maximum": 100,
                        "description": "Denial risk 0 (certain clean) to 100 (certain denial)",
                    },
                    "confidence": {
                        "type": "number",
                        "minimum": 0.0,
                        "maximum": 1.0,
                        "description": "Confidence in the risk_score assessment",
                    },
                    "predicted_denial_code": {
                        "type": ["string", "null"],
                        "enum": carc_enum + [None],
                        "description": "Most likely CARC code if denied, or null if risk_score < 30",
                    },
                    "driving_fields": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Claim fields driving the risk (e.g. ['procedure_codes', 'modifiers'])",
                    },
                    "recommended_action": {
                        "type": "string",
                        "enum": list(VALID_ACTIONS),
                        "description": "Routing action: auto_correct | flag | hold | escalate",
                    },
                    "rationale": {
                        "type": "string",
                        "description": "Plain-English explanation for billing staff (1-3 sentences)",
                    },
                },
                "required": [
                    "risk_score",
                    "confidence",
                    "driving_fields",
                    "recommended_action",
                    "rationale",
                ],
            },
        }


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _elapsed_ms(start_ns: int) -> int:
    return (time.monotonic_ns() - start_ns) // 1_000_000
