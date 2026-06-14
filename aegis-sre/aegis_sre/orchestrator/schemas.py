from enum import Enum
from pydantic import BaseModel, Field, field_validator
from typing import Optional, Dict, Any, List

class TelemetryEvent(BaseModel):
    """
    Data model representing a crash or error intercepted from the telemetry layer.
    """
    event_id: str
    service_name: str
    crash_log: str = Field(description="The stack trace or error log of the crash.")
    metadata: Dict[str, Any] = Field(default_factory=dict, description="Additional context like memory usage or pod name.")


class SignalKind(str, Enum):
    """The class of incident-triggering input. CRASH is today's only live path;
    the others are the generalization targets (metric alerts, log anomalies)."""
    CRASH = "crash"
    METRIC_ALERT = "metric_alert"
    LOG_ANOMALY = "log_anomaly"
    GENERIC = "generic"


class Signal(BaseModel):
    """Stone 1: the generalized incident input (supersedes the crash-only
    `TelemetryEvent`). A `Signal` is any thing that can trigger remediation —
    a crash, a metric alert, a log anomaly — normalized to one shape.

    `TelemetryEvent` stays the canonical type the pipeline runs on, so this is
    purely additive: `from_telemetry` / `to_telemetry` give a lossless bridge so
    new sources can enter as `Signal`s while the crash path is byte-for-byte
    unchanged. Later phases (B2-B5) migrate the core onto `Signal` directly.
    """
    signal_id: str = Field(description="Stable id; doubles as the dedup/incident key.")
    service_name: str
    kind: SignalKind = Field(default=SignalKind.CRASH, description="What kind of signal this is.")
    body: str = Field(description="The diagnostic text: crash log, alert summary, anomaly detail.")
    metadata: Dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def from_telemetry(cls, event: "TelemetryEvent") -> "Signal":
        """Adapt a legacy crash `TelemetryEvent` into a `Signal` (kind=crash)."""
        return cls(
            signal_id=event.event_id,
            service_name=event.service_name,
            kind=SignalKind.CRASH,
            body=event.crash_log,
            metadata=dict(event.metadata),
        )

    def to_telemetry(self) -> "TelemetryEvent":
        """Project a `Signal` back onto the canonical `TelemetryEvent` the graph
        consumes today. The originating `kind` is preserved in metadata so no
        information is lost when a non-crash signal flows the crash path."""
        meta = dict(self.metadata)
        meta.setdefault("signal_kind", self.kind.value)
        return TelemetryEvent(
            event_id=self.signal_id,
            service_name=self.service_name,
            crash_log=self.body,
            metadata=meta,
        )

class RemediationKind(str, Enum):
    """The class of fix a `Remediation` represents. CODE_PATCH is today's only
    live path; ACTION_PLAN is the Stone-3 (gated infra action) target."""
    CODE_PATCH = "code_patch"
    ACTION_PLAN = "action_plan"


class Remediation(BaseModel):
    """Stone 1: the base type for any proposed fix. Carries the diagnosis fields
    common to every remediation; concrete subclasses add their payload. Lets the
    executor/reviewer/deploy path become remediation-type-agnostic in later
    phases without the crash→patch path changing today."""
    kind: RemediationKind
    root_cause_analysis: str = Field(description="The exact failure and its root cause.")
    explanation: str = Field(description="A brief explanation of why this fixes the issue.")


class CodePatch(Remediation):
    """A source-code fix: replace `target_content` with `replacement_content` in
    `file_path`. This is the body of the former `PatchProposal` (kept as an alias
    below for back-compat) now expressed as a `Remediation`."""
    kind: RemediationKind = RemediationKind.CODE_PATCH
    file_path: str = Field(description="The strictly relative path to the file to be patched.")
    target_content: str = Field(description="The exact chunk of code to replace.")
    replacement_content: str = Field(description="The new code to insert.")

    @field_validator('file_path')
    @classmethod
    def validate_file_path(cls, v: str) -> str:
        if ".." in v or v.startswith("/"):
            raise ValueError("Path traversal detected. file_path must be strictly relative to the repository root and cannot contain '..'")
        return v


# Back-compat alias: existing imports/constructions of `PatchProposal` continue to
# work unchanged (it *is* a CodePatch). Phases B2+ migrate call sites to CodePatch.
PatchProposal = CodePatch


class BlastRadius(str, Enum):
    """How much an action can affect if it goes wrong — the key input to the
    Stone-3 risk-tiered approval policy. LOW = single reversible resource;
    HIGH = cluster-wide or irreversible."""
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class ActionStep(BaseModel):
    """One typed step in an `ActionPlan`: an `act` tool call with its arguments."""
    tool: str = Field(description="The act-tool to invoke, e.g. 'k8s.cordon_node'.")
    args: Dict[str, Any] = Field(default_factory=dict, description="Arguments for the tool.")
    description: str = Field(default="", description="Human-readable intent of this step.")

    @field_validator('tool')
    @classmethod
    def tool_not_empty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("ActionStep.tool must be a non-empty tool name")
        return v


class Comparator(str, Enum):
    """How an observed metric is compared to its healthy threshold (D4)."""
    LT = "lt"
    LTE = "lte"
    GT = "gt"
    GTE = "gte"
    EQ = "eq"


class VerificationCheck(BaseModel):
    """A post-action recovery check: re-read `query` and confirm it satisfies
    `comparator threshold` (e.g. error-rate LT 0.05, or up GTE 1)."""
    query: str = Field(description="PromQL that should return the metric to check.")
    comparator: Comparator
    threshold: float


class ActionPlan(Remediation):
    """A non-code remediation: an ordered set of gated infrastructure actions
    (cordon a node, requeue a job, scale a deployment). Stone-3 executes these
    behind a policy; until then this is schema-only. `dry_run` defaults True so a
    plan is inert-by-default — it must be explicitly armed before it can act."""
    kind: RemediationKind = RemediationKind.ACTION_PLAN
    steps: List[ActionStep] = Field(description="Ordered, non-empty list of steps.")
    blast_radius: BlastRadius = Field(default=BlastRadius.HIGH,
                                      description="Fail-safe default: assume HIGH until assessed.")
    dry_run: bool = Field(default=True, description="Safe by default; must be armed to execute.")
    rollback_steps: List[ActionStep] = Field(
        default_factory=list,
        description="Compensating actions run automatically if post-action verification fails.")
    verification: Optional[VerificationCheck] = Field(
        default=None, description="Recovery check re-read after a live execution to confirm the fix.")

    @field_validator('steps')
    @classmethod
    def steps_not_empty(cls, v: List[ActionStep]) -> List[ActionStep]:
        if not v:
            raise ValueError("ActionPlan must contain at least one step")
        return v

class SecurityReview(BaseModel):
    """
    A strictly typed review generated by the Reviewer Agent (Nemotron).
    """
    is_safe: bool = Field(description="True if the patch is safe and logically sound.")
    vulnerability_found: bool = Field(description="True if a CVE or malicious payload is detected.")
    feedback: str = Field(description="Feedback or reasons for rejection.")
