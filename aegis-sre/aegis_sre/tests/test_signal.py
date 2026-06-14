"""
Tests for the Stone-1 `Signal` model and its `TelemetryEvent` adapter (B1).

The contract for B1 is "additive, zero behavior change on the crash path": the
adapter must be lossless for crashes, and a non-crash signal must still project
onto a `TelemetryEvent` the existing graph can run (kind preserved in metadata).
"""

from aegis_sre.orchestrator.schemas import Signal, SignalKind, TelemetryEvent


def test_signal_defaults_to_crash():
    s = Signal(signal_id="i1", service_name="svc", body="boom")
    assert s.kind is SignalKind.CRASH


def test_from_telemetry_maps_crash_fields():
    ev = TelemetryEvent(event_id="e1", service_name="payments",
                        crash_log="KeyError: 'x'", metadata={"pod": "p-1"})
    s = Signal.from_telemetry(ev)
    assert s.signal_id == "e1"
    assert s.service_name == "payments"
    assert s.kind is SignalKind.CRASH
    assert s.body == "KeyError: 'x'"
    assert s.metadata == {"pod": "p-1"}


def test_crash_roundtrip_is_lossless():
    ev = TelemetryEvent(event_id="e2", service_name="svc",
                        crash_log="Traceback ...", metadata={"region": "us-east"})
    back = Signal.from_telemetry(ev).to_telemetry()
    assert back.event_id == ev.event_id
    assert back.service_name == ev.service_name
    assert back.crash_log == ev.crash_log
    # metadata preserved; signal_kind tag added without clobbering existing keys
    assert back.metadata["region"] == "us-east"
    assert back.metadata["signal_kind"] == "crash"


def test_noncrash_signal_projects_onto_telemetry_with_kind_preserved():
    s = Signal(signal_id="a1", service_name="api", kind=SignalKind.METRIC_ALERT,
               body="error rate > 5% for 5m", metadata={"alertname": "HighErrorRate"})
    ev = s.to_telemetry()
    assert ev.event_id == "a1"
    assert ev.crash_log == "error rate > 5% for 5m"          # body -> crash_log
    assert ev.metadata["signal_kind"] == "metric_alert"      # kind not lost
    assert ev.metadata["alertname"] == "HighErrorRate"


def test_adapter_does_not_mutate_source():
    ev = TelemetryEvent(event_id="e3", service_name="svc", crash_log="x", metadata={"k": "v"})
    Signal.from_telemetry(ev).to_telemetry()
    assert ev.metadata == {"k": "v"}  # no signal_kind leaked back into the original


def test_explicit_signal_kind_tag_in_metadata_is_respected():
    # If a caller already set signal_kind, to_telemetry must not overwrite it.
    s = Signal(signal_id="a2", service_name="api", kind=SignalKind.LOG_ANOMALY,
               body="anomaly", metadata={"signal_kind": "custom"})
    assert s.to_telemetry().metadata["signal_kind"] == "custom"
