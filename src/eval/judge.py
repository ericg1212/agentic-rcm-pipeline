# Copyright (c) 2026 Eric Grynspan. All rights reserved.
"""
Phase 3 — LLM-as-Judge eval harness (ADR-008).

Grades every scorer recommendation against a fixed rubric using an independent
judge model, run through the Message Batches API at 50% off token pricing.

JUDGE INDEPENDENCE (own this cold):
The judge sees the claim, the gate decision, the scorer's OUTPUT, and the
governing rule text fetched DETERMINISTICALLY from the same seed policies the
scorer's tools read. It never sees the scorer's tool-call trace or reasoning
chain — fresh-context grading beats self-critique because a judge that reads
the scorer's reasoning inherits its framing (context contamination).

WHY HAIKU VIA BATCH:
Judging is classification-shaped — five independent pass/fail checks against
explicit criteria, no open-ended reasoning. Haiku 4.5 at batch pricing
($0.50/$2.50 effective per MTok) judges ~300 cases for under $1; the same
volume through synchronous Sonnet would run ~4x the cost for no measurable
gain on a rubric this constrained. The judge itself is validated by a
Sonnet spot-check sample (agreement rate), not by trusting Haiku blindly.

WHY A RUBRIC, NOT EXACT-MATCH ASSERTIONS:
The rationale field is natural language — exact-match assertions are brittle
against paraphrase. Independently gradeable criteria give per-dimension pass
rates that localize failures (e.g. "CARC plausible but guidance vague").
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field

import structlog

log = structlog.get_logger(__name__)

JUDGE_MODEL = "claude-haiku-4-5"
SPOT_CHECK_MODEL = "claude-sonnet-5"
RUBRIC_VERSION = "v1.0.0"
JUDGE_MAX_TOKENS = 1024

# ---------------------------------------------------------------------------
# Rubric — 5 independently gradeable criteria
# ---------------------------------------------------------------------------

CRITERIA: dict[str, str] = {
    "carc_plausible": (
        "The predicted_denial_code is plausible for the violation or risk present "
        "on the claim (or is null with risk_score < 30). A CARC that names a "
        "different failure mode than the evidence supports fails."
    ),
    "rule_applies": (
        "Any rule, policy, or coverage restriction the rationale relies on actually "
        "applies to this claim's procedure/diagnosis codes per the GOVERNING RULE "
        "CONTEXT provided. Citing a restriction the context does not support fails."
    ),
    "action_consistent": (
        "The recommended_action is consistent with the risk_score, confidence, and "
        "submitted_charge: auto_correct requires high confidence (>=0.92) and low "
        "charge (<=$500); risk_score >= 85 should not be auto-corrected; low-risk "
        "clean claims should not be held or escalated."
    ),
    "guidance_actionable": (
        "The rationale is imperative-voice, specific, and actionable by billing "
        "staff — it names what to do (attach, remove, verify, recode). Vague "
        "restatements of risk ('this claim may be denied') fail."
    ),
    "no_fabrication": (
        "Every procedure code, diagnosis code, modifier, CARC code, and policy "
        "referenced in the output exists on the claim, in the gate decision, or in "
        "the GOVERNING RULE CONTEXT. Any invented code or rule fails."
    ),
}

# Structured output schema — output_config.format json_schema.
# additionalProperties: false everywhere; all fields required (strict contract).
_CRITERION_SCHEMA = {
    "type": "object",
    "properties": {
        "passed": {"type": "boolean"},
        "reason": {"type": "string", "description": "One-line justification"},
    },
    "required": ["passed", "reason"],
    "additionalProperties": False,
}

JUDGE_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "criteria": {
            "type": "object",
            "properties": {name: _CRITERION_SCHEMA for name in CRITERIA},
            "required": list(CRITERIA),
            "additionalProperties": False,
        },
        "overall_pass": {
            "type": "boolean",
            "description": "True only if every criterion passed",
        },
    },
    "required": ["criteria", "overall_pass"],
    "additionalProperties": False,
}

JUDGE_SYSTEM_PROMPT = (
    "You are an eval judge for a Medicare claim denial-risk scorer. Grade the "
    "scorer's output against each rubric criterion independently — pass/fail plus "
    "a one-line reason. Judge only against the evidence provided (claim, gate "
    "decision, governing rule context). Do not reward confident-sounding language; "
    "penalize any code or rule not grounded in the provided context.\n\n"
    "RUBRIC (rubric_version " + RUBRIC_VERSION + "):\n"
    + "\n".join(f"- {name}: {desc}" for name, desc in CRITERIA.items())
)


# ---------------------------------------------------------------------------
# Verdicts
# ---------------------------------------------------------------------------

@dataclass
class JudgeVerdict:
    score_id: str
    claim_id: str
    judge_model: str
    rubric_version: str
    criteria: dict[str, dict]        # name -> {"passed": bool, "reason": str}
    overall_pass: bool
    error: str | None = None

    @property
    def failed_criteria(self) -> list[str]:
        return [k for k, v in self.criteria.items() if not v.get("passed")]


@dataclass
class JudgeMetrics:
    n_judged: int
    n_errors: int
    overall_pass_rate: float
    per_criterion_pass_rate: dict[str, float] = field(default_factory=dict)

    def summary(self) -> str:
        lines = [
            f"Judged: {self.n_judged} (errors: {self.n_errors})",
            f"Overall pass rate: {self.overall_pass_rate:.1%}",
        ]
        for name, rate in self.per_criterion_pass_rate.items():
            lines.append(f"  {name:<22} {rate:.1%}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Deterministic rule-context fetch (judge independence)
# ---------------------------------------------------------------------------

def build_rule_context(claim: dict, tool_registry) -> dict:
    """
    Fetch the governing rule text the same way the scorer's tools would — but
    deterministically (every relevant lookup, every code), so the judge grades
    against ground truth rather than whichever subset the scorer chose to fetch.
    """
    procs: list[str] = claim.get("procedure_codes", [])
    diags: list[str] = claim.get("diagnosis_codes", [])
    payer: str = claim.get("payer_id", "")

    context: dict = {"lcd_policies": [], "ncci_edits": [], "payer_rules": []}

    for proc in procs:
        for diag in diags or [None]:
            args = {"hcpcs_code": proc}
            if diag:
                args["diagnosis_code"] = diag
            context["lcd_policies"].append(tool_registry.execute("get_lcd_policy", args))
        context["payer_rules"].append(
            tool_registry.execute("get_payer_rules", {"payer_id": payer, "procedure_code": proc})
        )

    if len(procs) >= 2:
        for i in range(len(procs)):
            for j in range(len(procs)):
                if i == j:
                    continue
                context["ncci_edits"].append(
                    tool_registry.execute(
                        "lookup_ncci_edit",
                        {"col1_code": procs[i], "col2_code": procs[j]},
                    )
                )

    return context


# ---------------------------------------------------------------------------
# Request construction
# ---------------------------------------------------------------------------

def build_judge_user_message(case: dict, rule_context: dict) -> str:
    """
    Build the judge's user message from a case record ({claim, gate, scoring}).

    Deliberately EXCLUDES scoring['tool_calls'] — the scorer's reasoning chain
    stays out of the judge's context (independence).
    """
    scoring = case["scoring"]
    scorer_output = {
        "risk_score": scoring["risk_score"],
        "confidence": scoring["confidence"],
        "predicted_denial_code": scoring["predicted_denial_code"],
        "driving_fields": scoring["driving_fields"],
        "recommended_action": scoring["recommended_action"],
        "rationale": scoring["rationale"],
        "used_fallback": scoring["used_fallback"],
    }
    return (
        "CLAIM:\n" + json.dumps(case["claim"], indent=2, sort_keys=True)
        + "\n\nGATE DECISION:\n" + json.dumps(case["gate"], indent=2, sort_keys=True)
        + "\n\nGOVERNING RULE CONTEXT (ground truth — fetched deterministically):\n"
        + json.dumps(rule_context, indent=2, sort_keys=True)
        + "\n\nSCORER OUTPUT TO GRADE:\n" + json.dumps(scorer_output, indent=2, sort_keys=True)
        + "\n\nGrade each rubric criterion."
    )


def build_batch_request(case: dict, rule_context: dict, model: str = JUDGE_MODEL) -> dict:
    """One Batches-API request dict. custom_id = score_id (results arrive unordered)."""
    return {
        "custom_id": case["scoring"]["score_id"],
        "params": {
            "model": model,
            "max_tokens": JUDGE_MAX_TOKENS,
            "system": JUDGE_SYSTEM_PROMPT,
            "output_config": {
                "format": {"type": "json_schema", "schema": JUDGE_OUTPUT_SCHEMA}
            },
            "messages": [
                {"role": "user", "content": build_judge_user_message(case, rule_context)}
            ],
        },
    }


# ---------------------------------------------------------------------------
# Batch harness
# ---------------------------------------------------------------------------

class JudgeHarness:
    """
    Submits judge requests through the Message Batches API and collects
    verdicts keyed by custom_id (score_id) — never by position.
    """

    def __init__(self, client, tool_registry) -> None:
        self._client = client
        self._registry = tool_registry

    def submit(self, cases: list[dict], model: str = JUDGE_MODEL):
        """Build and submit one batch for all cases. Returns the batch object."""
        requests = [
            build_batch_request(case, build_rule_context(case["claim"], self._registry), model)
            for case in cases
        ]
        batch = self._client.messages.batches.create(requests=requests)
        log.info("judge_batch_submitted", batch_id=batch.id, n=len(requests), model=model)
        return batch

    def wait(self, batch_id: str, poll_s: float = 30.0, timeout_s: float = 3600.0):
        """Poll until the batch ends. Returns the final batch object."""
        deadline = time.monotonic() + timeout_s
        while True:
            batch = self._client.messages.batches.retrieve(batch_id)
            if batch.processing_status == "ended":
                return batch
            if time.monotonic() > deadline:
                raise TimeoutError(f"Batch {batch_id} did not end within {timeout_s}s")
            log.info(
                "judge_batch_polling",
                batch_id=batch_id,
                status=batch.processing_status,
                counts=str(batch.request_counts),
            )
            time.sleep(poll_s)

    def collect(self, batch_id: str, cases: list[dict], model: str = JUDGE_MODEL) -> list[JudgeVerdict]:
        """Stream batch results into JudgeVerdicts. Errors become error-verdicts."""
        claim_by_score = {c["scoring"]["score_id"]: c["claim"].get("claim_id", "") for c in cases}
        verdicts: list[JudgeVerdict] = []

        for result in self._client.messages.batches.results(batch_id):
            score_id = result.custom_id
            claim_id = claim_by_score.get(score_id, "")
            if result.result.type != "succeeded":
                verdicts.append(JudgeVerdict(
                    score_id=score_id, claim_id=claim_id, judge_model=model,
                    rubric_version=RUBRIC_VERSION, criteria={}, overall_pass=False,
                    error=result.result.type,
                ))
                continue
            msg = result.result.message
            text = next((b.text for b in msg.content if b.type == "text"), "")
            verdicts.append(parse_verdict(text, score_id, claim_id, model))
        return verdicts


def parse_verdict(text: str, score_id: str, claim_id: str, model: str = JUDGE_MODEL) -> JudgeVerdict:
    """Parse a judge response body (guaranteed-schema JSON) into a JudgeVerdict."""
    try:
        data = json.loads(text)
        criteria = data["criteria"]
        missing = [c for c in CRITERIA if c not in criteria]
        if missing:
            raise KeyError(f"missing criteria: {missing}")
        return JudgeVerdict(
            score_id=score_id, claim_id=claim_id, judge_model=model,
            rubric_version=RUBRIC_VERSION, criteria=criteria,
            overall_pass=bool(data["overall_pass"]),
        )
    except (json.JSONDecodeError, KeyError, TypeError) as e:
        log.warning("judge_parse_error", score_id=score_id, error=str(e))
        return JudgeVerdict(
            score_id=score_id, claim_id=claim_id, judge_model=model,
            rubric_version=RUBRIC_VERSION, criteria={}, overall_pass=False,
            error=f"parse_error: {e}",
        )


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------

def compute_metrics(verdicts: list[JudgeVerdict]) -> JudgeMetrics:
    ok = [v for v in verdicts if v.error is None]
    n_err = len(verdicts) - len(ok)
    if not ok:
        return JudgeMetrics(n_judged=0, n_errors=n_err, overall_pass_rate=0.0)

    per_criterion = {
        name: sum(1 for v in ok if v.criteria.get(name, {}).get("passed")) / len(ok)
        for name in CRITERIA
    }
    return JudgeMetrics(
        n_judged=len(ok),
        n_errors=n_err,
        overall_pass_rate=sum(1 for v in ok if v.overall_pass) / len(ok),
        per_criterion_pass_rate=per_criterion,
    )


def select_disagreements(verdicts: list[JudgeVerdict], n: int = 10) -> list[JudgeVerdict]:
    """Failed verdicts for manual review — calibrate the judge before trusting it."""
    failed = [v for v in verdicts if v.error is None and not v.overall_pass]
    return failed[:n]


def agreement_rate(a: list[JudgeVerdict], b: list[JudgeVerdict]) -> float:
    """Overall-verdict agreement between two judges on the same cases (by score_id).
    Used for the Sonnet spot-check: >90% agreement makes the Haiku judge defensible."""
    b_by_id = {v.score_id: v for v in b if v.error is None}
    pairs = [(va, b_by_id[va.score_id]) for va in a if va.error is None and va.score_id in b_by_id]
    if not pairs:
        return 0.0
    return sum(1 for va, vb in pairs if va.overall_pass == vb.overall_pass) / len(pairs)
