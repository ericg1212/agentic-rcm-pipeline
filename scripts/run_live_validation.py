# Copyright (c) 2026 Eric Grynspan. All rights reserved.
"""
Phase 3 — Live scorer validation runner (ADR-008).

Runs the gate -> scorer path against the LIVE Anthropic API and persists every
ScoringResult to a JSONL file (the source of truth for the README metrics).

Modes (mandatory order per the P4 planning standard):
  inspect  Eyeball 10 generated claims + 10 noise-injected claims + gate
           decisions. NO API calls. Run this first.
  smoke    20-record live run. Review results before proceeding.
  full     ~300-claim live run + the wrong-diagnosis noise-injection suite.

Usage:
  python scripts/run_live_validation.py inspect
  python scripts/run_live_validation.py smoke [--n 20]
  python scripts/run_live_validation.py full  [--n 300] [--dirty-fraction 0.30]

Outputs land in data/exports/phase3/:
  scoring_results_<mode>_<UTC timestamp>.jsonl   one ScoringResult per line
  summary printed to stdout (cost/claim, p50/p95 latency, cache hit rate,
  fallback rate, and — for full — live recovery rate on dirty claims)
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from src.consumer.ncci_gate import NCCIGate, Route
from src.eval.noise_injection import inject_wrong_diagnosis, LCD_RESTRICTED_PROCEDURES
from src.generator.claim_generator import ClaimGenerator

EXPORT_DIR = ROOT / "data" / "exports" / "phase3"
SEED = 42


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _result_to_json(result) -> dict:
    d = dataclasses.asdict(result)
    d["scored_at"] = result.scored_at.isoformat()
    return d


def _gen_claims(n: int, seed: int = SEED) -> list[dict]:
    gen = ClaimGenerator(seed=seed)
    return [gen.generate_one().to_dict() for _ in range(n)]


def _percentile(values: list[int | float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(int(round(pct * (len(ordered) - 1))), len(ordered) - 1)
    return float(ordered[idx])


def _print_summary(results: list, label: str) -> None:
    n = len(results)
    if n == 0:
        print("No results.")
        return
    latencies = [r.latency_ms for r in results]
    costs = [r.cost_usd for r in results]
    fallbacks = [r for r in results if r.used_fallback]
    # Cache hit = any request in the claim's tool loop read the cached prefix
    cache_hits = [r for r in results if r.cache_read_input_tokens > 0]
    total_cost = sum(costs)

    print(f"\n===== {label} SUMMARY ({n} claims) =====")
    print(f"total cost:        ${total_cost:.4f}")
    print(f"cost/claim:        ${total_cost / n:.6f}  (mean)")
    print(f"latency p50:       {_percentile(latencies, 0.50):.0f} ms")
    print(f"latency p95:       {_percentile(latencies, 0.95):.0f} ms")
    print(f"cache hit rate:    {len(cache_hits) / n:.1%}  ({len(cache_hits)}/{n} claims read cache)")
    print(f"fallback rate:     {len(fallbacks) / n:.1%}  ({len(fallbacks)}/{n})")
    if fallbacks:
        reasons = {}
        for r in fallbacks:
            reasons[r.fallback_reason] = reasons.get(r.fallback_reason, 0) + 1
        print(f"fallback reasons:  {reasons}")
    in_tok = sum(r.input_tokens for r in results)
    out_tok = sum(r.output_tokens for r in results)
    cr_tok = sum(r.cache_read_input_tokens for r in results)
    cw_tok = sum(r.cache_creation_input_tokens for r in results)
    print(f"tokens:            in={in_tok} out={out_tok} cache_read={cr_tok} cache_write={cw_tok}")


def _write_jsonl(cases: list[dict], mode: str) -> Path:
    """Persist full case records: {claim, gate, scoring}. The judge harness
    (src/eval/judge.py) consumes this file — it needs claim + gate context,
    not just the scoring output."""
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = EXPORT_DIR / f"scoring_results_{mode}_{stamp}.jsonl"
    with open(path, "w", encoding="utf-8") as f:
        for case in cases:
            f.write(json.dumps(case) + "\n")
    print(f"\nPersisted {len(cases)} case records -> {path}")
    return path


def _score_batch(claims: list[dict], gate: NCCIGate, scorer) -> tuple[list, list[dict]]:
    """Returns (results, case_records)."""
    results = []
    cases = []
    for i, claim in enumerate(claims, 1):
        decision = gate.evaluate(claim)
        r = scorer.score(claim, decision)
        results.append(r)
        cases.append({
            "claim": claim,
            "gate": decision.to_dict(),
            "scoring": _result_to_json(r),
        })
        flag = "FB" if r.used_fallback else "ok"
        hit = "hit" if r.cache_read_input_tokens > 0 else "---"
        print(
            f"[{i:>3}/{len(claims)}] {claim['claim_id'][:8]} gate={decision.route.value:<9} "
            f"risk={r.risk_score:>3} act={r.recommended_action:<12} carc={r.predicted_denial_code or '-':<6} "
            f"{r.latency_ms:>5}ms ${r.cost_usd:.5f} cache={hit} {flag}"
        )
    return results, cases


# ---------------------------------------------------------------------------
# Modes
# ---------------------------------------------------------------------------

def mode_inspect() -> int:
    """Data inspection — 10 generated + 10 noise-injected claims. No API calls."""
    gate = NCCIGate()
    gate.load()

    print("=" * 76)
    print("A) 10 GENERATED CLAIMS (mixed clean/dirty per generator distribution)")
    print("=" * 76)
    claims = _gen_claims(10)
    for c in claims:
        d = gate.evaluate(c)
        print(json.dumps(c, indent=2))
        print(f"  -> GATE: route={d.route.value} risk={d.risk_score:.2f} "
              f"carc={d.deterministic_carc or '-'} violations={len(d.violations or [])}\n")

    print("=" * 76)
    print("B) 10 NOISE-INJECTED (wrong_diagnosis) CLAIMS")
    print("=" * 76)
    # Generate a larger pool, keep gate-PASS claims with LCD-restricted procedures
    import random
    rng = random.Random(SEED)
    pool = _gen_claims(400, seed=SEED + 1)
    injected = []
    for c in pool:
        if len(injected) >= 10:
            break
        if gate.evaluate(c).route != Route.PASS:
            continue
        dirty = inject_wrong_diagnosis(c, rng)
        if dirty is None:
            continue
        injected.append((c, dirty))

    for orig, dirty in injected:
        d = gate.evaluate(dirty)
        print(f"claim {dirty['claim_id'][:8]}: procs={dirty['procedure_codes']} "
              f"diags {orig['diagnosis_codes']} -> {dirty['diagnosis_codes']}")
        print(f"  -> GATE ON DIRTY: route={d.route.value} risk={d.risk_score:.2f} "
              f"(gate is blind to diagnosis mismatch — expected PASS)\n")

    print(f"LCD-restricted procedure pool: {sorted(LCD_RESTRICTED_PROCEDURES)}")
    print(f"Injectable rate in 400-claim pool: {len(injected)}/10 requested found")
    return 0


def mode_smoke(n: int) -> int:
    """20-record live smoke test. STOP and review output before the full run."""
    from src.reasoning.scorer import ClaimScorer  # imports anthropic — keep lazy

    gate = NCCIGate()
    gate.load()
    scorer = ClaimScorer(gate)
    claims = _gen_claims(n)

    print(f"SMOKE TEST — {n} claims against the LIVE API ({scorer._all_tools[0]['name']}... "
          f"{len(scorer._all_tools)} tools, strict)")
    results, cases = _score_batch(claims, gate, scorer)
    _write_jsonl(cases, "smoke")
    _print_summary(results, "SMOKE")

    schema_violations = [r for r in results if r.fallback_reason == "validation_failed"]
    print(f"\nschema-violation fallbacks: {len(schema_violations)} "
          f"(strict tool use should keep this at 0)")
    print("CHECKPOINT: review the lines above before running `full`.")
    return 0


def mode_noise(dirty_fraction: float) -> int:
    """Noise-injection suite only (no main run) — for suite reruns."""
    from src.reasoning.scorer import ClaimScorer

    gate = NCCIGate()
    gate.load()
    scorer = ClaimScorer(gate)
    return _run_noise_suite(gate, scorer, dirty_fraction)


def mode_full(n: int, dirty_fraction: float) -> int:
    """Full live run: n claims through gate->scorer + noise-injection suite."""
    from src.reasoning.scorer import ClaimScorer

    gate = NCCIGate()
    gate.load()
    scorer = ClaimScorer(gate)

    # --- main run -----------------------------------------------------------
    claims = _gen_claims(n)
    print(f"FULL RUN — {n} claims against the LIVE API")
    results, cases = _score_batch(claims, gate, scorer)
    _write_jsonl(cases, "full")
    _print_summary(results, "FULL RUN")

    return _run_noise_suite(gate, scorer, dirty_fraction)


def _run_noise_suite(gate: NCCIGate, scorer, dirty_fraction: float) -> int:
    """Noise-injection suite: LIVE recovery rate on dirty claims."""
    from src.eval.noise_injection import run_noise_injection_eval

    print("\nNOISE-INJECTION SUITE (wrong_diagnosis, LIVE API)")
    # Clean pool: gate-PASS claims only, so injected dirt is the only signal
    pool = _gen_claims(400, seed=SEED + 1)
    clean = [c for c in pool if gate.evaluate(c).route == Route.PASS]
    eval_result = run_noise_injection_eval(
        clean_claims=clean,
        ncci_gate=gate,
        scorer=scorer,
        dirty_fraction=dirty_fraction,
        seed=SEED,
    )
    print(f"\n{eval_result.summary()}")
    print(f"LIVE LLM recovery rate on gate false negatives: {eval_result.lift:.1%}")

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    eval_path = EXPORT_DIR / f"noise_eval_{stamp}.json"
    with open(eval_path, "w", encoding="utf-8") as f:
        json.dump({
            "run_at": stamp,
            "n_clean_pool": len(clean),
            "n_injected": eval_result.n_dirty,
            "gate_false_negatives": eval_result.gate_false_negatives,
            "llm_recoveries": eval_result.llm_recoveries,
            "lift": eval_result.lift,
        }, f, indent=2)
    print(f"Noise eval persisted -> {eval_path}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("mode", choices=["inspect", "smoke", "full", "noise"])
    p.add_argument("--n", type=int, default=None, help="claim count (smoke=20, full=300)")
    p.add_argument("--dirty-fraction", type=float, default=0.30)
    args = p.parse_args()

    if args.mode == "inspect":
        return mode_inspect()
    if args.mode == "smoke":
        return mode_smoke(args.n or 20)
    if args.mode == "noise":
        return mode_noise(args.dirty_fraction)
    return mode_full(args.n or 300, args.dirty_fraction)


if __name__ == "__main__":
    sys.exit(main())
