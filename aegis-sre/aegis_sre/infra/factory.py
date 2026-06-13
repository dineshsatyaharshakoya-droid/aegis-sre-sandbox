"""
Backend factory — the single place that turns a `Settings` profile into
concrete store / broker / cache instances. Application code depends only on the
abstract interfaces; only this module knows the concrete classes.
"""

from __future__ import annotations

from aegis_sre.config import Settings, get_settings
from aegis_sre.infra.broker import Broker, InProcessBroker, RedisStreamBroker
from aegis_sre.infra.cache import Cache, InMemoryCache, RedisCache
from aegis_sre.infra.store import EventStore, PostgresEventStore, SqliteEventStore


def build_store(settings: Settings | None = None) -> EventStore:
    s = settings or get_settings()
    if s.store_backend == "postgres":
        if not s.database_url:
            raise ValueError("AEGIS_DATABASE_URL is required for the postgres store backend")
        return PostgresEventStore(s.database_url)
    return SqliteEventStore(s.sqlite_path)


def build_cache(settings: Settings | None = None) -> Cache:
    s = settings or get_settings()
    if s.cache_backend == "redis":
        return RedisCache(s.redis_url)
    return InMemoryCache()


def build_broker(settings: Settings | None = None) -> Broker:
    s = settings or get_settings()
    if s.broker_backend == "redis":
        return RedisStreamBroker(
            redis_url=s.redis_url,
            stream=s.broker_stream,
            group=s.broker_group,
            consumer=s.consumer_name,
            claim_idle_ms=s.broker_claim_idle_ms,
        )
    return InProcessBroker(max_size=s.queue_max_size)
