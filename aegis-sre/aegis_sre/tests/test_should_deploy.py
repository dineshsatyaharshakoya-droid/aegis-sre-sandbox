"""
Direct branch tests for should_deploy (the deploy-routing algorithm).

It was only exercised incidentally by the full-graph tests; these assert each of
its three outcomes (deploy / retry / fail) in isolation, including the guard that
deploy needs BOTH a safe review AND a passing sandbox.
"""

from aegis_sre.orchestrator.graph import should_deploy
from aegis_sre.orchestrator.safety import safety_policy
from aegis_sre.orchestrator.schemas import SecurityReview


def _safe():
    return SecurityReview(is_safe=True, vulnerability_found=False, feedback="ok")


def _unsafe():
    return SecurityReview(is_safe=False, vulnerability_found=False, feedback="bad")


def test_deploy_when_safe_and_sandbox_success():
    assert should_deploy({"review": _safe(), "sandbox_status": "success", "iteration_count": 1}) == "deploy"


def test_no_deploy_if_sandbox_failed_even_when_safe():
    # Both gates required: a safe review with a failed sandbox must not deploy.
    out = should_deploy({"review": _safe(), "sandbox_status": "failed", "iteration_count": 1})
    assert out != "deploy"


def test_no_deploy_if_unsafe_even_when_sandbox_success():
    out = should_deploy({"review": _unsafe(), "sandbox_status": "success", "iteration_count": 1})
    assert out != "deploy"


def test_retry_when_not_deployable_and_under_retry_cap():
    out = should_deploy({"review": _unsafe(), "sandbox_status": "failed", "iteration_count": 1})
    assert out == "retry"


def test_fail_when_retry_cap_exceeded():
    # iteration_count >= safety_policy.max_retries -> abort to "fail".
    state = {"review": _unsafe(), "sandbox_status": "failed",
             "iteration_count": safety_policy.max_retries}
    assert should_deploy(state) == "fail"


def test_missing_review_does_not_deploy():
    out = should_deploy({"review": None, "sandbox_status": "success", "iteration_count": 1})
    assert out != "deploy"
