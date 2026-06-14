"""
Tests for the Stone-1 Remediation hierarchy (B2).

CodePatch is the former PatchProposal expressed as a Remediation subclass, and
PatchProposal remains a back-compat alias so every existing call site is
unchanged. (The broad "no regression" guarantee is the rest of the suite.)
"""

import pytest

from aegis_sre.orchestrator.schemas import (
    CodePatch,
    PatchProposal,
    Remediation,
    RemediationKind,
)


def test_patchproposal_is_codepatch_alias():
    assert PatchProposal is CodePatch


def test_codepatch_is_a_remediation():
    p = CodePatch(file_path="a.py", target_content="x", replacement_content="y",
                  root_cause_analysis="rc", explanation="why")
    assert isinstance(p, Remediation)
    assert p.kind is RemediationKind.CODE_PATCH


def test_legacy_construction_still_works_with_all_fields():
    # The exact shape existing code (and the LLM executor) constructs.
    p = PatchProposal(file_path="f.py", target_content="a", replacement_content="b",
                      root_cause_analysis="rc", explanation="fix")
    assert p.file_path == "f.py"
    assert p.replacement_content == "b"
    assert p.kind is RemediationKind.CODE_PATCH  # defaulted, not required from caller


def test_file_path_traversal_validator_preserved():
    with pytest.raises(ValueError, match="Path traversal"):
        CodePatch(file_path="../escape.py", target_content="x", replacement_content="y",
                  root_cause_analysis="rc", explanation="why")


def test_remediation_base_requires_diagnosis_fields():
    with pytest.raises(Exception):
        CodePatch(file_path="a.py", target_content="x", replacement_content="y")  # no rca/explanation
