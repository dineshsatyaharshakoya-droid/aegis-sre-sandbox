"""
Aegis worker entrypoint (cloud tier).

Run one or more of these as separate processes/pods. Each consumes incidents
from the broker and runs the LangGraph repair swarm, fully decoupled from the
API process. Scale throughput by scaling worker replicas.

    AEGIS_PROFILE=cloud \
    AEGIS_DATABASE_URL=postgres://... \
    AEGIS_REDIS_URL=redis://... \
    python worker.py

On the on-prem profile the API process runs an in-process consumer instead, so
this file is only needed for the cloud tier — but it works on both.
"""

from __future__ import annotations

import asyncio
import os

from aegis_sre.config import get_settings
from aegis_sre.core.service import ConsumerRunner
from aegis_sre.infra.factory import build_broker, build_store
from aegis_sre.orchestrator.schemas import TelemetryEvent
from aegis_sre.orchestrator.safety import safety_policy
from aegis_sre.telemetry.logger import logger


def make_processor(checkpointer):
    """
    Build the async processor that runs the LangGraph orchestrator for one event.
    Imports are lazy so the infra layer can be tested without heavy deps.

    The graph (and its `checkpointer`) is built once and reused for every
    incident, instead of opening/closing a fresh SQLite connection per event.
    """
    from aegis_sre.orchestrator.graph import build_graph

    graph_app = build_graph(checkpointer=checkpointer)

    async def processor(event: TelemetryEvent) -> None:
        initial_state = {
            "telemetry": event,
            "code_context": None,
            "current_patch": None,
            "sandbox_status": "pending",
            "review": None,
            "iteration_count": 0,
            "resolved": False,
        }
        config = {"configurable": {"thread_id": event.event_id}}
        async for _output in graph_app.astream(initial_state, config=config):
            pass  # In cloud, stream node updates to Redis pub/sub for WS fan-out.
        logger.info("incident_processed", event_id=event.event_id)

    return processor


async def main() -> None:
    settings = get_settings()
    logger.info("worker_booting", profile=settings.profile, concurrency=settings.worker_concurrency)

    store = build_store(settings)
    broker = build_broker(settings)
    await store.init()

    # Open the LangGraph checkpointer once for the worker's lifetime and reuse it
    # across every incident (absolute path via settings, not a cwd-relative one).
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

    async with AsyncSqliteSaver.from_conn_string(settings.state_db_path) as checkpointer:
        processor = make_processor(checkpointer)

        runners = [
            ConsumerRunner(
                broker=broker,
                store=store,
                processor=processor,
                timeout_seconds=safety_policy.get_timeout(),
            )
            for _ in range(max(1, settings.worker_concurrency))
        ]
        try:
            await asyncio.gather(*(r.run() for r in runners))
        finally:
            await broker.close()
            await store.close()


if __name__ == "__main__":
    asyncio.run(main())
