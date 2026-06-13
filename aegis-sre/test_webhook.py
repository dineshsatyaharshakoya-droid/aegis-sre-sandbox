import pytest
from fastapi.testclient import TestClient
from aegis_sre.telemetry.api_receiver import app

client = TestClient(app)

def test_health_check():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "healthy", "service": "Aegis SRE"}

def test_sentry_webhook_idempotency():
    payload = {
        "action": "created",
        "project_name": "test-service",
        "id": "1111",
        "data": {
            "event": {
                "event_id": "CRASH-TEST-555",
                "title": "Exception: Test Error",
                "exception": {
                    "values": [{"type": "Exception", "value": "Test Error", "stacktrace": {"frames": []}}]
                }
            }
        }
    }
    
    # First request should be accepted
    response1 = client.post("/webhook/sentry", json=payload)
    assert response1.status_code == 200
    assert response1.json()["status"] == "accepted"
    
    # Second request with exact same payload should be ignored (Idempotency)
    response2 = client.post("/webhook/sentry", json=payload)
    assert response2.status_code == 200
    assert response2.json()["status"] == "ignored"
    assert response2.json()["reason"] == "duplicate_event"

def test_get_incidents_history():
    response = client.get("/incidents")
    assert response.status_code == 200
    assert "incidents" in response.json()
    assert isinstance(response.json()["incidents"], list)
