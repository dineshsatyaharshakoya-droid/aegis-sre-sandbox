"""A11: recover_pending claim-before-republish guard (no double-process)."""

import asyncio
import json

from aegis_sre.config import Settings
from aegis_sre.core.service import IncidentService
from aegis_sre.infra.broker import InProcessBroker
from aegis_sre.infra.cache import InMemoryCache
from aegis_sre.infra.store import SqliteEventStore


def _service(store, cache):
    # Distinct brokers (separate "replicas"), SHARED cache (the cluster-wide claim).
    return IncidentService(store=store, broker=InProcessBroker(), cache=cache, settings=Settings())


def test_concurrent_recovery_publishes_once(tmp_path):
    async def go():
        store = SqliteEventStore(str(tmp_path / "rec.db"))
        await store.init()
        await store.save_incoming_event("evt-1", "svc",
                                        json.dumps({"event_id": "evt-1", "service_name": "svc",
                                                    "crash_log": "boom", "metadata": {}}))
        shared_cache = InMemoryCache()
        replica_a = _service(store, shared_cache)
        replica_b = _service(store, shared_cache)

        first = await replica_a.recover_pending()   # claims + republishes
        second = await replica_b.recover_pending()  # claim already held -> skips
        return first, second

    first, second = asyncio.run(go())
    assert first == 1
    assert second == 0  # peer already claimed it -> no double-process
