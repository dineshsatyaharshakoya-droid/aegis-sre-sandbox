# Aegis — Stone Progress Dashboard

Live tracker of build progress against `SCALE_PLAN.md` (the bigger picture).
Updated every test-build-test cycle. The point isn't tasks ticked — it's whether
each increment relaxes the two market-gating limiters and moves us toward the
**sellable Stone 3** (gated, MCP-connected live actions).

**The two limiters (SCALE_PLAN §0):**
- **#1 Trigger modality** — crash/stack-trace → must become metric/alert/stream.
- **#2 Remediation modality** — code patch/PR → must become live actions.

_Last updated: Stone 2 + 3 complete; Stone 0 ~90%. Tests: 214 passing. Coverage: ~81% overall; decision/business-logic core 85–100%. Remaining gaps are deliberately-out-of-scope live I/O (rag_engine chromadb, api_receiver lifespan, sandbox E2B). Eval corpus: 18 labeled cases. Latest fix-rate: 0.50 (2-case sample; full 18-case run pending)._

---

## Stone status

| Stone | Goal | Status | Limiter moved |
|-------|------|--------|---------------|
| 0 — foundation | see/measure/operate the existing product | 🟩🟩🟩🟨 ~90% | recover-guard + CI gate added |
| 1 — Signal/Remediation | model non-crash triggers + non-code fixes | ✅ done | #1 & #2 (model) |
| 2 — MCP eyes | alert-triggered + live-context diagnosis | ✅ **done** | **#1 (live) — metrics+logs+registry+adapters** |
| 3 — MCP hands (sellable) | gated live execution | ✅ **done** | **#2 (live) — full loop, e2e signed off** |
| 4 — productionize | multi-tenant, secrets, CI gate | ⬜ 0% | — |
| 5 — HPC/GPU wedge | premium vertical | ⬜ 0% | — |

---

## Shipped cycles (newest first)

| Cycle | What | Bigger-picture contribution | Commit |
|-------|------|-----------------------------|--------|
| A11+A5 | recover_pending claim-before-republish guard (no double-process across replicas) + GitHub Actions CI gate (suite + coverage + corpus schema) now live on remote | **Stone 0 hardening**: HA correctness + an enforced quality gate. | `2928312`/`65a81ee` |
| C3/C5/C6/C7 | Stone 2 to 100%: logs read tool (C3), Datadog+PagerDuty inbound adapters (C5), per-tool call/latency metrics (C6), with/without-context eval delta (C7) | **Completes the "eyes"**: broader live evidence + per-tool observability. | `eb33944` |
| D7 | staging end-to-end sign-off: alert→approve→execute→verify→rollback-on-forced-failure + dry-run-safe + idempotent, all real components | **Sellable product exit criterion met** — Stone 3 complete. | `cb29fd9` |
| D3+D6 | wire the runner into approval (`approve(ActionPlan)` → execute→verify→rollback) + audit records + `aegis_actions_executed_total{type,result}` | **Limiter #2 fully live & observable**: approving an action drives the gated loop; every outcome audited + counted. | `c699eff` |
| D5 | execute→verify→rollback spine (`remediation_runner.py` + `ActionPlan.rollback_steps` + executor rollback) | **Completes the safe-action loop**: failed verification auto-runs compensating steps; only proven recoveries stand. | `df991d4` |
| D4 | `verifier.py` — re-read the metric to confirm recovery (per-series, fail-closed) + debug-pass fix of a GTE worst-case-aggregation bug | **Makes actions non-fire-and-forget**: proof a remediation worked, the gate before rollback (D5). | `caf7a0f` |
| D2 | gated `ActionExecutor` (runs act-tools only when policy permits; refuses non-act/unknown/handler-less; stops on failure) | **Limiter #2 executes (safely)**: the only place live mutation happens, fully gated by D1. | `34552bd` |
| D1 | `policy.py` action gate (dry-run default, blast caps, allow/deny, audit) + debug-pass fix of a live-on-unarmed safety hole | **Starts limiter #2 live (safely)**: the gate the sellable product rests on; gates `registry` act-tools by risk + blast radius. | `6b47e30` |
| C4 | Alertmanager webhook → `Signal(metric_alert)` → swarm | **Relaxes limiter #1 live**: a metric alert (not just a crash) now triggers the repair loop. Completes Stone 2's trigger half. | `81d370c` |
| C1 | Risk-classed tool registry (`read`/`notify`/`act`) | Keystone for the sellable Stone 3: its policy engine gates `act` tools by risk. Retrofits ad-hoc tools. | `9f08744` |
| B7 | Regression sweep | Fix-rate 0.50 unchanged — Stone 1 added zero quality regression. | verify |
| B6 | Polymorphic approvals | Approve a `CodePatch` (PR) **or** an `ActionPlan` (gated) — limiter #2 in the approval path. | `875ef2d` |
| B5 | `sandbox_node` → Validator | Validation gate is remediation-type-agnostic. | `9b8e06e` |
| B4 | Type-dispatching Validator | `CodePatch`→compile, `ActionPlan`→dry-run. | `a17a9b8` |
| B3 | `ActionPlan` schema | Models limiter #2: live infra actions (typed, blast-radius, dry-run). | `a2bdac0` |
| B2 | `Remediation` base / `CodePatch` | Remediation becomes polymorphic. | `2e3f845` |
| B1 | `Signal` + adapter | Models limiter #1: any signal, not just crashes. | `3a21724` |
| A3–A4 | Eval harness tested + live | Establishes the fix-rate yardstick (KPI #3) every later stone is judged by. | `b9c35f9` |

---

## North-star KPIs (SCALE_PLAN §"how we measure scale")

| KPI | Now | Notes |
|-----|-----|-------|
| Fix/remediation quality (fix-rate) | **0.50** (2-case) | needs full-corpus run; corpus = 10 cases |
| Autonomy rate | n/a | measured once live actions land (Stone 3) |
| Remediation success + verification | n/a | verification loop is Stone 3 (D4) |
| Safety (unapproved mutations) | 0 | `act` tools gated by registry; PR path human-approved |
| Value protected (GPU-hours) | n/a | Stone 5 |

---

## Next up

**Finish Stone 0 (the remaining ~10%).** Done: P0 safety, metrics, eval harness,
verified backends, recover-guard (A11), CI gate (A5). Remaining — heavier infra,
each its own focused cycle: **A8** Redis-backed approval registry, **A9** Redis
rate limiter (cluster-wide), **A10** WS fan-out via Redis pub/sub, **A1–A2** OTel
tracing, **A6–A7** RAG ingest wired into startup + content-hash cache, **A12**
3-replica smoke test. These involve async-path refactors, an optional OTel dep,
and pub/sub background tasks — best done deliberately, not crammed.

**Then Stone 4 — productionize live actions.** Stone 3 (the sellable product) is done.
Stone 4 makes it safe to point at a real customer prod: idempotent action
execution + concurrent-remediation dedup, per-integration credential scoping /
secrets management, multi-tenant isolation + per-tenant quotas, a CI gate running
the eval + integration suites, Postgres migrations (Alembic), and a dead-letter
queue for poison signals. Also worth folding in: the deferred Stone-2 items (C3
logs tool, C6 per-tool metrics) and a full 18-case eval fix-rate run.
