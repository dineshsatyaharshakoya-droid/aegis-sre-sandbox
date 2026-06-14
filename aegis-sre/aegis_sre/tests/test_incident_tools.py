"""
Tests for the incident alerting tool (`orchestrator/incident_tools.py`).

No live webhook needed: an `httpx.MockTransport` captures the POST bodies so the
PagerDuty-v2 payload shape, severity coercion, and the trigger/ack/resolve
lifecycle are asserted deterministically. The notifier's never-raise contract is
checked against an unroutable endpoint.
"""

import asyncio

import httpx
import pytest

from aegis_sre.orchestrator import incident_tools
from aegis_sre.orchestrator.incident_tools import IncidentNotifier


def _notifier_capturing(sink: list) -> IncidentNotifier:
    """Notifier whose POSTs are captured into `sink` and answered 200."""
    def handler(request):
        return httpx.Response(200, json={"status": "ok"})

    transport = httpx.MockTransport(handler)
    n = IncidentNotifier("https://hook.test/abc", routing_key="rk-test")

    async def _post(body):
        async with httpx.AsyncClient(transport=transport) as ac:
            resp = await ac.post(n.webhook_url, json=body)
        sink.append(body)  # capture only the JSON body we sent
        return {"ok": resp.is_success, "status": resp.status_code,
                "action": body.get("event_action"), "dedup_key": body.get("dedup_key")}

    n._post = _post  # type: ignore[method-assign]
    return n


def test_trigger_payload_is_pagerduty_v2_shaped():
    bodies: list = []
    n = _notifier_capturing(bodies)
    res = asyncio.run(n.trigger(dedup_key="INC-1", severity="error",
                                description="boom", service="payments"))
    assert res["ok"] and res["action"] == "trigger" and res["dedup_key"] == "INC-1"
    body = bodies[-1]
    assert body["routing_key"] == "rk-test"
    assert body["event_action"] == "trigger"
    assert body["dedup_key"] == "INC-1"
    p = body["payload"]
    assert p["summary"] == "boom"
    assert p["severity"] == "error"
    assert p["source"] == "aegis-sre"
    assert "timestamp" in p
    assert p["custom_details"]["service"] == "payments"  # extra kwargs -> custom_details


def test_invalid_severity_coerced_to_critical():
    bodies: list = []
    n = _notifier_capturing(bodies)
    asyncio.run(n.trigger(dedup_key="INC-2", severity="SEV0", description="x"))
    assert bodies[-1]["payload"]["severity"] == "critical"


def test_acknowledge_and_resolve_lifecycle():
    bodies: list = []
    n = _notifier_capturing(bodies)
    asyncio.run(n.trigger(dedup_key="INC-3", severity="critical", description="d"))
    ack = asyncio.run(n.acknowledge("INC-3", note="owned"))
    res = asyncio.run(n.resolve("INC-3", note="fixed"))
    actions = [b["event_action"] for b in bodies]
    assert actions == ["trigger", "acknowledge", "resolve"]
    assert all(b["dedup_key"] == "INC-3" for b in bodies)
    assert ack["action"] == "acknowledge" and res["action"] == "resolve"
    assert bodies[-1]["payload"]["summary"] == "fixed"


def test_send_never_raises_on_transport_error():
    """A real connection failure returns ok=False instead of throwing."""
    n = IncidentNotifier("http://127.0.0.1:1/")  # unroutable
    n.timeout = 0.5
    out = asyncio.run(n.trigger(dedup_key="INC-4", severity="critical", description="d"))
    assert out["ok"] is False
    assert "error" in out


def test_get_incident_notifier_none_when_unset(monkeypatch):
    incident_tools._notifier = None
    monkeypatch.setattr(incident_tools, "get_settings",
                        lambda: type("S", (), {"alert_webhook_url": "", "alert_routing_key": "x"})())
    assert incident_tools.get_incident_notifier() is None
