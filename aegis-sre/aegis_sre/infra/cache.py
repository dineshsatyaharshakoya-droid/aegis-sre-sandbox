"""
Cache abstraction.

The only semantically critical operation is `claim()`: an *atomic*
test-and-set used for idempotency / de-duplication. Returning True means the
caller is the first to claim the key within the TTL window and should process
the event; False means a duplicate.

  - InMemoryCache: single-process, thread-safe, TTL + LRU bounded (on-prem).
  - RedisCache:    cross-process atomic claim via SET NX EX (cloud / HA).
"""

from __future__ import annotations

import asyncio
import threading
import time
from abc import ABC, abstractmethod
from collections import OrderedDict
from typing import Optional


class Cache(ABC):
    @abstractmethod
    async def claim(self, key: str, ttl_seconds: int) -> bool:
        """Atomically claim `key`. True = first claimer (process it). False = duplicate."""

    @abstractmethod
    async def get(self, key: str) -> Optional[str]:
        ...

    @abstractmethod
    async def set(self, key: str, value: str, ttl_seconds: Optional[int] = None) -> None:
        ...

    async def close(self) -> None:  # pragma: no cover - optional
        return None


class InMemoryCache(Cache):
    """Thread-safe, TTL + size-bounded. Suitable for single-instance on-prem."""

    def __init__(self, max_size: int = 100_000):
        self.max_size = max_size
        self._claims: "OrderedDict[str, float]" = OrderedDict()
        self._kv: "OrderedDict[str, tuple[str, Optional[float]]]" = OrderedDict()
        self._lock = threading.Lock()

    async def claim(self, key: str, ttl_seconds: int) -> bool:
        now = time.time()
        with self._lock:
            # Evict expired claims.
            expired = [k for k, ts in self._claims.items() if now - ts >= ttl_seconds]
            for k in expired:
                del self._claims[k]

            ts = self._claims.get(key)
            if ts is not None and (now - ts) < ttl_seconds:
                return False  # duplicate

            self._claims[key] = now
            self._claims.move_to_end(key)
            while len(self._claims) > self.max_size:
                self._claims.popitem(last=False)
            return True

    async def get(self, key: str) -> Optional[str]:
        now = time.time()
        with self._lock:
            item = self._kv.get(key)
            if not item:
                return None
            value, expires_at = item
            if expires_at is not None and now >= expires_at:
                del self._kv[key]
                return None
            return value

    async def set(self, key: str, value: str, ttl_seconds: Optional[int] = None) -> None:
        with self._lock:
            expires_at = time.time() + ttl_seconds if ttl_seconds else None
            self._kv[key] = (value, expires_at)
            self._kv.move_to_end(key)
            while len(self._kv) > self.max_size:
                self._kv.popitem(last=False)


class RedisCache(Cache):
    """Cross-process cache. `claim` uses SET NX EX for a distributed atomic lock."""

    def __init__(self, redis_url: str):
        self._redis_url = redis_url
        self._client = None  # lazy

    async def _conn(self):
        if self._client is None:
            import redis.asyncio as redis  # imported lazily so on-prem needs no redis dep
            self._client = redis.from_url(self._redis_url, decode_responses=True)
        return self._client

    async def claim(self, key: str, ttl_seconds: int) -> bool:
        client = await self._conn()
        # SET key 1 NX EX ttl -> returns True only if the key did not exist.
        was_set = await client.set(f"aegis:claim:{key}", "1", nx=True, ex=ttl_seconds)
        return bool(was_set)

    async def get(self, key: str) -> Optional[str]:
        client = await self._conn()
        return await client.get(f"aegis:kv:{key}")

    async def set(self, key: str, value: str, ttl_seconds: Optional[int] = None) -> None:
        client = await self._conn()
        await client.set(f"aegis:kv:{key}", value, ex=ttl_seconds)

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
