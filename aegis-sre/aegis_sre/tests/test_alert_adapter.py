"""
Tests for the Alertmanager adapter + /webhook/alert endpoint (C4).

The adapter tests are pure; the endpoint test uses the in-memory incident_service
(conftest) so a firing alert flows through real ingest without containers.
"""

from fastapi.testclient import TestClient

from aegis_sre.orchestrator.schemas import SignalKind
from aegis_sre.telemetry.alert_adapter import parse_alertmanager
from aegis_sre.telemetry.api_receiver import app

client = TestClient(app)


def _firing(name="HighErrorRate", service="payments", **labels):
    return {
        "status": "firing",
        "fingerprint": "abc123",
        "labels": {"alertname": name, "service": service, "severity": "critical", **labels},
        "annotations": {"summary": "error rate > 5%", "description": "5xx spiking"},
        "startsAt": "2026-06-14T00:00:00Z",
    }


def test_parse_firing_alert_to_signal():
    [sig] = parse_alertmanager({"alerts": [_firing()]})
    assert sig.kind is SignalKind.METRIC_ALERT
    assert sig.signal_id == "ALERT-abc123"
    assert sig.service_name == "payments"
    assert "HighErrorRate" in sig.body and "error rate > 5%" in sig.body
    assert sig.metadata["severity"] == "critical"
    assert sig.metadata["source"] == "alertmanager"


def test_resolved_alerts_are_skipped():
    payload = {"alerts": [dict(_firing(), status="resolved"), _firing(name="OOMKilled")]}
    sigs = parse_alertmanager(payload)
    assert len(sigs) == 1
    assert "OOMKilled" in sigs[0].body


def test_service_falls_back_through_labels():
    alert = {"status": "firing", "labels": {"alertname": "X", "job": "api"}, "annotations": {}}
    [sig] = parse_alertmanager({"alerts": [alert]})
    assert sig.service_name == "api"


def test_empty_payload_yields_no_signals():
    assert parse_alertmanager({}) == []
    assert parse_alertmanager({"alerts": []}) == []


def test_signal_projects_onto_telemetry_for_the_swarm():
    [sig] = parse_alertmanager({"alerts": [_firing()]})
    ev = sig.to_telemetry()
    assert ev.event_id == "ALERT-abc123"
    assert ev.metadata["signal_kind"] == "metric_alert"
    assert "HighErrorRate" in ev.crash_log


def test_webhook_alert_endpoint_accepts_firing_alert():
    res = client.post("/webhook/alert", json={"alerts": [_firing()]})
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "accepted"
    assert body["source"] == "alertmanager"
    assert body["firing"] == 1 and body["accepted"] == 1


def test_webhook_alert_endpoint_ignores_all_resolved():
    res = client.post("/webhook/alert", json={"alerts": [dict(_firing(), status="resolved")]})
    assert res.status_code == 200
    assert res.json()["status"] == "ignored"


# --- C5: Datadog + PagerDuty adapters ---

from aegis_sre.telemetry.alert_adapter import parse_datadog, parse_pagerduty


def test_parse_datadog_firing():
    [s] = parse_datadog({"id": "dd1", "title": "High CPU", "body": "cpu>90%",
                         "alert_type": "error", "tags": "service:payments,env:prod"})
    assert s.signal_id == "DD-dd1" and s.service_name == "payments"
    assert s.kind is SignalKind.METRIC_ALERT and "High CPU" in s.body


def test_parse_datadog_recovery_skipped():
    assert parse_datadog({"id": "dd2", "title": "ok", "alert_type": "recovery"}) == []


def test_parse_pagerduty_triggered():
    [s] = parse_pagerduty({"event": {"event_type": "incident.triggered",
        "data": {"id": "pd1", "title": "DB down", "urgency": "high",
                 "service": {"summary": "orders-db"}}}})
    assert s.signal_id == "PD-pd1" and s.service_name == "orders-db" and "DB down" in s.body


def test_parse_pagerduty_resolved_skipped():
    assert parse_pagerduty({"event": {"event_type": "incident.resolved", "data": {}}}) == []


def test_datadog_and_pagerduty_endpoints():
    r1 = client.post("/webhook/datadog", json={"id": "dd9", "title": "T", "alert_type": "error",
                                               "tags": "service:api"})
    assert r1.status_code == 200 and r1.json()["source"] == "datadog"
    r2 = client.post("/webhook/pagerduty", json={"event": {"event_type": "incident.triggered",
        "data": {"id": "pd9", "title": "T", "service": {"summary": "api"}}}})
    assert r2.status_code == 200 and r2.json()["source"] == "pagerduty"
    r3 = client.post("/webhook/pagerduty", json={"event": {"event_type": "incident.acknowledged", "data": {}}})
    assert r3.json()["status"] == "ignored"
