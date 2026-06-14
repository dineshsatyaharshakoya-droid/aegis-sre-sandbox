"""
Tests for api_receiver orchestration glue: the _alert lifecycle helper, the
trigger_repair_loop processor, and the read endpoints. The LLM graph, alert
notifier, and WS manager are all mocked so nothing external is touched.
"""

import asyncio

import pytest
from fastapi.testclient import TestClient

import aegis_sre.orchestrator.graph as graph_mod
import aegis_sre.orchestrator.incident_tools as inc_mod
from aegis_sre.telemetry import api_receiver as ar
from aegis_sre.orchestrator.schemas import CodePatch, SecurityReview, TelemetryEvent

client = TestClient(ar.app)
TELE = TelemetryEvent(event_id="e1", service_name="svc", crash_log="boom")


# --- read endpoints ---

def test_health_ready_metrics_incidents():
    assert client.get("/health").status_code == 200
    assert client.get("/ready").status_code == 200           # conftest wired incident_service
    assert client.get("/metrics").status_code == 200
    r = client.get("/incidents")
    assert r.status_code == 200 and "incidents" in r.json()


# --- _alert helper ---

def test_alert_noop_when_notifier_unconfigured(monkeypatch):
    monkeypatch.setattr(inc_mod, "get_incident_notifier", lambda: None)
    # Should simply return without error.
    asyncio.run(ar._alert("trigger", "i1", severity="critical", description="x"))


def test_alert_dispatches_to_notifier(monkeypatch):
    calls = []

    class FakeNotifier:
        async def trigger(self, **kw): calls.append(("trigger", kw))
        async def acknowledge(self, dedup_key, **kw): calls.append(("ack", dedup_key))
        async def resolve(self, dedup_key, **kw): calls.append(("resolve", dedup_key))

    monkeypatch.setattr(inc_mod, "get_incident_notifier", lambda: FakeNotifier())
    asyncio.run(ar._alert("trigger", "i1", severity="critical", description="d"))
    asyncio.run(ar._alert("acknowledge", "i1", note="n"))
    asyncio.run(ar._alert("resolve", "i1", note="done"))
    assert [c[0] for c in calls] == ["trigger", "ack", "resolve"]


def test_alert_swallows_notifier_errors(monkeypatch):
    class BadNotifier:
        async def trigger(self, **kw): raise RuntimeError("alert backend down")
    monkeypatch.setattr(inc_mod, "get_incident_notifier", lambda: BadNotifier())
    # Must not raise — alerting can never break the repair loop.
    asyncio.run(ar._alert("trigger", "i1", severity="critical", description="d"))


# --- trigger_repair_loop ---

class _FakeManager:
    def __init__(self): self.events = []
    async def broadcast(self, msg): self.events.append(msg)


class _FakeGraph:
    def __init__(self, updates): self._updates = updates
    async def astream(self, state, config=None):
        for u in self._updates:
            yield u


def _wire(monkeypatch, updates):
    mgr = _FakeManager()
    alerts = []
    monkeypatch.setattr(ar, "manager", mgr)
    async def fake_alert(action, dedup_key, **kw): alerts.append((action, dedup_key))
    monkeypatch.setattr(ar, "_alert", fake_alert)
    monkeypatch.setattr(graph_mod, "build_graph", lambda checkpointer=None: _FakeGraph(updates))
    return mgr, alerts


def test_repair_loop_patch_ready_registers_and_acknowledges(monkeypatch):
    patch = CodePatch(file_path="a.py", target_content="x", replacement_content="y",
                      root_cause_analysis="rc", explanation="why")
    mgr, alerts = _wire(monkeypatch, [{"executor": {"current_patch": patch, "sandbox_status": "success"}}])
    before = ar.approval_registry.pending_count()
    asyncio.run(ar.trigger_repair_loop(TELE))
    types = [e["type"] for e in mgr.events]
    assert "telemetry_received" in types and "patch_ready" in types
    assert ("trigger", "e1") in alerts and ("acknowledge", "e1") in alerts
    assert ar.approval_registry.pending_count() == before + 1


def test_repair_loop_no_patch_does_not_register(monkeypatch):
    mgr, alerts = _wire(monkeypatch, [{"executor": {"current_patch": None}}])
    before = ar.approval_registry.pending_count()
    asyncio.run(ar.trigger_repair_loop(TELE))
    assert ar.approval_registry.pending_count() == before
    assert ("acknowledge", "e1") not in alerts


def test_repair_loop_reraises_and_alerts_on_failure(monkeypatch):
    class BoomGraph:
        async def astream(self, state, config=None):
            raise RuntimeError("graph exploded")
            yield  # pragma: no cover
    mgr, alerts = _wire(monkeypatch, [])
    monkeypatch.setattr(graph_mod, "build_graph", lambda checkpointer=None: BoomGraph())
    with pytest.raises(RuntimeError, match="graph exploded"):
        asyncio.run(ar.trigger_repair_loop(TELE))
    assert any(e["type"] == "error" for e in mgr.events)
    assert ("trigger", "e1") in alerts  # escalated
