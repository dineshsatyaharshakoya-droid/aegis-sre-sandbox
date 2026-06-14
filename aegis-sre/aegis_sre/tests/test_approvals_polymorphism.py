"""
Tests for polymorphic approval handling (B6).

A CodePatch approval opens a PR; an ActionPlan approval is recorded but NOT acted
on (gated execution is Stone-3) and must never reach the VCS provider.
"""

import asyncio

from aegis_sre.core.approvals import ApprovalRegistry
from aegis_sre.orchestrator.schemas import (
    ActionPlan,
    ActionStep,
    BlastRadius,
    CodePatch,
    TelemetryEvent,
)


class _FakeVCS:
    def __init__(self):
        self.calls = 0

    async def create_pull_request(self, remediation, telemetry):
        self.calls += 1
        return "https://github.com/acme/repo/pull/9"


TELE = TelemetryEvent(event_id="i1", service_name="svc", crash_log="boom")


def _patch():
    return CodePatch(file_path="a.py", target_content="x", replacement_content="y",
                     root_cause_analysis="rc", explanation="why")


def _plan():
    return ActionPlan(steps=[ActionStep(tool="k8s.cordon_node", args={"node": "n1"})],
                      blast_radius=BlastRadius.MEDIUM, root_cause_analysis="rc", explanation="why")


def test_codepatch_approval_opens_pr():
    reg = ApprovalRegistry()
    vcs = _FakeVCS()
    reg.register("i1", _patch(), TELE)
    res = asyncio.run(reg.approve("i1", vcs))
    assert res["status"] == "deployed"
    assert res["file"] == "a.py"
    assert res["pr_url"].endswith("/pull/9")
    assert vcs.calls == 1


def test_actionplan_approval_is_recorded_not_executed():
    reg = ApprovalRegistry()
    vcs = _FakeVCS()
    reg.register("i2", _plan(), TELE)
    res = asyncio.run(reg.approve("i2", vcs))
    assert res["status"] == "approved_pending_execution"
    assert res["kind"] == "action_plan"
    assert res["steps"] == 1
    assert vcs.calls == 0  # an action plan must never be PR'd


def test_actionplan_approval_is_idempotent():
    reg = ApprovalRegistry()
    vcs = _FakeVCS()
    reg.register("i3", _plan(), TELE)
    asyncio.run(reg.approve("i3", vcs))
    again = asyncio.run(reg.approve("i3", vcs))
    assert again["status"] == "already_approved"
    assert vcs.calls == 0


def test_unknown_incident_is_not_found():
    res = asyncio.run(ApprovalRegistry().approve("nope", _FakeVCS()))
    assert res["status"] == "not_found"
