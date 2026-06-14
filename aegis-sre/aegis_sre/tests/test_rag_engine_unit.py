"""Coverage: AST splitter + RAGEngine guard/fingerprint paths (no embeddings)."""

from aegis_sre.orchestrator.rag_engine import ASTCodeSplitter, RAGEngine


def test_ast_splitter_splits_funcs_classes_and_module_level():
    src = (
        "import os\n"
        "X = 1\n"
        "def foo():\n    return 1\n\n"
        "class Bar:\n    def m(self):\n        return 2\n"
    )
    docs = ASTCodeSplitter().split_file("f.py", src)
    types = {d.metadata["type"] for d in docs}
    names = {d.metadata.get("name") for d in docs}
    assert "FunctionDef" in types and "ClassDef" in types and "ModuleLevel" in types
    assert "foo" in names and "Bar" in names


def test_ast_splitter_syntax_error_falls_back_to_raw():
    docs = ASTCodeSplitter().split_file("bad.py", "def broken(:\n")
    assert len(docs) == 1 and docs[0].metadata["type"] == "raw"


def test_query_methods_return_empty_without_index():
    eng = RAGEngine.__new__(RAGEngine)        # skip heavy __init__ (chroma/ollama)
    eng.code_index = None
    eng.skills_index = None
    assert eng.query_codebase("anything") == ""
    assert eng.query_skills("anything") == ""


def test_ingest_is_noop_without_stores():
    eng = RAGEngine.__new__(RAGEngine)
    eng.code_store = None
    eng.skills_store = None
    eng.code_index = eng.skills_index = None
    eng.ingest_workspace()                     # returns early, no exception
    eng.ingest_skills([{"issue_type": "x", "resolution": "y"}])


def test_workspace_fingerprint_is_stable_hex(tmp_path):
    (tmp_path / "a.py").write_text("print('hi')\n")
    eng = RAGEngine.__new__(RAGEngine)
    eng.workspace_path = str(tmp_path)
    fp1 = eng._workspace_fingerprint()
    fp2 = eng._workspace_fingerprint()
    assert fp1 == fp2 and len(fp1) == 64        # sha256 hexdigest, deterministic
