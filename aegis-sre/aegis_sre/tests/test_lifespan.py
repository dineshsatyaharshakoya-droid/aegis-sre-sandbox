"""Coverage: the FastAPI lifespan (startup wiring + graceful shutdown), on-prem."""

from fastapi.testclient import TestClient

from aegis_sre.telemetry import api_receiver as ar


def test_lifespan_startup_and_shutdown(tmp_path, monkeypatch):
    # Force on-prem/in-memory backends (the ambient .env may point at Redis/PG,
    # which would make the Batch-4 data-plane check fail closed) and temp DB paths.
    for attr, val in {"profile": "onprem", "store_backend": "sqlite",
                      "broker_backend": "inprocess", "cache_backend": "memory",
                      "sqlite_path": str(tmp_path / "events.db"),
                      "state_db_path": str(tmp_path / "state.db")}.items():
        monkeypatch.setattr(ar.settings, attr, val)
    # `with TestClient(app)` runs lifespan startup on enter and shutdown on exit.
    with TestClient(ar.app) as c:
        assert c.get("/health").status_code == 200
        assert c.get("/ready").status_code == 200    # incident_service built by lifespan
    # exiting the context drained the consumer + closed backends without error
