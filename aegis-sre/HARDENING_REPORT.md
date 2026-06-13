# Aegis SRE — Hardening Pass

A four-perspective pass (Architect → Engineer → Reviewer → Optimizer) over the
**existing** `aegis-sre` code. Unlike `DEBUG_REPORT.md` (which fixed crash-level
defects), this pass hardens reliability, performance, and lifecycle on code that
already ran. Severity legend: 🔴 reliability/correctness gap · 🟠 lifecycle/robustness · 🟡 perf/hygiene.

| # | Severity | File | Issue addressed |
|---|----------|------|-----------------|
| 1 | 🔴 | `infra/broker.py` | Redis at-least-once was a lie: crashed-worker messages were never reclaimed from the PEL |
| 2 | 🟠 | `telemetry/api_receiver.py` | No graceful shutdown; deprecated `@app.on_event`; consumer task + backends leaked on exit |
| 3 | 🟡 | `api_receiver.py`, `worker.py` | A fresh SQLite checkpointer was opened/closed per incident, on a cwd-relative path |
| 4 | 🟠 | `api_receiver.py` | Top-level LangGraph/LLM imports made the API (and tests) un-importable without the full stack |
| 5 | 🟡 | `api_receiver.py` | `broadcast()` used a bare `except:` that could swallow `CancelledError` |

---

## 1 — Redis Streams never reclaimed orphaned deliveries 🔴

**Architect.** `ARCHITECTURE.md` §3/§7 promise at-least-once delivery: "a worker
that crashes mid-run leaves the message in the broker's pending list for
redelivery." But `RedisStreamBroker.consume()` only ever issued
`XREADGROUP ... >`, which returns **new** messages exclusively. A message that a
crashed consumer read but never `XACK`ed stayed in that consumer's
pending-entries list (PEL) forever — silently lost, never reprocessed. The
guarantee the rest of the system relies on did not actually hold.

**Engineer.** `consume()` now reclaims before reading new work. A new
`_reclaim_idle()` issues `XAUTOCLAIM` for messages idle longer than
`claim_idle_ms` (default 60s, env `AEGIS_CLAIM_IDLE_MS`), walking the PEL via a
rolling cursor so a backlog drains in bounded batches. Tombstoned entries
(claimed but since deleted from the stream) are `XACK`ed to clear them.

**Reviewer.** `test_redis_broker_reclaims_orphaned_delivery` injects an
in-memory `FakeRedis`: a message read by `consumerA` and left un-ACKed is, after
aging past the idle threshold, reclaimed by `consumerB`; the tombstone path is
asserted to ACK-and-skip. Reclaim is best-effort (`consume()` logs and continues
on error) so a transient `XAUTOCLAIM` failure can't kill the consumer loop.

---

## 2 — No graceful shutdown; deprecated startup hook 🟠

**Architect.** Startup wiring lived in `@app.on_event("startup")` (deprecated in
current FastAPI, flagged in `DEBUG_REPORT.md`) and there was **no** shutdown
hook. On exit the in-process consumer task was abandoned mid-`get()`, and the
broker/store/cache (and their SQLite WAL / Redis / Postgres connections) were
never closed.

**Engineer.** Migrated to a single `lifespan` async context manager.
Startup builds backends, recovers pending events, opens the shared checkpointer,
and launches the consumer. Shutdown now `stop()`s the runner, cancels and awaits
its task, exits the checkpointer, and `close()`s broker, store, and cache —
each guarded so one failure can't block the rest.

**Reviewer.** Lifecycle requires the full ASGI stack to exercise end-to-end, so
it's covered by compile + review here; the ingest/consume halves it orchestrates
are unit-tested (`test_ingest_*`, `test_consumer_runner_*`).

---

## 3 — Checkpointer opened per incident, on a cwd-relative path 🟡

**Optimizer.** Both `trigger_repair_loop` (API) and the worker did
`AsyncSqliteSaver.from_conn_string("aegis_state.db")` **inside the per-event
path**, opening and tearing down a SQLite connection for every single incident,
against a literal relative path that broke whenever the process cwd differed.

**Engineer.** The checkpointer is now opened **once** — in the API lifespan and
in `worker.main()` — and reused across all incidents via `make_processor(checkpointer)`
(the graph is also built once per worker). The path comes from the new
`settings.state_db_path` (env `AEGIS_STATE_DB`), resolved to an absolute path.

**Reviewer.** `test_config_profile_derivation_and_state_path` asserts the path is
absolute and ends in `aegis_state.db`.

---

## 4 — API un-importable without the LLM stack 🟠

**Architect.** `api_receiver.py` imported `build_graph` and `AsyncSqliteSaver` at
module top, so importing the app — even just to hit `/health`, `/ready`, or the
ingest path — required langgraph + litellm + the whole orchestrator to be
installed and importable. That also made the existing `test_api.py` impossible to
run without the full stack.

**Engineer.** Those imports are now deferred into the lifespan and into
`trigger_repair_loop`, so the module imports cleanly with only FastAPI present.

---

## 5 — Bare `except` in WebSocket broadcast 🟡

**Engineer.** `ConnectionManager.broadcast` caught bare `except:`, which would
also swallow `CancelledError`/`KeyboardInterrupt` during shutdown. Narrowed to
`except Exception`.

---

## Verification

- `python -m py_compile` passes for the entire `aegis_sre` package plus `worker.py` / `main.py`.
- A new dependency-light suite, `aegis_sre/tests/test_hardening.py`, runs under
  pytest **or** standalone (`python -m aegis_sre.tests.test_hardening`) and needs
  no Redis/Postgres/LLM:

  ```
  9/9 hardening tests passed
  ```

  Covering: config profile derivation + absolute state path; `InMemoryCache`
  claim TTL/LRU; `SqliteEventStore` lifecycle; `InProcessBroker` ack-balance +
  back-pressure; **Redis orphan recovery** (FakeRedis); factory cloud fail-fast;
  `IncidentService.ingest` (accept/dedup/drop); and `ConsumerRunner`
  at-least-once ack ordering + poison-message handling.

## Not changed (recommended next)

- Webhook endpoints (`/webhook/crash`, `/webhook/sentry`, `/ws`) remain
  unauthenticated — anyone who can reach the API can trigger the (expensive) LLM
  swarm. A shared-secret / Sentry HMAC check is the obvious next hardening step.
- `approve_patch` over the WebSocket still never calls `vcs.create_pull_request`
  (already on the §9 roadmap).
- With `AEGIS_WORKER_CONCURRENCY > 1`, all runners share one broker instance and
  consumer name; giving each runner a unique consumer name would make the
  per-consumer PEL semantics cleaner.
