"""
Prometheus metrics for Aegis.

`prometheus_client` is an OPTIONAL dependency: when it is not installed every
metric becomes a no-op and `/metrics` returns an empty body, so the zero-SaaS
on-prem build keeps running with no extra deps. Installing `prometheus-client`
turns the same call sites into real instrumentation — no code changes needed.

Call sites stay identical in both modes, e.g.:

    from aegis_sre.telemetry import metrics
    metrics.events_ingested.labels(status="accepted").inc()
    metrics.repair_duration.observe(elapsed)
"""

from __future__ import annotations

try:
    from prometheus_client import Counter, Histogram, Gauge, generate_latest, CONTENT_TYPE_LATEST
    PROMETHEUS_AVAILABLE = True
except Exception:  # noqa: BLE001 - any import failure -> degrade to no-op  # pragma: no cover
    PROMETHEUS_AVAILABLE = False

    class _Noop:  # pragma: no cover
        """Mimics Counter/Histogram/Gauge so call sites never branch."""

        def labels(self, *a, **k) -> "_Noop":
            return self

        def inc(self, *a, **k) -> None:
            pass

        def observe(self, *a, **k) -> None:
            pass

        def set(self, *a, **k) -> None:
            pass

    def Counter(*a, **k) -> _Noop:  # type: ignore[misc]
        return _Noop()

    def Histogram(*a, **k) -> _Noop:  # type: ignore[misc]
        return _Noop()

    def Gauge(*a, **k) -> _Noop:  # type: ignore[misc]
        return _Noop()

    CONTENT_TYPE_LATEST = "text/plain; charset=utf-8"

    def generate_latest(*a, **k) -> bytes:  # type: ignore[misc]
        return b""


# --- Ingest (API process) --------------------------------------------------
events_ingested = Counter(
    "aegis_events_ingested_total",
    "Telemetry events by ingest outcome",
    ["status"],  # accepted | ignored | dropped
)
queue_depth = Gauge(
    "aegis_queue_depth",
    "Current in-process broker queue depth (on-prem tier)",
)

# --- Process (worker / consumer) -------------------------------------------
incidents_processed = Counter(
    "aegis_incidents_processed_total",
    "Incidents by terminal result",
    ["result"],  # completed | failed
)
repair_duration = Histogram(
    "aegis_repair_duration_seconds",
    "End-to-end repair processing time per incident",
    buckets=(0.5, 1, 2, 5, 10, 20, 30, 60, 90, 120, 180),
)

# --- Repair swarm internals ------------------------------------------------
patches_generated = Counter(
    "aegis_patches_generated_total",
    "Patches produced by the executor",
)
sandbox_validations = Counter(
    "aegis_sandbox_validations_total",
    "Sandbox validations by result",
    ["result"],  # success | failed
)
patches_deployed = Counter(
    "aegis_patches_deployed_total",
    "Human-approved patches that opened a PR",
)
actions_executed = Counter(
    "aegis_actions_executed_total",
    "Remediations by type and outcome",
    ["type", "result"],  # type: code_patch|action_plan · result: deployed|dry_run|live|rolled_back|blocked|error
)

# --- MCP / tool registry (C6) ----------------------------------------------
tool_calls = Counter(
    "aegis_tool_calls_total",
    "Registry tool invocations by tool and result",
    ["tool", "result"],  # result: ok | error
)
tool_latency = Histogram(
    "aegis_tool_latency_seconds",
    "Registry tool invocation latency",
    ["tool"],
    buckets=(0.01, 0.05, 0.1, 0.5, 1, 2, 5, 10),
)

# --- Prompt-injection / unsafe-remediation defense (Batch 2) ----------------
injection_flags = Counter(
    "aegis_injection_flags_total",
    "Suspected prompt-injection inputs / unsafe-remediation vetoes",
    ["stage"],  # input | code_patch | action_plan
)

# --- Security --------------------------------------------------------------
auth_rejections = Counter(
    "aegis_auth_rejections_total",
    "Rejected webhook/WS requests",
    ["reason"],  # rate_limit | unauthorized | sentry_signature
)


def render() -> "tuple[bytes, str]":
    """Return (body, content_type) for the /metrics endpoint."""
    return generate_latest(), CONTENT_TYPE_LATEST
