"""Tests for the optional OTel tracing layer (A1-A2). Works whether or not the
OTel SDK is installed (asserts the real path when present, the no-op path when not)."""

from aegis_sre.telemetry import tracing


def test_span_context_manager_does_not_raise():
    with tracing.span("x", **{"incident.id": "i1", "service.name": "svc"}):
        pass  # enter/exit cleanly; yields a span (SDK) or None (no-op)


def test_span_propagates_exceptions():
    try:
        with tracing.span("x"):
            raise ValueError("boom")
        assert False
    except ValueError:
        pass


def test_inject_extract_do_not_break_the_payload():
    # inject/extract must never raise and must round-trip a plain dict, whether or
    # not a tracer provider is configured (carries trace context when it is).
    with tracing.span("parent"):
        carrier = tracing.inject({})
    assert isinstance(carrier, dict)
    tracing.extract(carrier)  # returns a Context or None; must not raise


def test_setup_tracing_no_endpoint_is_disabled(monkeypatch):
    # Without OTEL_EXPORTER_OTLP_ENDPOINT, tracing stays off even if the SDK exists.
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    assert tracing.setup_tracing("svc") is False
