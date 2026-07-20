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

Retry policy: transient API failures (429 rate limit, 5xx, timeouts) get
LLMConfig.MAX_RETRIES attempts with the SDK's exponential backoff + jitter
before the deterministic fallback fires. The fallback is the last resort,
not the first response to a rate-limit blip — used_fallback rates stay
meaningful in the drift monitor.

Why determinism without temperature=0: Sonnet 5 removed sampling params (non-default
temperature returns 400). Determinism now comes from structural enforcement — strict
tool schemas guarantee schema-valid inputs at the API boundary, the CARC enum constrains
denial codes, and _validate() bounds-checks every field. Sampling never guaranteed
identical outputs anyway; the input_hash + model_id + prompt_version stamps every
result for replay, and the audit story is enforcement, not sampling.

Why thinking disabled: Sonnet 5 runs adaptive thinking by default when the field is
omitted. This scorer sits in a latency-gated streaming path; thinking is disabled
explicitly to preserve the pre-migration latency profile (see ADR-008).

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
# Matches the prompt's own stated rule ("null if risk_score < 30") — enforced
# here as the deterministic safety net, not just prompt instruction. Phase 3
# live run found ~1/3 of high-risk claims violated this (some cited a real
# CARC in the rationale while leaving the structured field null).
HIGH_RISK_CARC_THRESHOLD = 30


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
    # Prompt-cache accounting: usage.input_tokens excludes cached tokens, so
    # these are tracked separately for honest cost + cache-hit-rate metrics
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0
    cost_usd: float = 0.0
    # Phase 2 — PA Pre-Check fields (None when PA tool was not called)
    pa_required: bool | None = None
    pa_approval_likelihood: float | None = None
    pa_criteria_met: list = None  # list[str]

    def __post_init__(self):
        if self.pa_criteria_met is None:
            self.pa_criteria_met = []

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
            "PA_REQUIRED": self.pa_required,
            "PA_APPROVAL_LIKELIHOOD": self.pa_approval_likelihood,
            "PA_CRITERIA_MET": json.dumps(self.pa_criteria_met),
        }


def _load_carc_codes() -> frozenset[str]:
    with open(DataConfig.CARC_FILE, encoding="utf-8") as f:
        data = json.load(f)
    return frozenset(data.get("carc", {}).keys())


def _make_strict(tool: dict) -> dict:
    """
    Enable strict tool use: the API guarantees tool inputs validate against the
    schema exactly — one more hallucination-defense layer on top of the CARC
    enum and _validate() (the FCA-audit story is layered enforcement).
    Requires additionalProperties: false on the schema.
    """
    tool["strict"] = True
    tool["input_schema"]["additionalProperties"] = False
    return tool


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
        self._client = anthropic.Anthropic(
            api_key=LLMConfig.API_KEY,
            max_retries=LLMConfig.MAX_RETRIES,
            timeout=LLMConfig.TIMEOUT_S,
        )
        self._tool_registry = ToolRegistry(ncci_gate)
        self._carc_codes = _load_carc_codes()
        self._submit_tool = _make_strict(self._build_submit_tool())
        self._all_tools = [_make_strict(t) for t in self._build_lookup_tools()] + [self._submit_tool]

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

            submit_inputs, tool_trace, usage = self._run_tool_loop(messages)
            input_tokens, output_tokens = usage["input"], usage["output"]
            cost_usd = (
                usage["input"] * LLMConfig.INPUT_COST_PER_MTOK
                + usage["cache_creation"] * LLMConfig.INPUT_COST_PER_MTOK * LLMConfig.CACHE_WRITE_MULT
                + usage["cache_read"] * LLMConfig.INPUT_COST_PER_MTOK * LLMConfig.CACHE_READ_MULT
                + usage["output"] * LLMConfig.OUTPUT_COST_PER_MTOK
            ) / 1_000_000

            if submit_inputs is None:
                r = self._fallback(
                    claim, gate_decision, "max_iterations_exceeded",
                    input_hash, _elapsed_ms(start_ns),
                )
                self._attach_usage(r, usage, cost_usd)
                return r

            if not self._validate(submit_inputs):
                r = self._fallback(
                    claim, gate_decision, "validation_failed",
                    input_hash, _elapsed_ms(start_ns),
                )
                self._attach_usage(r, usage, cost_usd)
                return r

            risk_score = int(submit_inputs["risk_score"])
            # Extract PA result from tool trace if check_prior_auth_required was called
            pa_tool_result: dict = next(
                (t["result"] for t in tool_trace if t["tool"] == "check_prior_auth_required"),
                {},
            )
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
                cache_creation_input_tokens=usage["cache_creation"],
                cache_read_input_tokens=usage["cache_read"],
                cost_usd=cost_usd,
                pa_required=pa_tool_result.get("pa_required"),
                pa_approval_likelihood=pa_tool_result.get("pa_approval_likelihood"),
                pa_criteria_met=pa_tool_result.get("pa_criteria_met", []),
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
                cache_read_tokens=usage["cache_read"],
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

    @staticmethod
    def _attach_usage(r: ScoringResult, usage: dict, cost_usd: float) -> None:
        """Attach accumulated token usage + cost to a fallback result."""
        r.input_tokens = usage["input"]
        r.output_tokens = usage["output"]
        r.cache_creation_input_tokens = usage["cache_creation"]
        r.cache_read_input_tokens = usage["cache_read"]
        r.cost_usd = cost_usd

    def _run_tool_loop(
        self, messages: list[dict]
    ) -> tuple[dict | None, list[dict], dict]:
        """
        Run the agentic tool loop. Returns (submit_inputs, tool_trace, usage)
        where usage accumulates input/output/cache_creation/cache_read tokens
        across all iterations for cost-per-claim attribution.

        On the final iteration, tools are restricted to submit_scoring_decision
        and tool_choice is forced to "any" — the model MUST submit.
        """
        tool_trace: list[dict] = []
        usage = {"input": 0, "output": 0, "cache_creation": 0, "cache_read": 0}

        for iteration in range(MAX_TOOL_ITERATIONS):
            is_last = iteration == MAX_TOOL_ITERATIONS - 1
            tools = [self._submit_tool] if is_last else self._all_tools
            tool_choice: dict = {"type": "any"} if is_last else {"type": "auto"}

            # Prompt caching: cache_control on the system block caches the
            # tools + system prefix across claims (~90% input discount on
            # cache reads). The forced-submit last iteration uses a smaller
            # tool set, so it keys a separate (rarely hit) cache prefix.
            # thinking is disabled explicitly — Sonnet 5 defaults to adaptive
            # when the field is omitted, and this path is latency-gated.
            response = self._client.messages.create(
                model=LLMConfig.MODEL_VERSION_TAG,
                max_tokens=LLMConfig.MAX_TOKENS,
                thinking={"type": "disabled"},
                system=[{
                    "type": "text",
                    "text": SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"},
                }],
                tools=tools,
                tool_choice=tool_choice,
                messages=messages,
            )

            usage["input"] += response.usage.input_tokens
            usage["output"] += response.usage.output_tokens
            usage["cache_creation"] += getattr(response.usage, "cache_creation_input_tokens", 0) or 0
            usage["cache_read"] += getattr(response.usage, "cache_read_input_tokens", 0) or 0
            messages.append({"role": "assistant", "content": response.content})

            if response.stop_reason == "max_tokens":
                log.warning("scorer_max_tokens_hit", iteration=iteration)
                return None, tool_trace, usage

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
                return submit_inputs, tool_trace, usage

            if tool_results:
                messages.append({"role": "user", "content": tool_results})
            # else: stop_reason=end_turn with no tools → loop will force submit next iteration

        return None, tool_trace, usage

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

            if risk_score >= HIGH_RISK_CARC_THRESHOLD and denial_code is None:
                log.warning("validation_null_carc_at_risk", risk_score=risk_score)
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
            {
                "name": "get_payer_rules",
                "description": (
                    "Phase 2: retrieve payer-specific coverage rule from the Payer Rule Intelligence "
                    "Graph. Returns NCD/LCD source type, coverage status, prior auth requirement, "
                    "PA criteria, and conflict type (lcd_adds_restriction | null). "
                    "Use when the claim involves a procedure with known payer-specific restrictions "
                    "or prior authorization requirements."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "payer_id": {"type": "string", "description": "Payer identifier from the claim"},
                        "procedure_code": {"type": "string", "description": "HCPCS/CPT procedure code"},
                        "diagnosis_code": {
                            "type": "string",
                            "description": "ICD-10-CM code to check diagnosis coverage (optional)",
                        },
                    },
                    "required": ["payer_id", "procedure_code"],
                },
            },
            {
                "name": "check_prior_auth_required",
                "description": (
                    "Phase 2: check whether prior authorization is required for a procedure/payer "
                    "combination and estimate approval likelihood. Returns pa_required bool, "
                    "pa_approval_likelihood (0-1), criteria met/unmet, and the denial code if "
                    "PA is denied (CO-197). Use when get_payer_rules indicates requires_prior_auth=true "
                    "or when the claim involves a high-cost surgical or imaging procedure."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "payer_id": {"type": "string", "description": "Payer identifier from the claim"},
                        "hcpcs_code": {"type": "string", "description": "HCPCS/CPT procedure code"},
                        "diagnosis_code": {
                            "type": "string",
                            "description": "ICD-10-CM code for diagnosis-specific PA criteria (optional)",
                        },
                        "provider_npi": {
                            "type": "string",
                            "description": "Provider NPI for gold-card exemption check (optional)",
                        },
                    },
                    "required": ["payer_id", "hcpcs_code"],
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
                    # NOTE: strict tool use rejects minimum/maximum keywords —
                    # numeric bounds live in the description and are enforced
                    # post-hoc by _validate() (layered enforcement).
                    "risk_score": {
                        "type": "integer",
                        "description": "Denial risk, integer 0-100: 0 = certain clean, 100 = certain denial",
                    },
                    "confidence": {
                        "type": "number",
                        "description": "Confidence in the risk_score assessment, 0.0-1.0",
                    },
                    # anyOf instead of type:["string","null"] + enum — strict
                    # mode rejects enum values that don't match a union type
                    "predicted_denial_code": {
                        "anyOf": [
                            {"type": "string", "enum": carc_enum},
                            {"type": "null"},
                        ],
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
                        "description": (
                            "Plain-English action instruction for billing staff — imperative voice, "
                            "specific and actionable (e.g., 'Attach the operative note for modifier 59 "
                            "to override the CO-97 bundling flag'). 1-3 sentences max."
                        ),
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
