"""
IncidentService — the backend-agnostic application core.

It splits the pipeline into the two halves that must scale independently:

  ingest()  : fast, synchronous-to-the-caller path run by the API process.
              de-dup (Cache.claim) -> persist (EventStore) -> publish (Broker).
              Returns immediately so the webhook responds in milliseconds.

  ConsumerRunner : the slow path run by worker processes. Pulls from the broker,
              runs an injected async `processor` under the God-Node timeout,
              updates durable status, and ACKs for at-least-once delivery.

The heavy LangGraph orchestrator is injected as `processor` so this module stays
free of model/graph dependencies and is unit-testable on its own.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from typing import Awaitable, Callable, Dict, Optional

from aegis_sre.config import Settings, get_settings
from aegis_sre.infra.broker import Broker, Delivery
from aegis_sre.infra.cache import Cache
from aegis_sre.infra.store import EventStore
from aegis_sre.orchestrator.schemas import TelemetryEvent
from aegis_sre.telemetry.logger import logger
from aegis_sre.telemetry import metrics


def compute_signature(service_name: str, crash_log: str) -> str:
    """Stable de-dup key: service + tail of the crash log."""
    raw = f"{service_name}:{crash_log[-200:]}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


class IncidentService:
    def __init__(
        self,
        store: EventStore,
        broker: Broker,
        cache: Cache,
        settings: Optional[Settings] = None,
    ):
        self.store = store
        self.broker = broker
        self.cache = cache
        self.settings = settings or get_settings()

    async def init(self) -> None:
        await self.store.init()

    async def ingest(self, event: TelemetryEvent) -> Dict:
        """De-dup, persist, and publish. Returns a webhook-ready result dict."""
        crash_hash = compute_signature(event.service_name, event.crash_log)

        # Atomic claim closes the TOCTOU race across concurrent duplicate hooks.
        claimed = await self.cache.claim(crash_hash, ttl_seconds=self.settings.dedup_ttl_seconds)
        if not claimed:
            logger.info("dropping_duplicate_event", service_name=event.service_name, hash=crash_hash[:8])
            metrics.events_ingested.labels(status="ignored").inc()
            return {"status": "ignored", "reason": "duplicate_event", "hash": crash_hash}

        await self.store.save_incoming_event(
            event.event_id, event.service_name, event.model_dump_json()
        )

        published = await self.broker.publish(event.model_dump(mode="json"))
        self._sample_queue_depth()
        if not published:
            # Back-pressure: broker full. Surface so the API can return 429.
            logger.warning("broker_full_dropping_event", event_id=event.event_id)
            metrics.events_ingested.labels(status="dropped").inc()
            return {"status": "dropped", "message": "System at capacity. Retry later.", "hash": crash_hash}

        logger.info("event_accepted", event_id=event.event_id, hash=crash_hash[:8])
        metrics.events_ingested.labels(status="accepted").inc()
        return {"status": "accepted", "message": "Autonomous repair loop triggered.", "hash": crash_hash}

    def _sample_queue_depth(self) -> None:
        """Best-effort gauge update; only the in-process broker exposes a size."""
        qsize = getattr(self.broker, "qsize", None)
        if callable(qsize):
            metrics.queue_depth.set(qsize())

    async def recover_pending(self) -> int:
        """Re-publish events left `pending` by a prior crash. Returns count."""
        count = 0
        for payload_json in await self.store.get_pending_payloads():
            try:
                event = TelemetryEvent.model_validate_json(payload_json)
                # Claim-before-republish (A11): the cache claim is atomic across
                # replicas (Redis SET NX), so only one replica re-publishes a given
                # pending event — no double-processing during concurrent recovery.
                if not await self.cache.claim(
                    f"recovery:{event.event_id}", ttl_seconds=self.settings.dedup_ttl_seconds
                ):
                    logger.info("recovery_skipped_claimed_by_peer", event_id=event.event_id)
                    continue
                if await self.broker.publish(event.model_dump(mode="json")):
                    count += 1
            except Exception as e:  # noqa: BLE001
                logger.error("failed_to_recover_pending_event", error=str(e))
        if count:
            logger.info("recovered_pending_events", count=count)
        return count


class ConsumerRunner:
    """
    Worker-side loop. Decoupled from ingestion so workers scale horizontally and
    independently of the API. One ConsumerRunner == one consumer; run N of them
    (or N pods) for throughput.
    """

    def __init__(
        self,
        broker: Broker,
        store: EventStore,
        processor: Callable[[TelemetryEvent], Awaitable[None]],
        timeout_seconds: int = 120,
    ):
        self.broker = broker
        self.store = store
        self.processor = processor
        self.timeout_seconds = timeout_seconds
        self._stopped = False

    async def run(self) -> None:
        logger.info("consumer_started", timeout=self.timeout_seconds)
        async for delivery in self.broker.consume():
            if self._stopped:
                break
            await self._handle(delivery)

    async def _handle(self, delivery: Delivery) -> None:
        event_id = delivery.payload.get("event_id", "unknown")
        try:
            event = TelemetryEvent.model_validate(delivery.payload)
        except Exception as e:  # noqa: BLE001
            logger.error("undeserializable_delivery_acked", event_id=event_id, error=str(e))
            await self.broker.ack(delivery.id)  # poison message: drop, don't redeliver forever
            return

        started = time.monotonic()
        result = "failed"
        try:
            # God-Node kill switch: bound the LangGraph swarm.
            await asyncio.wait_for(self.processor(event), timeout=self.timeout_seconds)
            result = "completed"
        except asyncio.TimeoutError:
            logger.error("god_node_kill_switch_activated", event_id=event.event_id, reason="timeout")
        except Exception as e:  # noqa: BLE001
            logger.error("processing_failed", event_id=event.event_id, error=str(e))

        # Persist the terminal status separately and guarded (SV-2): a transient
        # status-write failure must not crash the consumer or mislabel `result`.
        try:
            await self.store.mark_event_status(event.event_id, result)
        except Exception as e:  # noqa: BLE001
            logger.error("status_write_failed", event_id=event.event_id, result=result, error=str(e))
        finally:
            metrics.repair_duration.observe(time.monotonic() - started)
            metrics.incidents_processed.labels(result=result).inc()
            self._sample_queue_depth()
            # At-least-once: ack after terminal status so a worker crash mid-run
            # leaves the message in the pending list for redelivery.
            await self.broker.ack(delivery.id)

    def _sample_queue_depth(self) -> None:
        qsize = getattr(self.broker, "qsize", None)
        if callable(qsize):
            metrics.queue_depth.set(qsize())

    def stop(self) -> None:
        self._stopped = True
