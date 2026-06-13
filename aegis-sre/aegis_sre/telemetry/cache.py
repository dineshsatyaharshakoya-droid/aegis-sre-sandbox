"""
In-memory idempotency cache used by the telemetry ingestion layer.

This module restores the `IdempotencyCache` symbol that
`aegis_sre.telemetry.k8s_watcher` imports. It deduplicates crash events
within a sliding TTL window while remaining bounded in memory so a noisy
crash-looping pod can never exhaust the host's RAM.

It is intentionally dependency-free and thread-safe so it can be shared by
the (synchronous, generator-based) K8s watcher running in its own thread.
"""

import time
import threading
from collections import OrderedDict


class IdempotencyCache:
    """
    TTL + size bounded de-duplication cache.

    - `ttl_seconds`: how long a hash is considered a duplicate.
    - `max_size`: hard cap on tracked hashes (LRU eviction) so an unbounded
      stream of unique crashes cannot grow memory without limit.
    """

    def __init__(self, ttl_seconds: int = 300, max_size: int = 10_000):
        self.ttl_seconds = ttl_seconds
        self.max_size = max_size
        self._store: "OrderedDict[str, float]" = OrderedDict()
        self._lock = threading.Lock()

    def is_duplicate(self, key: str) -> bool:
        """
        Returns True if `key` was seen within the TTL window.
        Records/refreshes `key` as a side effect (sliding window), so a fresh
        key returns False and is remembered for subsequent calls.
        """
        now = time.time()
        with self._lock:
            self._evict_expired(now)

            ts = self._store.get(key)
            if ts is not None and (now - ts) < self.ttl_seconds:
                # Refresh recency for LRU + sliding TTL, then report duplicate.
                self._store.move_to_end(key)
                self._store[key] = now
                return True

            # New (or expired) key: record it.
            self._store[key] = now
            self._store.move_to_end(key)
            self._enforce_max_size()
            return False

    def _evict_expired(self, now: float) -> None:
        expired = [k for k, ts in self._store.items() if (now - ts) >= self.ttl_seconds]
        for k in expired:
            del self._store[k]

    def _enforce_max_size(self) -> None:
        while len(self._store) > self.max_size:
            # popitem(last=False) removes the oldest (LRU) entry.
            self._store.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._store.clear()
