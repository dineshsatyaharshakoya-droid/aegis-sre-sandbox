"""
Remediation validator (Stone 1, B4).

The gate before deploy must work for *any* remediation, not just code patches.
`Validator.validate()` dispatches on remediation type:

  * CodePatch  -> the existing sandbox path: apply the patch to the real source,
                  compile it, and (if a trusted repro command is set) run it.
  * ActionPlan -> a DRY-RUN: render exactly what would happen, step by step,
                  WITHOUT executing anything. Real execution is gated by the
                  Stone-3 policy (D1/D2); the validator itself never acts.

This keeps the deploy path remediation-type-agnostic (wired in B5) while the
crash→patch behavior is unchanged (the patch branch delegates to the same
sandbox engine sandbox_node already used).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from aegis_sre.orchestrator.schemas import ActionPlan, CodePatch, Remediation
from aegis_sre.telemetry.logger import logger


@dataclass
class ValidationResult:
    success: bool
    output: str
    kind: str
    dry_run: bool = False


class Validator:
    """Type-dispatching remediation validator. The sandbox engine is injectable
    so the patch branch can be unit-tested without a real compiler."""

    def __init__(self, sandbox_engine=None):
        self._engine = sandbox_engine

    def _engine_or_default(self):
        if self._engine is not None:
            return self._engine
        from aegis_sre.orchestrator.sandbox_engine import get_sandbox_engine
        return get_sandbox_engine()

    async def validate(
        self,
        remediation: Remediation,
        *,
        original_source: Optional[str] = None,
        repro_command: Optional[str] = None,
    ) -> ValidationResult:
        if isinstance(remediation, CodePatch):
            engine = self._engine_or_default()
            success, output = await engine.compile_and_test(
                remediation, original_source=original_source, repro_command=repro_command
            )
            return ValidationResult(success=success, output=output, kind="code_patch")

        if isinstance(remediation, ActionPlan):
            return self._dry_run(remediation)

        # Unknown remediation type -> fail closed (never silently "pass").
        return ValidationResult(
            success=False,
            output=f"No validator for remediation type {type(remediation).__name__}",
            kind="unknown",
        )

    def _dry_run(self, plan: ActionPlan) -> ValidationResult:
        """Render the plan without executing it. Always succeeds at the validation
        stage (the plan is well-formed); whether it may *run* is the policy's call."""
        lines = [
            f"DRY-RUN action plan — blast_radius={plan.blast_radius.value}, "
            f"armed={not plan.dry_run}, steps={len(plan.steps)}:"
        ]
        for i, step in enumerate(plan.steps, 1):
            arglist = ", ".join(f"{k}={v!r}" for k, v in step.args.items())
            desc = f" — {step.description}" if step.description else ""
            lines.append(f"  {i}. {step.tool}({arglist}){desc}")
        lines.append("No actions executed (Stone-3 policy gates real execution).")
        logger.info("action_plan_dry_run", steps=len(plan.steps),
                    blast_radius=plan.blast_radius.value, armed=not plan.dry_run)
        return ValidationResult(success=True, output="\n".join(lines), kind="action_plan", dry_run=True)
