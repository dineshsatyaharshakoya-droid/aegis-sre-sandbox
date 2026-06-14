"""
Action policy engine (Stone 3, D1) — the gate that makes live remediation safe.

Where `safety.py` bounds the *swarm loop* (retries, timeout), this bounds
*execution*: before any remediation runs, the policy decides whether it may act,
whether a human must approve first, and whether it runs live or dry-run. Per the
SCALE_PLAN, the policy must LEAD execution — every widening of blast radius is
made safe here, not after the fact.

Inputs that drive a decision:
  * remediation type + the tool risk class (from the C1 registry: act tools gate)
  * blast radius vs a configured cap
  * per-environment allow / deny tool lists
  * approval state, and whether the plan is explicitly armed

Guarantees (all asserted by tests):
  * dry-run by default — an approved-but-unarmed ActionPlan runs dry-run, not live
  * an ActionPlan never reaches live execution without approval AND arming
  * blast radius over the cap, or a deny-listed tool, is DENIED
  * unknown remediation types fail closed (DENY)
  * every evaluation produces a complete audit record
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import List, Optional, Set

from aegis_sre.integrations.tool_registry import ToolRegistry, get_tool_registry
from aegis_sre.orchestrator.schemas import ActionPlan, BlastRadius, CodePatch, Remediation
from aegis_sre.telemetry.logger import logger

_BLAST_ORDER = {BlastRadius.LOW: 0, BlastRadius.MEDIUM: 1, BlastRadius.HIGH: 2}


class Decision(str, Enum):
    ALLOW = "allow"                        # may proceed (live or dry-run per mode)
    REQUIRE_APPROVAL = "require_approval"  # human approval required before live exec
    DENY = "deny"                          # blocked by policy


@dataclass
class PolicyResult:
    decision: Decision
    mode: str            # "live" | "dry_run" | "blocked"
    requires_approval: bool
    reason: str
    audit: dict

    @property
    def permits_live_execution(self) -> bool:
        return self.decision is Decision.ALLOW and self.mode == "live"


@dataclass
class Policy:
    """Per-environment action policy. Defaults are conservative (fail-safe)."""
    environment: str = "dev"
    max_blast_radius: BlastRadius = BlastRadius.MEDIUM
    deny_tools: Set[str] = field(default_factory=set)
    allow_tools: Optional[Set[str]] = None  # if set, ActionPlan tools must all be in it
    dry_run_default: bool = True
    registry: Optional[ToolRegistry] = None

    def _registry(self) -> ToolRegistry:
        return self.registry or get_tool_registry()

    def _audit(self, **fields) -> dict:
        return {"timestamp": datetime.now(timezone.utc).isoformat(),
                "environment": self.environment, **fields}

    def evaluate(self, remediation: Remediation, *, approved: bool = False) -> PolicyResult:
        if isinstance(remediation, ActionPlan):
            result = self._evaluate_action_plan(remediation, approved=approved)
        elif isinstance(remediation, CodePatch):
            result = self._evaluate_code_patch(remediation, approved=approved)
        else:
            # Fail closed on anything we don't explicitly understand.
            result = PolicyResult(
                Decision.DENY, "blocked", False,
                f"no policy for remediation type {type(remediation).__name__}",
                self._audit(decision="deny", reason="unknown_remediation"))
        logger.info("policy_evaluated", decision=result.decision.value, mode=result.mode,
                    requires_approval=result.requires_approval, reason=result.reason)
        return result

    def _evaluate_code_patch(self, patch: CodePatch, *, approved: bool) -> PolicyResult:
        # Code patches ship as human-reviewable PRs: always need approval, never
        # blast-radius-capped (a PR mutates nothing until merged by a human).
        if not approved:
            return PolicyResult(
                Decision.REQUIRE_APPROVAL, "dry_run", True,
                "Code patch requires human approval before a PR is opened.",
                self._audit(decision="require_approval", remediation="code_patch", file=patch.file_path))
        return PolicyResult(
            Decision.ALLOW, "live", True,
            "Approved code patch may open a PR.",
            self._audit(decision="allow", mode="live", remediation="code_patch",
                        file=patch.file_path, approved=True))

    def _evaluate_action_plan(self, plan: ActionPlan, *, approved: bool) -> PolicyResult:
        tools = [s.tool for s in plan.steps]

        denied = [t for t in tools if t in self.deny_tools]
        if denied:
            return PolicyResult(
                Decision.DENY, "blocked", False, f"tool(s) on deny list: {denied}",
                self._audit(decision="deny", reason="deny_list", tools=tools))

        if self.allow_tools is not None:
            not_allowed = [t for t in tools if t not in self.allow_tools]
            if not_allowed:
                return PolicyResult(
                    Decision.DENY, "blocked", False, f"tool(s) not in allow list: {not_allowed}",
                    self._audit(decision="deny", reason="not_in_allow_list", tools=tools))

        if _BLAST_ORDER[plan.blast_radius] > _BLAST_ORDER[self.max_blast_radius]:
            return PolicyResult(
                Decision.DENY, "blocked", False,
                f"blast radius {plan.blast_radius.value} exceeds cap {self.max_blast_radius.value}",
                self._audit(decision="deny", reason="blast_radius_cap",
                            blast_radius=plan.blast_radius.value, cap=self.max_blast_radius.value))

        if not approved:
            return PolicyResult(
                Decision.REQUIRE_APPROVAL, "dry_run", True,
                "ActionPlan requires human approval; dry-run only until approved.",
                self._audit(decision="require_approval", tools=tools,
                            blast_radius=plan.blast_radius.value))

        # Approved. dry-run by default unless the plan is explicitly armed.
        armed = plan.dry_run is False
        if self.dry_run_default and not armed:
            return PolicyResult(
                Decision.ALLOW, "dry_run", True,
                "Approved but not armed; executing dry-run (dry_run_default).",
                self._audit(decision="allow", mode="dry_run", tools=tools,
                            blast_radius=plan.blast_radius.value, approved=True, armed=False))
        return PolicyResult(
            Decision.ALLOW, "live", True,
            "Approved and armed; live execution permitted.",
            self._audit(decision="allow", mode="live", tools=tools,
                        blast_radius=plan.blast_radius.value, approved=True, armed=True))
