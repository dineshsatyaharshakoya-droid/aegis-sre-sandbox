"""
Tests for the eval harness (`run_evals.py`) — A3/A4.

Deterministic and LLM-free: they validate the shipped corpus, the corpus
schema-guard, and the scoring/aggregation math (the CI-gate logic). The actual
fix-rate run against the live models is a separate, slow integration step.
"""

from pathlib import Path

import pytest

from run_evals import REQUIRED_FIELDS, aggregate, load_corpus, validate_corpus

CORPUS = Path(__file__).resolve().parents[2] / "eval" / "corpus.json"


def test_shipped_corpus_loads_and_validates():
    cases = load_corpus(str(CORPUS))
    assert len(cases) >= 10
    ids = [c["id"] for c in cases]
    assert len(ids) == len(set(ids)), "case ids must be unique"
    for c in cases:
        assert REQUIRED_FIELDS == ("id", "description", "log_snippet", "ground_truth_diff")
        assert all(c.get(f) for f in REQUIRED_FIELDS)
        assert "diff --git" in c["ground_truth_diff"] and "@@" in c["ground_truth_diff"]


def test_validate_rejects_missing_field():
    with pytest.raises(ValueError, match="missing/empty"):
        validate_corpus([{"id": "x", "description": "d", "log_snippet": "l"}])  # no diff


def test_validate_rejects_duplicate_ids():
    case = {"id": "dup", "description": "d", "log_snippet": "l",
            "ground_truth_diff": "diff --git a b\n@@ -1 +1 @@"}
    with pytest.raises(ValueError, match="duplicate case id"):
        validate_corpus([case, dict(case)])


def test_validate_rejects_non_diff_ground_truth():
    with pytest.raises(ValueError, match="not a unified git diff"):
        validate_corpus([{"id": "x", "description": "d", "log_snippet": "l",
                          "ground_truth_diff": "just change the thing"}])


def test_validate_rejects_empty_corpus():
    with pytest.raises(ValueError, match="non-empty"):
        validate_corpus([])


def _results(*scores_verdicts):
    return [{"score": s, "verdict": v} for s, v in scores_verdicts]


def test_aggregate_mean_counts_and_pass():
    agg = aggregate(_results((1.0, "correct"), (1.0, "correct"), (0.5, "partial"), (0.0, "incorrect")),
                    threshold=0.6)
    assert agg["n"] == 4
    assert agg["mean"] == pytest.approx(0.625)
    assert agg["counts"] == {"correct": 2, "partial": 1, "incorrect": 1}
    assert agg["passed"] is True


def test_aggregate_fails_below_threshold():
    agg = aggregate(_results((0.0, "incorrect"), (0.5, "partial")), threshold=0.6)
    assert agg["mean"] == pytest.approx(0.25)
    assert agg["passed"] is False


def test_aggregate_empty_is_zero_not_crash():
    agg = aggregate([], threshold=0.6)
    assert agg["mean"] == 0.0 and agg["passed"] is False and agg["n"] == 0
