#!/usr/bin/env python3
"""
run_evals.py — score the Aegis repair loop against a labeled crash-to-fix corpus.

For each case in the corpus:
  1. GENERATE — the executor model (Hermes) is given the messy production log and
     must diagnose the root cause and emit the fix as a unified git diff.
  2. GRADE — the reviewer model (Qwen) acts as judge: it compares the candidate
     diff to the ground-truth diff and returns a verdict + score (LLM-as-judge).

Aggregates a mean fix-rate and exits non-zero below --threshold, so it doubles
as a CI quality gate. Endpoint + models come from config/.env:
AEGIS_LLM_BASE_URL, AEGIS_EXECUTOR_MODEL, AEGIS_REVIEWER_MODEL.

Usage:
    python run_evals.py
    python run_evals.py --corpus eval/corpus.json --threshold 0.6 --report eval/report.json
    python run_evals.py --limit 3 --concurrency 1
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

from aegis_sre.config import get_settings
from aegis_sre.orchestrator.llm import chat_json

GENERATE_SYSTEM = (
    "You are Aegis, an autonomous Site Reliability Engineer. You are given a messy "
    "production crash log. Diagnose the true root cause and produce the MINIMAL fix "
    "as a unified git diff (with `diff --git` and `@@` hunks). Do not explain outside "
    "the JSON. Respond with a JSON object ONLY matching this schema:\n"
    '{"root_cause": "<one sentence>", "candidate_diff": "<unified git diff>"}'
)

GRADE_SYSTEM = (
    "You are a strict staff-level SRE reviewer grading a CANDIDATE fix against the "
    "known-correct GROUND-TRUTH fix. Judge whether the candidate targets the SAME "
    "root cause and makes a functionally equivalent change. Exact wording, file "
    "paths, or line numbers need not match — correctness of the fix does. Score 1.0 "
    "for a correct fix, ~0.5 if it is on the right track but incomplete/risky, 0.0 if "
    "it misdiagnoses or would not resolve the incident. Respond with a JSON object "
    "ONLY matching this schema:\n"
    '{"verdict": "correct|partial|incorrect", "score": <0.0-1.0>, '
    '"addresses_root_cause": <true|false>, "reasoning": "<two sentences>"}'
)


async def generate_fix(case: dict) -> tuple[str, str]:
    user = f"INCIDENT: {case.get('description', '')}\n\nLOG:\n{case['log_snippet']}"
    raw = await chat_json(get_settings().executor_model, GENERATE_SYSTEM, user)
    data = json.loads(raw)
    return data.get("candidate_diff", ""), data.get("root_cause", "")


async def grade_fix(case: dict, candidate_diff: str) -> dict:
    user = (
        f"INCIDENT: {case.get('description', '')}\n\n"
        f"GROUND-TRUTH FIX (diff):\n{case['ground_truth_diff']}\n\n"
        f"CANDIDATE FIX (diff):\n{candidate_diff or '(model produced no diff)'}"
    )
    raw = await chat_json(get_settings().reviewer_model, GRADE_SYSTEM, user)
    return json.loads(raw)


async def run_case(case: dict, sem: asyncio.Semaphore) -> dict:
    cid = case.get("id", "?")
    started = time.monotonic()
    async with sem:
        try:
            candidate_diff, root_cause = await generate_fix(case)
            verdict = await grade_fix(case, candidate_diff)
            return {
                "id": cid,
                "verdict": str(verdict.get("verdict", "incorrect")).lower(),
                "score": float(verdict.get("score", 0.0) or 0.0),
                "addresses_root_cause": bool(verdict.get("addresses_root_cause", False)),
                "reasoning": verdict.get("reasoning", ""),
                "predicted_root_cause": root_cause,
                "candidate_diff": candidate_diff,
                "seconds": round(time.monotonic() - started, 1),
                "error": None,
            }
        except Exception as e:  # one bad case must not sink the whole run
            return {
                "id": cid, "verdict": "error", "score": 0.0,
                "addresses_root_cause": False, "reasoning": "",
                "predicted_root_cause": "", "candidate_diff": "",
                "seconds": round(time.monotonic() - started, 1),
                "error": f"{type(e).__name__}: {e}",
            }


REQUIRED_FIELDS = ("id", "description", "log_snippet", "ground_truth_diff")


def validate_corpus(cases: list) -> None:
    """Raise ValueError if the corpus is malformed. Guards against silent drift:
    every case needs the required fields, ids must be unique, and each
    ground-truth fix must look like a unified git diff."""
    if not isinstance(cases, list) or not cases:
        raise ValueError("corpus must be a non-empty JSON array")
    seen: set[str] = set()
    for i, c in enumerate(cases):
        missing = [f for f in REQUIRED_FIELDS if not c.get(f)]
        if missing:
            raise ValueError(f"case #{i} ({c.get('id', '?')}) missing/empty fields: {missing}")
        cid = c["id"]
        if cid in seen:
            raise ValueError(f"duplicate case id: {cid!r}")
        seen.add(cid)
        diff = c["ground_truth_diff"]
        if "diff --git" not in diff or "@@" not in diff:
            raise ValueError(f"case {cid}: ground_truth_diff is not a unified git diff")


def load_corpus(path: str) -> list:
    cases = json.loads(Path(path).read_text())
    validate_corpus(cases)
    return cases


def aggregate(results: list, threshold: float) -> dict:
    """Reduce per-case results to a mean fix-rate, verdict counts, and pass/fail.
    Pure (no I/O) so the CI-gate math is unit-testable without an LLM."""
    n = len(results) or 1
    mean = sum(r["score"] for r in results) / n
    counts: dict[str, int] = {}
    for r in results:
        counts[r["verdict"]] = counts.get(r["verdict"], 0) + 1
    return {"mean": mean, "counts": counts, "passed": mean >= threshold, "n": len(results)}


async def main_async(args: argparse.Namespace) -> int:
    s = get_settings()
    cases = load_corpus(args.corpus)
    if args.limit:
        cases = cases[: args.limit]

    sem = asyncio.Semaphore(max(1, args.concurrency))
    print(f"Running {len(cases)} case(s): {s.executor_model}  ->  {s.reviewer_model}  @ {s.llm_base_url}")
    results = await asyncio.gather(*(run_case(c, sem) for c in cases))
    results.sort(key=lambda r: r["id"])

    agg = aggregate(results, args.threshold)
    mean, counts = agg["mean"], agg["counts"]

    print("\n" + "-" * 78)
    print(f"{'id':<10}{'verdict':<12}{'score':>7}{'root_cause':>13}{'sec':>8}")
    print("-" * 78)
    for r in results:
        line = f"{r['id']:<10}{r['verdict']:<12}{r['score']:>7.2f}{str(r['addresses_root_cause']):>13}{r['seconds']:>8.1f}"
        if r["error"]:
            line += f"   ERR: {r['error']}"
        print(line)
    print("-" * 78)
    summary = "  ".join(f"{k}={v}" for k, v in sorted(counts.items()))
    print(f"cases={len(results)}  mean_fix_rate={mean:.3f}  [{summary}]")

    passed = agg["passed"]
    print(f"THRESHOLD {args.threshold:.2f}  ->  {'PASS' if passed else 'FAIL'}")

    if args.report:
        Path(args.report).parent.mkdir(parents=True, exist_ok=True)
        Path(args.report).write_text(json.dumps({
            "corpus": args.corpus,
            "executor_model": s.executor_model,
            "reviewer_model": s.reviewer_model,
            "base_url": s.llm_base_url,
            "mean_fix_rate": mean,
            "threshold": args.threshold,
            "passed": passed,
            "counts": counts,
            "results": results,
        }, indent=2))
        print(f"report -> {args.report}")

    return 0 if passed else 1


def main() -> None:
    ap = argparse.ArgumentParser(description="Aegis crash-to-fix evaluation harness")
    ap.add_argument("--corpus", default="eval/corpus.json", help="path to the corpus JSON array")
    ap.add_argument("--threshold", type=float, default=0.6, help="min mean fix-rate to PASS (CI gate)")
    ap.add_argument("--concurrency", type=int, default=1, help="parallel cases (keep low for one local GPU)")
    ap.add_argument("--limit", type=int, default=0, help="run only the first N cases (0 = all)")
    ap.add_argument("--report", default="eval/report.json", help="write a JSON report here ('' to skip)")
    args = ap.parse_args()
    sys.exit(asyncio.run(main_async(args)))


if __name__ == "__main__":
    main()
