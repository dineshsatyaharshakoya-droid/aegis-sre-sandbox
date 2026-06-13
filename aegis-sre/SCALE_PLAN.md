# Aegis — Scale Plan: from crash-to-PR to a hardware-adjacent, actionable SRE agent

A stone-by-stone plan to move Aegis from where it is today (an autonomous
*code-repair* agent: crash → patch → PR) to the high-value positioning:
a **safe, live-actionable, MCP-connected SRE agent** that can diagnose and
remediate the hardware-adjacent infrastructure standard SaaS monitoring can't —
with **HPC/GPU** as the premium vertical wedge.

Effort sizes are indicative T-shirts (S ≈ 1–2 wk, M ≈ 3–5 wk, L ≈ 6–10 wk,
XL ≈ 10+ wk) for a small team, not commitments.

---

## 0. The honest starting point

What we have (reusable chassis): event ingestion → atomic dedup → durable
broker (at-least-once, orphan recovery) → bounded multi-agent swarm → fail-closed
safety → **human-approved action** → audit trail; profile-driven on-prem/cloud;
local-Ollama (air-gap friendly); metrics (`/metrics`) now emitting.

Two hard limiters that gate every target market:

1. **Trigger modality** is a *discrete crash/stack-trace* (`TelemetryEvent.crash_log`).
   The target markets are *metric/alert/stream* driven.
2. **Remediation modality** is a *source-code patch → PR* (`PatchProposal`).
   The target markets are mostly fixed by *live actions* (restart, cordon,
   requeue, retune, runbook), not code edits.

Everything below is organized around relaxing those two limiters without throwing
away the chassis. The single architectural throughline:

> **Signal → Diagnosis → Remediation**, where Remediation is polymorphic
> (`CodePatch` *or* `ActionPlan`), actions run through **MCP** under the existing
> approval + safety gates.

Positioning sequence: **#2 Actionable/MCP is the bridge** (extends the current
architecture, sellable on its own) and is the prerequisite platform for
**#1 HPC** (a deliberate vertical pivot) and **#3 Edge** (optional expansion).

---

## Stone 0 — Finish the deployable foundation (in flight)

**Goal.** You cannot sell a live-action agent you can't see, measure, or operate.
Close the trust-and-operate gap on the *existing* product first.

**Workstreams (mapped to repo).**
- ✅ P0 safety: sandbox applies+validates patches, fail-closed; webhook/WS auth.
- ✅ P1 metrics (`telemetry/metrics.py`, `/metrics`).
- ⬜ P1 tracing: OTel spans ingest→broker→worker→graph (`core/service.py`, `graph.py`).
- ⬜ P1 eval harness: labeled crash→fix corpus + scored gate (`benchmark_crucible.py`)
  → a **measured fix rate** (this becomes the yardstick for everything later).
- ⬜ P1 wire RAG ingest into startup (`api_receiver.py` lifespan / `worker.py`).
- ⬜ P2 HA state: move approval registry, rate limiter, WS fan-out, and
  `recover_pending` dedup behind Redis (`core/approvals.py`, `telemetry/auth.py`,
  `api_receiver.py`, `core/service.py`). **Prerequisite for live multi-replica actions.**

**Reuse vs new.** ~90% reuse; additive instrumentation + Redis-backing.
**Exit criteria.** Dashboards for ingest/queue/latency/fix-rate; eval reports a
fix-rate number; on-prem and 3-replica cloud both pass an integration smoke test.
**KPI.** Measured patch fix-rate (baseline); MTTR of the agent loop; p95 repair latency.
**Size.** M.

---

## Stone 1 — Generalize the core: `Signal` and `Remediation`

**Goal.** Make the data model able to express non-crash triggers and non-code
fixes, *without changing current behavior*.

**Workstreams.**
- `orchestrator/schemas.py`: introduce `Signal` (kind ∈ {`stack_trace`,
  `metric_alert`, `telemetry_snapshot`}) carrying typed evidence; keep
  `TelemetryEvent` as the `stack_trace` specialization for back-compat.
- New `Remediation` base with two concretes: `CodePatch` (= today's
  `PatchProposal`) and `ActionPlan` (ordered, typed, parameterized steps with a
  declared blast radius and a dry-run plan).
- `graph.py`: the executor emits a `Remediation` (either type); generalize
  `sandbox_node` → a `Validator` that (a) compiles/tests a `CodePatch` (current
  engine) or (b) **dry-run/plans** an `ActionPlan`.
- `should_deploy` and `core/approvals.py` become remediation-type-agnostic.

**Reuse vs new.** ~70% reuse (graph topology, safety, approval); new schema +
validator branch. Strict regression: crash→PR path must stay green.
**Exit criteria.** All existing tests pass against the new model; a synthetic
`ActionPlan` flows through diagnose→validate(dry-run)→approve with no executor yet.
**KPI.** Zero regressions in fix-rate; ActionPlan schema covered by tests.
**Size.** M.

---

## Stone 2 — MCP integration: give the agent *eyes* (read-only)

**Goal.** Be triggered by live alerts and gather live evidence — the
"advisory → actionable" first half. Still no live mutation.

**Workstreams.**
- New `integrations/mcp_client.py`: connect MCP servers; a tool registry with a
  **risk class** per tool (`read` vs `act`). Stone 2 wires only `read` tools
  (Prometheus, Datadog/logs, `kubectl get`, cloud read APIs).
- `graph.py` researcher node: pull live metrics/log context via MCP `read` tools
  (augments today's VCS file fetch + RAG).
- New ingestion adapters → `Signal(metric_alert)`: Alertmanager / Datadog /
  PagerDuty webhooks (`telemetry/` adapters next to `api_receiver.py`,
  reusing auth + dedup).
- Metrics: per-tool call counters/latency into `telemetry/metrics.py`.

**Reuse vs new.** Chassis fully reused; new MCP client + alert adapters.
**Exit criteria.** An Alertmanager alert triggers the swarm, which fetches live
Prometheus context and produces a diagnosis + proposed remediation (PR or
ActionPlan) — execution still gated/disabled.
**KPI.** % of incidents with successful live-context retrieval; diagnosis quality
on the eval set with vs without live context.
**Size.** L.

---

## Stone 3 — Actionable: give the agent *hands* (gated execution)

**Goal.** The saleable milestone (#2): alert → diagnose → propose action → human
approve → **execute via MCP** → verify → audit.

**Workstreams.**
- `integrations/mcp_client.py`: enable `act` tools (runbook servers, kubectl
  apply/rollout, scaling, restart) behind a hard policy.
- New `orchestrator/policy.py` (extends `safety.py`): **dry-run by default**,
  blast-radius caps, approval tier derived from tool risk + scope, allow/deny
  lists per environment, complete audit record.
- `core/approvals.py` + `/ws`: approve an `ActionPlan`; on approval the executor
  runs it via MCP `act` tools (the existing PR path remains one remediation type).
- **Post-action verification**: after execution, re-read the triggering metric to
  confirm recovery; auto-rollback/compensate on regression.
- Metrics: `aegis_actions_executed_total{type,result}`, verification outcomes.

**Reuse vs new.** Approval/audit/safety reused and extended; new policy engine +
execution + verification loop.
**Exit criteria.** End-to-end, on a staging cluster: an alert is autonomously
diagnosed, an action plan is approved by a human, executed via MCP, and verified
to resolve the alert — with full audit and a working rollback on a forced failure.
**KPI.** Action success rate; % auto-verified resolutions; rollback correctness;
zero unapproved mutations (audited).
**Size.** L–XL. **← This is the differentiated, sellable product.**

---

## Stone 4 — Productionize live actions (operability + multi-tenant)

**Goal.** Safe to point at a real customer prod.

**Workstreams.** Idempotent action execution + dedup of concurrent remediations
for the same incident; per-integration credential scoping & secrets management;
**multi-tenant isolation + per-tenant quotas/rate limits** (P3 #20); CI gate
running the eval + integration suites (P3 #18); Postgres migrations / Alembic
(P3 #19); dead-letter queue for poison signals (P2 #11); runbooks/SLOs (P3 #22).
**Reuse vs new.** Hardening of Stones 1–3; mostly process/ops.
**Exit criteria.** A reference customer pilot runs with guardrails, tenant
isolation, CI-enforced eval gate, and an on-call runbook.
**KPI.** Pilot uptime; mean human-approvals-per-incident trending down safely;
incidents auto-remediated end-to-end.
**Size.** L.

---

## Stone 5 — HPC / GPU vertical pack (the premium wedge, #1)

**Goal.** Diagnose+remediate GPU-cluster failure classes that SaaS APM can't see.
This is a deliberate vertical built *on* Stones 1–4, not a feature of crash→PR.

**Workstreams.**
- **Telemetry (Signals):** ingest DCGM-exporter / NVML metrics, NCCL + job logs,
  and Slurm/Ray/K8s device-plugin state → `Signal(telemetry_snapshot|metric_alert)`.
  Start with existing exporters; add deeper probes (eBPF/perf, `nvidia-smi`,
  PCIe/NVLink counters) iteratively — this is the "systems-level" depth, sourced
  from established tooling first rather than hand-written C.
- **Knowledge:** HPC failure-mode skills into the RAG skills store (`rag_engine.py`):
  CUDA OOM, NCCL collective hang, ECC errors, thermal/power throttling, stragglers,
  data-movement stalls.
- **Remediation (ActionPlans):** cordon/drain a faulty GPU node, requeue/restart a
  job, isolate a bad GPU, retune parallelism/batch-size/sharding — all via the
  Stone-3 gated action path.
- **Eval:** an HPC fault corpus + scored remediation rate (extends Stone 0 harness).

**Reuse vs new.** Control plane, MCP, policy, approval, verification all reused
(~30–40%); telemetry adapters, HPC knowledge, and HPC actions are net-new and
need domain expertise.
**Exit criteria.** Autonomous, verified remediation of ≥2 real GPU-cluster failure
classes on a test cluster, gated and audited.
**KPI.** HPC remediation success rate; GPU-hours protected / downtime avoided.
**Size.** XL. **← Highest value, largest lift, least leverage from today's code.**

---

## Stone 6 — Industrial edge / facilities pack (optional expansion, #3)

**Goal.** Central nervous system for air-gapped physical facilities, leveraging
the on-prem zero-SaaS profile.

**Workstreams.** Streaming ingestion (MQTT/OPC-UA/Modbus) → continuous `Signal`
stream; anomaly detection on time-series (vs stack-trace parsing); operational
ActionPlans (setpoint adjust, failover, operator alert); strict on-prem/offline
operation. **Reuse vs new.** On-prem profile + safety + approval reused; ingestion
+ detection paradigm is new. **Exit criteria.** A facility pilot. **Size.** XL.
*(Pursue only if a design-partner facility materializes; otherwise defer.)*

---

## Critical path & dependencies

```
Stone 0 (foundation)  ─┬─►  Stone 1 (Signal/Remediation)  ─►  Stone 2 (MCP eyes)
                       │                                         │
                       │                                         ▼
                       └────────────────────────────────►  Stone 3 (MCP hands, GATED)  ─►  Stone 4 (productionize)
                                                                                              │
                                                                                              ├─►  Stone 5 (HPC vertical)
                                                                                              └─►  Stone 6 (Edge, optional)
```

- Stones 0→3 are the **critical path to the differentiated product** and are
  almost entirely *additive* to the current codebase.
- Stone 5 (HPC) is where the blue-ocean premium is, but it **must** sit on the
  Stone-3 action platform — attempting it before then means building telemetry and
  remediation with no safe execution substrate.
- HA state (Stone 0, P2) is a hard prerequisite for any *live* multi-replica action.

## How we measure "scale" (north-star KPIs)

1. **Autonomy rate** — % of incidents resolved end-to-end with a single human approval (or zero, where policy allows).
2. **Remediation success + verification rate** — fixes that provably resolved the triggering signal.
3. **Fix/remediation quality** — from the eval harness (Stone 0), per domain (code, generic ops, HPC).
4. **Safety** — unapproved-mutation count (target: 0, audited) and rollback correctness.
5. **Value protected** — downtime avoided / GPU-hours protected (the premium metric for #1).

## Honest caveats

- The chassis is strong; the *engine* for #1 and #3 is largely new. Be explicit
  internally that HPC/edge are pivots reusing the control plane, not features.
- "Systems-level / C" depth (the GPU premium) is real new engineering and
  expertise; bootstrap from DCGM/NVML/eBPF tooling before writing low-level code.
- Every new action type widens blast radius — the policy engine (Stone 3) and the
  eval/verification loop are what keep autonomy safe, and must lead, not lag.
