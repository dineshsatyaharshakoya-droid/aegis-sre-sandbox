# Aegis SRE — Scalable Architecture & Minimal Production Build

Aegis is an autonomous SRE: it ingests crash telemetry, runs a multi-agent LangGraph "repair swarm" (research → patch → sandbox-test → security-review), and proposes a fix as a pull request, with a human approval gate. This document describes the scalable architecture and the minimal production implementation that ships alongside it.

The design goal is **one codebase, two tiers**. The same application runs as a single zero-SaaS binary on-prem, or as a horizontally-scaled cloud deployment, by switching one environment variable. This is achieved by putting every infrastructure dependency behind an interface and selecting the implementation from a profile.

---

## 1. Architecture

### Design principles

The central architectural move is **decoupling ingestion from processing**. A webhook must return in milliseconds, but a repair loop runs an LLM swarm for tens of seconds. If the HTTP handler ran the swarm inline, a burst of crashes would exhaust the web server's request workers and telemetry would be lost. So ingestion (fast: de-dup, persist, publish) and processing (slow: the graph) are separated by a durable broker, and they scale independently.

The second move is **profile-driven backends**. Each external dependency — durable store, message broker, dedup cache — is an abstract interface with two implementations: a stdlib/in-process one for on-prem, and a networked one for cloud. Application code only ever sees the interface.

```
                 ┌─────────────────────────────────────────────────────────┐
                 │                    Telemetry sources                      │
                 │   K8s watcher · Sentry · generic /webhook/crash           │
                 └───────────────┬─────────────────────────────────────────┘
                                 │ HTTP / watch
                 ┌───────────────▼───────────────┐
                 │      API process (FastAPI)     │   stateless, scale on RPS
                 │  ┌──────────────────────────┐  │
                 │  │ IncidentService.ingest()  │  │
                 │  │  1. Cache.claim (dedup)   │  │
                 │  │  2. Store.save (pending)  │  │
                 │  │  3. Broker.publish        │  │
                 │  └──────────────────────────┘  │
                 │   WebSocket /ws (live updates) │
                 └───────┬──────────────┬─────────┘
                         │              │
              ┌──────────▼───┐   ┌──────▼───────────┐
              │   Cache      │   │     Broker        │  at-least-once
              │ dedup claim  │   │ in-proc | Redis   │  delivery
              │ mem | Redis  │   │   Streams         │
              └──────────────┘   └──────┬───────────┘
                                        │ consume + ack
                 ┌──────────────────────▼──────────────────────┐
                 │   Worker process(es) — scale on queue depth  │
                 │   ConsumerRunner → LangGraph repair swarm:   │
                 │   planner→researcher→executor→sandbox→review │
                 │   (God-Node timeout wraps each run)          │
                 └───────┬───────────────────────────┬─────────┘
                         │                            │
                 ┌───────▼────────┐          ┌────────▼─────────┐
                 │  EventStore     │          │  RAG (Chroma) +  │
                 │ sqlite|postgres │          │  Sandbox (E2B/   │
                 │ system of record│          │  local) + VCS PR │
                 └─────────────────┘          └──────────────────┘
```

On the **on-prem** profile the broker is an in-process `asyncio.Queue`, so "API" and "worker" live in one process and there is a single container to operate. On the **cloud** profile the broker is Redis Streams, so the API and a fleet of worker pods are separate Deployments that scale on different signals.

### Why these boundaries

The repair swarm is the expensive, bursty, failure-prone part. Isolating it behind the broker means a crash storm queues instead of toppling the API; a hung LLM call is bounded by the per-message God-Node timeout and never blocks ingestion; and throughput scales by adding stateless worker replicas rather than rewriting code.

---

## 2. Component structure

| Layer | Module(s) | Responsibility |
|-------|-----------|----------------|
| Config | `aegis_sre/config.py` | `Settings` — resolves the profile and derives store/broker/cache backends from env. |
| Ingestion adapters | `telemetry/api_receiver.py`, `telemetry/k8s_watcher.py` | Normalize Sentry / K8s / generic payloads into a `TelemetryEvent`. |
| Application core | `core/service.py` | `IncidentService` (ingest) and `ConsumerRunner` (process). Backend-agnostic; no model/graph imports. |
| Infra: store | `infra/store.py` | `EventStore` → `SqliteEventStore`, `PostgresEventStore`. System of record + crash recovery. |
| Infra: broker | `infra/broker.py` | `Broker` → `InProcessBroker`, `RedisStreamBroker`. Decouples ingest from process. |
| Infra: cache | `infra/cache.py` | `Cache` → `InMemoryCache`, `RedisCache`. Atomic idempotency `claim()`. |
| Infra: factory | `infra/factory.py` | The only module that maps a profile to concrete classes. |
| Orchestrator | `orchestrator/graph.py` + nodes | The LangGraph repair swarm and its agents (unchanged domain logic). |
| Tooling | `orchestrator/rag_engine.py`, `sandbox_engine.py`, `vcs_provider.py`, `safety.py` | RAG retrieval, sandboxed compile/test, PR creation, unified safety policy. |
| Entrypoints | `main.py --api`, `worker.py` | API server; standalone worker (cloud tier). |

The dependency rule is one-directional: adapters and entrypoints depend on the core; the core depends on infra **interfaces**; only the factory depends on concrete infra. The orchestrator is a pluggable `processor` injected into `ConsumerRunner`, so the core can be unit-tested with no LLM stack present.

---

## 3. Data flow

### Ingest (fast path, API process)

1. A source posts a crash; the adapter normalizes it to a `TelemetryEvent`.
2. `IncidentService.ingest()` computes a stable signature `sha256(service:crash_tail)`.
3. `Cache.claim(signature, ttl)` is an **atomic** test-and-set. Duplicate within the TTL window → return `ignored` (no work done). This closes the check-then-act race the previous code had.
4. `EventStore.save_incoming_event(...)` records the incident as `pending` (durable).
5. `Broker.publish(event)` hands it off. If the broker is full → return `dropped` (HTTP 429) so the source backs off. The HTTP response returns here, in milliseconds.

### Process (slow path, worker)

6. `ConsumerRunner` pulls a delivery from the broker.
7. It runs the injected `processor` (the LangGraph swarm) under `asyncio.wait_for(timeout)` — the God-Node kill switch.
8. On success → `EventStore.mark_event_status(id, "completed")`; on timeout/error → `"failed"`.
9. It **ACKs** the delivery only after the terminal status is written, giving at-least-once delivery: a worker that crashes mid-run leaves the message in the broker's pending list for redelivery.
10. Node-by-node progress streams to dashboard clients over WebSocket (on-prem direct; cloud via Redis pub/sub fan-out).

### Recovery

On startup `IncidentService.recover_pending()` reads `status='pending'` rows and re-publishes them, so events accepted but not yet processed survive a restart.

---

## 4. API design

All endpoints are on the FastAPI app (`aegis_sre.telemetry.api_receiver:app`).

| Method | Path | Purpose | Success | Notes |
|--------|------|---------|---------|-------|
| POST | `/webhook/crash` | Generic ingestion of a `TelemetryEvent`. | `202 {status: accepted, hash}` | `429` when at capacity; `{status: ignored}` for duplicates. |
| POST | `/webhook/sentry` | Sentry alert adapter; parses + normalizes. | `202 {status: accepted, source: sentry}` | Tolerant of partial Sentry payloads. |
| GET | `/incidents` | Recent incident history for the dashboard. | `200 {incidents: [...]}` | Reads from `EventStore`. |
| GET | `/health` | Liveness probe. | `200 {status: healthy}` | Process is up. |
| GET | `/ready` | Readiness probe. | `200 {status: ready}` / `503` | Backends wired — gate LB/k8s traffic. |
| WS | `/ws` | Live incident stream + human approval. | event frames | Client sends `{action: approve_patch, file}` (path-traversal guarded). |

`TelemetryEvent`: `{ event_id, service_name, crash_log, metadata }`.
Ingest response contract: `status ∈ {accepted, ignored, dropped}` + `hash`.

WebSocket frame types: `telemetry_received`, `node_update`, `patch_ready` (with `root_cause_analysis`, `explanation`, `diff`), `error`, `patch_deployed`.

---

## 5. Database schema

A single system-of-record table; idempotency lives in the cache layer (fast, ephemeral), not the DB.

**On-prem (SQLite)**

```sql
CREATE TABLE incoming_events (
    event_id     TEXT PRIMARY KEY,        -- natural idempotency key
    service_name TEXT,
    payload_json TEXT,                     -- full TelemetryEvent JSON
    status       TEXT,                     -- pending | completed | failed
    created_at   REAL                      -- epoch seconds
);
CREATE INDEX idx_events_created_at ON incoming_events(created_at DESC); -- history feed
CREATE INDEX idx_events_status     ON incoming_events(status);          -- recovery scan
-- PRAGMA journal_mode=WAL for read/write concurrency.
```

**Cloud (Postgres)**

```sql
CREATE TABLE incoming_events (
    event_id     TEXT PRIMARY KEY,
    service_name TEXT,
    payload_json JSONB,                                    -- queryable
    status       TEXT NOT NULL DEFAULT 'pending',
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_events_created_at ON incoming_events(created_at DESC);
CREATE INDEX idx_events_status     ON incoming_events(status) WHERE status = 'pending'; -- partial: cheap recovery
```

The partial index on Postgres keeps the recovery scan O(pending) rather than O(all incidents). `INSERT ... ON CONFLICT (event_id) DO NOTHING` makes persistence idempotent even under redelivery.

Vector data (RAG codebase + SRE skills) lives in ChromaDB, separate from the relational store — it has a different access pattern (embedding similarity) and lifecycle (rebuilt on ingest).

---

## 6. Caching strategy

Caching is used at four layers, each with a clear invalidation rule:

1. **Idempotency / de-dup (critical).** `Cache.claim(signature, ttl=300s)` is an atomic test-and-set. On-prem: in-memory, TTL + LRU-bounded so a crash-looping pod can't exhaust RAM. Cloud: Redis `SET NX EX`, which is atomic across all API replicas — essential because dedup must hold cluster-wide, not per-process. TTL expiry is the invalidation: a genuinely recurring incident is re-raised after the window.
2. **RAG embeddings.** Chunk → embedding is deterministic and expensive (a model call). Cache by content hash so unchanged files are never re-embedded; invalidate when file content changes.
3. **VCS file fetches.** `researcher_node` pulls source for stack-trace files. Cache per `(repo, path, commit_sha)` with a short TTL; the commit SHA makes it self-invalidating.
4. **LLM responses (optional).** Identical `(model, prompt)` can be cached briefly to cut cost on retry storms; keep the TTL short since determinism isn't guaranteed.

What is deliberately **not** cached: the durable incident status (must be strongly consistent) and security-review verdicts (must be fresh per patch — caching a "safe" verdict would reintroduce a fail-open hole).

---

## 7. Tiered deployment

| Concern | On-prem (`AEGIS_PROFILE=onprem`) | Cloud (`AEGIS_PROFILE=cloud`) |
|---------|----------------------------------|-------------------------------|
| Store | SQLite (WAL) | Managed Postgres |
| Broker | in-process `asyncio.Queue` | Redis Streams (consumer groups) |
| Dedup cache | in-memory (TTL+LRU) | Redis `SET NX EX` |
| Topology | one container (API + consumer) | API Deployment + Worker Deployment |
| Models | local Ollama (zero-SaaS) | Ollama / NIM / HF |
| Scaling | vertical | API autoscale on RPS; workers autoscale on Redis stream depth |
| Compose | `deploy/docker-compose.onprem.yml` | `deploy/docker-compose.cloud.yml` |

Switching tiers is one env var; backends are otherwise auto-derived (and individually overridable via `AEGIS_STORE` / `AEGIS_BROKER` / `AEGIS_CACHE`).

### Scaling characteristics

- **API** is stateless → scale horizontally behind a load balancer; dedup is cluster-wide via Redis so duplicates are caught regardless of which replica receives them.
- **Workers** are stateless and competing consumers on the Redis Streams group → add replicas to raise throughput; the pending-entries list gives crash recovery for in-flight messages.
- **Postgres** is the eventual write bottleneck; mitigations are the indexes above, batching status updates, and partitioning `incoming_events` by time if retention grows.
- **Bounded blast radius:** queue back-pressure (429) protects the system under a crash storm; the God-Node timeout bounds any single repair; fail-closed safety (see below) prevents an LLM outage from auto-deploying unreviewed code.

---

## 8. Implementation code (what ships in this repo)

The minimal production version is implemented and verified:

- `aegis_sre/config.py` — profile + backend resolution.
- `aegis_sre/infra/{cache,broker,store,factory}.py` — both implementations of each interface.
- `aegis_sre/core/service.py` — `IncidentService` (ingest) + `ConsumerRunner` (process).
- `worker.py` — cloud worker entrypoint; `main.py --api` — API + on-prem consumer.
- `aegis_sre/telemetry/api_receiver.py` — rewired to the core, with `/ready` and 429 back-pressure.
- `deploy/docker-compose.{onprem,cloud}.yml`, `requirements-cloud.txt`.

This layer builds on the production-bug fixes documented in `DEBUG_REPORT.md` (restored dedup cache, fail-closed safety, atomic idempotency, clean queue shutdown, feedback-aware retries).

### Verification

`python -m py_compile` passes for all modules. An end-to-end test of the on-prem path asserts: first claim wins / duplicate dropped; `pending → completed` lifecycle; broker delivery + ack; pending-event recovery; and full-broker back-pressure. The cloud profile is asserted to select Postgres/Redis backends and to fail fast when `AEGIS_DATABASE_URL` is missing.

```
profile: onprem | store: sqlite broker: inprocess cache: memory
claim first/dup (expect True/False): True False
incident status (expect completed): completed
pending payloads count (expect 1): 1
second publish on full broker (expect False): False
ALL E2E ASSERTIONS PASSED
```

### Run it

```bash
# On-prem (single binary): SQLite + in-process queue + in-memory dedup
AEGIS_PROFILE=onprem python main.py --api

# Cloud (scaled): Postgres + Redis Streams + Redis dedup; separate workers
docker compose -f deploy/docker-compose.cloud.yml up --scale worker=3
```

---

## 9. Roadmap (not yet built)

Behavioral sandbox testing (run the failing case against the patch, not just `py_compile`); Redis pub/sub WebSocket fan-out so any API replica streams updates from any worker; Prometheus metrics (ingest rate, queue depth, repair latency, deploy success) + OpenTelemetry traces across ingest→process; per-tenant isolation and rate limits for multi-tenant cloud; dead-letter stream for poison messages; and wiring the human `approve_patch` action to actually call `vcs.create_pull_request` (currently a no-op).
