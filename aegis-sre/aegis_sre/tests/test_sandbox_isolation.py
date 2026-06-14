"""Red-team Batch 5: sandbox-by-default selection + resource caps (S3)."""

import asyncio

import pytest

import aegis_sre.orchestrator.sandbox_engine as se
from aegis_sre.orchestrator.sandbox_engine import (
    ContainerEngine, E2BEngine, LocalProcessEngine, SandboxUnavailableError,
    _rlimit_preexec, get_sandbox_engine)
from aegis_sre.orchestrator.schemas import CodePatch
from aegis_sre.orchestrator.validator import Validator


def _patch(target="    return 1", replacement="    return 2", path="f.py"):
    return CodePatch(file_path=path, target_content=target, replacement_content=replacement,
                     root_cause_analysis="rc", explanation="why")


# --- selection: isolated-by-default ---

def _clear(monkeypatch):
    for k in ("SANDBOX_PROVIDER", "AEGIS_REQUIRE_SANDBOX", "E2B_API_KEY"):
        monkeypatch.delenv(k, raising=False)


def test_explicit_providers_honoured(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("SANDBOX_PROVIDER", "e2b")
    assert isinstance(get_sandbox_engine(), E2BEngine)
    monkeypatch.setenv("SANDBOX_PROVIDER", "container")
    assert isinstance(get_sandbox_engine(), ContainerEngine)
    monkeypatch.setenv("SANDBOX_PROVIDER", "local")
    assert isinstance(get_sandbox_engine(), LocalProcessEngine)


def test_prefers_e2b_then_container_then_local(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("E2B_API_KEY", "k")
    assert isinstance(get_sandbox_engine(), E2BEngine)
    monkeypatch.delenv("E2B_API_KEY")
    monkeypatch.setattr(ContainerEngine, "available", staticmethod(lambda: True))
    assert isinstance(get_sandbox_engine(), ContainerEngine)
    monkeypatch.setattr(ContainerEngine, "available", staticmethod(lambda: False))
    assert isinstance(get_sandbox_engine(), LocalProcessEngine)


def test_require_sandbox_fails_closed_without_isolation(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("AEGIS_REQUIRE_SANDBOX", "true")
    monkeypatch.setattr(ContainerEngine, "available", staticmethod(lambda: False))
    with pytest.raises(SandboxUnavailableError):
        get_sandbox_engine()
    # explicit local is also refused under require
    monkeypatch.setenv("SANDBOX_PROVIDER", "local")
    with pytest.raises(SandboxUnavailableError):
        get_sandbox_engine()


def test_validator_fails_closed_when_sandbox_unavailable(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("AEGIS_REQUIRE_SANDBOX", "true")
    monkeypatch.setattr(ContainerEngine, "available", staticmethod(lambda: False))
    res = asyncio.run(Validator().validate(_patch(), original_source="def f():\n    return 1\n"))
    assert res.success is False and "REQUIRE_SANDBOX" in res.output


# --- container engine fails closed without docker ---

def test_container_engine_fails_closed_without_docker(monkeypatch):
    monkeypatch.setattr(ContainerEngine, "available", staticmethod(lambda: False))
    ok, out = asyncio.run(ContainerEngine().compile_and_test(
        _patch(), original_source="def f():\n    return 1\n"))
    assert ok is False and "Docker is not available" in out


def test_docker_argv_is_locked_down():
    argv = ContainerEngine()._docker_argv("/tmp/wd", "python:3.12-slim", ["python3", "x.py"])
    joined = " ".join(argv)
    assert "--network none" in joined          # no egress for executed patch code
    assert "--cap-drop ALL" in joined
    assert "no-new-privileges" in joined
    assert "--pids-limit" in joined and "--memory" in joined and "--cpus" in joined


# --- rlimit preexec is constructed (and harmless to valid compiles) ---

def test_rlimit_preexec_present_and_local_still_compiles():
    fn = _rlimit_preexec()
    assert fn is None or callable(fn)
    ok, _ = asyncio.run(LocalProcessEngine().compile_and_test(
        _patch("    return 1", "    return 2"), original_source="def f():\n    return 1\n"))
    assert ok is True
