"""
Tests for the hardening pass (reliability / performance / correctness).

Covers the layers changed in this pass without requiring Redis, Postgres, an
LLM, or the LangGraph stack:

  - config profile derivation + absolute state-DB path
  - InMemoryCache atomic claim (TTL + LRU bound)
  - SqliteEventStore lifecycle (real sqlite3)
  - InProcessBroker publish / consume / ack balance + back-pressure
  - RedisStreamBroker orphan recovery via XAUTOCLAIM (with an in-memory fake)
  - factory backend selection + cloud fail-fast
  - IncidentService.ingest (dedup -> persist -> publish, drop on full)
  - ConsumerRunner at-least-once ack ordering + poison-message handling

Runs under pytest, or standalone: `python -m aegis_sre.tests.test_hardening`.
"""

from __future__ import annotations

import asyncio
import os
import tempfile

from aegis_sre.config import Settings
from aegis_sre.infra.cache import InMemoryCache
from aegis_sre.infra.broker import InProcessBroker, RedisStreamBroker, Delivery
from aegis_sre.infra.store import SqliteEventStore
from aegis_sre.infra import factory
from aegis_sre.core.service import IncidentService, ConsumerRunner, compute_signature
from aegis_sre.orchestrator.schemas import TelemetryEvent


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


async def _next(broker):
    """Pull one delivery and close the consume() generator (no dangling tasks)."""
    agen = broker.consume()
    try:
        return await agen.__anext__()
    finally:
        await agen.aclose()


def _event(eid="e1", svc="svc", log="boom"):
    return TelemetryEvent(event_id=eid, service_name=svc, crash_log=log)


# --------------------------------------------------------------------------- #
# config
# --------------------------------------------------------------------------- #
def test_config_profile_derivation_and_state_path(monkeypatch):
    # Hermetic: backend *derivation* must be tested without ambient overrides.
    # A local .env may set AEGIS_STORE/BROKER/CACHE (e.g. scratch Redis/Postgres),
    # which config.py loads via load_dotenv(); clear them so we test the defaults.
    for var in ("AEGIS_STORE", "AEGIS_BROKER", "AEGIS_CACHE"):
        monkeypatch.delenv(var, raising=False)

    cloud = Settings(profile="cloud")
    assert cloud.store_backend == "postgres"
    assert cloud.broker_backend == "redis"
    assert cloud.cache_backend == "redis"

    onprem = Settings(profile="onprem")
    assert onprem.store_backend == "sqlite"
    assert onprem.broker_backend == "inprocess"

    # New: checkpointer path is absolute (no longer cwd-relative "aegis_state.db").
    assert os.path.isabs(onprem.state_db_path)
    assert onprem.state_db_path.endswith("aegis_state.db")


# --------------------------------------------------------------------------- #
# cache
# --------------------------------------------------------------------------- #
def test_inmemory_cache_claim_ttl_and_lru():
    async def go():
        c = InMemoryCache(max_size=3)
        assert await c.claim("k", ttl_seconds=100) is True       # first wins
        assert await c.claim("k", ttl_seconds=100) is False      # duplicate
        # expiry: ttl=0 means the prior claim is already stale -> re-claimable
        assert await c.claim("k", ttl_seconds=0) is True
        # LRU bound: insert > max_size unique keys, size stays capped
        for i in range(10):
            await c.claim(f"u{i}", ttl_seconds=100)
        assert len(c._claims) <= 3
    _run(go())


# --------------------------------------------------------------------------- #
# store
# --------------------------------------------------------------------------- #
def test_sqlite_store_lifecycle():
    async def go():
        with tempfile.TemporaryDirectory() as d:
            store = SqliteEventStore(os.path.join(d, "ev.db"))
            await store.init()
            await store.save_incoming_event("e1", "svc", '{"crash_log":"x"}')
            assert await store.get_pending_payloads() == ['{"crash_log":"x"}']
            await store.mark_event_status("e1", "completed")
            assert await store.get_pending_payloads() == []
            recent = await store.get_recent_incidents(10)
            assert recent and recent[0]["status"] == "completed"
            # INSERT OR IGNORE: re-saving the same id must not duplicate / reset
            await store.save_incoming_event("e1", "svc", '{"crash_log":"y"}')
            assert len(await store.get_recent_incidents(10)) == 1
    _run(go())


# --------------------------------------------------------------------------- #
# in-process broker
# --------------------------------------------------------------------------- #
def test_inprocess_broker_ack_balance_and_backpressure():
    async def go():
        b = InProcessBroker(max_size=2)
        assert await b.publish({"event_id": "1"}) is True
        assert await b.publish({"event_id": "2"}) is True
        assert await b.publish({"event_id": "3"}) is False        # full -> back-pressure

        seen = []
        for _ in range(2):
            d = await _next(b)
            seen.append(d.payload["event_id"])
            await b.ack(d.id)                                      # one task_done per item
        assert seen == ["1", "2"]
        # ack balance is correct iff join() returns immediately.
        await asyncio.wait_for(b._queue.join(), timeout=1.0)
    _run(go())


# --------------------------------------------------------------------------- #
# redis stream broker — orphan recovery (the reliability fix)
# --------------------------------------------------------------------------- #
class FakeRedis:
    """Minimal in-memory Redis Streams stand-in for the consumer-group paths used
    by RedisStreamBroker. Enough to exercise XREADGROUP / XACK / XAUTOCLAIM."""

    def __init__(self):
        self.entries = {}        # msg_id -> fields (None == tombstone/deleted)
        self.delivered = set()   # ids handed out via xreadgroup ">"
        self.pel = {}            # msg_id -> {"consumer":..., "time":epoch}
        self._seq = 0

    async def xgroup_create(self, *a, **k):
        return True

    async def xadd(self, stream, fields, **k):
        self._seq += 1
        mid = f"{self._seq}-0"
        self.entries[mid] = dict(fields)
        return mid

    async def xreadgroup(self, group, consumer, streams, count=1, block=0):
        out = []
        for mid in list(self.entries):
            if mid in self.delivered:
                continue
            self.delivered.add(mid)
            self.pel[mid] = {"consumer": consumer, "time": __import__("time").time()}
            out.append((mid, self.entries[mid]))
            if len(out) >= count:
                break
        return [("stream", out)] if out else []

    async def xack(self, stream, group, mid):
        self.pel.pop(mid, None)
        return 1

    async def xautoclaim(self, stream, group, consumer, min_idle_time=0,
                         start_id="0-0", count=10):
        import time as _t
        now = _t.time()
        claimed = []
        for mid, meta in list(self.pel.items()):
            idle_ms = (now - meta["time"]) * 1000
            if idle_ms >= min_idle_time:
                meta["consumer"] = consumer
                claimed.append((mid, self.entries.get(mid)))  # None if tombstoned
                if len(claimed) >= count:
                    break
        return ["0-0", claimed, []]

    async def aclose(self):
        pass


def test_redis_broker_reclaims_orphaned_delivery():
    async def go():
        fake = FakeRedis()
        broker = RedisStreamBroker("redis://x", "s", "g", "consumerB", claim_idle_ms=50)
        broker._client = fake  # inject; skip real connection

        # Producer publishes; "consumerA" reads it then crashes without ACK.
        await broker.publish({"event_id": "orphan"})
        await fake.xreadgroup("g", "consumerA", {"s": ">"})
        assert "1-0" in fake.pel and fake.pel["1-0"]["consumer"] == "consumerA"

        # Age the PEL entry past the idle threshold (simulate the crash window).
        fake.pel["1-0"]["time"] = 0

        reclaimed = await broker._reclaim_idle(fake)
        assert [d.payload["event_id"] for d in reclaimed] == ["orphan"]
        assert fake.pel["1-0"]["consumer"] == "consumerB"  # now owned by us

        # Tombstone path: a claimed-but-deleted entry is ACKed and skipped.
        fake.entries["1-0"] = None
        fake.pel["1-0"]["time"] = 0
        assert await broker._reclaim_idle(fake) == []
        assert "1-0" not in fake.pel
    _run(go())


# --------------------------------------------------------------------------- #
# factory
# --------------------------------------------------------------------------- #
def test_factory_cloud_failfast_without_dsn():
    s = Settings(profile="cloud", database_url="")
    try:
        factory.build_store(s)
        assert False, "expected ValueError for missing AEGIS_DATABASE_URL"
    except ValueError:
        pass
    # Broker selection honours the cloud profile.
    assert isinstance(factory.build_broker(s), RedisStreamBroker)


# --------------------------------------------------------------------------- #
# incident service (ingest) + consumer runner (process)
# --------------------------------------------------------------------------- #
def _service(tmpdir, queue_max=10):
    s = Settings(profile="onprem", sqlite_path=os.path.join(tmpdir, "ev.db"))
    store = SqliteEventStore(s.sqlite_path)
    broker = InProcessBroker(max_size=queue_max)
    cache = InMemoryCache()
    return IncidentService(store=store, broker=broker, cache=cache, settings=s), store, broker


def test_ingest_accept_dedup_and_drop():
    async def go():
        with tempfile.TemporaryDirectory() as d:
            svc, store, broker = _service(d, queue_max=1)
            await svc.init()

            r1 = await svc.ingest(_event("a"))
            assert r1["status"] == "accepted"
            # identical signature -> deduped by atomic claim
            r2 = await svc.ingest(_event("a"))
            assert r2["status"] == "ignored"
            # distinct event but queue (max=1) is full -> back-pressure "dropped"
            r3 = await svc.ingest(_event("b", log="different"))
            assert r3["status"] == "dropped"
    _run(go())


def test_consumer_runner_ack_after_status_and_poison():
    async def go():
        with tempfile.TemporaryDirectory() as d:
            svc, store, broker = _service(d)
            await svc.init()

            processed = []

            async def processor(ev):
                processed.append(ev.event_id)

            runner = ConsumerRunner(broker, store, processor, timeout_seconds=5)

            # Good event: persisted pending, then processed + acked + completed.
            await svc.ingest(_event("ok"))
            good = await _next(broker)
            await runner._handle(good)
            assert processed == ["ok"]
            recent = {r["id"]: r["status"] for r in await store.get_recent_incidents(10)}
            assert recent["ok"] == "completed"

            # Poison (undeserializable) delivery, delivered via the broker so the
            # ack balances the queue: acked and dropped, processor untouched.
            await broker.publish({"not": "an event"})
            poison = await _next(broker)
            await runner._handle(poison)
            assert processed == ["ok"]

            # Failing processor -> status 'failed', still acked (no redelivery storm).
            await svc.ingest(_event("bad", log="kaboom"))
            bad = await _next(broker)

            async def boom(ev):
                raise RuntimeError("processor blew up")

            runner.processor = boom
            await runner._handle(bad)
            recent = {r["id"]: r["status"] for r in await store.get_recent_incidents(10)}
            assert recent["bad"] == "failed"

            # All deliveries were acked -> queue fully drained.
            await asyncio.wait_for(broker._queue.join(), timeout=1.0)
    _run(go())


def test_compute_signature_is_stable_and_tail_based():
    a = compute_signature("svc", "x" * 500)
    b = compute_signature("svc", "y" + "x" * 500)  # same 200-char tail
    assert a == b
    assert a != compute_signature("other", "x" * 500)


# --------------------------------------------------------------------------- #
# P0-1/2/3 — sandbox: patch application, behavioral test, fail-closed
# --------------------------------------------------------------------------- #
from aegis_sre.orchestrator.schemas import PatchProposal
from aegis_sre.orchestrator.sandbox_engine import (
    apply_patch_to_source, PatchApplicationError,
    LocalProcessEngine, E2BEngine, get_sandbox_engine,
)


def _patch(file_path="app.py", target="old", replacement="new"):
    return PatchProposal(
        file_path=file_path, target_content=target, replacement_content=replacement,
        root_cause_analysis="rca", explanation="why",
    )


def test_apply_patch_to_source_cases():
    src = "def f():\n    return old_value\n"
    # single occurrence -> replaced in context (full file returned)
    out = apply_patch_to_source(_patch(target="old_value", replacement="new_value"), src)
    assert out == "def f():\n    return new_value\n"
    # no original source -> replacement IS the new file
    assert apply_patch_to_source(_patch(replacement="WHOLE"), None) == "WHOLE"
    # target absent -> patch does not apply
    try:
        apply_patch_to_source(_patch(target="missing"), src); assert False
    except PatchApplicationError:
        pass
    # ambiguous (2 matches) -> refuse
    try:
        apply_patch_to_source(_patch(target="x"), "x = 1; y = x"); assert False
    except PatchApplicationError:
        pass
    # empty target with existing source -> cannot locate edit site
    try:
        apply_patch_to_source(_patch(target=""), src); assert False
    except PatchApplicationError:
        pass


def test_local_sandbox_applies_patch_and_compiles():
    async def go():
        eng = LocalProcessEngine()
        src = "def f():\n    return BROKEN\n"
        # valid replacement -> patched file compiles
        ok, out = await eng.compile_and_test(
            _patch(target="BROKEN", replacement="42"), original_source=src)
        assert ok is True, out
        # replacement introduces a syntax error -> compile fails (full file checked)
        ok2, _ = await eng.compile_and_test(
            _patch(target="BROKEN", replacement="def ("), original_source=src)
        assert ok2 is False
        # target not found -> does not apply, never reaches compiler
        ok3, msg3 = await eng.compile_and_test(
            _patch(target="NOPE"), original_source=src)
        assert ok3 is False and "does not apply" in msg3
        # unsupported language -> fail closed
        ok4, msg4 = await eng.compile_and_test(
            _patch(file_path="conf.xyz", target="BROKEN", replacement="42"), original_source=src)
        assert ok4 is False and "failing closed" in msg4
    _run(go())


def test_local_sandbox_runs_behavioral_repro():
    async def go():
        eng = LocalProcessEngine()
        src = "x = OLD\n"
        good = _patch(target="OLD", replacement="1")
        # repro that passes (exit 0) -> overall success
        ok, out = await eng.compile_and_test(good, original_source=src, repro_command="true")
        assert ok is True and "Reproduction passed" in out
        # repro that fails (exit 1) -> gate fails even though it compiled
        ok2, out2 = await eng.compile_and_test(good, original_source=src, repro_command="false")
        assert ok2 is False and "Reproduction failed" in out2
    _run(go())


def test_e2b_fails_closed_without_key():
    async def go():
        prev = os.environ.pop("E2B_API_KEY", None)
        try:
            ok, msg = await E2BEngine().compile_and_test(_patch(), original_source="old\n")
            assert ok is False and "failing closed" in msg
        finally:
            if prev is not None:
                os.environ["E2B_API_KEY"] = prev
    _run(go())


def test_get_sandbox_engine_selection():
    prev_p = os.environ.pop("SANDBOX_PROVIDER", None)
    prev_k = os.environ.pop("E2B_API_KEY", None)
    try:
        # auto + no key -> isolated-by-default (Batch 5/S3): Docker container when
        # available, else the local host engine; never a fail-open E2B.
        from aegis_sre.orchestrator.sandbox_engine import ContainerEngine
        expected = ContainerEngine if ContainerEngine.available() else LocalProcessEngine
        assert isinstance(get_sandbox_engine(), expected)
        os.environ["SANDBOX_PROVIDER"] = "e2b"
        assert isinstance(get_sandbox_engine(), E2BEngine)
        os.environ["SANDBOX_PROVIDER"] = "local"
        assert isinstance(get_sandbox_engine(), LocalProcessEngine)
    finally:
        os.environ.pop("SANDBOX_PROVIDER", None)
        if prev_p is not None:
            os.environ["SANDBOX_PROVIDER"] = prev_p
        if prev_k is not None:
            os.environ["E2B_API_KEY"] = prev_k


# --------------------------------------------------------------------------- #
# P0-4 — webhook / WS auth + rate limiting
# --------------------------------------------------------------------------- #
from aegis_sre.telemetry import auth


def test_verify_token():
    assert auth.verify_token("anything", "") is True          # no token configured -> open
    assert auth.verify_token("secret", "secret") is True      # exact match
    assert auth.verify_token("wrong", "secret") is False      # mismatch
    assert auth.verify_token(None, "secret") is False         # missing when required


def test_verify_sentry_signature():
    import hmac, hashlib
    body = b'{"event":"x"}'
    secret = "shh"
    sig = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    assert auth.verify_sentry_signature(body, sig, secret) is True
    assert auth.verify_sentry_signature(body, "deadbeef", secret) is False
    assert auth.verify_sentry_signature(body, None, secret) is False
    assert auth.verify_sentry_signature(body, None, "") is True  # no secret -> open


def test_rate_limiter_sliding_window():
    rl = auth.SlidingWindowRateLimiter(max_per_minute=2, window_seconds=60)
    t = 1000.0
    assert rl.allow("ip", now=t) is True
    assert rl.allow("ip", now=t + 1) is True
    assert rl.allow("ip", now=t + 2) is False        # 3rd within window blocked
    assert rl.allow("ip", now=t + 61) is True        # window slid -> allowed again
    # disabled limiter always allows
    assert auth.SlidingWindowRateLimiter(0).allow("ip") is True


def test_rate_limiter_evicts_stale_keys():
    # Unbounded key growth (e.g. spoofed X-Forwarded-For) must be swept.
    rl = auth.SlidingWindowRateLimiter(max_per_minute=1, window_seconds=60, sweep_threshold=5)
    for i in range(20):
        rl.allow(f"ip-{i}", now=1000.0)          # 20 distinct keys at t=1000
    rl.allow("trigger", now=2000.0)              # well past window -> triggers sweep
    assert len(rl._hits) <= 2                     # stale keys evicted, only active remain


# --------------------------------------------------------------------------- #
# P1 — approve_patch -> real PR creation
# --------------------------------------------------------------------------- #
from aegis_sre.core.approvals import ApprovalRegistry


class _FakeVCS:
    def __init__(self, url="https://github.com/org/repo/pull/1", fail=False):
        self.url = url
        self.fail = fail
        self.calls = 0

    async def create_pull_request(self, patch, telemetry):
        self.calls += 1
        if self.fail:
            raise RuntimeError("github 503")
        return self.url


def test_approval_opens_pr_once_and_is_idempotent():
    async def go():
        reg = ApprovalRegistry()
        await reg.register("inc1", _patch(), _event("inc1"))
        vcs = _FakeVCS()

        r1 = await reg.approve("inc1", vcs)
        assert r1["status"] == "deployed" and r1["pr_url"].endswith("/pull/1")
        # Second approval is idempotent — no duplicate PR.
        r2 = await reg.approve("inc1", vcs)
        assert r2["status"] == "already_approved" and vcs.calls == 1
    _run(go())


def test_approval_unknown_incident():
    async def go():
        reg = ApprovalRegistry()
        r = await reg.approve("nope", _FakeVCS())
        assert r["status"] == "not_found"
    _run(go())


def test_approval_vcs_failure_restores_for_retry():
    async def go():
        reg = ApprovalRegistry()
        await reg.register("inc2", _patch(), _event("inc2"))
        # First attempt: VCS down -> error, entry preserved.
        r1 = await reg.approve("inc2", _FakeVCS(fail=True))
        assert r1["status"] == "error" and await reg.pending_count() == 1
        # Retry with a healthy VCS -> deployed.
        r2 = await reg.approve("inc2", _FakeVCS())
        assert r2["status"] == "deployed"
    _run(go())


def test_approval_registry_is_bounded():
    reg = ApprovalRegistry(max_size=3)
    for i in range(10):
        asyncio.run(reg.register(f"inc{i}", _patch(), _event(f"inc{i}")))
    assert asyncio.run(reg.pending_count()) == 3


# --------------------------------------------------------------------------- #
# P1 — metrics (no-op-safe instrumentation + /metrics render)
# --------------------------------------------------------------------------- #
from aegis_sre.telemetry import metrics as _metrics


def test_metrics_are_noop_safe_and_render():
    # Call sites must never raise, whether or not prometheus_client is installed.
    _metrics.events_ingested.labels(status="accepted").inc()
    _metrics.incidents_processed.labels(result="completed").inc()
    _metrics.repair_duration.observe(1.23)
    _metrics.queue_depth.set(7)
    _metrics.sandbox_validations.labels(result="failed").inc()
    _metrics.patches_generated.inc()
    _metrics.patches_deployed.inc()
    _metrics.auth_rejections.labels(reason="unauthorized").inc()
    body, content_type = _metrics.render()
    assert isinstance(body, (bytes, bytearray))
    assert isinstance(content_type, str)


ALL_TESTS = [v for k, v in sorted(globals().items()) if k.startswith("test_")]


def main():
    passed = 0
    for t in ALL_TESTS:
        t()
        print(f"PASS {t.__name__}")
        passed += 1
    print(f"\n{passed}/{len(ALL_TESTS)} hardening tests passed")


if __name__ == "__main__":
    main()
