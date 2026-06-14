"""
Remediation runner (Stone 3, D5 + the execute→verify→rollback spine).

Ties the gated executor (D2), post-action verification (D4), and rollback (D5)
into the loop the SCALE_PLAN calls the sellable milestone:

    execute (gated) -> verify the triggering metric cleared -> rollback if it didn't

Only a *live, fully successful* execution is verified; a blocked/dry-run/errored
execution never proceeds to verify or rollback. If verification fails, the plan's
compensating steps run automatically and the outcome is marked unresolved.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from aegis_sre.orchestrator.action_executor import ActionExecutor, ExecutionResult
from aegis_sre.orchestrator.schemas import ActionPlan
from aegis_sre.orchestrator.verifier import VerificationCheck, VerificationResult, Verifier
from aegis_sre.telemetry.logger import logger


@dataclass
class RemediationOutcome:
    executed: ExecutionResult
    verification: Optional[VerificationResult]
    rollback: Optional[ExecutionResult]
    resolved: bool

    @property
    def rolled_back(self) -> bool:
        return self.rollback is not None


class RemediationRunner:
    def __init__(self, executor: Optional[ActionExecutor] = None,
                 verifier: Optional[Verifier] = None):
        self.executor = executor or ActionExecutor()
        self.verifier = verifier or Verifier()

    async def run(self, plan: ActionPlan, *, approved: bool,
                  verification: Optional[VerificationCheck] = None) -> RemediationOutcome:
        executed = await self.executor.execute(plan, approved=approved)

        # Only a live, fully-successful execution is a candidate for verify/rollback.
        if executed.mode != "live" or not executed.success:
            return RemediationOutcome(executed, None, None, resolved=False)

        # No verification check supplied -> we executed but can't prove recovery.
        if verification is None:
            logger.info("remediation_executed_unverified")
            return RemediationOutcome(executed, None, None, resolved=True)

        ver = await self.verifier.verify(verification)
        if ver.verified:
            logger.info("remediation_verified", detail=ver.detail)
            return RemediationOutcome(executed, ver, None, resolved=True)

        # Regression / not cleared -> roll back automatically.
        logger.warning("remediation_failed_verification_rolling_back", detail=ver.detail)
        rollback = await self.executor.execute_rollback(plan)
        return RemediationOutcome(executed, ver, rollback, resolved=False)
