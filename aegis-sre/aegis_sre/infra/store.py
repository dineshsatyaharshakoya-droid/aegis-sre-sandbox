"""
Durable event/incident store abstraction.

Holds the system of record for incidents and their lifecycle status. Used for:
  - crash-recovery (re-enqueue `pending` events after a restart),
  - the dashboard `/incidents` history feed,
  - auditing every autonomous action.

Idempotency/de-dup is intentionally NOT here — that lives in the Cache layer
(`claim`) so it can be a fast atomic op independent of the durable store.

  - SqliteEventStore:   on-prem. sync sqlite3 wrapped in asyncio.to_thread.
  - PostgresEventStore: cloud. asyncpg pool, indexed for the history query.

Both expose the same async interface so application code is backend-agnostic.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional


class EventStore(ABC):
    @abstractmethod
    async def init(self) -> None:
        ...

    @abstractmethod
    async def save_incoming_event(self, event_id: str, service_name: str, payload_json: str) -> None:
        ...

    @abstractmethod
    async def mark_event_status(self, event_id: str, status: str) -> None:
        ...

    @abstractmethod
    async def get_pending_payloads(self) -> List[str]:
        ...

    @abstractmethod
    async def get_recent_incidents(self, limit: int = 20) -> List[Dict[str, Any]]:
        ...

    async def close(self) -> None:  # pragma: no cover
        return None


def _row_to_incident(event_id, service_name, payload_json, status, created_at) -> Dict[str, Any]:
    try:
        payload = json.loads(payload_json) if payload_json else {}
    except (ValueError, TypeError):
        payload = {}
    return {
        "id": event_id,
        "service": service_name,
        "status": status,
        "created_at": created_at,
        "crash_log": payload.get("crash_log", ""),
    }


class SqliteEventStore(EventStore):
    def __init__(self, db_path: str):
        self.db_path = db_path

    def _connect(self) -> sqlite3.Connection:
        # WAL improves read/write concurrency for the single-process on-prem tier.
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init_sync(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS incoming_events (
                    event_id     TEXT PRIMARY KEY,
                    service_name TEXT,
                    payload_json TEXT,
                    status       TEXT,
                    created_at   REAL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_events_created_at ON incoming_events(created_at DESC)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_events_status ON incoming_events(status)"
            )
            conn.commit()

    async def init(self) -> None:
        await asyncio.to_thread(self._init_sync)

    def _save_sync(self, event_id, service_name, payload_json) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO incoming_events "
                "(event_id, service_name, payload_json, status, created_at) "
                "VALUES (?, ?, ?, 'pending', ?)",
                (event_id, service_name, payload_json, time.time()),
            )
            conn.commit()

    async def save_incoming_event(self, event_id, service_name, payload_json) -> None:
        await asyncio.to_thread(self._save_sync, event_id, service_name, payload_json)

    def _mark_sync(self, event_id, status) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE incoming_events SET status = ? WHERE event_id = ?", (status, event_id)
            )
            conn.commit()

    async def mark_event_status(self, event_id, status) -> None:
        await asyncio.to_thread(self._mark_sync, event_id, status)

    def _pending_sync(self) -> List[str]:
        with self._connect() as conn:
            cur = conn.execute(
                "SELECT payload_json FROM incoming_events WHERE status = 'pending'"
            )
            return [r[0] for r in cur.fetchall()]

    async def get_pending_payloads(self) -> List[str]:
        return await asyncio.to_thread(self._pending_sync)

    def _recent_sync(self, limit) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            cur = conn.execute(
                "SELECT event_id, service_name, payload_json, status, created_at "
                "FROM incoming_events ORDER BY created_at DESC LIMIT ?",
                (limit,),
            )
            return [_row_to_incident(*row) for row in cur.fetchall()]

    async def get_recent_incidents(self, limit: int = 20) -> List[Dict[str, Any]]:
        return await asyncio.to_thread(self._recent_sync, limit)


class PostgresEventStore(EventStore):
    """Cloud tier. Requires asyncpg and a Postgres DSN."""

    def __init__(self, dsn: str):
        self.dsn = dsn
        self._pool = None

    async def _get_pool(self):
        if self._pool is None:
            import asyncpg  # lazy import so on-prem needs no driver
            self._pool = await asyncpg.create_pool(self.dsn, min_size=1, max_size=10)
        return self._pool

    async def init(self) -> None:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS incoming_events (
                    event_id     TEXT PRIMARY KEY,
                    service_name TEXT,
                    payload_json JSONB,
                    status       TEXT NOT NULL DEFAULT 'pending',
                    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
                )
                """
            )
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_events_created_at ON incoming_events(created_at DESC)"
            )
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_events_status ON incoming_events(status) WHERE status = 'pending'"
            )

    async def save_incoming_event(self, event_id, service_name, payload_json) -> None:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO incoming_events (event_id, service_name, payload_json, status) "
                "VALUES ($1, $2, $3::jsonb, 'pending') ON CONFLICT (event_id) DO NOTHING",
                event_id,
                service_name,
                payload_json,
            )

    async def mark_event_status(self, event_id, status) -> None:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE incoming_events SET status = $1 WHERE event_id = $2", status, event_id
            )

    async def get_pending_payloads(self) -> List[str]:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT payload_json::text AS p FROM incoming_events WHERE status = 'pending'"
            )
            return [r["p"] for r in rows]

    async def get_recent_incidents(self, limit: int = 20) -> List[Dict[str, Any]]:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT event_id, service_name, payload_json::text AS p, status, "
                "extract(epoch from created_at) AS ts "
                "FROM incoming_events ORDER BY created_at DESC LIMIT $1",
                limit,
            )
            return [
                _row_to_incident(r["event_id"], r["service_name"], r["p"], r["status"], r["ts"])
                for r in rows
            ]

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
