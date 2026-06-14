"""
Human-in-the-loop approval registry.

When the repair swarm produces a patch it is held here, keyed by incident id,
until a human approves it over the WebSocket. Approval is what actually calls
`vcs.create_pull_request` — previously the WS handler only logged and broadcast a
fake `patch_deployed`, so the human gate had no effect.

Concurrency model: this is designed for a single event loop (the on-prem API
process, or one worker). `approve()` atomically *claims* the pending entry with a
dict pop before it awaits the (slow) VCS call, so two racing approvals can't both
open a PR. A multi-replica cloud deployment must back this with shared state
(e.g. the EventStore / Redis) so approval works across replicas — noted as a
follow-up rather than silently assumed.
"""

from __future__ import annotations

from collections import OrderedDict
from typing import Dict, Optional, Tuple

from aegis_sre.orchestrator.schemas import ActionPlan, CodePatch, Remediation, TelemetryEvent
from aegis_sre.telemetry.logger import logger
from aegis_sre.telemetry import metrics


class ApprovalRegistry:
    def __init__(self, max_size: int = 1000):
        self.max_size = max_size
        self._pending: "OrderedDict[str, Tuple[Remediation, TelemetryEvent]]" = OrderedDict()
        self._approved: "OrderedDict[str, str]" = OrderedDict()  # incident_id -> pr_url (idempotency)

    def register(self, incident_id: str, patch: Remediation, telemetry: TelemetryEvent) -> None:
        """Hold a generated remediation pending human approval (LRU-bounded)."""
        self._pending[incident_id] = (patch, telemetry)
        self._pending.move_to_end(incident_id)
        while len(self._pending) > self.max_size:
            evicted, _ = self._pending.popitem(last=False)
            logger.warning("pending_patch_evicted_unapproved", incident_id=evicted)

    def pending_count(self) -> int:
        return len(self._pending)

    async def approve(self, incident_id: str, vcs_provider, runner=None) -> Dict:
        """Approve a remediation. Idempotent and safe against double-approval.

        Polymorphic by remediation type:
          * CodePatch  -> open a PR via the VCS provider (status `deployed`).
          * ActionPlan -> drive the gated execute->verify->rollback runner (D3).
            With the default policy this is a dry-run; live needs an environment
            that permits it plus an armed plan.

        Returns a result dict with `status` in {deployed, executed, rolled_back,
        blocked, already_approved, not_found, error}. Every outcome is audited and
        counted via aegis_actions_executed_total (D6).
        """
        # Idempotency: an incident already actioned returns the same record.
        if incident_id in self._approved:
            return {"status": "already_approved", "incident_id": incident_id,
                    "pr_url": self._approved[incident_id]}

        # Atomically claim the entry *before* the slow await so a concurrent
        # approval of the same incident finds nothing to do.
        entry = self._pending.pop(incident_id, None)
        if entry is None:
            return {"status": "not_found", "incident_id": incident_id}

        remediation, telemetry = entry

        if isinstance(remediation, ActionPlan):
            return await self._approve_action_plan(incident_id, remediation, entry, runner)
        return await self._approve_code_patch(incident_id, remediation, telemetry, entry, vcs_provider)

    async def _approve_code_patch(self, incident_id, patch, telemetry, entry, vcs_provider) -> Dict:
        try:
            pr_url = await vcs_provider.create_pull_request(patch, telemetry)
        except Exception as e:  # noqa: BLE001
            # Restore so the operator can retry after a transient VCS failure.
            self._pending[incident_id] = entry
            self._pending.move_to_end(incident_id, last=False)
            metrics.actions_executed.labels(type="code_patch", result="error").inc()
            logger.error("pull_request_creation_failed", incident_id=incident_id, error=str(e))
            return {"status": "error", "incident_id": incident_id, "error": str(e)}

        self._approved[incident_id] = pr_url
        self._trim_approved()
        metrics.patches_deployed.inc()
        metrics.actions_executed.labels(type="code_patch", result="deployed").inc()
        logger.info("patch_approved_pr_opened", incident_id=incident_id,
                    file=patch.file_path, pr_url=pr_url)
        return {"status": "deployed", "incident_id": incident_id,
                "file": patch.file_path, "pr_url": pr_url}

    async def _approve_action_plan(self, incident_id, plan, entry, runner) -> Dict:
        # Lazy import keeps approvals importable without the executor stack.
        if runner is None:
            from aegis_sre.orchestrator.remediation_runner import RemediationRunner
            runner = RemediationRunner()
        outcome = await runner.run(plan, approved=True, verification=plan.verification)

        mode = outcome.executed.mode  # live | dry_run | blocked
        if mode == "blocked":
            # Policy refused — restore so the operator can adjust and retry.
            self._pending[incident_id] = entry
            self._pending.move_to_end(incident_id, last=False)
            status, result = "blocked", "blocked"
        elif outcome.rolled_back:
            status, result = "rolled_back", "rolled_back"
        else:
            status, result = "executed", ("live" if mode == "live" else "dry_run")

        if status != "blocked":
            self._approved[incident_id] = f"action-plan:{result}"
            self._trim_approved()
        metrics.actions_executed.labels(type="action_plan", result=result).inc()
        audit = {"steps": [s.__dict__ for s in outcome.executed.steps], **outcome.executed.audit}
        logger.info("action_plan_executed", incident_id=incident_id, mode=mode,
                    status=status, resolved=outcome.resolved, rolled_back=outcome.rolled_back)
        return {"status": status, "incident_id": incident_id, "kind": "action_plan",
                "mode": mode, "resolved": outcome.resolved, "rolled_back": outcome.rolled_back,
                "reason": outcome.executed.reason, "audit": audit}

    def reject(self, incident_id: str) -> bool:
        """Drop a pending remediation so it can never be approved/executed.
        Returns True if something was pending and is now discarded."""
        entry = self._pending.pop(incident_id, None)
        if entry is None:
            return False
        logger.info("remediation_rejected", incident_id=incident_id)
        return True

    def _trim_approved(self) -> None:
        while len(self._approved) > self.max_size:
            self._approved.popitem(last=False)
