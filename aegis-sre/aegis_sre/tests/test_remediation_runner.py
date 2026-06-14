"""
Tests for the execute -> verify -> rollback spine (D5).

Uses a registry with recording act-tools + a fake verifier so the full loop is
deterministic: a passing verification leaves no rollback; a failing one triggers
the compensating steps; non-live executions never verify or roll back.
"""

import asyncio

from aegis_sre.integrations.tool_registry import RiskClass, ToolRegistry
from aegis_sre.orchestrator.action_executor import ActionExecutor
from aegis_sre.orchestrator.policy import Policy
from aegis_sre.orchestrator.remediation_runner import RemediationRunner
from aegis_sre.orchestrator.schemas import ActionPlan, ActionStep, BlastRadius
from aegis_sre.orchestrator.verifier import VerificationCheck, VerificationResult, Comparator


def _registry(log):
    reg = ToolRegistry()

    async def act(**kw):
        log.append(kw.get("_name", "act"))
        return "ok"

    async def cordon(**kw):
        log.append("cordon")
        return "ok"

    async def uncordon(**kw):
        log.append("uncordon")
        return "ok"

    reg.register("k8s.cordon_node", RiskClass.ACT, "cordon", handler=cordon)
    reg.register("k8s.uncordon_node", RiskClass.ACT, "uncordon", handler=uncordon)
    return reg


def _plan(dry_run=False):
    return ActionPlan(
        steps=[ActionStep(tool="k8s.cordon_node", args={"node": "n1"})],
        rollback_steps=[ActionStep(tool="k8s.uncordon_node", args={"node": "n1"})],
        blast_radius=BlastRadius.LOW, dry_run=dry_run,
        root_cause_analysis="rc", explanation="why")


class _FakeVerifier:
    def __init__(self, verified):
        self._verified = verified

    async def verify(self, check):
        return VerificationResult(self._verified, 0.0 if self._verified else 1.0, check.query, "fake")


def _runner(log, verified):
    ex = ActionExecutor(registry=_registry(log),
                        policy=Policy(max_blast_radius=BlastRadius.HIGH, dry_run_default=False))
    return RemediationRunner(executor=ex, verifier=_FakeVerifier(verified))


CHECK = VerificationCheck(query="up", comparator=Comparator.GTE, threshold=1.0)


def test_verified_recovery_no_rollback():
    log = []
    out = asyncio.run(_runner(log, verified=True).run(_plan(), approved=True, verification=CHECK))
    assert out.resolved is True and out.rolled_back is False
    assert log == ["cordon"]  # action ran, no rollback


def test_failed_verification_triggers_rollback():
    log = []
    out = asyncio.run(_runner(log, verified=False).run(_plan(), approved=True, verification=CHECK))
    assert out.resolved is False and out.rolled_back is True
    assert log == ["cordon", "uncordon"]  # action then compensation
    assert out.rollback.mode == "rollback"


def test_dry_run_execution_never_verifies_or_rolls_back():
    log = []
    # not armed -> dry_run -> no live action, no verify, no rollback
    out = asyncio.run(_runner(log, verified=False).run(_plan(dry_run=True), approved=True, verification=CHECK))
    assert out.executed.mode == "dry_run"
    assert out.verification is None and out.rolled_back is False
    assert log == []


def test_live_without_check_is_resolved_but_unverified():
    log = []
    out = asyncio.run(_runner(log, verified=True).run(_plan(), approved=True, verification=None))
    assert out.resolved is True and out.verification is None
    assert log == ["cordon"]


def test_blocked_plan_does_not_verify():
    log = []
    ex = ActionExecutor(registry=_registry(log),
                        policy=Policy(max_blast_radius=BlastRadius.LOW, dry_run_default=False))
    runner = RemediationRunner(executor=ex, verifier=_FakeVerifier(True))
    plan = ActionPlan(steps=[ActionStep(tool="k8s.cordon_node")],
                      rollback_steps=[], blast_radius=BlastRadius.HIGH, dry_run=False,
                      root_cause_analysis="rc", explanation="why")  # HIGH > LOW cap
    out = asyncio.run(runner.run(plan, approved=True, verification=CHECK))
    assert out.executed.mode == "blocked"
    assert out.resolved is False and log == []
