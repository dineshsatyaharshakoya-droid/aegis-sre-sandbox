"""
Message broker abstraction — the seam that decouples *ingestion* (fast, must
not block the webhook response) from *processing* (slow LangGraph repair loop).

  - InProcessBroker:  asyncio.Queue. Producer and consumer live in one process
                      (on-prem single binary). Bounded; back-pressures by
                      reporting `full()` so the API can return 429/"dropped".
  - RedisStreamBroker: Redis Streams + consumer groups. Many stateless worker
                       pods consume competitively with at-least-once delivery,
                       explicit ACK, and a pending-entries list for crash
                       recovery (cloud tier).

Delivery contract: `publish(payload)` enqueues a JSON-serializable dict.
`consume()` yields `Delivery(id, payload)`; the caller MUST `ack(delivery.id)`
after successful processing so redelivery works on worker crash.
"""

from __future__ import annotations

import asyncio
import json
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import AsyncIterator, Dict, Optional

from aegis_sre.telemetry.logger import logger


@dataclass
class Delivery:
    id: str
    payload: Dict


class Broker(ABC):
    @abstractmethod
    async def publish(self, payload: Dict) -> bool:
        """Enqueue payload. Returns False if rejected (e.g. queue full)."""

    @abstractmethod
    async def consume(self) -> AsyncIterator[Delivery]:
        """Yield deliveries until cancelled."""

    @abstractmethod
    async def ack(self, delivery_id: str) -> None:
        ...

    async def close(self) -> None:  # pragma: no cover
        return None


class InProcessBroker(Broker):
    def __init__(self, max_size: int = 1000):
        self._queue: "asyncio.Queue[Delivery]" = asyncio.Queue(maxsize=max_size)
        self._seq = 0

    async def publish(self, payload: Dict) -> bool:
        if self._queue.full():
            return False
        self._seq += 1
        await self._queue.put(Delivery(id=str(self._seq), payload=payload))
        return True

    async def consume(self) -> AsyncIterator[Delivery]:
        while True:
            try:
                delivery = await self._queue.get()
            except asyncio.CancelledError:
                break
            yield delivery

    async def ack(self, delivery_id: str) -> None:
        # asyncio.Queue tracks completion via task_done(); balance exactly once.
        self._queue.task_done()

    def qsize(self) -> int:
        return self._queue.qsize()


class RedisStreamBroker(Broker):
    def __init__(
        self,
        redis_url: str,
        stream: str,
        group: str,
        consumer: str,
        block_ms: int = 5000,
        maxlen: int = 100_000,
        claim_idle_ms: int = 60_000,
        claim_batch: int = 10,
    ):
        self._redis_url = redis_url
        self.stream = stream
        self.group = group
        self.consumer = consumer
        self.block_ms = block_ms
        self.maxlen = maxlen
        # Reclaim messages abandoned by a crashed consumer once they have been
        # idle in the pending-entries list (PEL) for this long.
        self.claim_idle_ms = claim_idle_ms
        self.claim_batch = claim_batch
        self._autoclaim_cursor = "0-0"
        self._client = None

    async def _conn(self):
        if self._client is None:
            import redis.asyncio as redis
            self._client = redis.from_url(self._redis_url, decode_responses=True)
            # Create the consumer group (idempotent). MKSTREAM creates the stream.
            try:
                await self._client.xgroup_create(self.stream, self.group, id="0", mkstream=True)
            except Exception:
                pass  # BUSYGROUP: group already exists
        return self._client

    async def publish(self, payload: Dict) -> bool:
        client = await self._conn()
        await client.xadd(
            self.stream,
            {"data": json.dumps(payload)},
            maxlen=self.maxlen,
            approximate=True,
        )
        return True

    async def consume(self) -> AsyncIterator[Delivery]:
        client = await self._conn()
        while True:
            # 1. Reclaim deliveries abandoned by a crashed/stalled consumer.
            #    `xreadgroup(">")` only ever returns *new* messages, so without
            #    this an un-ACKed message left in another consumer's PEL would
            #    sit there forever and the at-least-once guarantee would be a
            #    lie. XAUTOCLAIM moves messages idle past the threshold to us.
            try:
                for delivery in await self._reclaim_idle(client):
                    yield delivery
            except asyncio.CancelledError:
                break
            except Exception as e:  # noqa: BLE001 - reclaim is best-effort
                logger.warning("reclaim_idle_failed", error=str(e))

            # 2. Read newly published messages.
            try:
                resp = await client.xreadgroup(
                    self.group,
                    self.consumer,
                    {self.stream: ">"},
                    count=1,
                    block=self.block_ms,
                )
            except asyncio.CancelledError:
                break
            if not resp:
                continue  # block timeout, loop again
            for _stream, messages in resp:
                for msg_id, fields in messages:
                    payload = json.loads(fields["data"])
                    yield Delivery(id=msg_id, payload=payload)

    async def _reclaim_idle(self, client) -> "list[Delivery]":
        """Claim messages idle longer than `claim_idle_ms` from the group PEL.

        Walks the PEL via a rolling cursor so a backlog is drained in batches
        across successive `consume()` iterations rather than all at once.
        Tombstoned entries (claimed but since deleted from the stream) are
        ACKed to clear them from the PEL.
        """
        result = await client.xautoclaim(
            self.stream,
            self.group,
            self.consumer,
            min_idle_time=self.claim_idle_ms,
            start_id=self._autoclaim_cursor,
            count=self.claim_batch,
        )
        # redis-py returns (next_cursor, claimed[, deleted_ids]); older servers
        # omit the third element. Normalise defensively.
        next_cursor = result[0] if result else "0-0"
        messages = result[1] if result and len(result) > 1 else []
        # A returned cursor of "0-0" means the PEL has been fully scanned.
        self._autoclaim_cursor = next_cursor or "0-0"

        deliveries: "list[Delivery]" = []
        for msg_id, fields in messages:
            if not fields or "data" not in fields:
                # Entry was deleted from the stream after being claimed; clear it.
                await client.xack(self.stream, self.group, msg_id)
                continue
            deliveries.append(Delivery(id=msg_id, payload=json.loads(fields["data"])))
        if deliveries:
            logger.info("reclaimed_idle_deliveries", count=len(deliveries))
        return deliveries

    async def ack(self, delivery_id: str) -> None:
        client = await self._conn()
        await client.xack(self.stream, self.group, delivery_id)

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
