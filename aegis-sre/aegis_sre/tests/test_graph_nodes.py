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


def test_researcher_no_source_message_in_prod(monkeypatch):
    # Without the dev flag, the researcher must NOT inject fictional code (audit #10).
    monkeypatch.delenv("AEGIS_ALLOW_MOCK_PATCH", raising=False)
    class FakeVCS:
        async def fetch_file_content(self, path): return None
    monkeypatch.setattr(graph, "get_vcs_provider", lambda: FakeVCS())
    monkeypatch.setattr(graph, "get_rag_engine", lambda: (_ for _ in ()).throw(RuntimeError("rag off")))
    async def no_metrics(_): return None
    monkeypatch.setattr(graph, "_gather_live_metrics", no_metrics)
    out = asyncio.run(graph.researcher_node(
        {"telemetry": TelemetryEvent(event_id="e", service_name="s", crash_log="no files here")}))
    assert "No local source" in out["code_context"]
    assert "Mock Context" not in out["code_context"]


def test_researcher_mock_context_only_in_dev(monkeypatch):
    monkeypatch.setenv("AEGIS_ALLOW_MOCK_PATCH", "true")
    class FakeVCS:
        async def fetch_file_content(self, path): return None
    monkeypatch.setattr(graph, "get_vcs_provider", lambda: FakeVCS())
    monkeypatch.setattr(graph, "get_rag_engine", lambda: (_ for _ in ()).throw(RuntimeError("rag off")))
    async def no_metrics(_): return None
    monkeypatch.setattr(graph, "_gather_live_metrics", no_metrics)
    out = asyncio.run(graph.researcher_node(
        {"telemetry": TelemetryEvent(event_id="e", service_name="s", crash_log="no files")}))
    assert "Mock Context" in out["code_context"]


def test_planner_triages_crash_to_code_patch():
    assert graph.planner_node({"telemetry": TELE})["signal_kind"] == "crash"


def test_planner_triages_metric_alert():
    ev = TelemetryEvent(event_id="a", service_name="s", crash_log="alert",
                        metadata={"signal_kind": "metric_alert"})
    assert graph.planner_node({"telemetry": ev})["signal_kind"] == "metric_alert"


# --- audit #8: executor produces ActionPlan for non-crash signals ---

def test_executor_produces_action_plan_for_metric_alert(monkeypatch):
    import json as _json
    from aegis_sre.orchestrator.schemas import ActionPlan
    plan_json = _json.dumps({
        "steps": [{"tool": "k8s.cordon_node", "args": {"node": "n1"}, "description": "cordon"}],
        "rollback_steps": [{"tool": "k8s.uncordon_node", "args": {"node": "n1"}}],
        "blast_radius": "low",
        "verification": {"query": "up", "comparator": "gte", "threshold": 1.0},
        "root_cause_analysis": "node not ready", "explanation": "cordon it",
    })
    monkeypatch.setattr(graph, "chat_json", _chat(plan_json))
    alert = TelemetryEvent(event_id="a1", service_name="svc", crash_log="NodeNotReady",
                           metadata={"signal_kind": "metric_alert"})
    out = asyncio.run(graph.executor_node({"telemetry": alert, "iteration_count": 0, "signal_kind": "metric_alert"}))
    assert isinstance(out["current_patch"], ActionPlan)
    assert out["current_patch"].steps[0].tool == "k8s.cordon_node"
    assert out["current_patch"].dry_run is True  # safe by default


def test_executor_still_produces_code_patch_for_crash(monkeypatch):
    from aegis_sre.orchestrator.schemas import CodePatch
    payload = '{"file_path":"a.py","target_content":"x","replacement_content":"y","root_cause_analysis":"r","explanation":"e"}'
    monkeypatch.setattr(graph, "chat_json", _chat(payload))
    out = asyncio.run(graph.executor_node({"telemetry": TELE, "iteration_count": 0, "signal_kind": "crash"}))
    assert isinstance(out["current_patch"], CodePatch)


def test_deploy_node_marks_resolved():
    assert graph.deploy_node({"current_patch": None})["resolved"] is True


# --- RAG-5: error summary extraction for embedding queries ---

def test_error_summary_extracts_exception_line():
    log = 'Traceback (most recent call last):\n  File "x.py", line 3\nValueError: bad thing happened'
    assert graph._error_summary(log).startswith("ValueError: bad thing")


def test_error_summary_truncates_and_handles_empty():
    assert len(graph._error_summary("x" * 999, max_len=240)) == 240
    assert graph._error_summary("") == ""
