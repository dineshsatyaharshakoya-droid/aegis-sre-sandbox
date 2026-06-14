"""
Tests for the action policy engine (D1).

These assert the safety guarantees the sellable product rests on: dry-run by
default, no live action without approval AND arming, blast-radius caps, allow/deny
lists, fail-closed on unknown types, and a complete audit record every time.
"""

from aegis_sre.orchestrator.policy import Decision, Policy
from aegis_sre.orchestrator.schemas import (
    ActionPlan,
    ActionStep,
    BlastRadius,
    CodePatch,
    Remediation,
    RemediationKind,
)


def _patch():
    return CodePatch(file_path="a.py", target_content="x", replacement_content="y",
                     root_cause_analysis="rc", explanation="why")


def _plan(blast=BlastRadius.LOW, dry_run=True, tool="k8s.cordon_node"):
    return ActionPlan(steps=[ActionStep(tool=tool, args={"node": "n1"})],
                      blast_radius=blast, dry_run=dry_run,
                      root_cause_analysis="rc", explanation="why")


# --- code patch ---

def test_codepatch_unapproved_requires_approval():
    r = Policy().evaluate(_patch(), approved=False)
    assert r.decision is Decision.REQUIRE_APPROVAL
    assert r.permits_live_execution is False


def test_codepatch_approved_allows_live_pr():
    r = Policy().evaluate(_patch(), approved=True)
    assert r.decision is Decision.ALLOW and r.mode == "live"
    assert r.permits_live_execution is True


# --- action plan: the core safety guarantees ---

def test_actionplan_unapproved_is_dry_run_not_live():
    r = Policy(max_blast_radius=BlastRadius.HIGH).evaluate(_plan(), approved=False)
    assert r.decision is Decision.REQUIRE_APPROVAL
    assert r.mode == "dry_run"
    assert r.permits_live_execution is False


def test_actionplan_approved_but_unarmed_runs_dry_run_only():
    # dry_run=True (not armed) -> even approved, must not go live.
    r = Policy(max_blast_radius=BlastRadius.HIGH).evaluate(_plan(dry_run=True), approved=True)
    assert r.decision is Decision.ALLOW and r.mode == "dry_run"
    assert r.permits_live_execution is False


def test_actionplan_approved_and_armed_permits_live():
    # Live needs BOTH gates open: env allows live (dry_run_default=False) AND armed.
    pol = Policy(max_blast_radius=BlastRadius.HIGH, dry_run_default=False)
    r = pol.evaluate(_plan(dry_run=False), approved=True)
    assert r.permits_live_execution is True


def test_default_environment_is_dry_run_only():
    # Default policy (dry_run_default=True) never goes live, even armed + approved.
    r = Policy(max_blast_radius=BlastRadius.HIGH).evaluate(_plan(dry_run=False), approved=True)
    assert r.decision is Decision.ALLOW and r.mode == "dry_run"
    assert r.permits_live_execution is False


def test_unarmed_plan_never_goes_live_even_when_env_allows_live():
    # Regression: an unarmed plan (plan.dry_run=True) must stay dry-run even if the
    # environment permits live actions. Previously this leaked to live.
    pol = Policy(max_blast_radius=BlastRadius.HIGH, dry_run_default=False)
    r = pol.evaluate(_plan(dry_run=True), approved=True)
    assert r.mode == "dry_run"
    assert r.permits_live_execution is False
    assert r.audit["armed"] is False  # audit must report the truth


def test_blast_radius_over_cap_is_denied():
    r = Policy(max_blast_radius=BlastRadius.LOW).evaluate(
        _plan(blast=BlastRadius.HIGH, dry_run=False), approved=True)
    assert r.decision is Decision.DENY
    assert "exceeds cap" in r.reason
    assert r.permits_live_execution is False


def test_deny_list_blocks_tool():
    pol = Policy(max_blast_radius=BlastRadius.HIGH, deny_tools={"k8s.delete_namespace"})
    r = pol.evaluate(_plan(tool="k8s.delete_namespace", dry_run=False), approved=True)
    assert r.decision is Decision.DENY and "deny list" in r.reason


def test_allow_list_blocks_unlisted_tool():
    pol = Policy(max_blast_radius=BlastRadius.HIGH, allow_tools={"job.requeue"})
    r = pol.evaluate(_plan(tool="k8s.cordon_node", dry_run=False), approved=True)
    assert r.decision is Decision.DENY and "allow list" in r.reason


def test_allow_list_permits_listed_tool():
    pol = Policy(max_blast_radius=BlastRadius.HIGH, allow_tools={"k8s.cordon_node"},
                 dry_run_default=False)
    r = pol.evaluate(_plan(tool="k8s.cordon_node", dry_run=False), approved=True)
    assert r.permits_live_execution is True


def test_unknown_remediation_fails_closed():
    class Weird(Remediation):
        kind: RemediationKind = RemediationKind.CODE_PATCH
    r = Policy().evaluate(Weird(root_cause_analysis="rc", explanation="e"), approved=True)
    assert r.decision is Decision.DENY


def test_audit_record_is_complete():
    r = Policy(environment="prod").evaluate(_plan(dry_run=False), approved=True)
    assert r.audit["environment"] == "prod"
    assert "timestamp" in r.audit
    assert r.audit["decision"] == "allow"
