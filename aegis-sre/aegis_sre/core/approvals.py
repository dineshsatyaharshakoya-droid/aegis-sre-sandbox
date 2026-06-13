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

from aegis_sre.orchestrator.schemas import PatchProposal, TelemetryEvent
from aegis_sre.telemetry.logger import logger
from aegis_sre.telemetry import metrics


class ApprovalRegistry:
    def __init__(self, max_size: int = 1000):
        self.max_size = max_size
        self._pending: "OrderedDict[str, Tuple[PatchProposal, TelemetryEvent]]" = OrderedDict()
        self._approved: "OrderedDict[str, str]" = OrderedDict()  # incident_id -> pr_url (idempotency)

    def register(self, incident_id: str, patch: PatchProposal, telemetry: TelemetryEvent) -> None:
        """Hold a generated patch pending human approval (LRU-bounded)."""
        self._pending[incident_id] = (patch, telemetry)
        self._pending.move_to_end(incident_id)
        while len(self._pending) > self.max_size:
            evicted, _ = self._pending.popitem(last=False)
            logger.warning("pending_patch_evicted_unapproved", incident_id=evicted)

    def pending_count(self) -> int:
        return len(self._pending)

    async def approve(self, incident_id: str, vcs_provider) -> Dict:
        """Approve and open a PR. Idempotent and safe against double-approval.

        Returns a result dict with `status` in
        {deployed, already_approved, not_found, error}.
        """
        # Idempotency: an incident already deployed returns the same PR url.
        if incident_id in self._approved:
            return {"status": "already_approved", "incident_id": incident_id,
                    "pr_url": self._approved[incident_id]}

        # Atomically claim the entry *before* the slow await so a concurrent
        # approval of the same incident finds nothing to do.
        entry = self._pending.pop(incident_id, None)
        if entry is None:
            return {"status": "not_found", "incident_id": incident_id}

        patch, telemetry = entry
        try:
            pr_url = await vcs_provider.create_pull_request(patch, telemetry)
        except Exception as e:  # noqa: BLE001
            # Restore so the operator can retry after a transient VCS failure.
            self._pending[incident_id] = entry
            self._pending.move_to_end(incident_id, last=False)
            logger.error("pull_request_creation_failed", incident_id=incident_id, error=str(e))
            return {"status": "error", "incident_id": incident_id, "error": str(e)}

        self._approved[incident_id] = pr_url
        while len(self._approved) > self.max_size:
            self._approved.popitem(last=False)
        metrics.patches_deployed.inc()
        logger.info("patch_approved_pr_opened", incident_id=incident_id, file=patch.file_path, pr_url=pr_url)
        return {"status": "deployed", "incident_id": incident_id, "file": patch.file_path, "pr_url": pr_url}
