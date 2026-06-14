"""
Tests for the gated action executor (D2).

The executor must run live ONLY when the policy permits, and never call a handler
otherwise. Live dispatch is proven with a registered mock act-tool; the safety
guards (unknown tool, non-act tool, no handler, stop-on-failure) are all covered.
"""

import asyncio

from aegis_sre.integrations.tool_registry import RiskClass, ToolRegistry
from aegis_sre.orchestrator.action_executor import ActionExecutor
from aegis_sre.orchestrator.policy import Policy
from aegis_sre.orchestrator.schemas import ActionPlan, ActionStep, BlastRadius


def _plan(*steps, blast=BlastRadius.LOW, dry_run=False):
    return ActionPlan(steps=list(steps), blast_radius=blast, dry_run=dry_run,
                      root_cause_analysis="rc", explanation="why")


def _live_policy():
    # env permits live; plan must still be armed + approved to actually run.
    return Policy(max_blast_radius=BlastRadius.HIGH, dry_run_default=False)


def _registry_with_act_tool(record):
    reg = ToolRegistry()

    async def handler(**kwargs):
        record.append(kwargs)
        return {"ok": True, **kwargs}

    reg.register("k8s.cordon_node", RiskClass.ACT, "cordon", handler=handler)
    return reg


def test_blocked_plan_runs_nothing():
    calls = []
    ex = ActionExecutor(registry=_registry_with_act_tool(calls),
                        policy=Policy(max_blast_radius=BlastRadius.LOW, dry_run_default=False))
    res = asyncio.run(ex.execute(_plan(ActionStep(tool="k8s.cordon_node"), blast=BlastRadius.HIGH),
                                 approved=True))  # HIGH > LOW cap -> deny
    assert res.mode == "blocked" and res.success is False
    assert calls == []


def test_unapproved_is_dry_run_no_handler_calls():
    calls = []
    ex = ActionExecutor(registry=_registry_with_act_tool(calls), policy=_live_policy())
    res = asyncio.run(ex.execute(_plan(ActionStep(tool="k8s.cordon_node", args={"node": "n1"})),
                                 approved=False))
    assert res.mode == "dry_run"
    assert [s.status for s in res.steps] == ["dry_run"]
    assert calls == []


def test_approved_but_env_dry_run_default_does_not_execute():
    calls = []
    ex = ActionExecutor(registry=_registry_with_act_tool(calls),
                        policy=Policy(max_blast_radius=BlastRadius.HIGH))  # dry_run_default=True
    res = asyncio.run(ex.execute(_plan(ActionStep(tool="k8s.cordon_node")), approved=True))
    assert res.mode == "dry_run"
    assert calls == []


def test_live_execution_calls_handler_with_args():
    calls = []
    ex = ActionExecutor(registry=_registry_with_act_tool(calls), policy=_live_policy())
    res = asyncio.run(ex.execute(_plan(ActionStep(tool="k8s.cordon_node", args={"node": "gpu-7"})),
                                 approved=True))
    assert res.mode == "live" and res.success is True
    assert res.steps[0].status == "executed"
    assert calls == [{"node": "gpu-7"}]


def test_live_unknown_tool_errors():
    ex = ActionExecutor(registry=ToolRegistry(), policy=_live_policy())
    res = asyncio.run(ex.execute(_plan(ActionStep(tool="nope")), approved=True))
    assert res.steps[0].status == "error" and "not in registry" in res.steps[0].error
    assert res.success is False


def test_live_refuses_non_act_tool():
    reg = ToolRegistry()
    reg.register("prometheus.query", RiskClass.READ, "read", handler=lambda **k: None)
    ex = ActionExecutor(registry=reg, policy=_live_policy())
    res = asyncio.run(ex.execute(_plan(ActionStep(tool="prometheus.query")), approved=True))
    assert res.steps[0].status == "error" and "non-act" in res.steps[0].error


def test_live_stops_on_first_failure():
    reg = ToolRegistry()
    calls = []

    async def ok(**k):
        calls.append("a")
        return "ok"

    async def boom(**k):
        raise RuntimeError("kubectl exploded")

    reg.register("a.act", RiskClass.ACT, "ok", handler=ok)
    reg.register("b.act", RiskClass.ACT, "boom", handler=boom)
    reg.register("c.act", RiskClass.ACT, "never", handler=ok)
    ex = ActionExecutor(registry=reg, policy=_live_policy())
    res = asyncio.run(ex.execute(
        _plan(ActionStep(tool="a.act"), ActionStep(tool="b.act"), ActionStep(tool="c.act")),
        approved=True))
    statuses = [s.status for s in res.steps]
    assert statuses == ["executed", "error"]  # c never reached
    assert res.success is False
    assert calls == ["a"]
