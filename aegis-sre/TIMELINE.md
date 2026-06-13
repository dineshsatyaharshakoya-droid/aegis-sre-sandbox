# Aegis — Build Timeline & Atomic Task Backlog

Companion to `SCALE_PLAN.md`. Every task here is **atomic** (PR-sized, ~0.5–3
days, single clear acceptance test). Durations are dev-days; the calendar assumes
a small team (2–3 engineers) running the parallel tracks below — *indicative, not
a commitment*. Solo/serial execution = the summed dev-days.

Legend: **est** = dev-days · **dep** = prerequisite task ids.

---

## Phase A — Stone 0: finish the deployable foundation  (Weeks 1–4)

| id | atomic task | est | dep | acceptance |
|----|-------------|-----|-----|------------|
| A1 | OTel spans across ingest→broker→worker→graph (optional dep, no-op fallback) | 2 | — | trace shows full path for one incident |
| A2 | Propagate trace/incident id through broker payload + structured logs | 1 | A1 | logs/traces correlate by id |
| A3 | Eval corpus: 15–20 labeled crash→expected-fix cases + schema | 2 | — | corpus loads, schema validated |
| A4 | Eval runner: run swarm headless, score (applies?/compiles?/repro pass?) | 3 | A3 | prints a fix-rate number |
| A5 | Turn `benchmark_crucible.py` into a scored CI gate w/ threshold | 1 | A4 | CI fails below threshold |
| A6 | Wire RAG ingest into API lifespan + worker startup (guarded/async) | 2 | — | live path returns non-empty RAG context |
| A7 | Incremental RAG ingest cached by content hash | 2 | A6 | unchanged files not re-embedded |
| A8 | Redis-backed approval registry (shared across replicas) | 3 | — | approval works from a 2nd replica |
| A9 | Redis-backed rate limiter (replace in-memory) | 2 | — | limit holds across replicas |
| A10 | WebSocket fan-out via Redis pub/sub | 3 | — | client sees updates from any worker |
| A11 | `recover_pending` dedup guard (claim before republish) | 2 | — | no double-process across replicas |
| A12 | 3-replica cloud integration smoke test (compose + script) | 2 | A8–A11 | green multi-replica run |

**Phase A subtotal: 25 dev-days.** Exit: dashboards + measured fix-rate + HA state. *Almost entirely additive.*

---

## Phase B — Stone 1: generalize core to Signal/Remediation  (Weeks 4–6)

| id | atomic task | est | dep | acceptance |
|----|-------------|-----|-----|------------|
| B1 | `Signal` schema (kinds) + `TelemetryEvent` back-compat adapter | 2 | — | crash path unchanged on new type |
| B2 | `Remediation` base; refactor `PatchProposal`→`CodePatch` | 2 | B1 | existing tests green |
| B3 | `ActionPlan` schema (typed steps, blast radius, dry-run plan) | 2 | B2 | schema + validators tested |
| B4 | `Validator`: patch→compile path + action→dry-run path | 3 | B3 | both branches covered |
| B5 | Executor emits `Remediation`; `should_deploy` type-agnostic | 2 | B4 | crash→PR regression green |
| B6 | `approvals` + `/ws` handle remediation polymorphism | 1 | B5 | approve a synthetic ActionPlan |
| B7 | Full regression sweep on new model | 1 | B6 | fix-rate unchanged vs A4 |

**Phase B subtotal: 13 dev-days.** Exit: model supports actions; zero behavior change.

---

## Phase C — Stone 2: MCP eyes (read-only)  (Weeks 6–9)

| id | atomic task | est | dep | acceptance |
|----|-------------|-----|-----|------------|
| C1 | MCP client + server config + tool registry w/ risk class | 3 | B1 | registry lists read/act tools |
| C2 | Prometheus read tool; researcher pulls live metrics | 2 | C1 | diagnosis includes live metric |
| C3 | Logs/Datadog read tool | 2 | C1 | log context retrieved |
| C4 | Alertmanager webhook adapter → `Signal(metric_alert)` | 2 | B1 | alert triggers swarm |
| C5 | Datadog/PagerDuty webhook adapters | 2 | C4 | both sources normalize |
| C6 | Per-tool metrics (calls/latency) | 1 | C1 | counters visible on /metrics |
| C7 | Eval: diagnosis quality with vs without live context | 2 | C2,A4 | delta reported |

**Phase C subtotal: 14 dev-days.** Exit: alert-triggered, live-context diagnosis (still advisory).

---

## Phase D — Stone 3: MCP hands (gated execution)  (Weeks 9–13)  ← sellable product

| id | atomic task | est | dep | acceptance |
|----|-------------|-----|-----|------------|
| D1 | `policy.py`: dry-run default, blast-radius caps, risk-tiered approval | 3 | B3 | policy unit tests |
| D2 | MCP `act` tool execution path (gated) | 3 | C1,D1 | act tool blocked without approval |
| D3 | `ActionPlan` execution in deploy path | 3 | B5,D2 | approved plan runs |
| D4 | Post-action verification (re-read triggering metric) | 2 | D3,C2 | confirms resolve/regress |
| D5 | Rollback/compensation on regression | 3 | D4 | forced-fail auto-rolls back |
| D6 | Action audit records + metrics | 2 | D3 | every action audited |
| D7 | Staging e2e: alert→diagnose→approve→execute→verify→rollback | 3 | D5,D6 | green on test cluster |

**Phase D subtotal: 19 dev-days.** Exit: the differentiated, sellable actionable agent (#2).

---

## Phase E — Stone 4: productionize live actions  (Weeks 13–17)

| id | atomic task | est | dep | acceptance |
|----|-------------|-----|-----|------------|
| E1 | Idempotent actions + concurrent-remediation dedup | 2 | D3 | no double execution |
| E2 | Secrets mgmt + per-integration credential scoping | 3 | D2 | no plaintext creds; scoped |
| E3 | Multi-tenant isolation + per-tenant quotas | 5 | A8,A9 | tenants isolated |
| E4 | CI pipeline (lint, tests, eval gate) | 2 | A5 | PRs gated |
| E5 | Alembic migrations for Postgres | 2 | — | versioned schema |
| E6 | Dead-letter queue for poison signals | 2 | — | poison inspectable |
| E7 | Runbooks + SLO doc | 2 | D7 | on-call ready |

**Phase E subtotal: 18 dev-days.** Exit: reference customer pilot-ready.

---

## Phase F — Stone 5: HPC/GPU vertical pack  (Weeks 17–27)  ← premium wedge

| id | atomic task | est | dep | acceptance |
|----|-------------|-----|-----|------------|
| F1 | DCGM/NVML metric ingestion → `Signal` | 3 | B1 | GPU metrics flow |
| F2 | NCCL + job log ingestion | 3 | B1 | collective-hang signal seen |
| F3 | Slurm/Ray/K8s device-plugin state adapter | 3 | B1 | node/job state ingested |
| F4 | HPC failure-mode skills into RAG | 3 | A6 | retrieval hits HPC skills |
| F5 | ActionPlan: cordon/drain faulty GPU node | 2 | D3 | node drained (gated) |
| F6 | ActionPlan: requeue/restart job | 2 | D3 | job requeued |
| F7 | ActionPlan: retune parallelism/batch/sharding | 3 | D3 | config applied |
| F8 | eBPF/perf/nvidia-smi deeper probe (iterative) | 5 | F1 | low-level fault surfaced |
| F9 | HPC eval corpus + scored remediation rate | 3 | A4 | HPC fix-rate number |
| F10 | GPU test-cluster e2e on ≥2 failure classes | 5 | F5–F9 | verified remediation |

**Phase F subtotal: 32 dev-days.** Exit: autonomous verified GPU-cluster remediation. *~30–40% reuse; rest is new + needs domain expertise.*

---

## Phase G — Stone 6: industrial edge (optional, defer)  (post-27)

Streaming/OT ingestion (MQTT/OPC-UA/Modbus), time-series anomaly detection,
operational ActionPlans on the on-prem profile. **~20 dev-days.** Pursue only with
a design-partner facility.

---

## Roll-up & read-outs

| milestone | cumulative dev-days | small-team calendar |
|-----------|---------------------|---------------------|
| Foundation solid (A) | 25 | ~Week 4 |
| Model generalized (B) | 38 | ~Week 6 |
| Live-context diagnosis (C) | 52 | ~Week 9 |
| **Sellable actionable agent (D)** | **71** | **~Week 13 (≈ 3 months)** |
| Pilot-ready (E) | 89 | ~Week 17 |
| **HPC wedge MVP (F)** | **121** | **~Week 27 (≈ 6 months)** |

- **Critical path to product:** A → B → C → D. Nearly all additive to today's code.
- **Two headline dates:** *actionable product* ≈ Week 13; *HPC premium* ≈ Week 27.
- HA state (A8–A11) is a hard prerequisite for any live multi-replica action (Phase D).
- Phases B/C and E/F have internal parallelism; the calendar assumes 2–3 engineers.

## Parallel-track view (small team)

```
Wk:  1   2   3   4   5   6   7   8   9  10  11  12  13 ... 17 ... 27
A : [=========obs/eval/HA========]
B :                 [==generalize==]
C :                       [====MCP eyes====]
D :                                   [=====MCP hands (GATED)=====]
E :                                                 [==productionize==]
F :                                                       [========HPC vertical========]
```

## Definition of done (every task)

Code + an automated test (or a documented manual acceptance for infra-bound
items) + metrics/audit where it touches a new action + no regression in the
eval fix-rate. Live-action tasks additionally require a policy entry and an audit
record before they can run unattended.
