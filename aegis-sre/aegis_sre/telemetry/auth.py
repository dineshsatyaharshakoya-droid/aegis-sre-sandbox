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


class RedisRateLimiter:
    """Cluster-wide fixed-window limiter (A9). The same per-client limit holds
    across all API replicas because the counter lives in Redis. `allow()` is
    async (a Redis round-trip); `max_per_minute <= 0` disables limiting."""

    def __init__(self, redis_url: str, max_per_minute: int, window_seconds: int = 60):
        self._url = redis_url
        self.max = max_per_minute
        self.window = window_seconds
        self._client = None

    async def _conn(self):
        if self._client is None:
            import redis.asyncio as redis
            self._client = redis.from_url(self._url, decode_responses=True)
        return self._client

    async def allow(self, key: str, now: Optional[float] = None) -> bool:
        if self.max <= 0:
            return True
        now = time.time() if now is None else now
        bucket = int(now // self.window)
        rk = f"aegis:rl:{key}:{bucket}"
        client = await self._conn()
        count = await client.incr(rk)
        if count == 1:
            await client.expire(rk, self.window + 1)
        return count <= self.max


def build_rate_limiter(settings):
    """Redis limiter on the cloud tier (limit holds across replicas); in-memory
    sliding window on-prem."""
    if settings.cache_backend == "redis" or settings.is_cloud:
        return RedisRateLimiter(settings.redis_url, settings.rate_limit_rpm)
    return SlidingWindowRateLimiter(settings.rate_limit_rpm)


# --- Identity + RBAC (red-team Batch 3 / S1) ---------------------------------
# Replaces the single anonymous shared token with per-identity API keys carrying
# a role, so actions are authorized AND attributable. Back-compat: no keys + no
# token => open (dev); a legacy `webhook_token` acts as a single admin key.
from dataclasses import dataclass  # noqa: E402

ROLE_RANK = {"ingest": 1, "approver": 2, "admin": 3}


@dataclass(frozen=True)
class Identity:
    name: str
    role: str


class IdentityRegistry:
    def __init__(self, keys: dict, legacy_token: str = ""):
        self._keys = dict(keys)          # token -> Identity
        self._legacy = legacy_token or ""

    @property
    def auth_configured(self) -> bool:
        return bool(self._keys or self._legacy)

    def resolve(self, token: Optional[str]) -> Optional[Identity]:
        """Identity for a presented token, or None if unauthorized. When no auth
        is configured at all, returns an anonymous admin (open dev posture)."""
        if not self.auth_configured:
            return Identity("anonymous", "admin")
        if token:
            ident = self._keys.get(token)
            if ident is not None:
                return ident
            if self._legacy and hmac.compare_digest(token, self._legacy):
                return Identity("legacy-token", "admin")
        return None

    def authorized(self, token: Optional[str], min_role: str) -> Optional[Identity]:
        """Return the Identity iff it meets `min_role`, else None."""
        ident = self.resolve(token)
        if ident is None:
            return None
        if ROLE_RANK.get(ident.role, 0) >= ROLE_RANK[min_role]:
            return ident
        return None


def build_identity_registry(settings) -> IdentityRegistry:
    """Parse AEGIS_API_KEYS = 'key:name:role,key2:name2:role2'; fall back to the
    legacy single webhook_token (treated as an admin key)."""
    keys: dict = {}
    raw = getattr(settings, "api_keys", "") or ""
    for entry in [e for e in raw.split(",") if e.strip()]:
        parts = entry.split(":")
        if len(parts) >= 3 and parts[2] in ROLE_RANK:
            keys[parts[0].strip()] = Identity(parts[1].strip(), parts[2].strip())
    return IdentityRegistry(keys, settings.webhook_token)
