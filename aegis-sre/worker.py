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


def make_processor(checkpointer, pubsub=None):
    """
    Build the async processor that runs the LangGraph orchestrator for one event.
    Imports are lazy so the infra layer can be tested without heavy deps.

    The graph (and its `checkpointer`) is built once and reused for every
    incident, instead of opening/closing a fresh SQLite connection per event.

    Node-by-node progress is published to `pubsub` so the API replicas can fan it
    out to WebSocket clients (audit #18 — was previously discarded with `pass`).
    """
    from aegis_sre.orchestrator.graph import build_graph
    from aegis_sre.infra.pubsub import NoOpPubSub
    from aegis_sre.core.approvals import build_approval_registry

    graph_app = build_graph(checkpointer=checkpointer)
    pubsub = pubsub or NoOpPubSub()
    # Register produced remediations into the SHARED (Redis) approval registry so
    # a separate API replica can approve them — without this, cloud approvals
    # always 404'd because the registry lived only in the API process (F2/A8).
    approvals = build_approval_registry(get_settings())

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
        await pubsub.publish({"incident_id": event.event_id, "type": "telemetry_received",
                              "service": event.service_name})
        final_state = dict(initial_state)
        async for output in graph_app.astream(initial_state, config=config):
            for node_name, state_update in output.items():
                final_state.update(state_update)
                await pubsub.publish({"incident_id": event.event_id, "type": "node_update",
                                      "node": node_name})
        patch = final_state.get("current_patch")
        if patch is not None:
            # Hold it for human approval in shared state (so any API replica can
            # approve), then announce patch_ready to WS clients via pub/sub.
            await approvals.register(event.event_id, patch, event)
            await pubsub.publish({
                "incident_id": event.event_id, "type": "patch_ready",
                "service": event.service_name, "kind": type(patch).__name__,
                "file": getattr(patch, "file_path", None),
                "root_cause_analysis": patch.root_cause_analysis,
                "explanation": patch.explanation,
                "diff": getattr(patch, "replacement_content", None),
            })
        logger.info("incident_processed", event_id=event.event_id)

    return processor


async def main() -> None:
    settings = get_settings()
    logger.info("worker_booting", profile=settings.profile, concurrency=settings.worker_concurrency)

    store = build_store(settings)
    broker = build_broker(settings)
    await store.init()

    # Warm the RAG index once at worker startup (A6), in the background.
    from aegis_sre.orchestrator.graph import warm_rag_engine
    asyncio.create_task(warm_rag_engine())

    # WS fan-out: publish graph progress so API replicas can stream it (A10/#18).
    from aegis_sre.infra.pubsub import build_pubsub
    pubsub = build_pubsub(settings)

    # Open the LangGraph checkpointer once for the worker's lifetime and reuse it
    # across every incident (absolute path via settings, not a cwd-relative one).
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

    async with AsyncSqliteSaver.from_conn_string(settings.state_db_path) as checkpointer:
        processor = make_processor(checkpointer, pubsub=pubsub)

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
