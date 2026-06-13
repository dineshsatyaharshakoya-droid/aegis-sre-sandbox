"""
Authentication & abuse-protection primitives for the telemetry webhooks.

These are deliberately framework-free, constant-time, and side-effect-free so
they can be unit-tested in isolation and reused by any transport (HTTP webhook,
WebSocket). The FastAPI handlers in `api_receiver.py` are thin wrappers over
these functions.

Design notes:
  - All comparisons use `hmac.compare_digest` to avoid timing side channels.
  - An unconfigured secret (`expected`/`secret` falsy) means "no gate" so local
    / on-prem dev keeps working; production posture (require a token on the
    cloud profile) is enforced at startup, not here.
"""

from __future__ import annotations

import hashlib
import hmac
import threading
import time
from collections import defaultdict, deque
from typing import Optional


def verify_token(provided: Optional[str], expected: Optional[str]) -> bool:
    """Return True iff the request is authorised.

    - `expected` falsy -> no token configured -> allow (gate disabled).
    - `expected` set    -> require an exact, constant-time match.
    """
    if not expected:
        return True
    if not provided:
        return False
    return hmac.compare_digest(provided, expected)


def verify_sentry_signature(body: bytes, signature: Optional[str], secret: Optional[str]) -> bool:
    """Verify a Sentry webhook HMAC-SHA256 signature over the raw request body.

    - `secret` falsy -> no signature check configured -> allow.
    - otherwise the hex digest of HMAC(secret, body) must match `signature`.
    """
    if not secret:
        return True
    if not signature:
        return False
    expected = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


class SlidingWindowRateLimiter:
    """Thread-safe per-key sliding-window limiter.

    `max_per_minute <= 0` disables limiting. State is in-process, which is the
    right first step for a single API instance; a multi-replica cloud deployment
    should back this with Redis so the limit holds cluster-wide.
    """

    def __init__(self, max_per_minute: int, window_seconds: float = 60.0, sweep_threshold: int = 10_000):
        self.max = max_per_minute
        self.window = window_seconds
        # When the key count exceeds this, evict fully-expired keys so a flood of
        # distinct (e.g. spoofed X-Forwarded-For) keys cannot exhaust memory.
        self.sweep_threshold = sweep_threshold
        self._hits: "defaultdict[str, deque]" = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, key: str, now: Optional[float] = None) -> bool:
        if self.max <= 0:
            return True
        now = time.time() if now is None else now
        cutoff = now - self.window
        with self._lock:
            if len(self._hits) > self.sweep_threshold:
                self._sweep(cutoff)
            dq = self._hits[key]
            while dq and dq[0] < cutoff:
                dq.popleft()
            if len(dq) >= self.max:
                return False
            dq.append(now)
            return True

    def _sweep(self, cutoff: float) -> None:
        """Drop keys whose most recent hit is older than the window (caller holds lock)."""
        stale = [k for k, dq in self._hits.items() if not dq or dq[-1] < cutoff]
        for k in stale:
            del self._hits[k]
