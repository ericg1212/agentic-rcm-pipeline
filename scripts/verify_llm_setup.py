# Copyright (c) 2026 Eric Grynspan. All rights reserved.
"""
Phase 3 pre-flight: verify API key + request shape at $0 cost.

Checks, in order:
  1. ANTHROPIC_API_KEY is set — printed masked so the operator can confirm
     it is the intended Console account before any billable call.
  2. count_tokens against the live API with the exact system + tool payload
     the scorer sends. This is free and validates the strict tool schemas
     server-side — a schema the API rejects fails here, not mid-run.
  3. Reports the tools+system prefix size vs the minimum cacheable prefix,
     so we know whether prompt caching will actually engage.

Usage:  python scripts/verify_llm_setup.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import anthropic

from src.config.settings import LLMConfig
from src.consumer.ncci_gate import NCCIGate
from src.reasoning.prompt import SYSTEM_PROMPT
from src.reasoning.scorer import ClaimScorer

# Minimum cacheable prefix (tokens). Published per-model; Sonnet-line models
# range 1024-2048 — use the conservative bound.
CACHE_MIN_TOKENS = 2048


def main() -> int:
    key = LLMConfig.API_KEY
    if not key:
        print("FAIL: ANTHROPIC_API_KEY is not set")
        return 1
    print(f"API key (masked): {key[:12]}...{key[-4:]}  (len={len(key)})")
    print(f"Model:            {LLMConfig.MODEL_VERSION_TAG}")

    gate = NCCIGate()
    gate.load()
    scorer = ClaimScorer(gate)

    client = anthropic.Anthropic(api_key=key)
    try:
        resp = client.messages.count_tokens(
            model=LLMConfig.MODEL_VERSION_TAG,
            system=[{
                "type": "text",
                "text": SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},
            }],
            tools=scorer._all_tools,
            messages=[{"role": "user", "content": "ping"}],
        )
    except anthropic.APIStatusError as e:
        print(f"FAIL: count_tokens rejected the request ({e.status_code}): {e.message}")
        return 1

    prefix = resp.input_tokens
    print(f"count_tokens OK:  tools+system prefix = {prefix} tokens")
    if prefix >= CACHE_MIN_TOKENS:
        print(f"Prompt caching:   ENGAGES (prefix >= {CACHE_MIN_TOKENS})")
    else:
        print(f"Prompt caching:   NOT APPLICABLE (prefix < {CACHE_MIN_TOKENS}; no padding per plan)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
