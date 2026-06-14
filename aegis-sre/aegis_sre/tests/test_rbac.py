"""Red-team Batch 3: identity + RBAC + approval attribution (S1)."""

import asyncio

from aegis_sre.config import Settings
from aegis_sre.telemetry.auth import (
    Identity, IdentityRegistry, build_identity_registry)


def test_no_auth_configured_is_open_admin():
    reg = IdentityRegistry({}, "")
    ident = reg.resolve(None)
    assert ident is not None and ident.role == "admin"  # dev posture: open


def test_legacy_token_is_admin():
    reg = IdentityRegistry({}, "secret")
    assert reg.resolve("secret").role == "admin"
    assert reg.resolve("wrong") is None
    assert reg.resolve(None) is None


def test_per_key_roles_and_authorization():
    reg = IdentityRegistry({
        "k_ing": Identity("ci-bot", "ingest"),
        "k_app": Identity("alice", "approver"),
    })
    # ingest key can ingest but NOT approve
    assert reg.authorized("k_ing", "ingest").name == "ci-bot"
    assert reg.authorized("k_ing", "approver") is None
    # approver key can do both
    assert reg.authorized("k_app", "approver").name == "alice"
    assert reg.authorized("k_app", "ingest").name == "alice"
    # unknown key denied
    assert reg.authorized("nope", "ingest") is None


def test_build_registry_parses_keys_and_falls_back():
    s = Settings(profile="onprem", cache_backend="memory", broker_backend="inprocess",
                 store_backend="sqlite")
    object.__setattr__(s, "api_keys", "k1:alice:approver,k2:bot:ingest,bad-entry")
    object.__setattr__(s, "webhook_token", "")
    reg = build_identity_registry(s)
    assert reg.authorized("k1", "approver").name == "alice"
    assert reg.authorized("k2", "ingest").name == "bot"
    assert reg.resolve("k2").role == "ingest"


def test_approval_records_approver_attribution():
    from aegis_sre.core.approvals import ApprovalRegistry
    from aegis_sre.orchestrator.schemas import CodePatch, TelemetryEvent

    class _VCS:
        async def create_pull_request(self, p, t): return "https://x/pull/1"

    reg = ApprovalRegistry()
    patch = CodePatch(file_path="a.py", target_content="x", replacement_content="y",
                      root_cause_analysis="r", explanation="e")
    asyncio.run(reg.register("i1", patch, TelemetryEvent(event_id="i1", service_name="s", crash_log="c")))
    res = asyncio.run(reg.approve("i1", _VCS(), approver="alice"))
    assert res["status"] == "deployed" and res["approver"] == "alice"
