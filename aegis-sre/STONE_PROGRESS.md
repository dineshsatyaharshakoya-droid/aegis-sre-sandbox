# Aegis — Stone Progress Dashboard

Live tracker of build progress against `SCALE_PLAN.md` (the bigger picture).
Updated every test-build-test cycle. The point isn't tasks ticked — it's whether
each increment relaxes the two market-gating limiters and moves us toward the
**sellable Stone 3** (gated, MCP-connected live actions).

**The two limiters (SCALE_PLAN §0):**
- **#1 Trigger modality** — crash/stack-trace → must become metric/alert/stream.
- **#2 Remediation modality** — code patch/PR → must become live actions.

_Last updated: through cycle D4. Tests: 138 passing. Coverage: ~78% overall; decision/business-logic core 90–100% (schemas, validator, policy, executor, verifier, approvals, adapters). Gaps are external-I/O boundaries (LLM, RAG, Redis/Postgres live branches, GitHub API, async glue) validated via live smoke runs. Latest fix-rate: 0.50 (2-case sample)._

---

## Stone status

| Stone | Goal | Status | Limiter moved |
|-------|------|--------|---------------|
| 0 — foundation | see/measure/operate the existing product | 🟩🟩🟩⬜ ~80% | — |
| 1 — Signal/Remediation | model non-crash triggers + non-code fixes | ✅ done | #1 & #2 (model) |
| 2 — MCP eyes | alert-triggered + live-context diagnosis | 🟩🟩🟩⬜ ~70% | **#1 (live)** |
| 3 — MCP hands (sellable) | gated live execution | 🟩🟩🟩⬜ ~45% | **#2 (live) — policy + executor + verify** |
| 4 — productionize | multi-tenant, secrets, CI gate | ⬜ 0% | — |
| 5 — HPC/GPU wedge | premium vertical | ⬜ 0% | — |

---

## Shipped cycles (newest first)

| Cycle | What | Bigger-picture contribution | Commit |
|-------|------|-----------------------------|--------|
| D4 | `verifier.py` — re-read the metric to confirm recovery (per-series, fail-closed) + debug-pass fix of a GTE worst-case-aggregation bug | **Makes actions non-fire-and-forget**: proof a remediation worked, the gate before rollback (D5). | `pending` |
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

**Stone 3 / D5 — rollback on regression.** Policy (D1), executor (D2), and
verification (D4) are in. D5: if `verify()` says the signal did NOT clear (or
regressed), run the plan's compensating/rollback steps automatically. Then D3
threads it all together (approve → execute → verify → rollback) into the deploy
path so a live action is never fire-and-forget — completing the sellable loop.
