import sqlite3
import json
import time
from typing import List, Dict, Any

import os
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "aegis_events.db")

def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        # Create idempotency cache table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS idempotency_cache (
                hash TEXT PRIMARY KEY,
                timestamp REAL
            )
        ''')
        # Create events queue / history table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS incoming_events (
                event_id TEXT PRIMARY KEY,
                service_name TEXT,
                payload_json TEXT,
                status TEXT,
                created_at REAL
            )
        ''')
        conn.commit()

def save_cache_hash(hash_val: str):
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT OR REPLACE INTO idempotency_cache (hash, timestamp) VALUES (?, ?)", 
            (hash_val, time.time())
        )
        conn.commit()

def is_hash_cached(hash_val: str, ttl_seconds: int = 300) -> bool:
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT timestamp FROM idempotency_cache WHERE hash = ?", (hash_val,))
        row = cursor.fetchone()
        if row:
            timestamp = row[0]
            if time.time() - timestamp < ttl_seconds:
                # Update timestamp on hit
                save_cache_hash(hash_val)
                return True
            else:
                # Expired
                cursor.execute("DELETE FROM idempotency_cache WHERE hash = ?", (hash_val,))
                conn.commit()
        return False

def claim_event_hash(hash_val: str, ttl_seconds: int = 300) -> bool:
    """
    Atomically claim an idempotency hash.

    Returns True if THIS caller claimed the hash (i.e. it is NOT a duplicate and
    should be processed). Returns False if another caller already holds a
    non-expired claim (duplicate -> drop).

    This replaces the previous check-then-act pattern (is_hash_cached() followed
    by save_cache_hash()), which had a TOCTOU race: two concurrent identical
    webhooks could both read "not cached" and both enqueue the same incident.

    Atomicity is provided by a single IMMEDIATE write transaction:
      1. An expired claim for this hash is deleted so it can be re-claimed.
      2. `INSERT OR IGNORE` succeeds (rowcount == 1) only for the first writer;
         racing duplicates see rowcount == 0.
    """
    now = time.time()
    with sqlite3.connect(DB_PATH, isolation_level="IMMEDIATE", timeout=30) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "DELETE FROM idempotency_cache WHERE hash = ? AND ? - timestamp >= ?",
            (hash_val, now, ttl_seconds),
        )
        cursor.execute(
            "INSERT OR IGNORE INTO idempotency_cache (hash, timestamp) VALUES (?, ?)",
            (hash_val, now),
        )
        claimed = cursor.rowcount == 1
        conn.commit()
        return claimed

def save_incoming_event(event_id: str, service_name: str, payload_json: str):
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT OR IGNORE INTO incoming_events (event_id, service_name, payload_json, status, created_at) VALUES (?, ?, ?, 'pending', ?)",
            (event_id, service_name, payload_json, time.time())
        )
        conn.commit()

def mark_event_completed(event_id: str):
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE incoming_events SET status = 'completed' WHERE event_id = ?", (event_id,))
        conn.commit()

def get_recent_incidents(limit: int = 20) -> List[Dict[str, Any]]:
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT event_id, service_name, payload_json, status, created_at FROM incoming_events ORDER BY created_at DESC LIMIT ?", (limit,))
        rows = cursor.fetchall()
        
        incidents = []
        for row in rows:
            try:
                payload = json.loads(row[2])
            except:
                payload = {}
            incidents.append({
                "id": row[0],
                "service": row[1],
                "status": row[3],
                "created_at": row[4],
                "crash_log": payload.get("crash_log", "")
            })
        return incidents

# Initialize on import
init_db()
