"""
Central configuration for Aegis SRE.

A single `Settings` object selects the deployment *profile* and wires every
pluggable backend (store / broker / cache) accordingly. The same application
code runs unchanged on both tiers:

  - profile=onprem  -> SQLite  + in-process asyncio queue + in-memory cache
  - profile=cloud   -> Postgres + Redis Streams broker     + Redis cache

Everything is environment driven so a container only needs env vars to switch
tiers. No secrets are hardcoded.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Optional

# Load a local .env (if present) so on-prem runs pick up AEGIS_* overrides.
# Guarded so python-dotenv stays optional; load_dotenv never overrides a real
# environment variable that is already set (explicit env wins).
try:
    from dotenv import load_dotenv as _load_dotenv
    _load_dotenv()
except Exception:  # noqa: BLE001 - dotenv optional; absence must not break import
    pass


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


@dataclass
class Settings:
    # --- Profile -----------------------------------------------------------
    # "onprem" (default, zero-SaaS) or "cloud" (scaled).
    profile: str = field(default_factory=lambda: _env("AEGIS_PROFILE", "onprem").lower())

    # --- Backend selection (auto-derived from profile, overridable) ---------
    store_backend: str = field(default_factory=lambda: _env("AEGIS_STORE", "").lower())
    broker_backend: str = field(default_factory=lambda: _env("AEGIS_BROKER", "").lower())
    cache_backend: str = field(default_factory=lambda: _env("AEGIS_CACHE", "").lower())

    # --- Connection strings ------------------------------------------------
    database_url: str = field(default_factory=lambda: _env("AEGIS_DATABASE_URL", ""))
    redis_url: str = field(default_factory=lambda: _env("AEGIS_REDIS_URL", "redis://localhost:6379/0"))
    sqlite_path: str = field(
        default_factory=lambda: _env(
            "AEGIS_SQLITE_PATH",
            os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "aegis_events.db"),
        )
    )
    # LangGraph checkpointer DB. Resolved to an absolute path so it does not
    # depend on the process working directory (the previous code passed the
    # relative literal "aegis_state.db", which broke when cwd differed).
    state_db_path: str = field(
        default_factory=lambda: _env(
            "AEGIS_STATE_DB",
            os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "aegis_state.db"),
        )
    )

    # --- Behavior knobs ----------------------------------------------------
    dedup_ttl_seconds: int = field(default_factory=lambda: _env_int("AEGIS_DEDUP_TTL", 300))
    queue_max_size: int = field(default_factory=lambda: _env_int("AEGIS_QUEUE_MAX", 1000))
    broker_stream: str = field(default_factory=lambda: _env("AEGIS_STREAM", "aegis.incidents"))
    broker_group: str = field(default_factory=lambda: _env("AEGIS_CONSUMER_GROUP", "aegis-workers"))
    consumer_name: str = field(
        default_factory=lambda: _env("AEGIS_CONSUMER_NAME", f"worker-{os.getpid()}")
    )
    worker_concurrency: int = field(default_factory=lambda: _env_int("AEGIS_WORKER_CONCURRENCY", 1))
    # Redis Streams: reclaim deliveries that have sat un-ACKed in another
    # consumer's pending-entries list for longer than this (ms). This is what
    # actually realises the at-least-once guarantee after a worker crash.
    broker_claim_idle_ms: int = field(default_factory=lambda: _env_int("AEGIS_CLAIM_IDLE_MS", 60_000))

    # --- Security ----------------------------------------------------------
    # Shared secret required on /webhook/crash and /ws (X-Aegis-Token / ?token=).
    # Empty => no token gate (dev/on-prem convenience); REQUIRED on the cloud
    # profile (startup fails closed if missing).
    webhook_token: str = field(default_factory=lambda: _env("AEGIS_WEBHOOK_TOKEN", ""))
    # Sentry client secret for HMAC verification of /webhook/sentry. Empty => no
    # signature check (dev); set in production.
    sentry_secret: str = field(default_factory=lambda: _env("AEGIS_SENTRY_SECRET", ""))
    # Per-client requests/minute on the webhooks. 0 => disabled.
    rate_limit_rpm: int = field(default_factory=lambda: _env_int("AEGIS_RATE_LIMIT_RPM", 0))

    # --- LLM (OpenAI-compatible endpoint; e.g. local Ollama) ----------------
    # Aegis talks to models over the OpenAI Chat Completions API. A local Ollama
    # instance exposes exactly this at /v1, so no SaaS egress is required.
    llm_base_url: str = field(default_factory=lambda: _env("AEGIS_LLM_BASE_URL", "http://localhost:11434/v1"))
    llm_api_key: str = field(default_factory=lambda: _env("AEGIS_LLM_API_KEY", "ollama"))
    # Executor / reasoning model (proposes patches).
    executor_model: str = field(default_factory=lambda: _env("AEGIS_EXECUTOR_MODEL", "hermes3:8b"))
    # Reviewer / validation model (security + logic review).
    reviewer_model: str = field(default_factory=lambda: _env("AEGIS_REVIEWER_MODEL", "qwen2.5-coder:7b"))
    # Request timeout (seconds) for a single LLM call.
    llm_timeout_seconds: int = field(default_factory=lambda: _env_int("AEGIS_LLM_TIMEOUT", 90))

    def __post_init__(self) -> None:
        # Derive backends from profile when not explicitly overridden.
        if self.profile not in ("onprem", "cloud"):
            self.profile = "onprem"
        if not self.store_backend:
            self.store_backend = "sqlite" if self.profile == "onprem" else "postgres"
        if not self.broker_backend:
            self.broker_backend = "inprocess" if self.profile == "onprem" else "redis"
        if not self.cache_backend:
            self.cache_backend = "memory" if self.profile == "onprem" else "redis"

    @property
    def is_cloud(self) -> bool:
        return self.profile == "cloud"


_settings: Optional[Settings] = None


def get_settings() -> Settings:
    """Process-wide singleton."""
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
