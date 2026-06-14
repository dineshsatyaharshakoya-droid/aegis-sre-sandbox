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


def test_actionplan_approval_executes_dry_run_by_default():
    # D3: approving an ActionPlan now drives the runner; default policy = dry-run.
    reg = ApprovalRegistry()
    vcs = _FakeVCS()
    reg.register("i2", _plan(), TELE)
    res = asyncio.run(reg.approve("i2", vcs))
    assert res["status"] == "executed"
    assert res["mode"] == "dry_run"
    assert res["resolved"] is False
    assert vcs.calls == 0  # an action plan must never be PR'd


class _FakeOutcome:
    def __init__(self, mode, resolved, rolled_back):
        self.executed = type("E", (), {"mode": mode, "reason": "r", "audit": {}, "steps": []})()
        self.resolved = resolved
        self.rolled_back = rolled_back


class _FakeRunner:
    def __init__(self, outcome): self._o = outcome
    async def run(self, plan, *, approved, verification=None): return self._o


def test_actionplan_live_resolved_via_injected_runner():
    reg = ApprovalRegistry()
    reg.register("i5", _plan(), TELE)
    runner = _FakeRunner(_FakeOutcome("live", resolved=True, rolled_back=False))
    res = asyncio.run(reg.approve("i5", _FakeVCS(), runner=runner))
    assert res["status"] == "executed" and res["mode"] == "live" and res["resolved"] is True


def test_actionplan_rolled_back_via_injected_runner():
    reg = ApprovalRegistry()
    reg.register("i6", _plan(), TELE)
    runner = _FakeRunner(_FakeOutcome("live", resolved=False, rolled_back=True))
    res = asyncio.run(reg.approve("i6", _FakeVCS(), runner=runner))
    assert res["status"] == "rolled_back" and res["rolled_back"] is True


def test_actionplan_blocked_is_restored_for_retry():
    reg = ApprovalRegistry()
    reg.register("i7", _plan(), TELE)
    runner = _FakeRunner(_FakeOutcome("blocked", resolved=False, rolled_back=False))
    res = asyncio.run(reg.approve("i7", _FakeVCS(), runner=runner))
    assert res["status"] == "blocked"
    assert reg.pending_count() == 1  # restored, not consumed


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


# --- audit #14: reject ---

def test_reject_drops_pending():
    reg = ApprovalRegistry()
    reg.register("i8", _plan(), TELE)
    assert reg.reject("i8") is True
    assert reg.pending_count() == 0
    assert reg.reject("i8") is False  # already gone


def test_reject_unknown_is_false():
    assert ApprovalRegistry().reject("nope") is False
