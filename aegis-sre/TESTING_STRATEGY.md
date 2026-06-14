# Aegis SRE — Extensive Testing Strategy & Resource Plan

Research pass (2026-06-14): what we have, the gaps, the resources/tooling to
gather, and concrete **sample sizes + load profiles** to test this product
extensively. This is the gathering/plan phase — implementation follows.

---

## 1. Current state (baseline)

| Metric | Value |
|---|---|
| Test functions | **277** (282 passing w/ parametrization) |
| Source modules | 39 |
| **Line coverage** | **79%** (2905 stmts, 602 missed) |
| Test framework | pytest 9.1, pytest-asyncio 1.4 (strict) |
| Already installed | pytest, pytest-asyncio, httpx, coverage 7.14 |
| Existing manual scripts | `storm_test.py` (50 concurrent), `benchmark_crucible.py` (latency), `fire_three_incidents.py`, `sentry_test.py`, `test_webhook.py`, `test_checkpointer.py` |
| LLM eval harness | `run_evals.py` (LLM-as-judge) + `eval/corpus.json` = **18 cases** |
| Live infra on hand | Docker ✅, Ollama ✅, Prometheus ✅; Redis/Postgres via compose |

### Coverage hot-spots (lowest → highest value to close)
| Module | Cover | What's untested |
|---|---|---|
| `orchestrator/rag_engine.py` | **33%** | ingest/embed/retrieve |
| `orchestrator/k8s_tools.py` | **48%** | the ACT (kubectl) tools |
| `infra/pubsub.py` | **51%** | Redis pub/sub WS fanout |
| `telemetry/api_receiver.py` | **55%** | lifespan, WS endpoint, webhook handlers |
| `orchestrator/sandbox_engine.py` | **59%** | Container/E2B paths, rlimit branches |
| `infra/factory.py` / `cache.py` / `tracing.py` / `tool_registry.py` | 68–80% | backend wiring, Redis cache |

> Coverage measures *line execution*, not assertion strength — mutation testing
> (§4.10) is what proves the tests actually catch bugs.

---

## 2. The 10 testing dimensions for this system

Aegis is event-driven + multi-agent + LLM-backed: webhooks (FastAPI) → broker
(in-proc / Redis Streams) → LLM swarm (LangGraph) → gated actions → approval
data-plane. Each surface needs a different kind of test.

1. **Functional unit** — pure logic. *Have it; push 79% → 90%+.*
2. **Property-based** — invariants over generated inputs (parsers, patch-apply, signer, policy).
3. **API contract / fuzz** — generate from the OpenAPI schema against the live app.
4. **Integration** — real Redis / Postgres / Prometheus (not just in-memory fakes).
5. **Concurrency / race** — exactly-once consume, double-approval, rate-limit accuracy.
6. **Load / throughput / latency / soak** — the ingest + fanout data plane.
7. **Resilience / chaos** — dependency down / slow / flapping → fail-closed or degrade.
8. **Security / adversarial** — a reusable attack corpus (extends the red-team batches).
9. **LLM quality eval** — fix-rate on a labeled crash→patch corpus with real statistics.
10. **Mutation** — does the suite kill injected bugs?

---

## 3. Sample sizes & load profiles (concrete)

### Property-based (hypothesis)
- Default **100 examples/property**; security-critical properties
  (`apply_patch_to_source`, `BlobSigner`, policy blast-radius, alert parsers)
  → `@settings(max_examples=500)`.
- Targets: signal/alert parsers round-trip, patch apply (0/1/N match invariants),
  signer wrap→unwrap == identity & any 1-bit flip → reject, policy never lets a
  `live` action through when unarmed.

### API fuzz (schemathesis, from `/openapi.json`)
- `--hypothesis-max-examples=200` per operation × ~8 endpoints ≈ **1,600 cases**.
- Asserts: no 500s, schema-conformant responses, auth enforced, oversized/malformed rejected (413/422).

### Load — ingest path `/webhook/crash` (async enqueue, **not** the LLM)
| Profile | Shape | Purpose | SLO target |
|---|---|---|---|
| Smoke | 1 user, 60s | wiring sanity | 0 errors |
| **Baseline** | 50 RPS, 5 min | p50/p95/p99 enqueue latency | p95 < 200 ms, err < 0.1% |
| Stress ramp | 10 → 500 concurrent / 10 min | find the knee | identify max stable RPS |
| **Spike** | 0 → 200 in 10 s | alert-storm | no crash, dedup holds, 429s clean |
| **Soak** | 30 RPS, 1–2 h | mem/fd leak, WS cap | flat memory, stable fds |

> The full repair loop is **LLM-bound** (seconds–minutes), so load-test the
> *enqueue* + *WS fanout*, and separately measure **worker drain rate**
> (incidents fully processed/min per worker) as the throughput number that matters.

### Concurrency / race
- **Exactly-once**: 3 workers × 300 messages → each processed exactly once, 0 dupes.
- **Double-approval**: 50 concurrent `approve()` on one incident → exactly 1 deploy.
- **Rate limiter** (cluster Redis): fire 2× limit in one window → exactly `limit` pass.
- **WS cap**: open `max_ws+50` sockets → excess rejected, server stable.

### LLM eval corpus — **biggest sample-size gap**
- Now **18 cases** → a fix-rate from n=18 has a 95% CI of **±~23 pp** (useless as a gate).
- Targets:
  - **n ≥ 100** cases, stratified by language (py/js/go), error class
    (null-deref, off-by-one, race, config, dependency, resource-leak), difficulty.
  - **k ≥ 3 seeds/case** (LLM nondeterminism); report **mean ± 95% CI**.
  - To detect a **10 pp** regression at 80% power ≈ **150–200 cases**.

### Chaos matrix
- {Redis, Postgres, Prometheus, LLM, VCS, alert-webhook} × {down, slow/timeout, error, flapping} ≈ **24 scenarios**; each asserts fail-closed or correct degradation + recovery.

---

## 4. Resources to gather

### 4.1 Python tooling (add to `requirements-dev.txt`)
| Package | Dimension |
|---|---|
| `hypothesis` | property-based (§3) |
| `schemathesis` | OpenAPI contract/fuzz |
| `locust` | load (Python, fits stack) — or `k6` binary for higher RPS |
| `fakeredis` | deterministic Redis paths in CI (closes the 51–75% Redis gaps) |
| `pytest-cov` | coverage gate in CI (`--cov-fail-under=85`) |
| `pytest-benchmark` | latency micro-SLOs |
| `pytest-xdist` | parallelize the suite (speed) |
| `respx` / `pytest-httpx` | mock outbound httpx (metrics/logs/incident/VCS) |
| `freezegun` | rate-limit windows, dedup TTL |
| `faker` | synthetic telemetry at volume |
| `mutmut` | mutation testing (test effectiveness) |

### 4.2 Infra (`docker-compose.test.yml`)
- redis (+ password/TLS to exercise Batch-4 paths), postgres, prometheus + pushgateway.
- Optional: `gitea` (real VCS for `git_tools` PR e2e), webhook sink, `toxiproxy`/`pumba` for network chaos.
- Ollama (have it) for `run_evals.py`.

### 4.3 Datasets / corpora to build
1. **Crash→fix eval corpus** — grow `eval/corpus.json` 18 → 100+ (stratified, labeled diffs).
2. **Attack corpus** (`tests/corpora/attacks/`) — prompt-injection strings, path-traversal paths, oversized/binary payloads, malformed alert JSON per source (alertmanager/datadog/pagerduty/sentry), forged/tampered Redis approval blobs.
3. **Load payload generator** — faker-based realistic crash logs for storm/soak.

---

## 5. Proposed execution order
1. **✅ DONE — coverage 79% → 90%** (324 tests). Tooling installed
   (`requirements-dev.txt`); closed hot-spots: k8s_tools 48→100, pubsub 51→92,
   api_receiver 55→87 (lifespan + WS + sentry adapter), sandbox_engine 59→78
   (Container/E2B mocked), infra factory/cache, tool_registry, rag AST splitter.
   *Note: run coverage via `./venv/bin/python -m pytest --cov` (the bare
   `./venv/bin/coverage` shim resolves to a stray Python 3.14 where asyncpg fails).*
2. **Property-based** suite for parsers + security-critical invariants.
3. **Attack corpus** as data-driven security tests (consolidates Batches 1–5).
4. **API fuzz** (schemathesis) in CI.
5. **Load harness** (locust) + SLO assertions; wire `storm/soak` profiles.
6. **Concurrency/race** + **chaos** matrix against compose infra.
7. **Grow the eval corpus** to 100+ and turn `run_evals.py` into a real CI quality gate.
8. **Mutation testing** pass to validate suite strength; fix survivors.

CI gate proposal: `pytest --cov-fail-under=85`, schemathesis green, eval fix-rate ≥ threshold, load smoke p95 within SLO.
