import asyncio
import json
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, Header, Request, WebSocket, WebSocketDisconnect
from typing import List, Dict, Any, Optional
from aegis_sre.orchestrator.schemas import TelemetryEvent, CodePatch, ActionPlan
from aegis_sre.telemetry.logger import logger
from aegis_sre.telemetry.auth import verify_sentry_signature, build_rate_limiter, build_identity_registry
from aegis_sre.telemetry import metrics
from aegis_sre.config import get_settings
from aegis_sre.core.service import IncidentService, ConsumerRunner
from aegis_sre.core.approvals import build_approval_registry
from aegis_sre.infra.factory import build_store, build_broker, build_cache
from aegis_sre.infra.broker import InProcessBroker
from aegis_sre.orchestrator.safety import safety_policy

# NOTE: heavyweight imports (`build_graph`, `AsyncSqliteSaver`) are deliberately
# deferred into the lifespan/handler so this module — and therefore the /health,
# /ready and ingest paths — can be imported and tested without the LangGraph /
# LLM stack present.

# Built on startup from the active profile (on-prem SQLite / cloud Postgres+Redis).
settings = get_settings()
# Cluster-wide on cloud (Redis), in-memory on-prem (A9).
_rate_limiter = build_rate_limiter(settings)
# Per-identity API keys + roles (Batch 3). No config => open dev; legacy
# webhook_token => single admin key.
_identity = build_identity_registry(settings)


async def _rate_ok(key: str) -> bool:
    """Rate-limit check tolerant of a sync (in-memory) or async (Redis) limiter."""
    res = _rate_limiter.allow(key)
    if asyncio.iscoroutine(res):
        return await res
    return res
# Holds generated patches awaiting human approval (incident_id -> patch).
# Shared on the cloud tier (Redis) so worker-registered remediations are
# approvable from any API replica; in-memory on-prem (A8 / F2).
approval_registry = build_approval_registry(settings)


def _client_key(request: Request) -> str:
    """Client identity for rate limiting. X-Forwarded-For is only trusted when
    explicitly enabled (behind a known proxy); otherwise a spoofed header could
    mint unlimited 'clients' and defeat the limiter (P9)."""
    if settings.trust_forwarded_for:
        fwd = request.headers.get("x-forwarded-for")
        if fwd:
            return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _reject_oversized(request: Request) -> None:
    """Reject a request whose declared body exceeds the cap before we read/parse
    it into memory or feed it to the LLM (P10)."""
    cl = request.headers.get("content-length")
    if cl and cl.isdigit() and int(cl) > settings.max_body_bytes:
        metrics.auth_rejections.labels(reason="oversized").inc()
        raise HTTPException(status_code=413, detail="Payload too large")


async def _enforce(request: Request, token: Optional[str], min_role: str = "ingest"):
    """Shared webhook guard: size cap, rate limit, then RBAC. Returns the
    authenticated Identity (or raises 401/403/429)."""
    _reject_oversized(request)
    if not await _rate_ok(_client_key(request)):
        logger.warning("rate_limited", client=_client_key(request))
        metrics.auth_rejections.labels(reason="rate_limit").inc()
        raise HTTPException(status_code=429, detail="Rate limit exceeded")
    ident = _identity.authorized(token, min_role)
    if ident is None:
        logger.warning("unauthorized_webhook", client=_client_key(request), need_role=min_role)
        metrics.auth_rejections.labels(reason="unauthorized").inc()
        raise HTTPException(status_code=401, detail="Unauthorized")
    return ident


incident_service: IncidentService | None = None
_consumer_task: asyncio.Task | None = None
_consumer_runner: ConsumerRunner | None = None
# A single shared LangGraph checkpointer, opened once at startup and reused for
# every incident, instead of opening/closing a fresh SQLite connection per event.
_checkpointer = None
_checkpointer_cm = None
_ws_pubsub = None
_ws_fanout_task: asyncio.Task | None = None
# Compiled LangGraph app, built once and reused across incidents (API-2 — was
# recompiled per incident). Reset to None if the checkpointer is reopened.
_graph_app = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup/shutdown wiring (replaces the deprecated @app.on_event hooks).

    Startup: build backends, recover pending events, open the shared graph
    checkpointer, and (on-prem) launch the in-process consumer.
    Shutdown: stop the consumer, cancel/await its task, and close every backend
    so connections and the WAL are released cleanly instead of being leaked.
    """
    global incident_service, _consumer_task, _consumer_runner
    global _checkpointer, _checkpointer_cm, _ws_pubsub, _ws_fanout_task

    # Fail closed in production: a cloud (internet-exposed) deployment MUST set a
    # webhook token, otherwise anyone can trigger the LLM repair swarm.
    if settings.is_cloud and not settings.webhook_token:
        raise RuntimeError(
            "AEGIS_WEBHOOK_TOKEN is required on the cloud profile (refusing to start unauthenticated)."
        )
    if not settings.webhook_token:
        logger.warning("webhook_auth_disabled", profile=settings.profile,
                       detail="AEGIS_WEBHOOK_TOKEN unset; webhooks are unauthenticated.")

    # Best-effort OTel tracing (A1-A2): real spans when the SDK + an OTLP
    # endpoint are configured, no-op otherwise.
    from aegis_sre.telemetry import tracing
    tracing.setup_tracing("aegis-api")

    store = build_store(settings)
    broker = build_broker(settings)
    cache = build_cache(settings)
    incident_service = IncidentService(store=store, broker=broker, cache=cache, settings=settings)
    await incident_service.init()

    # Open one checkpointer for the process lifetime (lazy import keeps the LLM
    # stack optional for tests). Path is absolute via settings so it no longer
    # depends on the process working directory.
    try:
        from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
        _checkpointer_cm = AsyncSqliteSaver.from_conn_string(settings.state_db_path)
        _checkpointer = await _checkpointer_cm.__aenter__()
    except Exception as e:  # noqa: BLE001 - checkpointer is best-effort
        logger.warning("checkpointer_init_failed", error=str(e))
        _checkpointer, _checkpointer_cm = None, None

    # Re-publish events left 'pending' by a prior crash so they survive restarts.
    await incident_service.recover_pending()

    # Warm the RAG index in the background so the researcher has real code/skill
    # context (A6). Non-blocking: readiness doesn't wait on embedding.
    try:
        from aegis_sre.orchestrator.graph import warm_rag_engine
        asyncio.create_task(warm_rag_engine())
    except Exception as e:  # noqa: BLE001
        logger.warning("rag_warm_dispatch_failed", error=str(e))

    # On-prem (in-process broker) runs the consumer inside the API process.
    # Cloud (Redis broker) relies on separate `worker.py` replicas, so we skip it.
    if isinstance(broker, InProcessBroker):
        _consumer_runner = ConsumerRunner(
            broker=broker,
            store=store,
            processor=trigger_repair_loop,
            timeout_seconds=safety_policy.get_timeout(),
        )
        _consumer_task = asyncio.create_task(_consumer_runner.run())
        logger.info("in_process_consumer_started", profile=settings.profile)
    else:
        logger.info("external_workers_expected", profile=settings.profile)

    # WS fan-out (A10/#18): on the cloud tier, subscribe to worker progress on
    # Redis pub/sub and rebroadcast to this replica's WS clients. No-op on-prem
    # (the in-process consumer already broadcasts directly).
    from aegis_sre.infra.pubsub import build_pubsub
    _ws_pubsub = build_pubsub(settings)

    async def _fanout():
        try:
            async for message in _ws_pubsub.listen():
                await manager.broadcast(message)
        except asyncio.CancelledError:
            pass
        except Exception as e:  # noqa: BLE001
            logger.warning("ws_fanout_failed", error=str(e))

    _ws_fanout_task = asyncio.create_task(_fanout())

    try:
        yield
    finally:
        # --- Graceful shutdown ------------------------------------------------
        if _consumer_runner is not None:
            _consumer_runner.stop()
        if _consumer_task is not None:
            _consumer_task.cancel()
            try:
                await _consumer_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        if _ws_fanout_task is not None:
            _ws_fanout_task.cancel()
            try:
                await _ws_fanout_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        if _ws_pubsub is not None:
            try:
                await _ws_pubsub.close()
            except Exception:  # noqa: BLE001
                pass
        if _checkpointer_cm is not None:
            try:
                await _checkpointer_cm.__aexit__(None, None, None)
            except Exception as e:  # noqa: BLE001
                logger.warning("checkpointer_close_failed", error=str(e))
        for backend in (broker, store, cache):
            try:
                await backend.close()
            except Exception as e:  # noqa: BLE001
                logger.warning("backend_close_failed", backend=type(backend).__name__, error=str(e))
        logger.info("shutdown_complete", profile=settings.profile)


app = FastAPI(title="Aegis SRE - Universal Telemetry Webhook", lifespan=lifespan)

class ConnectionManager:
    def __init__(self):
        self.active_connections: List[WebSocket] = []

    async def connect(self, websocket: WebSocket) -> bool:
        """Accept a WS client unless the connection cap is reached (P12 — an
        unbounded client list is a memory + broadcast-amplification DoS)."""
        if len(self.active_connections) >= settings.max_ws_connections:
            await websocket.close(code=1013)  # try again later
            metrics.auth_rejections.labels(reason="ws_capacity").inc()
            return False
        await websocket.accept()
        self.active_connections.append(websocket)
        return True

    def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)

    async def broadcast(self, message: dict):
        # Prevent "dictionary changed size during iteration" if clients disconnect
        for connection in list(self.active_connections):
            try:
                await connection.send_json(message)
            except Exception:
                # Narrowed from a bare `except` so we never swallow
                # CancelledError / KeyboardInterrupt during shutdown.
                self.disconnect(connection)

manager = ConnectionManager()


async def _alert(action: str, dedup_key: str, **kwargs) -> None:
    """Fire an incident-alert lifecycle event, fully guarded. Alerting must never
    break the repair loop, so any failure here is logged and swallowed."""
    try:
        from aegis_sre.orchestrator.incident_tools import get_incident_notifier

        notifier = get_incident_notifier()
        if notifier is None:
            return  # alerting disabled (ALERT_WEBHOOK_URL unset)
        if action == "trigger":
            await notifier.trigger(dedup_key=dedup_key, **kwargs)
        elif action == "acknowledge":
            await notifier.acknowledge(dedup_key, **kwargs)
        elif action == "resolve":
            await notifier.resolve(dedup_key, **kwargs)
    except Exception as e:  # noqa: BLE001
        logger.warning("incident_alert_dispatch_failed", action=action, dedup_key=dedup_key, error=str(e))


async def trigger_repair_loop(telemetry: TelemetryEvent):
    """
    Runs the Aegis LangGraph orchestrator asynchronously in the background.
    """
    logger.info("Triggering autonomous repair loop", service_name=telemetry.service_name)
    initial_state = {
        "telemetry": telemetry,
        "code_context": None,
        "current_patch": None,
        "sandbox_status": "pending",
        "review": None,
        "iteration_count": 0,
        "resolved": False
    }
    
    try:
        await manager.broadcast({
            "incident_id": telemetry.event_id,
            "type": "telemetry_received",
            "service": telemetry.service_name,
            "crash": telemetry.crash_log
        })

        # Fire the incident alert as repair begins (guarded: alerting must never
        # break the repair loop). Keyed by event_id so ack/resolve correlate.
        await _alert("trigger", telemetry.event_id, severity="critical",
                     description=f"Aegis repair started for {telemetry.service_name}",
                     service=telemetry.service_name,
                     crash_tail=telemetry.crash_log[-280:])

        # Stream graph execution steps to the WebSocket clients
        final_state = initial_state
        config = {"configurable": {"thread_id": telemetry.event_id}}

        # Build the graph once and reuse it across incidents (API-2). The
        # checkpointer is opened once at startup, so a single compiled app is safe.
        global _graph_app
        if _graph_app is None:
            from aegis_sre.orchestrator.graph import build_graph
            _graph_app = build_graph(checkpointer=_checkpointer)
        graph_app = _graph_app

        async for output in graph_app.astream(initial_state, config=config):
            for node_name, state_update in output.items():
                await manager.broadcast({
                    "incident_id": telemetry.event_id,
                    "type": "node_update",
                    "node": node_name,
                    # send stringified keys so we don't blow up JSON serializers
                    "state_summary": list(state_update.keys())
                })
                # Keep track of state
                final_state.update(state_update)
        
        # In a real async production environment, we'd log the final patch ID
        patch = final_state.get('current_patch')
        if patch:
            kind = type(patch).__name__
            logger.info("Successfully generated remediation", service_name=telemetry.service_name, kind=kind)
            # Hold the remediation pending human approval so /ws approve_patch can
            # ship it (PR for a CodePatch, gated execution for an ActionPlan).
            await approval_registry.register(telemetry.event_id, patch, telemetry)
            # Build the patch_ready frame polymorphically — an ActionPlan has no
            # file_path/replacement_content (F1: this previously crashed the whole
            # alert path with AttributeError).
            msg = {
                "incident_id": telemetry.event_id,
                "type": "patch_ready",
                "service": telemetry.service_name,
                "kind": kind,
                "root_cause_analysis": patch.root_cause_analysis,
                "explanation": patch.explanation,
            }
            if isinstance(patch, CodePatch):
                msg["file"] = patch.file_path
                msg["diff"] = patch.replacement_content
                ack_note = f"Patch proposed for {patch.file_path}; awaiting approval."
            else:  # ActionPlan
                msg["file"] = None
                msg["steps"] = [s.tool for s in patch.steps]
                msg["blast_radius"] = patch.blast_radius.value
                ack_note = f"ActionPlan ({len(patch.steps)} steps) proposed; awaiting approval."
            await manager.broadcast(msg)
            # A remediation is proposed and awaiting human approval -> acknowledge.
            await _alert("acknowledge", telemetry.event_id, note=ack_note)
        else:
            logger.warning("Orchestrator completed but no patch was generated", service_name=telemetry.service_name)
        # Note: durable status ('completed'/'failed') is owned by ConsumerRunner,
        # which wraps this processor with the God-Node timeout. This function only
        # runs the graph and streams progress to WebSocket clients.
    except Exception as e:
        logger.error("Critical failure during background orchestrator execution", error=str(e))
        await manager.broadcast({
            "incident_id": telemetry.event_id,
            "type": "error",
            "message": str(e)
        })
        # Escalate: the autonomous repair failed -> keep the incident firing.
        await _alert("trigger", telemetry.event_id, severity="critical",
                     description=f"Aegis repair FAILED for {telemetry.service_name}: {e}",
                     service=telemetry.service_name)
        raise  # re-raise so ConsumerRunner records 'failed' status

async def _process_telemetry(event: TelemetryEvent) -> dict:
    """Shared ingestion pipeline for all webhook adapters: de-dup, persist, publish."""
    if incident_service is None:  # pragma: no cover - startup guard
        raise HTTPException(status_code=503, detail="Service not ready")
    # Truncate an oversized crash_log before it hits the store / LLM prompt (P10):
    # a 10MB stack trace is a cost + memory DoS, not useful diagnostic signal.
    cap = settings.max_crash_log_chars
    if len(event.crash_log) > cap:
        logger.warning("crash_log_truncated", event_id=event.event_id, original_len=len(event.crash_log))
        event = event.model_copy(update={"crash_log": event.crash_log[:cap] + "\n...[truncated]"})
    return await incident_service.ingest(event)

@app.post("/webhook/crash")
async def receive_crash_telemetry(
    event: TelemetryEvent,
    request: Request,
    x_aegis_token: Optional[str] = Header(default=None, alias="X-Aegis-Token"),
):
    """
    Original Universal entrypoint for Aegis telemetry.
    Receives a TelemetryEvent JSON, checks idempotency, and fires the repair swarm.
    Requires the shared-secret `X-Aegis-Token` header when a token is configured.
    """
    await _enforce(request, x_aegis_token)
    result = await _process_telemetry(event)
    if result.get("status") == "dropped":
        raise HTTPException(status_code=429, detail=result.get("message", "At capacity"))
    return result

@app.post("/webhook/sentry")
async def receive_sentry_webhook(request: Request):
    """
    Adapter endpoint specifically for Sentry Issue Alert Webhooks.
    Parses the massive Sentry JSON payload and normalizes it into a TelemetryEvent.
    Verifies the Sentry HMAC signature over the raw body when a secret is set.
    """
    _reject_oversized(request)
    # Fail closed on cloud: an internet-exposed Sentry endpoint MUST verify HMAC
    # (P7 — it was open by default whenever sentry_secret was unset).
    if settings.is_cloud and not settings.sentry_secret:
        raise HTTPException(status_code=401, detail="Sentry signature secret required")

    # Read the raw body once so we can both verify the HMAC and parse it.
    raw = await request.body()

    if not await _rate_ok(_client_key(request)):
        raise HTTPException(status_code=429, detail="Rate limit exceeded")

    signature = request.headers.get("sentry-hook-signature") or request.headers.get("sentry-hook-signature-256")
    if not verify_sentry_signature(raw, signature, settings.sentry_secret):
        logger.warning("sentry_signature_invalid", client=_client_key(request))
        metrics.auth_rejections.labels(reason="sentry_signature").inc()
        raise HTTPException(status_code=401, detail="Invalid Sentry signature")

    try:
        payload = json.loads(raw)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    # Sentry payload parsing heuristics
    project_name = payload.get("project_name", "unknown-sentry-service")
    event_data = payload.get("data", {}).get("event", {})
    
    event_id = event_data.get("event_id") or payload.get("id") or "sentry-unknown-id"
    
    # Attempt to extract stacktrace or just the error title/culprit
    title = event_data.get("title", "Unknown Sentry Error")
    culprit = event_data.get("culprit", "")
    
    # Try to extract the actual stack trace if available
    exception_values = event_data.get("exception", {}).get("values", [])
    stack_trace = ""
    if exception_values and isinstance(exception_values, list):
        for exc in exception_values:
            stack_trace += f"{exc.get('type')}: {exc.get('value')}\n"
            stacktrace_obj = exc.get("stacktrace", {})
            for frame in stacktrace_obj.get("frames", []):
                file = frame.get("filename", "")
                line = frame.get("lineno", "")
                func = frame.get("function", "")
                stack_trace += f"  File '{file}', line {line}, in {func}\n"
    
    if not stack_trace:
        stack_trace = f"{title}\nCulprit: {culprit}\nNo explicit stacktrace provided by Sentry hook."

    normalized_event = TelemetryEvent(
        event_id=f"SENTRY-{event_id}",
        service_name=project_name,
        crash_log=stack_trace,
        metadata={"source": "sentry", "url": payload.get("url")}
    )
    
    result = await _process_telemetry(normalized_event)
    if result.get("status") == "accepted":
        return {"status": "accepted", "source": "sentry"}
    return result

@app.post("/webhook/alert")
async def receive_alertmanager_webhook(
    request: Request,
    x_aegis_token: Optional[str] = Header(default=None, alias="X-Aegis-Token"),
):
    """Alertmanager webhook adapter (C4): a live metric alert triggers the swarm.

    Each firing alert is normalized to a Signal(metric_alert), projected onto the
    canonical TelemetryEvent via the Stone-1 adapter, and run through the same
    ingest pipeline as crashes. Resolved alerts are ignored.
    """
    await _enforce(request, x_aegis_token)
    from aegis_sre.telemetry.alert_adapter import parse_alertmanager
    return await _ingest_signals(parse_alertmanager(await _json(request)), "alertmanager")


@app.post("/webhook/datadog")
async def receive_datadog_webhook(
    request: Request,
    x_aegis_token: Optional[str] = Header(default=None, alias="X-Aegis-Token"),
):
    """Datadog alert webhook adapter (C5) -> Signal(metric_alert) -> swarm."""
    await _enforce(request, x_aegis_token)
    from aegis_sre.telemetry.alert_adapter import parse_datadog
    return await _ingest_signals(parse_datadog(await _json(request)), "datadog")


@app.post("/webhook/pagerduty")
async def receive_pagerduty_webhook(
    request: Request,
    x_aegis_token: Optional[str] = Header(default=None, alias="X-Aegis-Token"),
):
    """PagerDuty v3 webhook adapter (C5) -> Signal(metric_alert) -> swarm."""
    await _enforce(request, x_aegis_token)
    from aegis_sre.telemetry.alert_adapter import parse_pagerduty
    return await _ingest_signals(parse_pagerduty(await _json(request)), "pagerduty")


async def _json(request: Request) -> dict:
    try:
        return json.loads(await request.body())
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON payload")


async def _ingest_signals(signals, source: str) -> dict:
    """Shared path for all alert adapters: each firing Signal -> TelemetryEvent ->
    the existing ingest pipeline."""
    if not signals:
        return {"status": "ignored", "reason": "no_firing_alerts", "source": source}
    results = []
    for signal in signals:
        result = await _process_telemetry(signal.to_telemetry())
        results.append({"signal_id": signal.signal_id, **result})
    accepted = sum(1 for r in results if r.get("status") == "accepted")
    return {"status": "accepted", "source": source,
            "firing": len(signals), "accepted": accepted, "results": results}

@app.get("/metrics")
async def metrics_endpoint():
    """Prometheus scrape endpoint. Empty body when prometheus_client is absent."""
    from fastapi import Response
    body, content_type = metrics.render()
    return Response(content=body, media_type=content_type)

@app.get("/health")
async def health_check():
    """Liveness: process is up."""
    return {"status": "healthy", "service": "Aegis SRE"}

@app.get("/ready")
async def readiness_check():
    """Readiness: backends wired. Used by k8s readiness probes / load balancers."""
    if incident_service is None:
        raise HTTPException(status_code=503, detail="initializing")
    return {"status": "ready", "profile": settings.profile}

@app.get("/incidents")
async def get_incident_history(
    request: Request,
    x_aegis_token: Optional[str] = Header(default=None, alias="X-Aegis-Token"),
):
    """Returns recent incidents for the dashboard. Token-gated (API-1): incident
    history includes crash logs, so it's protected by the same shared secret as
    the webhooks/WS. Open when no token is configured (dev)."""
    if _identity.authorized(x_aegis_token or request.query_params.get("token"), "ingest") is None:
        metrics.auth_rejections.labels(reason="unauthorized").inc()
        raise HTTPException(status_code=401, detail="Unauthorized")
    if incident_service is None:
        raise HTTPException(status_code=503, detail="Service not ready")
    return {"incidents": await incident_service.store.get_recent_incidents(20)}

def _ws_token(websocket: WebSocket) -> Optional[str]:
    """Prefer the token in the Sec-WebSocket-Protocol header (keeps it OUT of the
    URL/logs, P6); fall back to ?token= with a deprecation warning."""
    proto = websocket.headers.get("sec-websocket-protocol")
    if proto:
        # format: "aegis, <token>"
        parts = [p.strip() for p in proto.split(",")]
        if len(parts) >= 2:
            return parts[1]
    q = websocket.query_params.get("token")
    if q:
        logger.warning("ws_token_in_url_deprecated")
    return q


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    # Authenticate BEFORE accepting. Any authenticated identity may watch the
    # stream; approve/reject additionally require the `approver` role (Batch 3).
    ident = _identity.resolve(_ws_token(websocket))
    if ident is None:
        logger.warning("unauthorized_ws_connection")
        await websocket.close(code=1008)  # policy violation
        return
    if not await manager.connect(websocket):
        return  # at WS capacity (P12)
    from aegis_sre.telemetry.auth import ROLE_RANK
    can_approve = ROLE_RANK.get(ident.role, 0) >= ROLE_RANK["approver"]
    try:
        while True:
            # Client approves a specific incident's patch: {action, incident_id}.
            data = await websocket.receive_json()
            if data.get("action") == "approve_patch":
                if not can_approve:
                    await websocket.send_json({"type": "error", "message": "approver role required"})
                    continue
                incident_id = data.get("incident_id")
                if not incident_id:
                    await websocket.send_json({"type": "error", "message": "approve_patch requires incident_id"})
                    continue

                # VCS import is lazy so the module stays importable without PyGithub.
                from aegis_sre.orchestrator.vcs_provider import get_vcs_provider

                # `arm: true` (P-1) authorizes LIVE execution of an ActionPlan;
                # default (omitted/false) keeps it dry-run.
                arm = bool(data.get("arm", False))
                logger.info("human_approved_patch", incident_id=incident_id, arm=arm, approver=ident.name)
                result = await approval_registry.approve(incident_id, get_vcs_provider(), arm=arm,
                                                         approver=ident.name)

                if result["status"] == "deployed":
                    if incident_service is not None:
                        await incident_service.store.mark_event_status(incident_id, "deployed")
                    await manager.broadcast({
                        "type": "patch_deployed",
                        "incident_id": incident_id,
                        "file": result["file"],
                        "pr_url": result["pr_url"],
                    })
                    # Fix shipped (PR opened) -> resolve the incident alert.
                    await _alert("resolve", incident_id,
                                 note=f"Fix PR opened for {result['file']}: {result['pr_url']}")
                elif result["status"] in ("executed", "rolled_back"):
                    # ActionPlan terminal states (F3): update the store + alert +
                    # dashboard, which previously only happened for CodePatch PRs.
                    resolved = result["status"] == "executed" and result.get("resolved")
                    if incident_service is not None:
                        await incident_service.store.mark_event_status(
                            incident_id, "deployed" if resolved else "failed")
                    await manager.broadcast({
                        "type": "patch_deployed" if resolved else "patch_rejected",
                        "incident_id": incident_id, "kind": "action_plan",
                        "mode": result.get("mode"), "rolled_back": result.get("rolled_back"),
                    })
                    if resolved:
                        await _alert("resolve", incident_id, note="ActionPlan executed and verified.")
                    await websocket.send_json({"type": "approval_result", **result})
                else:
                    # not_found / already_approved / error / blocked -> tell the approver.
                    await websocket.send_json({"type": "approval_result", **result})

            elif data.get("action") == "reject_patch":
                if not can_approve:
                    await websocket.send_json({"type": "error", "message": "approver role required"})
                    continue
                incident_id = data.get("incident_id")
                if not incident_id:
                    await websocket.send_json({"type": "error", "message": "reject_patch requires incident_id"})
                    continue
                rejected = await approval_registry.reject(incident_id, approver=ident.name)
                logger.info("human_rejected_patch", incident_id=incident_id, rejected=rejected)
                if incident_service is not None and rejected:
                    await incident_service.store.mark_event_status(incident_id, "rejected")
                await manager.broadcast({"type": "patch_rejected", "incident_id": incident_id})
    except WebSocketDisconnect:
        manager.disconnect(websocket)
