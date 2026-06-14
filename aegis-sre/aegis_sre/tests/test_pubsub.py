"""Coverage: WS fan-out pub/sub — NoOp, selection, Redis publish/listen (fakeredis)."""

import asyncio
import json

import pytest

import fakeredis.aioredis as fakeaio

from aegis_sre.config import Settings
from aegis_sre.infra.pubsub import NoOpPubSub, RedisPubSub, build_pubsub


def _settings(**kw):
    s = Settings(profile="onprem", store_backend="sqlite", broker_backend="inprocess",
                 cache_backend="memory")
    for k, v in kw.items():
        object.__setattr__(s, k, v)
    return s


def test_build_pubsub_selects_backend():
    assert isinstance(build_pubsub(_settings()), NoOpPubSub)
    assert isinstance(build_pubsub(_settings(cache_backend="redis")), RedisPubSub)


def test_noop_pubsub_is_inert():
    ns = NoOpPubSub()
    asyncio.run(ns.publish({"x": 1}))
    async def drain():
        out = [m async for m in ns.listen()]
        await ns.close()
        return out
    assert asyncio.run(drain()) == []


def test_redis_publish_swallows_errors():
    ps = RedisPubSub("redis://x")
    class _Boom:
        async def publish(self, *a, **k): raise RuntimeError("redis down")
    ps._client = _Boom()
    asyncio.run(ps.publish({"x": 1}))   # must not raise — fan-out can't break the loop


def test_close_is_safe_when_unconnected():
    asyncio.run(RedisPubSub("redis://x").close())


def test_redis_pubsub_roundtrip_skips_bad_json():
    ps = RedisPubSub("redis://x", channel="aegis:test")
    ps._client = fakeaio.FakeRedis(decode_responses=True)

    async def scenario():
        received = []
        async def reader():
            async for m in ps.listen():
                received.append(m)
                break
        task = asyncio.create_task(reader())
        await asyncio.sleep(0.1)                       # let it SUBSCRIBE first
        await ps._client.publish("aegis:test", "not-json{")          # decode-skip branch
        await ps._client.publish("aegis:test", json.dumps({"type": "patch_ready"}))
        await asyncio.wait_for(task, timeout=3)
        await ps.close()
        return received

    got = asyncio.run(scenario())
    assert got == [{"type": "patch_ready"}]            # bad payload skipped, good one delivered
