# Aegis SRE — P0 Production Fixes

Senior-engineer debugging pass on the four P0 blockers. Each entry follows:
functionality → problem → why it fails → edge cases → fix. All changes ship with
tests in `aegis_sre/tests/test_hardening.py` (17/17 passing, no Redis/Postgres/LLM
needed). Severity: all 🔴 (block production).

| # | File | Defect |
|---|------|--------|
| 1 | `orchestrator/sandbox_engine.py`, `graph.py` | Sandbox validated a code *fragment*, not the patched file |
| 2 | `orchestrator/sandbox_engine.py`, `graph.py` | Validation was syntax-only — no behavioral test |
| 3 | `orchestrator/sandbox_engine.py` | E2B failed **open**: missing API key returned "success" |
| 4 | `telemetry/api_receiver.py`, `telemetry/auth.py`, `config.py` | Webhooks + WebSocket had no authentication |

---

## P0-1 — The sandbox tested a fragment, not the patch

**Code functionality.** `sandbox_node` is the gate between a generated `PatchProposal`
and deployment. It is supposed to prove the patch makes the target file sound.

**What the problem is.** Both engines wrote `patch.replacement_content` — the
*replacement chunk* — out as a standalone file and compiled that. The patch was
never applied to the real source; `target_content` and the rest of the file were
ignored entirely.

**Why it fails.** `replacement_content` is meant to *replace* `target_content`
inside the existing file. Compiling it alone tests an out-of-context snippet:
a method body fails to compile on its own (false reject), or a trivial valid line
passes while the real file would break (false accept). Either way "sandbox passed"
carried almost no signal — yet `should_deploy` trusts it.

**Edge cases handled.** New-file creation (no original source) → the replacement
*is* the file. Target string absent → patch does not apply (hard fail, never
reaches the compiler). Target matching *multiple* sites → ambiguous, refuse rather
than edit the wrong one. Empty target with an existing file → cannot locate the
edit site → fail.

**Fix.** New `apply_patch_to_source(patch, original_source)` splices
target→replacement (exactly-once) into a copy of the real file; `sandbox_node`
now fetches the current source via the VCS provider and passes it, so the **full
patched file** is what gets validated.

---

## P0-2 — Validation was syntax-only

**Code functionality.** After applying the patch, the sandbox decides pass/fail.

**What the problem is.** It only ran `py_compile` / `node --check` — a syntax
check. A patch that parses but doesn't fix (or breaks) behavior passed.

**Why it fails.** Syntax validity says nothing about whether the original crash is
resolved or a regression was introduced. For an autonomous actor that then ships
the change, "it compiles" is not an acceptable bar.

**Edge cases handled.** Reproductions must never come from the crash telemetry
(that payload is attacker-influenceable → remote code execution); the repro is
taken only from a trusted operator env var `AEGIS_REPRO_COMMAND`. Compile and repro
each run under hard timeouts (`AEGIS_COMPILE_TIMEOUT` / `AEGIS_REPRO_TIMEOUT`) so a
pathological patch can't hang the swarm. Absent repro is reported explicitly as
"syntax-only validation" rather than silently implying behavioral coverage.

**Fix.** The patched file is written into an isolated temp workspace; when a repro
command is configured it runs against that workspace and a non-zero exit fails the
gate. Compilation now uses `create_subprocess_exec` (argv, no shell) so a file path
can't be interpreted as shell syntax, plus a path-escape guard on the write.

---

## P0-3 — E2B failed open

**Code functionality.** `E2BEngine` runs the compile/test inside an ephemeral E2B
cloud sandbox; `get_sandbox_engine()` picks the engine.

**What the problem is.** With no `E2B_API_KEY`, `compile_and_test` returned
`True, "Mock compilation success."` — and the default provider was `e2b`.

**Why it fails.** A missing/rotated secret is a *configuration error*, but it was
converted into a passing safety check. Combined with P0-1/P0-2, the default
deployment validated nothing and reported success — the worst kind of silent
fail-open right inside the safety gate.

**Edge cases handled.** Any E2B sandbox exception now also fails closed.
`get_sandbox_engine()` no longer defaults to an engine that can't run: with no
explicit `SANDBOX_PROVIDER`, it picks E2B only when a key exists, otherwise the
local engine (which can actually execute).

**Fix.** Missing key → `(False, "… failing closed")`. Explicit
`SANDBOX_PROVIDER=e2b|local` is still honoured.

---

## P0-4 — No authentication on webhooks / WebSocket

**Code functionality.** `/webhook/crash`, `/webhook/sentry`, and `/ws` ingest
telemetry and stream/approve repairs.

**What the problem is.** All three were open. Anyone who could reach the service
could trigger the (expensive) LLM repair swarm, and — once PR creation is wired —
drive code changes; `/ws` let anyone send `approve_patch`.

**Why it fails.** An autonomous system that spends money and changes code must
authenticate its triggers. Unauthenticated webhooks are a DoS/cost-abuse vector and
an integrity risk.

**Edge cases handled.** Constant-time comparisons (`hmac.compare_digest`) avoid
timing leaks. Sentry HMAC is verified over the **raw** body (read once, then
parsed). Rate limiting honours one `X-Forwarded-For` proxy hop. Unconfigured
secret = open gate so on-prem/dev and existing tests keep working — but the **cloud
profile refuses to start** without `AEGIS_WEBHOOK_TOKEN` (fail-closed where it
matters). WS auth happens *before* `accept()`; bad token → close 1008.

**Fix.** New `telemetry/auth.py` (`verify_token`, `verify_sentry_signature`,
`SlidingWindowRateLimiter`). `/webhook/crash` requires `X-Aegis-Token`,
`/webhook/sentry` verifies `Sentry-Hook-Signature`, `/ws` requires `?token=`. New
config: `AEGIS_WEBHOOK_TOKEN`, `AEGIS_SENTRY_SECRET`, `AEGIS_RATE_LIMIT_RPM`.

---

## Verification

```
17/17 hardening tests passed
```

New coverage: `apply_patch_to_source` (all branches), local sandbox applies-and-
compiles + syntax-error rejection + does-not-apply + unsupported-language fail-
closed, behavioral repro pass/fail, E2B fail-closed without key, engine selection,
token / Sentry-HMAC / rate-limiter logic. Full `py_compile` passes for the package.

## Recheck addendum (second pass)

A follow-up recheck of the P0 changes found two latent bugs, now fixed and tested:

- **Orphaned subprocess on timeout** (`sandbox_engine.py`). `_compile` / `_run_repro`
  returned on `asyncio.TimeoutError` without killing the child, so a hung
  compiler/repro leaked the process the timeout was meant to bound. Added
  `_terminate()` (kill + reap) on every timeout path.
- **Rate-limiter unbounded memory** (`auth.py`). The per-key map never evicted
  entries, so spoofed `X-Forwarded-For` keys could exhaust memory — a DoS in the
  abuse-protection layer itself. Added a bounded `_sweep()` of fully-expired keys
  (`test_rate_limiter_evicts_stale_keys`).

Result after recheck: **18/18 tests passing**, full `py_compile` clean.

## Operator notes

- Set `AEGIS_WEBHOOK_TOKEN` (required on cloud), `AEGIS_SENTRY_SECRET`, and
  optionally `AEGIS_RATE_LIMIT_RPM`.
- Set `AEGIS_REPRO_COMMAND` to a trusted command (e.g. `pytest -q tests/test_x.py`)
  to enable behavioral validation; without it the sandbox is syntax-only and says so.
- The rate limiter is in-process; a multi-replica cloud deployment should move it
  behind Redis so the limit holds cluster-wide.
