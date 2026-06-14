"""
Tool registry with risk classes (Stone 2, C1) — the substrate the whole
actionable vision stands on.

SCALE_PLAN throughline: "actions run through MCP under approval + safety gates",
with "a tool registry with a risk class per tool (read vs act)". Stone 3's policy
engine derives its approval tier from that risk class, so this registry is the
keystone for the sellable product — every tool the agent can call is declared
here with its blast-risk, and the gate keys off it.

Risk classes (read/act from the plan, plus a notify tier for outbound comms):
  * READ   — observe only; no side effects; never gated (Prometheus, logs).
  * NOTIFY — outbound notification only; no managed-state mutation; not gated
             (incident alerting).
  * ACT    — mutates managed code/infra; GATED: requires human approval
             (GitOps PR, and later kubectl apply / scale / cordon).

This module classifies the tools Aegis already has (built ad-hoc before the
registry existed) so they become first-class, policy-gateable entries.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Callable, List, Optional


class RiskClass(str, Enum):
    READ = "read"
    NOTIFY = "notify"
    ACT = "act"


@dataclass
class Tool:
    name: str
    risk: RiskClass
    description: str
    handler: Optional[Callable] = None

    @property
    def requires_approval(self) -> bool:
        """ACT tools mutate managed state and must pass the human/approval gate
        (Stone-3 policy). READ/NOTIFY do not."""
        return self.risk is RiskClass.ACT


class ToolRegistry:
    """An in-process registry of callable tools, each tagged with a risk class."""

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, name: str, risk: RiskClass, description: str,
                 handler: Optional[Callable] = None) -> Tool:
        if not isinstance(risk, RiskClass):
            raise ValueError(f"risk must be a RiskClass, got {risk!r}")
        if name in self._tools:
            raise ValueError(f"tool already registered: {name!r}")
        tool = Tool(name=name, risk=risk, description=description, handler=handler)
        self._tools[name] = tool
        return tool

    def get(self, name: str) -> Tool:
        if name not in self._tools:
            raise KeyError(f"no such tool: {name!r}")
        return self._tools[name]

    def list(self, risk: Optional[RiskClass] = None) -> List[Tool]:
        tools = list(self._tools.values())
        if risk is not None:
            tools = [t for t in tools if t.risk is risk]
        return sorted(tools, key=lambda t: t.name)

    def requires_approval(self, name: str) -> bool:
        return self.get(name).requires_approval

    def gated_tools(self) -> List[Tool]:
        """The tools the Stone-3 policy must gate (everything that can mutate)."""
        return self.list(risk=RiskClass.ACT)

    async def invoke(self, name: str, **kwargs):
        """Call a tool's handler, recording per-tool call count + latency (C6).
        Raises if the tool is unknown or has no handler; records result=error and
        re-raises if the handler itself fails."""
        import time
        from aegis_sre.telemetry import metrics

        tool = self.get(name)
        if tool.handler is None:
            raise ValueError(f"tool {name!r} has no handler")
        started = time.monotonic()
        try:
            result = await tool.handler(**kwargs)
        except Exception:
            metrics.tool_calls.labels(tool=name, result="error").inc()
            metrics.tool_latency.labels(tool=name).observe(time.monotonic() - started)
            raise
        metrics.tool_calls.labels(tool=name, result="ok").inc()
        metrics.tool_latency.labels(tool=name).observe(time.monotonic() - started)
        return result


# --- handlers (thin, lazy wrappers over the existing typed tools) -------------


async def _prometheus_query(promql: str, **kwargs):
    from aegis_sre.orchestrator.metrics_tools import get_metrics_client
    client = get_metrics_client()
    if client is None:
        raise RuntimeError("PROMETHEUS_URL not configured")
    return await client.query(promql, **kwargs)


async def _prometheus_query_range(promql: str, start: float, end: float, step: str = "60s"):
    from aegis_sre.orchestrator.metrics_tools import get_metrics_client
    client = get_metrics_client()
    if client is None:
        raise RuntimeError("PROMETHEUS_URL not configured")
    return await client.query_range(promql, start=start, end=end, step=step)


async def _logs_query(logql: str, **kwargs):
    from aegis_sre.orchestrator.logs_tools import get_logs_client
    client = get_logs_client()
    if client is None:
        raise RuntimeError("LOKI_URL not configured")
    return await client.query(logql, **kwargs)


async def _gitops_create_pull_request(patch, telemetry):
    from aegis_sre.orchestrator.vcs_provider import get_vcs_provider
    return await get_vcs_provider().create_pull_request(patch, telemetry)


def _incident_handler(action: str):
    async def handler(**kwargs):
        from aegis_sre.orchestrator.incident_tools import get_incident_notifier
        notifier = get_incident_notifier()
        if notifier is None:
            raise RuntimeError("ALERT_WEBHOOK_URL not configured")
        if action == "trigger":
            return await notifier.trigger(**kwargs)
        if action == "acknowledge":
            return await notifier.acknowledge(kwargs.pop("dedup_key"), **kwargs)
        return await notifier.resolve(kwargs.pop("dedup_key"), **kwargs)
    return handler


def build_default_registry() -> ToolRegistry:
    """Register the tools Aegis ships today, classified by risk."""
    reg = ToolRegistry()
    # READ — the observability "eyes" (Stone 2).
    reg.register("prometheus.query", RiskClass.READ,
                 "Run an instant PromQL query.", handler=_prometheus_query)
    reg.register("prometheus.query_range", RiskClass.READ,
                 "Run a range PromQL query.", handler=_prometheus_query_range)
    reg.register("logs.query", RiskClass.READ,
                 "Query recent logs via LogQL.", handler=_logs_query)
    # NOTIFY — outbound incident comms (the "voice"). Wired to the IncidentNotifier.
    reg.register("incident.trigger", RiskClass.NOTIFY, "Fire an incident alert.",
                 handler=_incident_handler("trigger"))
    reg.register("incident.acknowledge", RiskClass.NOTIFY, "Acknowledge an incident.",
                 handler=_incident_handler("acknowledge"))
    reg.register("incident.resolve", RiskClass.NOTIFY, "Resolve an incident.",
                 handler=_incident_handler("resolve"))
    # ACT — mutates managed state; GATED behind approval (the "hands").
    reg.register("gitops.create_pull_request", RiskClass.ACT,
                 "Open a fix PR (clone -> branch -> patch -> push -> PR).",
                 handler=_gitops_create_pull_request)
    # ACT — Kubernetes remediation tools (kubectl-backed).
    from aegis_sre.orchestrator.k8s_tools import K8S_ACT_TOOLS
    for name, handler in K8S_ACT_TOOLS.items():
        verb = name.split(".", 1)[1].replace("_", " ")
        reg.register(name, RiskClass.ACT, f"Kubernetes: {verb}.", handler=handler)
    return reg


_registry: Optional[ToolRegistry] = None


def get_tool_registry() -> ToolRegistry:
    """Process-wide default registry."""
    global _registry
    if _registry is None:
        _registry = build_default_registry()
    return _registry
