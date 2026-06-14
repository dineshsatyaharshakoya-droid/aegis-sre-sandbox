"""Tests for the local sandbox engine: apply -> compile -> (optional) repro."""

import asyncio

from aegis_sre.orchestrator.sandbox_engine import LocalProcessEngine
from aegis_sre.orchestrator.schemas import CodePatch


def _patch(target, replacement, path="f.py"):
    return CodePatch(file_path=path, target_content=target, replacement_content=replacement,
                     root_cause_analysis="rc", explanation="why")


def test_compiles_valid_python_patch():
    eng = LocalProcessEngine()
    ok, out = asyncio.run(eng.compile_and_test(
        _patch("    return 1/0", "    return 1"), original_source="def f():\n    return 1/0\n"))
    assert ok is True


def test_rejects_invalid_python_patch():
    eng = LocalProcessEngine()
    ok, out = asyncio.run(eng.compile_and_test(
        _patch("    return 1", "    return (1"), original_source="def f():\n    return 1\n"))
    assert ok is False


def test_new_file_creation_compiles():
    eng = LocalProcessEngine()
    ok, _ = asyncio.run(eng.compile_and_test(
        _patch("", "x = 1\n", path="new.py"), original_source=None))
    assert ok is True


def test_repro_command_pass_and_fail():
    eng = LocalProcessEngine()
    src = "def f():\n    return 1\n"
    ok_pass, _ = asyncio.run(eng.compile_and_test(
        _patch("    return 1", "    return 2"), original_source=src, repro_command="true"))
    ok_fail, _ = asyncio.run(eng.compile_and_test(
        _patch("    return 1", "    return 2"), original_source=src, repro_command="false"))
    assert ok_pass is True and ok_fail is False
