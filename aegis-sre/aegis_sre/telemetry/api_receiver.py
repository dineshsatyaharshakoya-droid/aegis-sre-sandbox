import asyncio
import json
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, Header, Request, WebSocket, WebSocketDisconnect
from typing import List, Dict, Any, Optional
from aegis_sre.orchestrator.schemas import TelemetryEvent
from aegis_sre.telemetry.logger import logger
from aegis_sre.telemetry.auth import verify_token, verify_sentry_signature, SlidingWindowRateLimiter
from aegis_sre.telemetry import metrics
from aegis_sre.config import get_settings
from aegis_sre.core.service import IncidentService, ConsumerRunner
from aegis_sre.core.approvals import ApprovalRegistry
from aegis_sre.infra.factory import build_store, build_broker, build_cache
from aegis_sre.infra.broker import InProcessBroker
from aegis_sre.orchestrator.safety import safety_policy

# NOTE: heavyweight imports (`build_graph`, `AsyncSqliteSaver`) are deliberately
# deferred into the lifespan/handler so this module — and therefore the /health,
# /ready and ingest paths — can be imported and tested without the LangGraph /
# LLM stack present.

# Built on startup from the active profile (on-prem SQLite / cloud Postgres+Redis).
settings = get_settings()
_rate_limiter = SlidingWindowRateLimiter(settings.rate_limit_rpm)
# Holds generated patches awaiting human approval (incident_id -> patch).
approval_registry = ApprovalRegistry()


def _client_key(request: Request) -> str:
    """Best-effort client identity for rate limiting (honours a single proxy hop)."""
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _enforce(request: Request, token: Optional[str]) -> None:
    """Shared webhook guard: rate limit, then shared-secret token. Raises HTTPException."""
    if not _rate_limiter.allow(_client_key(request)):
        logger.warning("rate_limited", client=_client_key(request))
        metrics.auth_rejections.labels(reason="rate_limit").inc()
        raise HTTPException(status_code=429, detail="Rate limit exceeded")
    if not verify_token(token, settings.webhook_token):
        logger.warning("unauthorized_webhook", client=_client_key(request))
        metrics.auth_rejections.labels(reason="unauthorized").inc()
        raise HTTPException(status_code=401, detail="Unauthorized")


incident_service: IncidentService | None = None
_consumer_task: asyncio.Task | None = None
_consumer_runner: ConsumerRunner | None = None
# A single shared LangGraph checkpointer, opened once at startup and reused for
# every incident, instead of opening/closing a fresh SQLite connection per event.
_checkpointer = None
_checkpointer_cm = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup/shutdown wiring (replaces the deprecated @app.on_event hooks).

    Startup: build backends, recover pending events, open the shared graph
    checkpointer, and (on-prem) launch the in-process consumer.
    Shutdown: stop the consumer, cancel/await its task, and close every backend
    so connections and the WAL are released cleanly instead of being leaked.
    """
    global incident_service, _consumer_task, _consumer_runner
    global _checkpointer, _checkpointer_cm

    # Fail closed in production: a cloud (internet-exposed) deployment MUST set a
    # webhook token, otherwise anyone can trigger the LLM repair swarm.
    if settings.is_cloud and not settings.webhook_token:
        raise RuntimeError(
            "AEGIS_WEBHOOK_TOKEN is required on the cloud profile (refusing to start unauthenticated)."
        )
    if not settings.webhook_token:
        logger.warning("webhook_auth_disabled", profile=settings.profile,
                       detail="AEGIS_WEBHOOK_TOKEN unset; webhooks are unauthenticated.")

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

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)

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

        # Lazy import + reuse of the process-wide checkpointer opened at startup
        # (no per-incident SQLite open/close).
        from aegis_sre.orchestrator.graph import build_graph
        graph_app = build_graph(checkpointer=_checkpointer)

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
            logger.info("Successfully generated patch", service_name=telemetry.service_name, file_path=patch.file_path)
            # Hold the patch pending human approval so /ws approve_patch can open
            # a real PR for it (the human gate is no longer a no-op).
            approval_registry.register(telemetry.event_id, patch, telemetry)
            await manager.broadcast({
                "incident_id": telemetry.event_id,
                "type": "patch_ready",
                "service": telemetry.service_name,
                "file": patch.file_path,
                "root_cause_analysis": patch.root_cause_analysis,
                "explanation": patch.explanation,
                "diff": patch.replacement_content
            })
            # A fix is proposed and awaiting human approval -> acknowledge.
            await _alert("acknowledge", telemetry.event_id,
                         note=f"Patch proposed for {patch.file_path}; awaiting approval.")
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
    _enforce(request, x_aegis_token)
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
    # Read the raw body once so we can both verify the HMAC and parse it.
    raw = await request.body()

    if not _rate_limiter.allow(_client_key(request)):
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
async def get_incident_history():
    """Returns a list of recent incidents to populate the dashboard on load."""
    if incident_service is None:
        raise HTTPException(status_code=503, detail="Service not ready")
    return {"incidents": await incident_service.store.get_recent_incidents(20)}

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    # Authenticate BEFORE accepting: token via `?token=` query param. When no
    # token is configured the gate is open (verify_token returns True).
    if not verify_token(websocket.query_params.get("token"), settings.webhook_token):
        logger.warning("unauthorized_ws_connection")
        await websocket.close(code=1008)  # policy violation
        return
    await manager.connect(websocket)
    try:
        while True:
            # Client approves a specific incident's patch: {action, incident_id}.
            data = await websocket.receive_json()
            if data.get("action") == "approve_patch":
                incident_id = data.get("incident_id")
                if not incident_id:
                    await websocket.send_json({"type": "error", "message": "approve_patch requires incident_id"})
                    continue

                # VCS import is lazy so the module stays importable without PyGithub.
                from aegis_sre.orchestrator.vcs_provider import get_vcs_provider

                logger.info("human_approved_patch", incident_id=incident_id)
                result = await approval_registry.approve(incident_id, get_vcs_provider())

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
                else:
                    # not_found / already_approved / error -> tell the approver only.
                    await websocket.send_json({"type": "approval_result", **result})
    except WebSocketDisconnect:
        manager.disconnect(websocket)
