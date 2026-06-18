# ADR-004: Action Layer Routing — 3-Condition Auto-Correct Gate

**Status:** Accepted  
**Date:** 2026-06-17 (Layer 3 build session)  
**Decider:** Eric Grynspan

---

## Decision

Auto-correct requires all three conditions to pass simultaneously:
1. `recommended_action == "auto_correct"` (LLM explicitly recommended a correction, not just flagged risk)
2. `confidence >= 0.92` (model certainty above threshold)
3. `submitted_charge <= 500.00` (low-dollar claim — high-dollar corrections require human review)

Any single condition failure routes the claim to `flag` instead.

## Why

**Condition 1 — action field:** The LLM returns one of four actions: auto_correct, flag, hold, escalate. Requiring `auto_correct` explicitly means a claim scored as `flag` or `escalate` with high confidence still goes to a human. The LLM must recommend the action, not just score risk.

**Condition 2 — confidence floor (0.92):** FCA scienter defense. Reckless disregard for accuracy is the scienter standard. A confidence floor ensures the system only auto-corrects when the model is highly certain. Claims between 0.75–0.92 confidence are flagged for human review with full LLM rationale attached.

**Condition 3 — charge ceiling ($500):** High-dollar claims carry higher financial and compliance risk. A $50 modifier mismatch is materially different from a $5,000 procedure mis-code. Claims above $500 are flagged regardless of confidence — a human must approve.

**Together:** The three conditions map directly to FCA liability elements. Falsity is addressed by grounding every correction in a cited rule (`governing_rule_cited` required). Scienter is addressed by the confidence floor and human review path for anything uncertain or high-value.

## Rejected

| Alternative | Why rejected |
|---|---|
| **Binary auto-correct / manual split** | No confidence nuance — a low-confidence auto_correct recommendation triggers the same action as a high-confidence one. Insufficient for FCA defense. |
| **Confidence-only gate** | Doesn't protect high-dollar claims. A $10,000 claim with 0.95 confidence would auto-correct without human review. |
| **Action-only gate** | Ignores model uncertainty. A 0.51-confidence auto_correct recommendation would trigger the same action as a 0.99-confidence one. |
| **Single charge threshold without confidence** | Protects on value but not on accuracy — a low-confidence correction on a $499 claim would still auto-correct. |
| **LLM-determined routing without any gate** | No compliance mechanism. The LLM deciding its own action threshold is not a defensible FCA position. |
