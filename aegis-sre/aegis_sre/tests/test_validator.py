"""
Tests for the remediation Validator (B4).

Both dispatch branches are covered without a real compiler or any side effects:
the CodePatch branch delegates to an injected fake sandbox engine; the ActionPlan
branch must render a dry-run and never execute.
"""

import asyncio

from aegis_sre.orchestrator.schemas import (
    ActionPlan,
    ActionStep,
    BlastRadius,
    CodePatch,
    Remediation,
    RemediationKind,
)
from aegis_sre.orchestrator.validator import Validator


class _FakeEngine:
    def __init__(self, result):
        self.result = result
        self.called_with = None

    async def compile_and_test(self, patch, original_source=None, repro_command=None):
        self.called_with = {"patch": patch, "original_source": original_source,
                            "repro_command": repro_command}
        return self.result


def _patch():
    return CodePatch(file_path="a.py", target_content="x", replacement_content="y",
                     root_cause_analysis="rc", explanation="why")


def test_codepatch_branch_delegates_to_sandbox():
    engine = _FakeEngine((True, "compiled ok"))
    v = Validator(sandbox_engine=engine)
    res = asyncio.run(v.validate(_patch(), original_source="def f(): pass", repro_command="pytest"))
    assert res.success is True and res.kind == "code_patch"
    assert res.output == "compiled ok"
    assert engine.called_with["original_source"] == "def f(): pass"
    assert engine.called_with["repro_command"] == "pytest"


def test_codepatch_failure_propagates():
    v = Validator(sandbox_engine=_FakeEngine((False, "SyntaxError")))
    res = asyncio.run(v.validate(_patch()))
    assert res.success is False and "SyntaxError" in res.output


def test_actionplan_branch_is_dry_run_and_does_not_execute():
    # If this engine were ever called, called_with would be set -> assert it isn't.
    engine = _FakeEngine((True, "should not run"))
    plan = ActionPlan(
        steps=[ActionStep(tool="k8s.cordon_node", args={"node": "gpu-7"}, description="cordon")],
        blast_radius=BlastRadius.MEDIUM, dry_run=True,
        root_cause_analysis="rc", explanation="why",
    )
    v = Validator(sandbox_engine=engine)
    res = asyncio.run(v.validate(plan))
    assert res.success is True and res.kind == "action_plan" and res.dry_run is True
    assert engine.called_with is None                       # never executed
    assert "k8s.cordon_node" in res.output
    assert "node='gpu-7'" in res.output
    assert "No actions executed" in res.output
    assert "blast_radius=medium" in res.output


def test_unknown_remediation_type_fails_closed():
    class WeirdRemediation(Remediation):
        kind: RemediationKind = RemediationKind.CODE_PATCH

    v = Validator(sandbox_engine=_FakeEngine((True, "x")))
    res = asyncio.run(v.validate(WeirdRemediation(root_cause_analysis="rc", explanation="e")))
    assert res.success is False and res.kind == "unknown"
