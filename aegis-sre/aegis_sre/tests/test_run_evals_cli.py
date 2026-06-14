"""Tests for run_evals.main_async (the CLI/report path) with the LLM mocked."""

import argparse
import asyncio
import json

import run_evals


def _args(corpus, report, threshold=0.6):
    return argparse.Namespace(corpus=corpus, threshold=threshold, concurrency=1,
                              limit=0, report=report)


def _write_corpus(path):
    path.write_text(json.dumps([
        {"id": "c1", "description": "d", "log_snippet": "boom",
         "ground_truth_diff": "diff --git a b\n@@ -1 +1 @@"},
    ]))


def test_main_async_passes_and_writes_report(tmp_path, monkeypatch):
    corpus = tmp_path / "corpus.json"
    report = tmp_path / "report.json"
    _write_corpus(corpus)

    async def chat(model, system, user):
        if "candidate_diff" in system or "git diff" in system.lower():
            return json.dumps({"candidate_diff": "D", "root_cause": "RC"})
        return json.dumps({"verdict": "correct", "score": 1.0,
                           "addresses_root_cause": True, "reasoning": "ok"})
    monkeypatch.setattr(run_evals, "chat_json", chat)

    rc = asyncio.run(run_evals.main_async(_args(str(corpus), str(report), threshold=0.6)))
    assert rc == 0  # mean fix-rate 1.0 >= 0.6 -> pass
    written = json.loads(report.read_text())
    assert written["passed"] is True and written["mean_fix_rate"] == 1.0


def test_main_async_fails_below_threshold(tmp_path, monkeypatch):
    corpus = tmp_path / "corpus.json"
    _write_corpus(corpus)

    async def chat(model, system, user):
        if "candidate_diff" in system or "git diff" in system.lower():
            return json.dumps({"candidate_diff": "", "root_cause": ""})
        return json.dumps({"verdict": "incorrect", "score": 0.0,
                           "addresses_root_cause": False, "reasoning": "no"})
    monkeypatch.setattr(run_evals, "chat_json", chat)

    rc = asyncio.run(run_evals.main_async(_args(str(corpus), "", threshold=0.6)))
    assert rc == 1  # below threshold -> non-zero exit (CI gate)
