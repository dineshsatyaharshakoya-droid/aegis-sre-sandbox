"""
Tests for the Validator-wired sandbox_node (B5).

test_graph.py mocks sandbox_node, so these drive the *real* node to prove the
type-agnostic gate: a CodePatch flows through the actual sandbox engine
(py_compile), and an ActionPlan dry-runs without any source fetch or execution.
"""

import asyncio

import aegis_sre.orchestrator.graph as graph
from aegis_sre.orchestrator.schemas import ActionPlan, ActionStep, BlastRadius, CodePatch


class _FakeVCS:
    def __init__(self, source):
        self.source = source
        self.asked = []

    async def fetch_file_content(self, path):
        self.asked.append(path)
        return self.source


def test_sandbox_node_codepatch_compiles_to_success(monkeypatch):
    # original has the target once; patched result is valid python -> compiles.
    vcs = _FakeVCS("def f():\n    return 1/0\n")
    monkeypatch.setattr(graph, "get_vcs_provider", lambda: vcs)

    patch = CodePatch(
        file_path="f.py",
        target_content="    return 1/0",
        replacement_content="    return 1",
        root_cause_analysis="division by zero",
        explanation="return a constant",
    )
    out = asyncio.run(graph.sandbox_node({"current_patch": patch}))
    assert out["sandbox_status"] == "success"
    assert vcs.asked == ["f.py"]  # code patch fetched the real source


def test_sandbox_node_codepatch_broken_python_fails(monkeypatch):
    vcs = _FakeVCS("def f():\n    return 1\n")
    monkeypatch.setattr(graph, "get_vcs_provider", lambda: vcs)
    patch = CodePatch(
        file_path="f.py",
        target_content="    return 1",
        replacement_content="    return (1",  # syntax error
        root_cause_analysis="rc", explanation="why",
    )
    out = asyncio.run(graph.sandbox_node({"current_patch": patch}))
    assert out["sandbox_status"] == "failed"


def test_sandbox_node_actionplan_dry_runs_without_vcs(monkeypatch):
    # If the node tried to fetch source for an ActionPlan, this would record a call.
    vcs = _FakeVCS("unused")
    monkeypatch.setattr(graph, "get_vcs_provider", lambda: vcs)
    plan = ActionPlan(
        steps=[ActionStep(tool="k8s.cordon_node", args={"node": "n1"})],
        blast_radius=BlastRadius.LOW,
        root_cause_analysis="rc", explanation="why",
    )
    out = asyncio.run(graph.sandbox_node({"current_patch": plan}))
    assert out["sandbox_status"] == "success"
    assert vcs.asked == []  # action plan never fetches source


def test_sandbox_node_no_remediation_fails():
    out = asyncio.run(graph.sandbox_node({"current_patch": None}))
    assert out["sandbox_status"] == "failed"
