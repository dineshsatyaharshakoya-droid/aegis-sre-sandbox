"""
Gated action executor (Stone 3, D2) — where an ActionPlan finally *runs*.

This is the only place live infrastructure mutation happens, and it delegates
EVERY gate to the D1 policy: it asks `Policy.evaluate(plan, approved=)` and then

  * DENY                      -> blocked, nothing runs
  * not permits_live_execution-> dry-run: render the steps, call no handlers
  * permits_live_execution    -> live: dispatch each step to its registry handler

Extra in-executor safety (defense in depth on top of the policy):
  * a step's tool must exist in the registry,
  * it must be risk-classed ACT (never execute a read/notify tool as an action),
  * it must have a handler; otherwise the step errors and execution stops.

No real cluster is wired here, so the default registry's infra act-tools have no
handler and will error out — live dispatch is proven in tests with a registered
mock act tool. Per SCALE_PLAN, verification (D4) and rollback (D5) come next.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

from aegis_sre.integrations.tool_registry import RiskClass, ToolRegistry, get_tool_registry
from aegis_sre.orchestrator.policy import Decision, Policy
from aegis_sre.orchestrator.schemas import ActionPlan
from aegis_sre.telemetry.logger import logger


@dataclass
class StepResult:
    tool: str
    status: str            # executed | dry_run | error
    output: str = ""
    error: Optional[str] = None


@dataclass
class ExecutionResult:
    mode: str              # live | dry_run | blocked
    decision: str
    reason: str
    steps: List[StepResult] = field(default_factory=list)
    audit: dict = field(default_factory=dict)

    @property
    def success(self) -> bool:
        return self.mode != "blocked" and all(s.status != "error" for s in self.steps)


class ActionExecutor:
    def __init__(self, registry: Optional[ToolRegistry] = None, policy: Optional[Policy] = None):
        self.registry = registry or get_tool_registry()
        self.policy = policy or Policy()

    async def execute(self, plan: ActionPlan, *, approved: bool = False) -> ExecutionResult:
        decision = self.policy.evaluate(plan, approved=approved)

        if decision.decision is Decision.DENY:
            logger.warning("action_execution_blocked", reason=decision.reason)
            return ExecutionResult(mode="blocked", decision=decision.decision.value,
                                   reason=decision.reason, audit=decision.audit)

        if not decision.permits_live_execution:
            steps = [StepResult(tool=s.tool, status="dry_run",
                                output=f"would run {s.tool}({s.args})") for s in plan.steps]
            logger.info("action_execution_dry_run", steps=len(steps), reason=decision.reason)
            return ExecutionResult(mode="dry_run", decision=decision.decision.value,
                                   reason=decision.reason, steps=steps, audit=decision.audit)

        # Live execution — every step gated again at the tool level.
        steps = await self._run_steps(plan.steps)
        return ExecutionResult(mode="live", decision=decision.decision.value,
                               reason=decision.reason, steps=steps, audit=decision.audit)

    async def execute_rollback(self, plan: ActionPlan) -> ExecutionResult:
        """Run a plan's compensating steps (D5). Used after a live action fails
        verification, to restore state. The steps still pass the per-tool guards
        (act-classed, registered, has a handler); no policy approval gate, since
        rolling back an already-approved action is the safe direction."""
        if not plan.rollback_steps:
            return ExecutionResult(mode="none", decision="n/a",
                                   reason="no rollback steps defined", steps=[])
        logger.warning("action_rollback_started", steps=len(plan.rollback_steps))
        steps = await self._run_steps(plan.rollback_steps)
        return ExecutionResult(mode="rollback", decision="n/a",
                               reason="compensating after failed verification", steps=steps)

    async def _run_steps(self, steps_in) -> List[StepResult]:
        """Dispatch a list of action steps to their registry handlers, stopping on
        the first error. Each step must be a registered, act-classed tool with a
        handler (the per-tool defense-in-depth guard)."""
        results: List[StepResult] = []
        for s in steps_in:
            err = self._step_guard(s.tool)
            if err:
                results.append(StepResult(tool=s.tool, status="error", error=err))
                logger.error("action_step_refused", tool=s.tool, error=err)
                break
            tool = self.registry.get(s.tool)
            try:
                out = await tool.handler(**s.args)
                results.append(StepResult(tool=s.tool, status="executed", output=str(out)))
                logger.info("action_step_executed", tool=s.tool)
            except Exception as e:  # noqa: BLE001 - record + stop on first failure
                results.append(StepResult(tool=s.tool, status="error", error=str(e)))
                logger.error("action_step_failed", tool=s.tool, error=str(e))
                break
        return results

    def _step_guard(self, tool_name: str) -> Optional[str]:
        """Return an error string if this tool must not be executed, else None."""
        try:
            tool = self.registry.get(tool_name)
        except KeyError:
            return "tool not in registry"
        if tool.risk is not RiskClass.ACT:
            return f"refusing to execute non-act tool (risk={tool.risk.value})"
        if tool.handler is None:
            return "no handler configured for tool"
        return None
