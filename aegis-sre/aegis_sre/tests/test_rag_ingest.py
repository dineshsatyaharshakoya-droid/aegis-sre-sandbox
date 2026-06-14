"""Tests for the A7 incremental-ingest fingerprint (no LlamaIndex/Chroma needed)."""

import time

from aegis_sre.orchestrator.rag_engine import RAGEngine


def _engine(path):
    # Bypass __init__ (chroma/ollama) — we only exercise the fingerprint logic.
    eng = RAGEngine.__new__(RAGEngine)
    eng.workspace_path = str(path)
    return eng


def test_fingerprint_stable_when_unchanged(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n")
    eng = _engine(tmp_path)
    assert eng._workspace_fingerprint() == eng._workspace_fingerprint()


def test_fingerprint_changes_on_new_file(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n")
    eng = _engine(tmp_path)
    fp1 = eng._workspace_fingerprint()
    (tmp_path / "b.py").write_text("y = 2\n")          # new file -> size set changes
    assert eng._workspace_fingerprint() != fp1


def test_fingerprint_changes_on_edit(tmp_path):
    f = tmp_path / "a.py"
    f.write_text("x = 1\n")
    eng = _engine(tmp_path)
    fp1 = eng._workspace_fingerprint()
    time.sleep(1.1)                                     # mtime is second-resolution
    f.write_text("x = 1\ny = 2\n")                      # size + mtime change
    assert eng._workspace_fingerprint() != fp1


def test_fingerprint_ignores_non_python_and_venv(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n")
    eng = _engine(tmp_path)
    fp1 = eng._workspace_fingerprint()
    (tmp_path / "notes.txt").write_text("hello")        # ignored
    (tmp_path / "venv").mkdir()
    (tmp_path / "venv" / "lib.py").write_text("z = 3")  # ignored (venv)
    assert eng._workspace_fingerprint() == fp1
