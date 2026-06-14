# Aegis — Stone Progress Dashboard

Live tracker of build progress against `SCALE_PLAN.md` (the bigger picture).
Updated every test-build-test cycle. The point isn't tasks ticked — it's whether
each increment relaxes the two market-gating limiters and moves us toward the
**sellable Stone 3** (gated, MCP-connected live actions).

**The two limiters (SCALE_PLAN §0):**
- **#1 Trigger modality** — crash/stack-trace → must become metric/alert/stream.
- **#2 Remediation modality** — code patch/PR → must become live actions.

_Last updated: through cycle C4. Tests: 105 passing. Latest fix-rate: 0.50 (2-case sample)._

---

## Stone status

| Stone | Goal | Status | Limiter moved |
|-------|------|--------|---------------|
| 0 — foundation | see/measure/operate the existing product | 🟩🟩🟩⬜ ~80% | — |
| 1 — Signal/Remediation | model non-crash triggers + non-code fixes | ✅ done | #1 & #2 (model) |
| 2 — MCP eyes | alert-triggered + live-context diagnosis | 🟩🟩🟩⬜ ~70% | **#1 (live)** |
| 3 — MCP hands (sellable) | gated live execution | ⬜ 0% | #2 (live) — next frontier |
| 4 — productionize | multi-tenant, secrets, CI gate | ⬜ 0% | — |
| 5 — HPC/GPU wedge | premium vertical | ⬜ 0% | — |

---

## Shipped cycles (newest first)

| Cycle | What | Bigger-picture contribution | Commit |
|-------|------|-----------------------------|--------|
| C4 | Alertmanager webhook → `Signal(metric_alert)` → swarm | **Relaxes limiter #1 live**: a metric alert (not just a crash) now triggers the repair loop. Completes Stone 2's trigger half. | `pending` |
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

**C6 / Stone 3 entry.** With triggers (C4) and the risk registry (C1) in place,
the critical-path frontier is **Stone 3 / D1 — `policy.py`**: gate
`registry.gated_tools()` by risk + blast radius. That's where limiter #2 goes
*live* and the sellable product takes shape. (Per SCALE_PLAN: the policy +
verification loop "must lead, not lag" the execution.)
