"""
Tests for the Stone-1 ActionPlan schema (B3).

ActionPlan is the non-code remediation type (gated infra actions, executed in
Stone-3). B3 is schema + validators only; the safety-relevant defaults
(dry_run=True, blast_radius=HIGH) are asserted here because later phases rely on
them being fail-safe.
"""

import pytest

from aegis_sre.orchestrator.schemas import (
    ActionPlan,
    ActionStep,
    BlastRadius,
    Remediation,
    RemediationKind,
)


def _step(tool="k8s.cordon_node"):
    return ActionStep(tool=tool, args={"node": "gpu-7"}, description="cordon faulty node")


def test_action_plan_is_a_remediation():
    plan = ActionPlan(steps=[_step()], root_cause_analysis="rc", explanation="why")
    assert isinstance(plan, Remediation)
    assert plan.kind is RemediationKind.ACTION_PLAN


def test_defaults_are_fail_safe():
    plan = ActionPlan(steps=[_step()], root_cause_analysis="rc", explanation="why")
    assert plan.dry_run is True                      # inert until armed
    assert plan.blast_radius is BlastRadius.HIGH     # assume worst until assessed


def test_steps_must_be_non_empty():
    with pytest.raises(ValueError, match="at least one step"):
        ActionPlan(steps=[], root_cause_analysis="rc", explanation="why")


def test_step_tool_must_be_non_empty():
    with pytest.raises(ValueError, match="non-empty tool name"):
        ActionStep(tool="   ", args={}, description="bad")


def test_step_args_default_empty():
    s = ActionStep(tool="job.requeue")
    assert s.args == {} and s.description == ""


def test_blast_radius_and_arming_are_explicit():
    plan = ActionPlan(steps=[_step()], blast_radius=BlastRadius.LOW, dry_run=False,
                      root_cause_analysis="rc", explanation="why")
    assert plan.blast_radius is BlastRadius.LOW
    assert plan.dry_run is False
    assert len(plan.steps) == 1 and plan.steps[0].tool == "k8s.cordon_node"
