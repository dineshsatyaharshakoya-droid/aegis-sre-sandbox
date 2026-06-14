"""Tests for the eval runner's generate/grade/run_case path (LLM mocked)."""

import asyncio
import json

import run_evals


def _chat(returns):
    async def _f(model, system, user):
        return returns
    return _f


CASE = {"id": "bug-x", "description": "d", "log_snippet": "boom",
        "ground_truth_diff": "diff --git a b\n@@ -1 +1 @@"}


def test_generate_fix_parses_diff(monkeypatch):
    monkeypatch.setattr(run_evals, "chat_json",
                        _chat(json.dumps({"candidate_diff": "DIFF", "root_cause": "RC"})))
    diff, rc = asyncio.run(run_evals.generate_fix(CASE))
    assert diff == "DIFF" and rc == "RC"


def test_grade_fix_parses_verdict(monkeypatch):
    monkeypatch.setattr(run_evals, "chat_json",
                        _chat(json.dumps({"verdict": "correct", "score": 1.0,
                                          "addresses_root_cause": True, "reasoning": "ok"})))
    verdict = asyncio.run(run_evals.grade_fix(CASE, "DIFF"))
    assert verdict["verdict"] == "correct" and verdict["score"] == 1.0


def test_run_case_happy_path(monkeypatch):
    calls = {"n": 0}
    async def chat(model, system, user):
        calls["n"] += 1
        if calls["n"] == 1:
            return json.dumps({"candidate_diff": "D", "root_cause": "RC"})
        return json.dumps({"verdict": "correct", "score": 1.0,
                           "addresses_root_cause": True, "reasoning": "ok"})
    monkeypatch.setattr(run_evals, "chat_json", chat)
    sem = asyncio.Semaphore(1)
    r = asyncio.run(run_evals.run_case(CASE, sem))
    assert r["id"] == "bug-x" and r["verdict"] == "correct" and r["score"] == 1.0
    assert r["error"] is None


def test_run_case_records_error_without_crashing(monkeypatch):
    async def boom(*a):
        raise RuntimeError("model down")
    monkeypatch.setattr(run_evals, "chat_json", boom)
    r = asyncio.run(run_evals.run_case(CASE, asyncio.Semaphore(1)))
    assert r["verdict"] == "error" and r["score"] == 0.0 and "model down" in r["error"]
