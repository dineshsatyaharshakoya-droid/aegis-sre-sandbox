"""
Stone 3 end-to-end sign-off (D7) — the sellable-product exit criterion.

Wires the REAL components together (alert adapter -> Signal -> approval registry
-> policy -> gated executor -> verifier -> rollback -> audit), mocking only the
two genuine external boundaries: the act-tool handlers (no live cluster) and the
Prometheus client (a controllable metric). Covers the SCALE_PLAN's Stone-3 exit:
alert -> diagnose -> approve -> execute -> verify -> rollback-on-forced-failure,
fully audited — plus the dry-run-by-default safety property.
"""

import asyncio

from aegis_sre.core.approvals import ApprovalRegistry
from aegis_sre.integrations.tool_registry import RiskClass, ToolRegistry
from aegis_sre.orchestrator.action_executor import ActionExecutor
from aegis_sre.orchestrator.metrics_tools import MetricSample
from aegis_sre.orchestrator.policy import Policy
from aegis_sre.orchestrator.remediation_runner import RemediationRunner
from aegis_sre.orchestrator.schemas import (
    ActionPlan, ActionStep, BlastRadius, Comparator, SignalKind, VerificationCheck,
)
from aegis_sre.orchestrator.verifier import Verifier
from aegis_sre.telemetry.alert_adapter import parse_alertmanager


class _FakeProm:
    """Prometheus stand-in returning a controllable metric value."""
    def __init__(self, value):
        self.value = value

    async def query(self, promql, **kwargs):
        return [MetricSample(metric={"__name__": "up"}, timestamp=1.0, value=self.value)]


def _registry(log):
    reg = ToolRegistry()

    async def cordon(**kw):
        log.append("cordon")
        return "cordoned"

    async def uncordon(**kw):
        log.append("uncordon")
        return "uncordoned"

    reg.register("k8s.cordon_node", RiskClass.ACT, "cordon node", handler=cordon)
    reg.register("k8s.uncordon_node", RiskClass.ACT, "uncordon node", handler=uncordon)
    return reg


def _diagnosed_plan():
    """What the swarm would emit for a NodeNotReady alert (synthetic here)."""
    return ActionPlan(
        steps=[ActionStep(tool="k8s.cordon_node", args={"node": "gpu-7"}, description="cordon faulty node")],
        rollback_steps=[ActionStep(tool="k8s.uncordon_node", args={"node": "gpu-7"})],
        blast_radius=BlastRadius.LOW, dry_run=False,
        verification=VerificationCheck(query="up{node='gpu-7'}", comparator=Comparator.GTE, threshold=1.0),
        root_cause_analysis="node gpu-7 NotReady", explanation="cordon and let the scheduler reschedule")


def _alert(fp="fp1"):
    return {"alerts": [{
        "status": "firing", "fingerprint": fp,
        "labels": {"alertname": "NodeNotReady", "service": "gpu-cluster", "severity": "critical"},
        "annotations": {"summary": "node gpu-7 NotReady"}}]}


def _live_executor(log):
    return ActionExecutor(registry=_registry(log),
                          policy=Policy(max_blast_radius=BlastRadius.HIGH, dry_run_default=False))


def test_e2e_alert_to_verified_remediation():
    log = []
    # 1. live alert -> Signal(metric_alert)
    [signal] = parse_alertmanager(_alert("fp1"))
    assert signal.kind is SignalKind.METRIC_ALERT

    # 2. diagnosis -> ActionPlan held for human approval
    reg = ApprovalRegistry()
    asyncio.run(reg.register(signal.signal_id, _diagnosed_plan(), signal.to_telemetry()))

    # 3. human approves -> gated live execute -> verify (healthy) -> resolved
    runner = RemediationRunner(executor=_live_executor(log), verifier=Verifier(client=_FakeProm(1.0)))
    res = asyncio.run(reg.approve(signal.signal_id, None, runner=runner))

    assert res["status"] == "executed" and res["mode"] == "live"
    assert res["resolved"] is True and res["rolled_back"] is False
    assert log == ["cordon"]                 # action ran, no rollback needed
    assert res["audit"]["steps"]             # fully audited


def test_e2e_forced_failure_triggers_rollback():
    log = []
    [signal] = parse_alertmanager(_alert("fp2"))
    reg = ApprovalRegistry()
    asyncio.run(reg.register(signal.signal_id, _diagnosed_plan(), signal.to_telemetry()))

    # Metric stays unhealthy (up=0) -> verification fails -> auto rollback.
    runner = RemediationRunner(executor=_live_executor(log), verifier=Verifier(client=_FakeProm(0.0)))
    res = asyncio.run(reg.approve(signal.signal_id, None, runner=runner))

    assert res["status"] == "rolled_back"
    assert res["resolved"] is False and res["rolled_back"] is True
    assert log == ["cordon", "uncordon"]     # action, then compensation
    assert res["audit"]["steps"]


def test_e2e_default_policy_is_dry_run_safe():
    # With the default (conservative) policy, approving an action plan must NOT
    # touch the cluster — it dry-runs. This is the headline safety guarantee.
    log = []
    [signal] = parse_alertmanager(_alert("fp3"))
    reg = ApprovalRegistry()
    asyncio.run(reg.register(signal.signal_id, _diagnosed_plan(), signal.to_telemetry()))

    runner = RemediationRunner(executor=ActionExecutor(registry=_registry(log)),  # default Policy()
                               verifier=Verifier(client=_FakeProm(1.0)))
    res = asyncio.run(reg.approve(signal.signal_id, None, runner=runner))

    assert res["mode"] == "dry_run"
    assert log == []                          # nothing executed live
    assert res["resolved"] is False


def test_e2e_idempotent_reapproval():
    log = []
    [signal] = parse_alertmanager(_alert("fp4"))
    reg = ApprovalRegistry()
    asyncio.run(reg.register(signal.signal_id, _diagnosed_plan(), signal.to_telemetry()))
    runner = RemediationRunner(executor=_live_executor(log), verifier=Verifier(client=_FakeProm(1.0)))
    asyncio.run(reg.approve(signal.signal_id, None, runner=runner))
    again = asyncio.run(reg.approve(signal.signal_id, None, runner=runner))
    assert again["status"] == "already_approved"
    assert log == ["cordon"]                  # not executed twice
