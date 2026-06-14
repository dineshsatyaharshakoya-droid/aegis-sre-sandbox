"""
Integration tests for the cloud-tier infra backends against LIVE Redis/Postgres.

These exercise the RedisCache / RedisStreamBroker / PostgresEventStore branches
that unit tests can't reach. They auto-skip when the containers aren't reachable,
so the suite stays green in environments without them.
"""

import asyncio
import json
import socket
import time

import pytest

REDIS_URL = "redis://localhost:6379/0"
PG_DSN = "postgresql://postgres:postgres@localhost:5432/aegis_scratch"


def _reachable(host, port):
    try:
        with socket.create_connection((host, port), timeout=1):
            return True
    except OSError:
        return False


redis_up = pytest.mark.skipif(not _reachable("localhost", 6379), reason="redis not running")
pg_up = pytest.mark.skipif(not _reachable("localhost", 5432), reason="postgres not running")


@redis_up
def test_redis_cache_atomic_claim_live():
    from aegis_sre.infra.cache import RedisCache
    async def go():
        c = RedisCache(REDIS_URL)
        key = f"itest-{time.time()}"
        first = await c.claim(key, 30)
        dup = await c.claim(key, 30)
        await c.set(f"kv-{key}", "v", 30)
        got = await c.get(f"kv-{key}")
        await c.close()
        return first, dup, got
    first, dup, got = asyncio.run(go())
    assert first is True and dup is False and got == "v"


@redis_up
def test_redis_broker_roundtrip_live():
    from aegis_sre.infra.broker import RedisStreamBroker
    async def go():
        b = RedisStreamBroker(redis_url=REDIS_URL, stream=f"itest.{time.time()}",
                              group="itest", consumer="c1")
        await b.publish({"event_id": "x", "service_name": "s"})
        got = None
        async for d in b.consume():
            got = d
            await b.ack(d.id)
            break
        await b.close()
        return got
    got = asyncio.run(go())
    assert got is not None and got.payload["event_id"] == "x"


@pg_up
def test_postgres_store_roundtrip_live():
    from aegis_sre.infra.store import PostgresEventStore
    async def go():
        s = PostgresEventStore(PG_DSN)
        await s.init()
        eid = f"itest-{time.time()}"
        await s.save_incoming_event(eid, "svc", json.dumps({"event_id": eid, "crash_log": "x"}))
        await s.mark_event_status(eid, "completed")
        pend = await s.get_pending_payloads()
        recent = await s.get_recent_incidents(5)
        await s.close()
        return eid, recent
    eid, recent = asyncio.run(go())
    assert any(r["id"] == eid and r["status"] == "completed" for r in recent)


# --- A8/F2: cross-process approval via shared Redis registry ---

@redis_up
def test_redis_approval_registry_cross_process_live():
    """Worker registers in one registry; a SEPARATE API registry (shared Redis)
    can approve it — previously impossible (in-memory per-process)."""
    from aegis_sre.core.approvals import ApprovalRegistry, RedisPendingStore
    from aegis_sre.orchestrator.schemas import CodePatch, TelemetryEvent

    class _VCS:
        async def create_pull_request(self, patch, telemetry):
            return "https://github.com/o/r/pull/42"

    async def go():
        iid = f"xproc-{time.time()}"
        worker_reg = ApprovalRegistry(RedisPendingStore(REDIS_URL))
        api_reg = ApprovalRegistry(RedisPendingStore(REDIS_URL))
        patch = CodePatch(file_path="a.py", target_content="x", replacement_content="y",
                          root_cause_analysis="rc", explanation="e")
        await worker_reg.register(iid, patch, TelemetryEvent(event_id=iid, service_name="s", crash_log="c"))
        r1 = await api_reg.approve(iid, _VCS())          # different instance, shared Redis
        r2 = await api_reg.approve(iid, _VCS())          # idempotent
        return r1, r2

    r1, r2 = asyncio.run(go())
    assert r1["status"] == "deployed" and r1["pr_url"].endswith("/pull/42")
    assert r2["status"] == "already_approved"
