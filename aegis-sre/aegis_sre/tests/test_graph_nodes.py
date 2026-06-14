"""
Unit tests for the individual graph nodes (executor / reviewer / researcher /
planner) with the LLM and I/O mocked — the node bodies were previously only run
through the full mocked graph.
"""

import asyncio
import json

import aegis_sre.orchestrator.graph as graph
from aegis_sre.orchestrator.schemas import SecurityReview, TelemetryEvent

TELE = TelemetryEvent(event_id="e1", service_name="svc",
                      crash_log='Traceback:\n  File "main.py", line 10, in f\nValueError')


def _chat(returns):
    async def _f(model, system, user):
        return returns
    return _f


# --- executor_node ---

def test_executor_success_builds_patch(monkeypatch):
    payload = json.dumps({"file_path": "main.py", "target_content": "a",
                          "replacement_content": "b", "root_cause_analysis": "rc",
                          "explanation": "fix"})
    monkeypatch.setattr(graph, "chat_json", _chat(payload))
    out = asyncio.run(graph.executor_node({"telemetry": TELE, "iteration_count": 0}))
    assert out["current_patch"].file_path == "main.py"
    assert out["iteration_count"] == 1


def test_executor_invalid_json_yields_no_patch(monkeypatch):
    monkeypatch.setattr(graph, "chat_json", _chat("not json"))
    out = asyncio.run(graph.executor_node({"telemetry": TELE, "iteration_count": 0}))
    assert out["current_patch"] is None


def test_executor_schema_violation_yields_no_patch(monkeypatch):
    monkeypatch.setattr(graph, "chat_json", _chat(json.dumps({"file_path": "x"})))
    out = asyncio.run(graph.executor_node({"telemetry": TELE, "iteration_count": 0}))
    assert out["current_patch"] is None


def test_executor_infra_error_fails_closed_without_mock_flag(monkeypatch):
    async def boom(*a):
        raise RuntimeError("network down")
    monkeypatch.setattr(graph, "chat_json", boom)
    monkeypatch.delenv("AEGIS_ALLOW_MOCK_PATCH", raising=False)
    out = asyncio.run(graph.executor_node({"telemetry": TELE, "iteration_count": 0}))
    assert out["current_patch"] is None  # no fabricated patch


def test_executor_mock_patch_only_in_dev(monkeypatch):
    async def boom(*a):
        raise RuntimeError("network down")
    monkeypatch.setattr(graph, "chat_json", boom)
    monkeypatch.setenv("AEGIS_ALLOW_MOCK_PATCH", "true")
    out = asyncio.run(graph.executor_node({"telemetry": TELE, "iteration_count": 0}))
    assert out["current_patch"] is not None and out["current_patch"].file_path == "main.py"


# --- reviewer_node ---

def test_reviewer_success(monkeypatch):
    from aegis_sre.orchestrator.schemas import CodePatch
    monkeypatch.setattr(graph, "chat_json",
                        _chat(json.dumps({"is_safe": True, "vulnerability_found": False,
                                          "feedback": "ok"})))
    state = {"telemetry": TELE, "current_patch": CodePatch(
        file_path="a.py", target_content="x", replacement_content="y",
        root_cause_analysis="rc", explanation="e")}
    out = asyncio.run(graph.reviewer_node(state))
    assert out["review"].is_safe is True


def test_reviewer_infra_error_fails_closed(monkeypatch):
    from aegis_sre.orchestrator.schemas import CodePatch
    async def boom(*a):
        raise RuntimeError("reviewer down")
    monkeypatch.setattr(graph, "chat_json", boom)
    state = {"telemetry": TELE, "current_patch": CodePatch(
        file_path="a.py", target_content="x", replacement_content="y",
        root_cause_analysis="rc", explanation="e")}
    out = asyncio.run(graph.reviewer_node(state))
    assert out["review"].is_safe is False  # fail closed


def test_reviewer_no_patch_returns_state(monkeypatch):
    out = asyncio.run(graph.reviewer_node({"telemetry": TELE, "current_patch": None}))
    assert "review" not in out or out.get("review") is None


# --- researcher_node ---

def test_researcher_gathers_vcs_context(monkeypatch):
    class FakeVCS:
        async def fetch_file_content(self, path):
            return "\n".join(f"line{i}" for i in range(30))
    class FakeRAG:
        def query_skills(self, search_term, top_k): return "SKILL: restart"
        def query_codebase(self, search_term, top_k): return "CODE: def f()"
    monkeypatch.setattr(graph, "get_vcs_provider", lambda: FakeVCS())
    monkeypatch.setattr(graph, "get_rag_engine", lambda: FakeRAG())
    async def no_metrics(_): return None
    monkeypatch.setattr(graph, "_gather_live_metrics", no_metrics)
    out = asyncio.run(graph.researcher_node({"telemetry": TELE}))
    assert "main.py" in out["code_context"]
    assert "SKILL: restart" in out["code_context"]


def test_researcher_falls_back_to_mock_when_no_files(monkeypatch):
    class FakeVCS:
        async def fetch_file_content(self, path): return None
    monkeypatch.setattr(graph, "get_vcs_provider", lambda: FakeVCS())
    monkeypatch.setattr(graph, "get_rag_engine", lambda: (_ for _ in ()).throw(RuntimeError("rag off")))
    async def no_metrics(_): return None
    monkeypatch.setattr(graph, "_gather_live_metrics", no_metrics)
    out = asyncio.run(graph.researcher_node(
        {"telemetry": TelemetryEvent(event_id="e", service_name="s", crash_log="no files here")}))
    assert "Mock Context" in out["code_context"]


def test_planner_node_passthrough():
    state = {"telemetry": TELE}
    assert graph.planner_node(state) is state
