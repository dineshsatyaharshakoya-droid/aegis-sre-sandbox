"""
Tests for WS fan-out pub/sub (A10) + the worker streaming progress (audit #18).

The worker previously discarded all graph output (`pass`); it now publishes
node-update + patch_ready messages to a pub/sub the API fans out to WS clients.
"""

import asyncio

from aegis_sre.config import Settings
from aegis_sre.infra.pubsub import NoOpPubSub, RedisPubSub, build_pubsub
from aegis_sre.orchestrator.schemas import CodePatch, TelemetryEvent
import worker


def test_build_pubsub_selects_by_profile():
    onprem = Settings(profile="onprem", cache_backend="memory", broker_backend="inprocess",
                      store_backend="sqlite")
    cloud = Settings(profile="cloud")
    assert isinstance(build_pubsub(onprem), NoOpPubSub)
    assert isinstance(build_pubsub(cloud), RedisPubSub)


def test_noop_pubsub_publish_and_listen():
    async def go():
        ps = NoOpPubSub()
        await ps.publish({"x": 1})  # no error
        got = [m async for m in ps.listen()]  # empty
        await ps.close()
        return got
    assert asyncio.run(go()) == []


class _RecordingPubSub(NoOpPubSub):
    def __init__(self):
        self.published = []

    async def publish(self, message):
        self.published.append(message)


class _FakeGraph:
    def __init__(self, updates):
        self._updates = updates

    async def astream(self, state, config=None):
        for u in self._updates:
            yield u


def test_worker_processor_publishes_progress(monkeypatch):
    patch = CodePatch(file_path="a.py", target_content="x", replacement_content="y",
                      root_cause_analysis="rc", explanation="why")
    monkeypatch.setattr(worker, "build_graph",
                        lambda checkpointer=None: _FakeGraph(
                            [{"executor": {"current_patch": patch}}]),
                        raising=False)
    # build_graph is imported lazily inside make_processor; patch at its source.
    import aegis_sre.orchestrator.graph as graph_mod
    monkeypatch.setattr(graph_mod, "build_graph",
                        lambda checkpointer=None: _FakeGraph([{"executor": {"current_patch": patch}}]))

    ps = _RecordingPubSub()
    processor = worker.make_processor(checkpointer=None, pubsub=ps)
    asyncio.run(processor(TelemetryEvent(event_id="e1", service_name="svc", crash_log="boom")))

    types = [m["type"] for m in ps.published]
    assert "telemetry_received" in types
    assert "node_update" in types
    assert "patch_ready" in types
    pr = next(m for m in ps.published if m["type"] == "patch_ready")
    assert pr["file"] == "a.py" and pr["incident_id"] == "e1"
