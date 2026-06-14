"""Red-team Batch 4: HMAC-signed approval blobs + data-plane fail-closed (S4 / F2)."""

import asyncio

import pytest

from aegis_sre.config import Settings
from aegis_sre.core.approvals import (
    ApprovalRegistry, BlobSigner, InMemoryPendingStore,
    build_approval_registry, data_plane_security_issues)
from aegis_sre.orchestrator.schemas import CodePatch, TelemetryEvent


class _VCS:
    async def create_pull_request(self, patch, tele):
        return "https://github.com/x/pull/7"


def _patch():
    return CodePatch(file_path="a.py", target_content="x", replacement_content="y",
                     root_cause_analysis="rc", explanation="why")


# --- BlobSigner ---

def test_signer_roundtrip_and_tamper():
    s = BlobSigner("topsecret")
    blob = s.wrap('{"hello":"world"}')
    assert blob.startswith("v1:") and s.unwrap(blob) == '{"hello":"world"}'
    # flip a byte in the payload -> rejected
    assert s.unwrap(blob[:-1] + ("Z" if blob[-1] != "Z" else "Y")) is None
    # wrong key -> rejected
    assert BlobSigner("other").unwrap(blob) is None


def test_signer_rejects_unsigned_when_enabled_and_passthrough_when_off():
    assert BlobSigner("k").unwrap('{"forged":true}') is None      # signing on, not signed
    off = BlobSigner("")
    assert off.unwrap('{"x":1}') == '{"x":1}'                      # passthrough plain
    assert off.unwrap("v1:deadbeef:{}") is None                   # signed blob, no key -> distrust


# --- registry refuses a forged Redis entry ---

def test_approve_rejects_forged_entry():
    store = InMemoryPendingStore()
    reg = ApprovalRegistry(store=store, signer=BlobSigner("k"))
    tele = TelemetryEvent(event_id="i1", service_name="s", crash_log="boom")
    # attacker writes an UNSIGNED blob straight into the store, bypassing register()
    import aegis_sre.core.approvals as ap
    asyncio.run(store.register("i1", ap._serialize(_patch(), tele)))
    res = asyncio.run(reg.approve("i1", _VCS()))
    assert res["status"] == "tampered"
    # and it is not left behind to be retried
    assert asyncio.run(store.claim("i1")) is None


def test_signed_entry_approves_normally():
    reg = ApprovalRegistry(store=InMemoryPendingStore(), signer=BlobSigner("k"))
    tele = TelemetryEvent(event_id="i2", service_name="s", crash_log="boom")
    asyncio.run(reg.register("i2", _patch(), tele))           # goes through signer
    res = asyncio.run(reg.approve("i2", _VCS(), approver="alice"))
    assert res["status"] == "deployed" and res["approver"] == "alice"
    # idempotent: the signed approved-record verifies on re-approve
    again = asyncio.run(reg.approve("i2", _VCS()))
    assert again["status"] == "already_approved"


# --- startup data-plane checks ---

def _settings(**kw):
    s = Settings(profile="onprem", store_backend="sqlite", broker_backend="inprocess",
                 cache_backend="memory")
    for k, v in kw.items():
        object.__setattr__(s, k, v)
    return s


def test_onprem_has_no_data_plane_issues():
    assert data_plane_security_issues(_settings()) == []


def test_redis_without_secret_is_fail_closed():
    s = _settings(cache_backend="redis", webhook_token="", approval_signing_secret="")
    levels = [lvl for lvl, _ in data_plane_security_issues(s)]
    assert "error" in levels


def test_redis_without_tls_warns_only_when_secret_present():
    s = _settings(cache_backend="redis", approval_signing_secret="k",
                  redis_url="redis://localhost:6379/0")
    issues = data_plane_security_issues(s)
    assert issues and all(lvl == "warn" for lvl, _ in issues)
    # rediss:// (TLS) with creds -> clean
    s2 = _settings(cache_backend="redis", approval_signing_secret="k",
                   redis_url="rediss://:pw@host:6379/0")
    assert data_plane_security_issues(s2) == []


def test_build_registry_signs_on_redis_tier():
    s = _settings(cache_backend="redis", approval_signing_secret="k")
    reg = build_approval_registry(s)
    assert reg._signer.enabled
