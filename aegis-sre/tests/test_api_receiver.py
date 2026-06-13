import pytest
from httpx import AsyncClient, ASGITransport
from aegis_sre.telemetry.api_receiver import app

@pytest.mark.asyncio
async def test_health_check():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        response = await ac.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "healthy", "service": "Aegis SRE"}

@pytest.mark.asyncio
async def test_webhook_idempotency():
    payload = {
        "event_id": "TEST-EVENT-1",
        "service_name": "test-service",
        "crash_log": "Traceback (most recent call last):\nValueError: Core System Failure",
        "timestamp": 1234567890
    }
    
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        # First request should be accepted
        res1 = await ac.post("/webhook/crash", json=payload)
        assert res1.status_code == 200
        assert res1.json()["status"] == "accepted"
        
        # Immediate duplicate should be ignored
        res2 = await ac.post("/webhook/crash", json=payload)
        assert res2.status_code == 200
        assert res2.json()["status"] == "ignored"
        assert res2.json()["reason"] == "duplicate_event"
