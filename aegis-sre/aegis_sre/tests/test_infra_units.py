"""Coverage: backend factory selection, InMemoryCache TTL, tool_registry handlers."""

import asyncio

import pytest
from freezegun import freeze_time

from aegis_sre.config import Settings
from aegis_sre.infra.factory import build_broker, build_cache, build_store
from aegis_sre.infra.broker import InProcessBroker, RedisStreamBroker
from aegis_sre.infra.cache import InMemoryCache, RedisCache
from aegis_sre.infra.store import PostgresEventStore, SqliteEventStore


def _s(**kw):
    base = dict(profile="onprem", store_backend="sqlite", broker_backend="inprocess",
                cache_backend="memory")
    base.update(kw)
    return Settings(**base)


def test_factory_selects_backends():
    assert isinstance(build_store(_s()), SqliteEventStore)
    assert isinstance(build_store(_s(store_backend="postgres", database_url="postgresql://x")),
                      PostgresEventStore)
    assert isinstance(build_cache(_s()), InMemoryCache)
    assert isinstance(build_cache(_s(cache_backend="redis")), RedisCache)
    assert isinstance(build_broker(_s()), InProcessBroker)
    assert isinstance(build_broker(_s(broker_backend="redis")), RedisStreamBroker)


def test_factory_postgres_requires_url():
    with pytest.raises(ValueError, match="AEGIS_DATABASE_URL"):
        build_store(_s(store_backend="postgres", database_url=""))


def test_inmemory_cache_claim_dedups_and_get_set_ttl():
    c = InMemoryCache()
    assert asyncio.run(c.claim("k", 60)) is True
    assert asyncio.run(c.claim("k", 60)) is False     # duplicate within TTL
    with freeze_time("2026-01-01 00:00:00") as f:
        asyncio.run(c.set("a", "v", ttl_seconds=10))
        assert asyncio.run(c.get("a")) == "v"
        f.tick(20)
        assert asyncio.run(c.get("a")) is None        # expiry branch
    assert asyncio.run(c.get("missing")) is None


# --- tool_registry lazy handlers fail clearly when unconfigured ---

import aegis_sre.integrations.tool_registry as tr


def test_handlers_raise_when_clients_unconfigured(monkeypatch):
    monkeypatch.setattr("aegis_sre.orchestrator.metrics_tools.get_metrics_client", lambda: None)
    monkeypatch.setattr("aegis_sre.orchestrator.logs_tools.get_logs_client", lambda: None)
    monkeypatch.setattr("aegis_sre.orchestrator.incident_tools.get_incident_notifier", lambda: None)
    with pytest.raises(RuntimeError, match="PROMETHEUS_URL"):
        asyncio.run(tr._prometheus_query("up"))
    with pytest.raises(RuntimeError, match="PROMETHEUS_URL"):
        asyncio.run(tr._prometheus_query_range("up", 0.0, 1.0))
    with pytest.raises(RuntimeError, match="LOKI_URL"):
        asyncio.run(tr._logs_query("{}"))
    with pytest.raises(RuntimeError, match="ALERT_WEBHOOK_URL"):
        asyncio.run(tr._incident_handler("trigger")())


def test_incident_handler_dispatch_and_dedup_guard(monkeypatch):
    calls = []
    class _N:
        async def trigger(self, **kw): calls.append(("trigger", kw)); return "t"
        async def acknowledge(self, dk, **kw): calls.append(("ack", dk)); return "a"
        async def resolve(self, dk, **kw): calls.append(("resolve", dk)); return "r"
    monkeypatch.setattr("aegis_sre.orchestrator.incident_tools.get_incident_notifier", lambda: _N())
    asyncio.run(tr._incident_handler("trigger")(summary="x"))
    asyncio.run(tr._incident_handler("acknowledge")(dedup_key="d1"))
    asyncio.run(tr._incident_handler("resolve")(dedup_key="d1"))
    assert [c[0] for c in calls] == ["trigger", "ack", "resolve"]
    with pytest.raises(ValueError, match="requires 'dedup_key'"):
        asyncio.run(tr._incident_handler("acknowledge")())   # missing dedup_key guard
