"""
Shared pytest fixtures for the Aegis test suite.

The webhook / incident endpoints guard on a module-global `incident_service`
that is normally built inside the FastAPI `lifespan`. The HTTP tests drive the
app with `TestClient(app)` / `AsyncClient(ASGITransport(app))`, neither of which
triggers lifespan — so `incident_service` stays `None` and every ingest endpoint
returns 503 (this is the real cause of the 503 failures, NOT database
connectivity: the containers were up and the tests still 503'd).

`wire_inmemory_incident_service` fixes that hermetically: it injects an
`IncidentService` backed by purely in-memory infra (SqliteEventStore on a temp
file + InProcessBroker + InMemoryCache), so the endpoints initialize with **no
Docker containers and no background consumer** (and therefore no LLM calls). It
is function-scoped so each test gets a fresh dedup cache and a fresh broker whose
asyncio.Queue binds to that test's event loop.
"""

import asyncio

import pytest

from aegis_sre.config import Settings
from aegis_sre.core.service import IncidentService
from aegis_sre.infra.broker import InProcessBroker
from aegis_sre.infra.cache import InMemoryCache
from aegis_sre.infra.store import SqliteEventStore
from aegis_sre.telemetry import api_receiver


@pytest.fixture(autouse=True)
def wire_inmemory_incident_service(tmp_path):
    # Force on-prem/in-memory backends regardless of any ambient .env overrides
    # (a local .env may point AEGIS_STORE/BROKER/CACHE at Postgres/Redis).
    settings = Settings(
        profile="onprem",
        store_backend="sqlite",
        broker_backend="inprocess",
        cache_backend="memory",
    )
    store = SqliteEventStore(str(tmp_path / "test_events.db"))
    # init() is file-backed (reconnects per call), so creating the schema in a
    # throwaway loop here does not bind the store to that loop.
    asyncio.run(store.init())

    service = IncidentService(
        store=store, broker=InProcessBroker(), cache=InMemoryCache(), settings=settings
    )

    previous = api_receiver.incident_service
    api_receiver.incident_service = service
    try:
        yield service
    finally:
        api_receiver.incident_service = previous
