"""
OpenTelemetry tracing for Aegis (A1-A2).

`opentelemetry` is an OPTIONAL dependency: when it's absent every span becomes a
no-op and the inject/extract helpers do nothing, so the zero-SaaS on-prem build
runs with no extra deps. Installing the OTel SDK (+ an OTLP endpoint via
`OTEL_EXPORTER_OTLP_ENDPOINT`) turns the same call sites into real spans — no code
changes needed, exactly like `telemetry/metrics.py`.

Two pieces:
  * `span(name, **attrs)` — a context manager wrapping a unit of work
    (ingest -> process -> graph), recording exceptions + error status.
  * `inject(carrier)` / `extract(carrier)` — carry the trace context through the
    broker payload so a worker's processing span links to the API's ingest span
    (the ingest->broker->worker hop, A2).
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Optional

from aegis_sre.telemetry.logger import logger

try:  # pragma: no cover - exercised only when the OTel SDK is installed
    from opentelemetry import trace, propagate
    from opentelemetry.trace import Status, StatusCode
    _tracer = trace.get_tracer("aegis-sre")
    OTEL_AVAILABLE = True
except Exception:  # noqa: BLE001 - any import failure -> no-op tracing
    OTEL_AVAILABLE = False


@contextmanager
def span(name: str, context=None, **attributes):
    """Start a span around a unit of work. `context` (from extract()) links it to
    a parent span across processes. No-op (yields None) without the SDK."""
    if not OTEL_AVAILABLE:
        yield None
        return
    with _tracer.start_as_current_span(name, context=context) as s:  # pragma: no cover
        for k, v in attributes.items():
            if v is not None:
                s.set_attribute(k, v)
        try:
            yield s
        except Exception as e:
            s.record_exception(e)
            s.set_status(Status(StatusCode.ERROR))
            raise


def inject(carrier: dict) -> dict:
    """Write the current trace context into `carrier` (e.g. a broker payload)."""
    if OTEL_AVAILABLE:  # pragma: no cover
        propagate.inject(carrier)
    return carrier


def extract(carrier: dict):
    """Return a context extracted from `carrier`, or None when tracing is off."""
    if OTEL_AVAILABLE and carrier:  # pragma: no cover
        return propagate.extract(carrier)
    return None


def setup_tracing(service_name: str = "aegis-sre") -> bool:
    """Best-effort tracer-provider setup from env (OTEL_EXPORTER_OTLP_ENDPOINT).
    Returns True if real tracing is active. Safe to call when the SDK is absent."""
    if not OTEL_AVAILABLE:
        return False
    try:  # pragma: no cover - needs the OTel SDK + an endpoint
        import os
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        if not os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"):
            return False
        provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
        trace.set_tracer_provider(provider)
        logger.info("tracing_enabled", service=service_name)
        return True
    except Exception as e:  # noqa: BLE001
        logger.warning("tracing_setup_failed", error=str(e))
        return False
