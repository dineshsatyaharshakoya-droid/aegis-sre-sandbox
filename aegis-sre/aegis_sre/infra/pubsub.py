"""
WebSocket fan-out pub/sub (roadmap A10, audit #18).

In the cloud tier the worker and the API are separate processes, so a worker's
graph progress can't reach the API's WebSocket clients directly. This bridges
them: the worker publishes node-update / patch-ready messages to a Redis channel,
and each API replica subscribes and rebroadcasts to its local WS clients — so the
dashboard sees live progress regardless of which worker handled the incident.

On-prem (no Redis) uses NoOpPubSub: the in-process consumer already broadcasts
directly, so there's nothing to bridge.
"""

from __future__ import annotations

import json
from typing import AsyncIterator

from aegis_sre.telemetry.logger import logger


class PubSub:
    async def publish(self, message: dict) -> None: ...
    async def listen(self) -> AsyncIterator[dict]:  # pragma: no cover - interface
        if False:
            yield {}
    async def close(self) -> None: ...


class NoOpPubSub(PubSub):
    """On-prem: the in-process broadcast already reaches WS clients."""
    async def publish(self, message: dict) -> None:
        return

    async def listen(self) -> AsyncIterator[dict]:
        return
        yield {}  # pragma: no cover - makes this an async generator

    async def close(self) -> None:
        return


class RedisPubSub(PubSub):
    def __init__(self, redis_url: str, channel: str = "aegis:ws"):
        self._url = redis_url
        self.channel = channel
        self._client = None
        self._pubsub = None

    async def _conn(self):
        if self._client is None:
            import redis.asyncio as redis
            self._client = redis.from_url(self._url, decode_responses=True)
        return self._client

    async def publish(self, message: dict) -> None:
        try:
            client = await self._conn()
            await client.publish(self.channel, json.dumps(message))
        except Exception as e:  # noqa: BLE001 - fan-out must never break the repair loop
            logger.warning("pubsub_publish_failed", error=str(e))

    async def listen(self) -> AsyncIterator[dict]:
        client = await self._conn()
        self._pubsub = client.pubsub()
        await self._pubsub.subscribe(self.channel)
        async for msg in self._pubsub.listen():
            if msg.get("type") == "message":
                try:
                    yield json.loads(msg["data"])
                except (ValueError, TypeError):
                    continue

    async def close(self) -> None:
        try:
            if self._pubsub is not None:
                await self._pubsub.aclose()
            if self._client is not None:
                await self._client.aclose()
        except Exception:  # noqa: BLE001
            pass


def build_pubsub(settings) -> PubSub:
    """Redis fan-out on the cloud tier (or whenever the cache is Redis), else no-op."""
    if settings.cache_backend == "redis" or settings.is_cloud:
        return RedisPubSub(settings.redis_url)
    return NoOpPubSub()
