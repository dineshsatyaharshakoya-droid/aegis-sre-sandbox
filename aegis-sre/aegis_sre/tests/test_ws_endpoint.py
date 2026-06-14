"""Coverage: /ws endpoint — auth, RBAC gating, approve/reject flow, _ws_token."""

import asyncio

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from aegis_sre.telemetry import api_receiver as ar
from aegis_sre.telemetry.auth import Identity, IdentityRegistry
from aegis_sre.orchestrator.schemas import CodePatch, TelemetryEvent

client = TestClient(ar.app)


def _patch():
    return CodePatch(file_path="a.py", target_content="x", replacement_content="y",
                     root_cause_analysis="rc", explanation="why")


# --- _ws_token ---

def test_ws_token_prefers_subprotocol_then_url():
    class _WS:
        def __init__(self, headers, qp): self.headers = headers; self.query_params = qp
    assert ar._ws_token(_WS({"sec-websocket-protocol": "aegis, tok123"}, {})) == "tok123"
    assert ar._ws_token(_WS({}, {"token": "urltok"})) == "urltok"   # deprecated fallback
    assert ar._ws_token(_WS({}, {})) is None


# --- auth gate ---

def test_ws_unauthorized_closed(monkeypatch):
    monkeypatch.setattr(ar, "_identity", IdentityRegistry({}, "s3cret"))
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws"):    # no token -> closed before accept
            pass


# --- RBAC: ingest identity may watch but not approve ---

def test_ws_ingest_role_cannot_approve(monkeypatch):
    monkeypatch.setattr(ar, "_identity", IdentityRegistry({"k": Identity("bot", "ingest")}))
    with client.websocket_connect("/ws", subprotocols=["aegis", "k"]) as ws:
        ws.send_json({"action": "approve_patch", "incident_id": "i1"})
        msg = ws.receive_json()
        assert msg["type"] == "error" and "approver" in msg["message"]


# --- approve happy path (default identity = anonymous admin when no auth set) ---

def test_ws_approve_deploys_codepatch(monkeypatch):
    class _VCS:
        async def create_pull_request(self, p, t): return "https://github.com/x/pull/9"
    monkeypatch.setattr("aegis_sre.orchestrator.vcs_provider.get_vcs_provider", lambda: _VCS())
    tele = TelemetryEvent(event_id="wsi", service_name="s", crash_log="boom")
    asyncio.run(ar.approval_registry.register("wsi", _patch(), tele))

    with client.websocket_connect("/ws") as ws:
        ws.send_json({"action": "approve_patch", "incident_id": "wsi"})
        msg = ws.receive_json()      # broadcast reaches this client
        assert msg["type"] == "patch_deployed" and msg["pr_url"].endswith("/pull/9")


def test_ws_approve_missing_incident_id():
    with client.websocket_connect("/ws") as ws:
        ws.send_json({"action": "approve_patch"})
        msg = ws.receive_json()
        assert msg["type"] == "error" and "incident_id" in msg["message"]


def test_ws_approve_not_found_reports_status():
    with client.websocket_connect("/ws") as ws:
        ws.send_json({"action": "approve_patch", "incident_id": "ghost"})
        msg = ws.receive_json()
        assert msg["type"] == "approval_result" and msg["status"] == "not_found"


def test_ws_reject_broadcasts(monkeypatch):
    tele = TelemetryEvent(event_id="rj", service_name="s", crash_log="boom")
    asyncio.run(ar.approval_registry.register("rj", _patch(), tele))
    with client.websocket_connect("/ws") as ws:
        ws.send_json({"action": "reject_patch", "incident_id": "rj"})
        msg = ws.receive_json()
        assert msg["type"] == "patch_rejected" and msg["incident_id"] == "rj"
