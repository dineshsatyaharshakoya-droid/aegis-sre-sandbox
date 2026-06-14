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

    async def approve(self, incident_id: str, vcs_provider) -> Dict:
        """Approve a remediation. Idempotent and safe against double-approval.

        Polymorphic by remediation type:
          * CodePatch  -> open a PR via the VCS provider (status `deployed`).
          * ActionPlan -> record approval only; gated execution lands in Stone-3,
            so we do NOT act yet (status `approved_pending_execution`).

        Returns a result dict with `status` in
        {deployed, approved_pending_execution, already_approved, not_found, error}.
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

        # ActionPlan: gated execution is not built yet (Stone-3). Record the
        # human approval but do not execute — never try to PR an action plan.
        if isinstance(remediation, ActionPlan):
            self._approved[incident_id] = "action-plan-pending-execution"
            self._trim_approved()
            logger.info("action_plan_approved_pending_execution", incident_id=incident_id,
                        steps=len(remediation.steps), blast_radius=remediation.blast_radius.value)
            return {"status": "approved_pending_execution", "incident_id": incident_id,
                    "kind": "action_plan", "steps": len(remediation.steps),
                    "detail": "ActionPlan approved; gated execution lands in Stone 3."}

        # CodePatch (default): open a PR.
        try:
            pr_url = await vcs_provider.create_pull_request(remediation, telemetry)
        except Exception as e:  # noqa: BLE001
            # Restore so the operator can retry after a transient VCS failure.
            self._pending[incident_id] = entry
            self._pending.move_to_end(incident_id, last=False)
            logger.error("pull_request_creation_failed", incident_id=incident_id, error=str(e))
            return {"status": "error", "incident_id": incident_id, "error": str(e)}

        self._approved[incident_id] = pr_url
        self._trim_approved()
        metrics.patches_deployed.inc()
        logger.info("patch_approved_pr_opened", incident_id=incident_id,
                    file=remediation.file_path, pr_url=pr_url)
        return {"status": "deployed", "incident_id": incident_id,
                "file": remediation.file_path, "pr_url": pr_url}

    def _trim_approved(self) -> None:
        while len(self._approved) > self.max_size:
            self._approved.popitem(last=False)
