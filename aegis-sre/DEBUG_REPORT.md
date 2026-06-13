# Aegis SRE — Production Debugging Report

Senior-engineer pass over `aegis-sre`. Five defects found, ranked by blast radius. Each fix has been applied to the source and the two highest-risk ones are backed by a reproduction test. Severity legend: 🔴 outage / security · 🟠 reliability · 🟡 correctness.

| # | Severity | File | Defect |
|---|----------|------|--------|
| 1 | 🔴 | `telemetry/k8s_watcher.py` | Imports a module (`cache`) that does not exist — K8s ingestion can't start |
| 2 | 🔴 | `orchestrator/graph.py` | Reviewer & executor **fail open**: an LLM outage auto-approves/forges patches |
| 3 | 🟠 | `telemetry/queue.py` | `task_done()` in `finally` crashes the worker on shutdown |
| 4 | 🟠 | `telemetry/database.py` + `api_receiver.py` | Idempotency check is a TOCTOU race → duplicate incident processing |
| 5 | 🟡 | `orchestrator/graph.py` | Retry loop discards reviewer feedback → burns all retries on the same patch |

---

## Bug 1 — Missing `cache` module crashes K8s telemetry 🔴

**Functionality.** `K8sTelemetryWatcher` watches a namespace for `CrashLoopBackOff`/`Error` pods and yields `TelemetryEvent`s. It de-duplicates repeated crashes through an `IdempotencyCache`.

**Problem.** Line 7 does `from aegis_sre.telemetry.cache import IdempotencyCache`, but `cache.py` does not exist in the package — only a stale `__pycache__/cache.cpython-313.pyc` remains, evidence the source was deleted.

**Why it fails.** Importing `k8s_watcher` raises `ModuleNotFoundError` immediately, before any watching can begin. Any deployment whose telemetry source is Kubernetes (the primary one, per the README) cannot boot. Confirmed statically:

```
$ grep -n "from aegis_sre.telemetry.cache" aegis_sre/telemetry/k8s_watcher.py
7:from aegis_sre.telemetry.cache import IdempotencyCache
$ ls aegis_sre/telemetry/cache.py
ls: cannot access ... No such file or directory
```

**Edge cases the replacement must handle.** A crash-looping pod emits the *same* signature thousands of times/minute — the cache must (a) expire entries after a TTL so a genuinely recurring incident is eventually re-raised, and (b) stay **bounded** so an unbounded stream of unique crashes can't exhaust host memory. It also runs in the watcher's own thread, so it must be thread-safe.

**Fix.** New `aegis_sre/telemetry/cache.py` providing a thread-safe, TTL + LRU-bounded `IdempotencyCache` (matches the `IdempotencyCache(ttl_seconds=...)` / `.is_duplicate(key)` API the watcher already calls). Verified: fresh key → `False`, immediate repeat → `True`, post-TTL → `False`, and size capped at `max_size`.

---

## Bug 2 — Fail-open safety logic auto-approves unreviewed code 🔴

**Functionality.** `executor_node` asks the LLM for a `PatchProposal`; `reviewer_node` asks a second model whether the patch is safe. `should_deploy` deploys only when `review.is_safe and sandbox_status == "success"`.

**Problem.** Both nodes treat an infrastructure failure as a *success*:

```python
# reviewer_node — original
except Exception as e:                     # network down, timeout, 429, 5xx...
    review = SecurityReview(is_safe=True,  # <-- approves on outage
                            vulnerability_found=False, feedback="Logic is sound.")
```
```python
# executor_node — original
except Exception as e:
    current_patch = PatchProposal(file_path="main.py", ...)  # fabricated patch
```

**Why it fails.** The reviewer is the last gate before deploy. If the reviewer LLM is unreachable, `is_safe=True` is returned; combined with a sandbox `py_compile` pass (which only checks syntax, not correctness), `should_deploy` returns `"deploy"`. A provider outage therefore ships **unreviewed** code. Worse, the executor's exception branch invents a hardcoded `main.py` patch from thin air, so an outage can deploy a fake null-check into an unrelated file.

**Edge cases.** Timeouts, rate limits (429), malformed-but-non-JSON responses, and the `God Node` kill-switch timeout all land in the catch-all `except`. None of them mean "safe."

**Fix (fail closed).** Reviewer now defaults to `is_safe=False` on any infrastructure error, so an outage routes to retry/abort instead of deploy. Executor returns `current_patch=None` on infrastructure error; the demo/mock patch is gated behind `AEGIS_ALLOW_MOCK_PATCH=true` so it can never fire in production.

---

## Bug 3 — Queue worker crashes its own cleanup on shutdown 🟠

**Functionality.** `TelemetryQueue` runs a single background worker that pulls events and runs the repair graph under a timeout ("God Node kill switch").

**Problem.** `task_done()` lived in a `finally` that runs on *every* path — including when `await self.queue.get()` itself is cancelled (graceful shutdown) and nothing was dequeued.

**Why it fails.** `asyncio.Queue.task_done()` must be called exactly once per delivered item. Calling it when no item was taken raises `ValueError: task_done() called too many times`, which pollutes shutdown logs and breaks any `queue.join()`-based drain. Reproduced against the original structure:

```
worker cancelled while idle
CONFIRMED BUG -> task_done() ValueError: task_done() called too many times
```

**Edge cases.** Cancellation while *idle* (no item — must not call `task_done`) vs. cancellation *mid-processing* (item owned — must call it exactly once). The callback raising, and the kill-switch `TimeoutError`, must each still balance the one delivered item.

**Fix.** `get()` is now in its own `try`; a cancel there breaks out with **no** `task_done()`. Once an item is dequeued, exactly one `task_done()` runs on every subsequent path (success, error, or mid-processing cancel).

---

## Bug 4 — Idempotency check is a TOCTOU race 🟠

**Functionality.** `_process_telemetry` hashes `service:crash_tail`, drops duplicates within a 300s window, otherwise persists and enqueues the event.

**Problem.** The original was check-then-act across two separate SQLite connections:

```python
if is_hash_cached(crash_hash, ttl_seconds=300):   # read
    return {"status": "ignored", ...}
save_cache_hash(crash_hash)                        # write (later, separate txn)
```

**Why it fails.** FastAPI handles webhooks concurrently. Two identical Sentry/crash deliveries (retries are common) can both execute `is_hash_cached` → both miss → both `save` and **both enqueue**, so the same incident runs the full repair swarm twice (double LLM spend, duplicate PRs). The gap between read and write is the race window; opening a fresh connection per call also invites `database is locked` under load.

**Edge cases.** N concurrent duplicates (not just 2); an expired entry that should be re-claimable by exactly one caller; SQLite lock contention.

**Fix.** New `claim_event_hash()` performs an atomic claim in one `IMMEDIATE` transaction: delete-if-expired, then `INSERT OR IGNORE`, and treat `rowcount == 1` as "claimed." `_process_telemetry` now calls it instead of check-then-act. Verified with 20 concurrent threads claiming the same hash:

```
claims granted (expect exactly 1): 1
duplicates dropped (expect 19): 19
```

---

## Bug 5 — Retry loop ignores reviewer feedback 🟡

**Functionality.** On rejection, `should_deploy` returns `"retry"`, looping `reviewer → executor` until `safety_policy.max_retries` (default 3).

**Problem.** `executor_node` rebuilt its prompt only from the crash log and code context. The prior rejection (`review.feedback`) and the rejected patch were already in state but never used.

**Why it fails.** A deterministic-ish model re-fed identical input tends to regenerate the *same* rejected patch, so all retries are spent producing the same output before the policy aborts — the self-healing loop does no actual healing.

**Edge cases.** First iteration has no prior review (must not inject feedback); a `None`/empty patch on the previous turn; only inject when the previous review was actually unsafe.

**Fix.** On `iteration > 0` with an unsafe prior review, the executor prompt now appends the reviewer feedback and the rejected replacement, instructing the model to produce a *different* patch. (Also parenthesized the K8s `and/or` state check in Bug 1's file so intent no longer relies on operator precedence.)

---

## Verification summary

- `python -m py_compile` passes for all six changed/added files.
- Bug 1: cache TTL + size-bound behavior asserted.
- Bug 3: original pattern reproduced the `ValueError`; new structure only calls `task_done()` for delivered items.
- Bug 4: 20-thread concurrent claim grants exactly one.

## Recommended follow-ups (not yet changed)

- The sandbox only runs `py_compile`/`node --check` — syntax, not behavior. Consider running the crashing test case against the patched file before allowing deploy.
- `@app.on_event("startup")` is deprecated in current FastAPI; migrate to the lifespan context manager.
- The WebSocket `approve_patch` handler logs approval and broadcasts `patch_deployed` but never actually calls `vcs.create_pull_request` — the human-in-the-loop approval is currently a no-op.
