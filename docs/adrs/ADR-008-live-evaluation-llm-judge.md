# ADR-008: Live Evaluation & LLM-as-Judge Methodology

**Status:** Accepted
**Date:** 2026-07-15 (Phase 3 live validation session)
**Decider:** Eric Grynspan

---

## Decision

Replace all mock-derived performance claims with live-measured numbers, and grade every scorer recommendation with an independent LLM judge:

1. **Live validation run** — the full gate → scorer path executes against the live Anthropic API (claude-sonnet-5) over ~300 generated claims plus the wrong-diagnosis noise-injection suite. Every `ScoringResult` is persisted to JSONL (source of truth for all published metrics: cost/claim, p50/p95 latency, cache hit rate, fallback rate, live recovery rate on dirty claims).
2. **Scorer migration to claude-sonnet-5** — sampling parameters removed (rejected by the API), determinism reframed as structural enforcement: strict tool schemas (`strict: true`, `additionalProperties: false`) guarantee schema-valid tool inputs at the API boundary, the CARC enum constrains denial codes, and `_validate()` bounds-checks post-hoc. Thinking is explicitly disabled — the scorer sits in a latency-gated path (ADR-003) and Sonnet 5 defaults to adaptive thinking when the field is omitted.
3. **Prompt caching** — `cache_control` on the system block caches the tools+system prefix (3,078 tokens, above the 2,048 minimum) across claims. Cache reads bill at 0.10x the input rate; the cost model accounts for reads and writes separately.
4. **LLM-as-Judge eval harness** — every scoring output is graded against a 5-criterion rubric (CARC plausibility, rule applicability, action consistency, guidance actionability, no fabrication) by claude-haiku-4-5 through the Message Batches API (50% off). The judge sees the claim, gate decision, scorer output, and deterministically fetched governing-rule context — never the scorer's tool-call trace. The judge itself is validated by re-judging a 30-case sample with claude-sonnet-5 and measuring verdict agreement.

## Why

**The honesty gap.** Through Phase 2, the README's noise-injection claim rested on a mocked scorer. "Measured, not assumed" was true of the architecture but not of the headline numbers. After this run, every published number is live-measured with a run date.

**Judge independence over self-critique.** A judge that reads the scorer's reasoning chain inherits its framing — if the scorer misread a coverage note, a judge shown that reading tends to accept it. The judge instead receives rule context fetched deterministically (every relevant lookup, every code pair), so it grades against ground truth rather than the scorer's selective retrieval.

**Haiku + Batch for the judge.** Judging here is classification-shaped: five independent pass/fail checks against explicit criteria. Haiku 4.5 at batch pricing ($0.50/$2.50 effective per MTok) grades ~300 cases for under $1 — roughly 4x cheaper than synchronous Sonnet with no measurable quality loss on a rubric this constrained. The Sonnet spot-check quantifies (rather than assumes) that claim: agreement above 90% makes the cheap judge defensible.

**The smoke gate earned its place.** The mandatory 20-record smoke test caught two classes of issue before the full-run budget was spent:

- *Strict-mode schema limits.* The API rejects `minimum`/`maximum` keywords and union-type enums under `strict: true`. Bounds moved into field descriptions and remain enforced by `_validate()`; the CARC enum became an `anyOf`. Layered enforcement is the design — the schema guarantees shape, the validator guarantees bounds.
- *Action-semantics drift on model migration.* Sonnet 5 interpreted `auto_correct` as "safe to submit," recommending it on 13/20 clean claims (Sonnet 4.6 defaulted to `flag`). The action enum has no submit-as-is option, so the model reached for the closest affirmative action. Prompt v1.2.0 restricts `auto_correct` to claims with a specific correctable defect; the re-smoke showed 0/13. This is the concrete argument for smoke-gating every model migration: behavior shifts that no unit test catches surface in 20 live records.

**The eval gates then caught two more issues — one in the harness, one in the judge:**

- *Injection-pool defect (noise suite iteration 1 → 2).* The first live noise suite measured 89.5% recovery (17/19). Root-causing the two misses showed both were psychotherapy claims (90834/90837) injected with F84.0 — which is an F-code, and the psychotherapy LCD covers all F-prefix diagnoses. The scorer had correctly judged those claims covered; the harness had injected a "wrong" diagnosis that wasn't wrong. The injection pool now filters candidates against each procedure's covered ICD-10 prefixes, and the corrected suite measured 100% recovery (18/18). Both numbers are reported: the 89.5% is what an unaudited harness would have published.
- *Judge calibration (rubric v1.0.0 → v1.1.0).* The first judge batch returned a 7.7% overall pass rate with 80% Haiku-vs-Sonnet agreement — below the 90% defensibility bar, which per this methodology means review before trust. Manual review of failures showed one dominant miscalibration: the judge failed `flag` on clean low-risk claims, not knowing the action space has no pass/submit option (`flag` is the minimum-intervention action). Rubric v1.1.0 states the action space explicitly. The same review confirmed two genuine scorer findings the judge was right about: null predicted CARC on high-risk claims (some citing CO-197 in the rationale while predicting null — an internal contradiction), and one auto_correct on a risk-92 hard-fail (blocked downstream by the router's escalation guard).

## Measured results (live run of 2026-07-15, 300 main claims + noise suite)

| Metric | Value |
|---|---|
| Cost per claim scored (mean, live) | $0.0119 (claude-sonnet-5 intro pricing, incl. cache accounting) |
| Latency p50 / p95 | 11.6s / 16.7s (2–3 tool calls per claim; sequential, production-path) |
| Cache hit rate | 100% (300/300 claims; 1.95M tokens served from cache) |
| Fallback rate | 2.3% (7/300, all max-iterations; 0 schema violations under strict tools) |
| Gate false negatives (corrected noise suite) | 18/18 injected wrong-diagnosis claims passed the gate (diagnosis-blind by design) |
| Live LLM recovery rate on dirty claims | **100% (18/18)** — first-iteration harness measured 89.5% before the injection-pool fix (see above) |
| Judge overall pass rate (Haiku, rubric v1.1.0, n=300) | {{JUDGE_PASS_RATE}} |
| Haiku-vs-Sonnet judge agreement (n=30) | {{AGREEMENT_RATE}} (v1.0.0 rubric: 80%, below bar → calibrated) |

## Rejected

| Alternative | Why rejected |
|---|---|
| **Self-critique by the scorer** | Context contamination: the model grades its own framing. A scorer that hallucinated a rule will cite that rule in its own defense. Fresh-context judging with independently fetched rule text breaks the loop |
| **Human-only eval** | Doesn't scale past double-digit samples and can't re-run per model/prompt version. Humans stay in the loop where they add unique value: the 10-case manual review of judge disagreements that calibrates the judge itself |
| **Exact-match assertions** | The rationale field is natural language; exact-match is brittle against paraphrase and penalizes correct-but-differently-worded guidance. Rubric criteria grade dimensions independently, so failures localize (e.g. plausible CARC, vague guidance) |
| **Synchronous judge calls** | ~2x the token cost versus the Batches API for a workload with zero latency requirement. Batch results land within the hour; the eval is offline by construction |
| **Sonnet-5-as-judge for everything** | ~4x the cost for a classification-shaped task. The right use of the stronger model is validating the cheaper judge (30-case agreement sample), not doing the whole job |
