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


# --- ContainerEngine orchestration (docker run mocked) ---

def _container(monkeypatch, runner):
    eng = ContainerEngine()
    monkeypatch.setattr(ContainerEngine, "available", staticmethod(lambda: True))
    async def fake_run(workdir, image, inner, timeout):
        return runner(inner)
    monkeypatch.setattr(eng, "_docker_run", fake_run)
    return eng


def test_container_compile_success_no_repro(monkeypatch):
    eng = _container(monkeypatch, lambda inner: (0, "ok"))
    ok, out = asyncio.run(eng.compile_and_test(_patch(), original_source="def f():\n    return 1\n"))
    assert ok is True and "compiled in container" in out


def test_container_compile_failure(monkeypatch):
    eng = _container(monkeypatch, lambda inner: (1, "SyntaxError"))
    ok, out = asyncio.run(eng.compile_and_test(_patch(), original_source="def f():\n    return 1\n"))
    assert ok is False and "SyntaxError" in out


def test_container_repro_pass_and_fail(monkeypatch):
    # compile (first call) ok; repro (second call) decides
    seq = {"n": 0}
    def runner(inner):
        seq["n"] += 1
        return (0, "ok") if seq["n"] == 1 else (0, "repro-ok")
    eng = _container(monkeypatch, runner)
    ok, _ = asyncio.run(eng.compile_and_test(
        _patch(), original_source="def f():\n    return 1\n", repro_command="pytest"))
    assert ok is True

    seq2 = {"n": 0}
    def runner_fail(inner):
        seq2["n"] += 1
        return (0, "ok") if seq2["n"] == 1 else (1, "assert failed")
    eng2 = _container(monkeypatch, runner_fail)
    ok2, out2 = asyncio.run(eng2.compile_and_test(
        _patch(), original_source="def f():\n    return 1\n", repro_command="pytest"))
    assert ok2 is False and "Reproduction failed" in out2


def test_container_patch_does_not_apply(monkeypatch):
    eng = _container(monkeypatch, lambda inner: (0, "ok"))
    ok, out = asyncio.run(eng.compile_and_test(
        _patch(target="nonexistent"), original_source="def f():\n    return 1\n"))
    assert ok is False and "does not apply" in out


def test_container_no_compiler_fails_closed(monkeypatch):
    eng = _container(monkeypatch, lambda inner: (0, "ok"))
    ok, out = asyncio.run(eng.compile_and_test(
        _patch(path="data.txt"), original_source=None))   # new-file: applies, no compiler
    assert ok is False and "No compiler" in out


# --- E2BEngine paths (no real E2B; SDK mocked) ---

def test_e2b_fails_closed_without_key(monkeypatch):
    monkeypatch.delenv("E2B_API_KEY", raising=False)
    ok, out = asyncio.run(E2BEngine().compile_and_test(
        _patch(), original_source="def f():\n    return 1\n"))
    assert ok is False and "E2B_API_KEY" in out


def test_e2b_patch_does_not_apply(monkeypatch):
    monkeypatch.setenv("E2B_API_KEY", "k")
    ok, out = asyncio.run(E2BEngine().compile_and_test(
        _patch(target="nope"), original_source="def f():\n    return 1\n"))
    assert ok is False and "does not apply" in out


def _install_fake_e2b(monkeypatch, exit_code=0, repro_code=0):
    import sys, types
    class _Proc:
        def __init__(self, code): self.exit_code = code; self.stderr = "err"
        def wait(self): pass
    class _Sandbox:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        filesystem = property(lambda self: self)
        def write(self, *a, **k): pass
        class _P:
            pass
        @property
        def process(self):
            outer = self
            class _PR:
                def start(self_inner, cmd):
                    # first call = compile, contains py_compile; else repro
                    return _Proc(exit_code if "py_compile" in cmd or "--check" in cmd or "gofmt" in cmd else repro_code)
            return _PR()
    mod = types.ModuleType("e2b")
    mod.Sandbox = _Sandbox
    monkeypatch.setitem(sys.modules, "e2b", mod)


def test_e2b_compile_success_no_repro(monkeypatch):
    monkeypatch.setenv("E2B_API_KEY", "k")
    _install_fake_e2b(monkeypatch, exit_code=0)
    ok, out = asyncio.run(E2BEngine().compile_and_test(
        _patch(), original_source="def f():\n    return 1\n"))
    assert ok is True and "E2B" in out


def test_e2b_compile_failure(monkeypatch):
    monkeypatch.setenv("E2B_API_KEY", "k")
    _install_fake_e2b(monkeypatch, exit_code=1)
    ok, out = asyncio.run(E2BEngine().compile_and_test(
        _patch(), original_source="def f():\n    return 1\n"))
    assert ok is False
