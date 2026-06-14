"""
Human-in-the-loop approval registry.

When the repair swarm produces a remediation it is held here, keyed by incident
id, until a human approves it over the WebSocket. Approval is what actually ships
it (a PR for a CodePatch, gated execute->verify->rollback for an ActionPlan).

Storage is pluggable (A8 / audit F2):
  * on-prem  -> InMemoryPendingStore (single process; the API runs the consumer).
  * cloud    -> RedisPendingStore, so the WORKER process can register a pending
    remediation and a SEPARATE API replica can approve it. Without this, cloud
    approvals were structurally impossible — the worker produced the remediation
    but the in-memory registry lived in the API process, so approve always 404'd.

Concurrency: `approve()` atomically *claims* the pending entry before the slow
await (dict pop in-memory; Redis GETDEL across replicas), so two racing approvals
can't both act on the same incident.
"""

from __future__ import annotations

import json
from collections import OrderedDict
from typing import Dict, Optional, Tuple

from aegis_sre.orchestrator.schemas import ActionPlan, CodePatch, Remediation, TelemetryEvent
from aegis_sre.telemetry.logger import logger
from aegis_sre.telemetry import metrics


# --- (de)serialization of a pending (Remediation, TelemetryEvent) -----------

def _serialize(remediation: Remediation, telemetry: TelemetryEvent) -> str:
    return json.dumps({
        "kind": remediation.kind.value,
        "remediation": remediation.model_dump(mode="json"),
        "telemetry": telemetry.model_dump(mode="json"),
    })


def _deserialize(blob: str) -> Tuple[Remediation, TelemetryEvent]:
    d = json.loads(blob)
    cls = ActionPlan if d["kind"] == "action_plan" else CodePatch
    return cls(**d["remediation"]), TelemetryEvent(**d["telemetry"])


# --- pending stores ----------------------------------------------------------

class InMemoryPendingStore:
    """Single-process store (on-prem). LRU-bounded."""

    def __init__(self, max_size: int = 1000):
        self.max_size = max_size
        self._pending: "OrderedDict[str, str]" = OrderedDict()
        self._approved: "OrderedDict[str, str]" = OrderedDict()

    async def register(self, incident_id: str, blob: str) -> None:
        self._pending[incident_id] = blob
        self._pending.move_to_end(incident_id)
        while len(self._pending) > self.max_size:
            evicted, _ = self._pending.popitem(last=False)
            logger.warning("pending_remediation_evicted_unapproved", incident_id=evicted)

    async def claim(self, incident_id: str) -> Optional[str]:
        return self._pending.pop(incident_id, None)

    async def restore(self, incident_id: str, blob: str) -> None:
        self._pending[incident_id] = blob
        self._pending.move_to_end(incident_id, last=False)

    async def reject(self, incident_id: str) -> bool:
        return self._pending.pop(incident_id, None) is not None

    async def claim_approved(self, incident_id: str) -> Optional[str]:
        return self._approved.get(incident_id)

    async def mark_approved(self, incident_id: str, record: str) -> None:
        self._approved[incident_id] = record
        while len(self._approved) > self.max_size:
            self._approved.popitem(last=False)

    async def count(self) -> int:
        return len(self._pending)


class RedisPendingStore:
    """Cross-process store (cloud). Pending/approved live in Redis so the worker
    registers and any API replica approves. Atomic claim via GETDEL."""

    def __init__(self, redis_url: str, ttl_seconds: int = 86_400):
        self._url = redis_url
        self.ttl = ttl_seconds
        self._client = None

    async def _conn(self):
        if self._client is None:
            import redis.asyncio as redis
            self._client = redis.from_url(self._url, decode_responses=True)
        return self._client

    def _pk(self, i: str) -> str: return f"aegis:approvals:pending:{i}"
    def _ak(self, i: str) -> str: return f"aegis:approvals:approved:{i}"

    async def register(self, incident_id: str, blob: str) -> None:
        await (await self._conn()).set(self._pk(incident_id), blob, ex=self.ttl)

    async def claim(self, incident_id: str) -> Optional[str]:
        return await (await self._conn()).getdel(self._pk(incident_id))  # atomic across replicas

    async def restore(self, incident_id: str, blob: str) -> None:
        await (await self._conn()).set(self._pk(incident_id), blob, ex=self.ttl)

    async def reject(self, incident_id: str) -> bool:
        return bool(await (await self._conn()).getdel(self._pk(incident_id)))

    async def claim_approved(self, incident_id: str) -> Optional[str]:
        return await (await self._conn()).get(self._ak(incident_id))

    async def mark_approved(self, incident_id: str, record: str) -> None:
        await (await self._conn()).set(self._ak(incident_id), record, ex=self.ttl)

    async def count(self) -> int:
        client = await self._conn()
        return sum(1 async for _ in client.scan_iter(match="aegis:approvals:pending:*"))


class ApprovalRegistry:
    def __init__(self, store=None, max_size: int = 1000):
        self._store = store if store is not None else InMemoryPendingStore(max_size)

    async def register(self, incident_id: str, patch: Remediation, telemetry: TelemetryEvent) -> None:
        """Hold a generated remediation pending human approval."""
        await self._store.register(incident_id, _serialize(patch, telemetry))

    async def pending_count(self) -> int:
        return await self._store.count()

    async def reject(self, incident_id: str) -> bool:
        existed = await self._store.reject(incident_id)
        if existed:
            logger.info("remediation_rejected", incident_id=incident_id)
        return existed

    async def approve(self, incident_id: str, vcs_provider, runner=None, arm: bool = False) -> Dict:
        """Approve a remediation. Idempotent and safe against double-approval.

        CodePatch -> open a PR (status `deployed`). ActionPlan -> gated
        execute->verify->rollback (dry-run by default; `arm` permits live).
        Returns `status` in {deployed, executed, rolled_back, blocked,
        already_approved, not_found, error}.
        """
        approved = await self._store.claim_approved(incident_id)
        if approved is not None:
            return {"status": "already_approved", "incident_id": incident_id, "pr_url": approved}

        blob = await self._store.claim(incident_id)  # atomic claim before slow await
        if blob is None:
            return {"status": "not_found", "incident_id": incident_id}

        remediation, telemetry = _deserialize(blob)
        if isinstance(remediation, ActionPlan):
            return await self._approve_action_plan(incident_id, remediation, blob, runner, arm)
        return await self._approve_code_patch(incident_id, remediation, telemetry, blob, vcs_provider)

    async def _approve_code_patch(self, incident_id, patch, telemetry, blob, vcs_provider) -> Dict:
        try:
            pr_url = await vcs_provider.create_pull_request(patch, telemetry)
        except Exception as e:  # noqa: BLE001
            await self._store.restore(incident_id, blob)  # retry after a transient VCS failure
            metrics.actions_executed.labels(type="code_patch", result="error").inc()
            logger.error("pull_request_creation_failed", incident_id=incident_id, error=str(e))
            return {"status": "error", "incident_id": incident_id, "error": str(e)}

        await self._store.mark_approved(incident_id, pr_url)
        metrics.patches_deployed.inc()
        metrics.actions_executed.labels(type="code_patch", result="deployed").inc()
        logger.info("patch_approved_pr_opened", incident_id=incident_id, file=patch.file_path, pr_url=pr_url)
        return {"status": "deployed", "incident_id": incident_id, "file": patch.file_path, "pr_url": pr_url}

    async def _approve_action_plan(self, incident_id, plan, blob, runner, arm=False) -> Dict:
        # P-1: arming is the explicit operator step that allows LIVE execution.
        if runner is None:
            from aegis_sre.orchestrator.remediation_runner import RemediationRunner
            if arm:
                from aegis_sre.orchestrator.action_executor import ActionExecutor
                from aegis_sre.orchestrator.policy import Policy
                plan.dry_run = False
                runner = RemediationRunner(executor=ActionExecutor(policy=Policy(dry_run_default=False)))
            else:
                runner = RemediationRunner()
        outcome = await runner.run(plan, approved=True, verification=plan.verification)

        mode = outcome.executed.mode  # live | dry_run | blocked
        if mode == "blocked":
            await self._store.restore(incident_id, blob)  # policy refused -> allow retry
            status, result = "blocked", "blocked"
        elif outcome.rolled_back:
            status, result = "rolled_back", "rolled_back"
        else:
            status, result = "executed", ("live" if mode == "live" else "dry_run")

        if status != "blocked":
            await self._store.mark_approved(incident_id, f"action-plan:{result}")
        metrics.actions_executed.labels(type="action_plan", result=result).inc()
        audit = {"steps": [s.__dict__ for s in outcome.executed.steps], **outcome.executed.audit}
        logger.info("action_plan_executed", incident_id=incident_id, mode=mode,
                    status=status, resolved=outcome.resolved, rolled_back=outcome.rolled_back)
        return {"status": status, "incident_id": incident_id, "kind": "action_plan",
                "mode": mode, "resolved": outcome.resolved, "rolled_back": outcome.rolled_back,
                "reason": outcome.executed.reason, "audit": audit}


def build_approval_registry(settings) -> ApprovalRegistry:
    """Redis-backed shared registry on the cloud tier (worker + API share state);
    in-memory on-prem."""
    if settings.cache_backend == "redis" or settings.is_cloud:
        logger.info("approval_registry_backend", backend="redis")
        return ApprovalRegistry(store=RedisPendingStore(settings.redis_url))
    return ApprovalRegistry(store=InMemoryPendingStore())
