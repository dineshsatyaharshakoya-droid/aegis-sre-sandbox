"""Red-team Batch 1: input firewall + abuse controls (P9/P10/P12/P7)."""

import asyncio

from fastapi.testclient import TestClient

from aegis_sre.telemetry import api_receiver as ar

client = TestClient(ar.app)


# --- P10: body size cap ---

def test_oversized_body_rejected():
    monkey = ar.settings
    huge = {"event_id": "big", "service_name": "s", "crash_log": "x"}
    headers = {"Content-Length": str(monkey.max_body_bytes + 1)}
    r = client.post("/webhook/crash", json=huge, headers=headers)
    assert r.status_code == 413


def test_crash_log_truncated(monkeypatch):
    monkeypatch.setattr(ar.settings, "max_crash_log_chars", 50)
    r = client.post("/webhook/crash", json={
        "event_id": "trunc-1", "service_name": "s", "crash_log": "A" * 5000})
    assert r.status_code == 200  # accepted, not rejected
    # the stored/ingested crash_log was capped (we assert via the in-memory store)
    incs = asyncio.run(ar.incident_service.store.get_recent_incidents(5))
    row = next(i for i in incs if i["id"] == "trunc-1")
    assert len(row["crash_log"]) < 200 and "truncated" in row["crash_log"]


# --- P9: X-Forwarded-For only trusted when enabled ---

def test_client_key_ignores_xff_by_default(monkeypatch):
    monkeypatch.setattr(ar.settings, "trust_forwarded_for", False)
    class _Req:
        headers = {"x-forwarded-for": "1.2.3.4"}
        client = type("C", (), {"host": "10.0.0.1"})()
    assert ar._client_key(_Req()) == "10.0.0.1"  # spoofed XFF ignored


def test_client_key_trusts_xff_when_enabled(monkeypatch):
    monkeypatch.setattr(ar.settings, "trust_forwarded_for", True)
    class _Req:
        headers = {"x-forwarded-for": "1.2.3.4, 5.6.7.8"}
        client = type("C", (), {"host": "10.0.0.1"})()
    assert ar._client_key(_Req()) == "1.2.3.4"


# --- P12: WS connection cap ---

def test_ws_connection_cap(monkeypatch):
    monkeypatch.setattr(ar.settings, "max_ws_connections", 1)
    mgr = ar.ConnectionManager()

    class _WS:
        def __init__(self): self.closed = None
        async def accept(self): pass
        async def close(self, code=None): self.closed = code

    a, b = _WS(), _WS()
    assert asyncio.run(mgr.connect(a)) is True
    assert asyncio.run(mgr.connect(b)) is False  # over cap
    assert b.closed == 1013


# --- P7: cloud Sentry fail-closed ---

def test_cloud_sentry_requires_secret(monkeypatch):
    monkeypatch.setattr(ar.settings, "profile", "cloud")
    monkeypatch.setattr(ar.settings, "sentry_secret", "")
    r = client.post("/webhook/sentry", json={"project_name": "x"})
    assert r.status_code == 401
