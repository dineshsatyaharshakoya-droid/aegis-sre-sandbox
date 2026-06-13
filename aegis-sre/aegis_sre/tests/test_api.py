from fastapi.testclient import TestClient
from aegis_sre.telemetry.api_receiver import app

client = TestClient(app)

def test_health_check():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "healthy", "service": "Aegis SRE"}

def test_receive_crash_telemetry():
    payload = {
        "event_id": "test-webhook-001",
        "service_name": "datadog-payment-service",
        "crash_log": "Traceback (most recent call last):\n  File 'main.py', line 1, in <module>\n    1/0\nZeroDivisionError: division by zero",
        "metadata": {"source": "datadog"}
    }
    
    # 1. First payload should be accepted
    response = client.post("/webhook/crash", json=payload)
    assert response.status_code == 200
    assert response.json()["status"] == "accepted"
    
    # 2. Duplicate payload should be rejected by idempotency cache
    response2 = client.post("/webhook/crash", json=payload)
    assert response2.status_code == 200
    assert response2.json()["status"] == "ignored"
    assert response2.json()["reason"] == "duplicate_event"
