"""Coverage: /webhook/crash 429 + /webhook/sentry adapter branches."""

import pytest
from fastapi.testclient import TestClient

from aegis_sre.telemetry import api_receiver as ar

client = TestClient(ar.app)


def test_crash_at_capacity_returns_429(monkeypatch):
    async def dropped(_event):
        return {"status": "dropped", "message": "At capacity"}
    monkeypatch.setattr(ar, "_process_telemetry", dropped)
    r = client.post("/webhook/crash", json={
        "event_id": "c1", "service_name": "svc", "crash_log": "boom"})
    assert r.status_code == 429


def test_sentry_full_payload_extracts_stacktrace():
    payload = {
        "project_name": "checkout", "id": "ISSUE-1",
        "data": {"event": {"event_id": "EV-1", "title": "NPE", "culprit": "pay.create",
            "exception": {"values": [{"type": "NullPointerException", "value": "x is null",
                "stacktrace": {"frames": [
                    {"filename": "/app/pay.py", "lineno": 42, "function": "create"}]}}]}}},
        "url": "https://sentry.io/issues/1/"}
    r = client.post("/webhook/sentry", json=payload)
    assert r.status_code == 200 and r.json()["source"] == "sentry"


def test_sentry_minimal_payload_uses_fallback():
    # no exception block -> the title/culprit fallback branch (no stacktrace)
    r = client.post("/webhook/sentry", json={"project_name": "svc", "id": "X"})
    assert r.status_code == 200


def test_sentry_invalid_json_is_400():
    r = client.post("/webhook/sentry", content=b"not-json{",
                    headers={"content-type": "application/json"})
    assert r.status_code == 400


def test_sentry_rate_limited_is_429(monkeypatch):
    async def no(_k): return False
    monkeypatch.setattr(ar, "_rate_ok", no)
    r = client.post("/webhook/sentry", json={"project_name": "svc"})
    assert r.status_code == 429


def test_sentry_invalid_signature_is_401(monkeypatch):
    monkeypatch.setattr(ar.settings, "sentry_secret", "shhh")
    r = client.post("/webhook/sentry", json={"project_name": "svc"})  # no signature header
    assert r.status_code == 401
