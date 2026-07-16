# Copyright (c) 2026 Eric Grynspan. All rights reserved.
"""
Phase 3 — LLM-as-Judge batch runner (ADR-008).

Reads a case-record JSONL produced by run_live_validation.py, judges every
case with Haiku via the Message Batches API (50% off), and persists verdicts
+ metrics. Optionally re-judges a sample with Sonnet 5 to measure agreement
with a stronger judge (validates the Haiku judge itself).

Usage:
  python scripts/run_judge.py data/exports/phase3/scoring_results_full_<ts>.jsonl
  python scripts/run_judge.py <cases.jsonl> --spot-check 30
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import anthropic

from src.config.settings import LLMConfig
from src.consumer.ncci_gate import NCCIGate
from src.eval.judge import (
    JUDGE_MODEL,
    SPOT_CHECK_MODEL,
    JudgeHarness,
    agreement_rate,
    compute_metrics,
    select_disagreements,
)
from src.reasoning.tools import ToolRegistry

EXPORT_DIR = ROOT / "data" / "exports" / "phase3"


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("cases_jsonl", type=Path)
    p.add_argument("--spot-check", type=int, default=0,
                   help="Re-judge the first N cases with Sonnet 5 and report agreement")
    args = p.parse_args()

    cases = [json.loads(line) for line in args.cases_jsonl.read_text(encoding="utf-8").splitlines() if line.strip()]
    print(f"Loaded {len(cases)} case records from {args.cases_jsonl}")

    gate = NCCIGate()
    gate.load()
    registry = ToolRegistry(gate)
    client = anthropic.Anthropic(api_key=LLMConfig.API_KEY)
    harness = JudgeHarness(client, registry)

    # --- Haiku batch ---------------------------------------------------------
    batch = harness.submit(cases, model=JUDGE_MODEL)
    print(f"Submitted batch {batch.id} ({len(cases)} requests, {JUDGE_MODEL}). Polling...")
    harness.wait(batch.id)
    verdicts = harness.collect(batch.id, cases, model=JUDGE_MODEL)

    metrics = compute_metrics(verdicts)
    print("\n===== JUDGE METRICS (Haiku batch) =====")
    print(metrics.summary())

    disagreements = select_disagreements(verdicts, n=10)
    print(f"\n{len(disagreements)} failed verdicts pulled for manual review:")
    for v in disagreements:
        print(f"  {v.claim_id[:8]} score_id={v.score_id[:8]} failed={v.failed_criteria}")

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = EXPORT_DIR / f"judge_verdicts_{stamp}.jsonl"
    with open(out, "w", encoding="utf-8") as f:
        for v in verdicts:
            f.write(json.dumps(dataclasses.asdict(v)) + "\n")
    print(f"\nVerdicts persisted -> {out}")

    metrics_out = EXPORT_DIR / f"judge_metrics_{stamp}.json"
    payload = dataclasses.asdict(metrics)

    # --- Sonnet spot-check (validate the judge) ------------------------------
    if args.spot_check > 0:
        sample = cases[: args.spot_check]
        print(f"\nSpot-check: re-judging {len(sample)} cases with {SPOT_CHECK_MODEL}...")
        sc_batch = harness.submit(sample, model=SPOT_CHECK_MODEL)
        harness.wait(sc_batch.id)
        sc_verdicts = harness.collect(sc_batch.id, sample, model=SPOT_CHECK_MODEL)
        agree = agreement_rate(verdicts, sc_verdicts)
        payload["sonnet_spot_check"] = {
            "n": len(sample),
            "agreement_rate": agree,
            "defensible": agree > 0.90,
        }
        print(f"Haiku-vs-Sonnet agreement: {agree:.1%} "
              f"({'defensible (>90%)' if agree > 0.90 else 'REVIEW — below 90%'})")

    with open(metrics_out, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    print(f"Metrics persisted -> {metrics_out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
