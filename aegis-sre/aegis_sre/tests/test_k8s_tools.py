"""Coverage: k8s ACT tools build the right kubectl argv; _kubectl success/failure."""

import asyncio

import pytest

import aegis_sre.orchestrator.k8s_tools as k8s


def _capture(monkeypatch):
    calls = []
    async def fake_kubectl(*args):
        calls.append(args)
        return "ok"
    monkeypatch.setattr(k8s, "_kubectl", fake_kubectl)
    return calls


def test_each_act_tool_builds_expected_argv(monkeypatch):
    calls = _capture(monkeypatch)
    asyncio.run(k8s.cordon_node("n1"))
    asyncio.run(k8s.uncordon_node("n1"))
    asyncio.run(k8s.drain_node("n1"))
    asyncio.run(k8s.scale_deployment("api", 3, namespace="prod"))
    asyncio.run(k8s.restart_deployment("api", namespace="prod"))
    asyncio.run(k8s.delete_pod("api-xyz", namespace="prod"))
    assert calls[0] == ("cordon", "n1")
    assert calls[1] == ("uncordon", "n1")
    assert calls[2] == ("drain", "n1", "--ignore-daemonsets", "--delete-emptydir-data", "--force")
    assert calls[3] == ("scale", "deployment/api", "--replicas=3", "-n", "prod")
    assert calls[4] == ("rollout", "restart", "deployment/api", "-n", "prod")
    assert calls[5] == ("delete", "pod", "api-xyz", "-n", "prod")


def test_registry_maps_names_to_handlers():
    assert k8s.K8S_ACT_TOOLS["k8s.cordon_node"] is k8s.cordon_node
    assert set(k8s.K8S_ACT_TOOLS) == {
        "k8s.cordon_node", "k8s.uncordon_node", "k8s.drain_node",
        "k8s.scale_deployment", "k8s.restart_deployment", "k8s.delete_pod"}


class _FakeProc:
    def __init__(self, rc, out=b"", err=b""):
        self.returncode, self._out, self._err = rc, out, err
    async def communicate(self):
        return self._out, self._err


def test_kubectl_success_returns_stdout(monkeypatch):
    async def fake_exec(*a, **k):
        return _FakeProc(0, out=b"node/n1 cordoned\n")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    out = asyncio.run(k8s._kubectl("cordon", "n1"))
    assert "cordoned" in out


def test_kubectl_failure_raises_with_stderr(monkeypatch):
    async def fake_exec(*a, **k):
        return _FakeProc(1, err=b"Error from server (NotFound)")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    with pytest.raises(RuntimeError, match="NotFound"):
        asyncio.run(k8s._kubectl("cordon", "ghost"))
