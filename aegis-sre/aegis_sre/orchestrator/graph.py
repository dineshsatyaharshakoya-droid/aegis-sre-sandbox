import os
import re
import json
import asyncio
from typing import TypedDict
from langgraph.graph import StateGraph, END
from pydantic import ValidationError

from aegis_sre.config import get_settings
from aegis_sre.orchestrator.llm import chat_json
from aegis_sre.orchestrator.schemas import (
    TelemetryEvent, PatchProposal, SecurityReview, Remediation, CodePatch,
    ActionPlan,
)
from aegis_sre.orchestrator.vcs_provider import get_vcs_provider
from aegis_sre.orchestrator.validator import Validator
from aegis_sre.orchestrator.rag_engine import RAGEngine
from aegis_sre.orchestrator.safety import safety_policy
from aegis_sre.telemetry.logger import logger
from aegis_sre.telemetry import metrics

_rag_engine_instance = None

def get_rag_engine() -> RAGEngine:
    global _rag_engine_instance
    if _rag_engine_instance is None:
        _rag_engine_instance = RAGEngine(workspace_path=os.environ.get("AEGIS_RAG_WORKSPACE", "."))
    return _rag_engine_instance


async def warm_rag_engine() -> None:
    """Ingest the workspace + default SRE skills into RAG once at startup (A6),
    so researcher_node's query_codebase/query_skills return real context instead
    of "" (they did nothing in the API/worker path before). Guarded and run in a
    thread, so a slow or unavailable index can never block or break the pipeline."""
    try:
        from aegis_sre.orchestrator.rag_engine import DEFAULT_SRE_SKILLS
        rag = get_rag_engine()
        await asyncio.to_thread(rag.ingest_workspace)
        await asyncio.to_thread(rag.ingest_skills, DEFAULT_SRE_SKILLS)
        logger.info("rag_warm_complete")
    except Exception as e:  # noqa: BLE001 - RAG is enrichment; never break startup
        logger.warning("rag_warm_failed", error=str(e))

class GraphState(TypedDict):
    telemetry: TelemetryEvent
    code_context: str | None
    # Holds any Remediation (CodePatch for crashes, ActionPlan for metric/log
    # signals). Kept named `current_patch` for back-compat with API/approval/WS.
    current_patch: Remediation | None
    sandbox_status: str
    review: SecurityReview | None
    iteration_count: int
    resolved: bool
    # Triage decision (set by planner): which remediation kind to produce.
    signal_kind: str

# Non-crash signal kinds route to an ActionPlan; crashes route to a CodePatch.
_ACTION_SIGNALS = {"metric_alert", "log_anomaly", "generic"}


def planner_node(state: GraphState) -> GraphState:
    """Triage: decide whether this signal needs a code patch or an infra action,
    based on the originating Signal kind (carried in telemetry metadata by the
    Stone-1 adapter). This is what routes crash -> CodePatch vs alert -> ActionPlan."""
    kind = (state["telemetry"].metadata or {}).get("signal_kind", "crash")
    remediation = "action_plan" if kind in _ACTION_SIGNALS else "code_patch"
    logger.info("triaging_signal", node="planner", service_name=state["telemetry"].service_name,
                signal_kind=kind, remediation=remediation)
    return {"signal_kind": kind}

async def researcher_node(state: GraphState) -> GraphState:
    logger.info("hunting_context", node="researcher", source="vcs")
    crash_log = state["telemetry"].crash_log
    context_blocks = []
    
    vcs = get_vcs_provider()
    
    # Extract file paths and line numbers from standard Python stack traces
    file_matches = re.findall(r'File\s+"([^"]+)",\s+line\s+(\d+)', crash_log)
    
    for file_path, line_num in file_matches:
        try:
            line_num = int(line_num)
            content = await vcs.fetch_file_content(file_path)
            if content:
                lines = content.splitlines()
                start = max(0, line_num - 20)
                end = min(len(lines), line_num + 20)
                snippet = "\n".join(lines[start:end])
                context_blocks.append(f"--- {file_path} (Lines {start+1}-{end}) ---\n{snippet}")
            else:
                logger.info("file_not_found_in_vcs", node="researcher", file_path=file_path)
        except Exception as e:
            logger.error("error_reading_file", node="researcher", file_path=file_path, error=str(e))
            
    if not context_blocks:
        # Don't invent fiction in production: only inject demo code context when
        # explicitly in dev/demo mode. Otherwise tell the model the source wasn't
        # available so it doesn't patch a hallucinated file (audit #10).
        if os.environ.get("AEGIS_ALLOW_MOCK_PATCH", "false").lower() == "true":
            logger.info("no_files_found_using_mock_DEV_ONLY", node="researcher")
            context_blocks.append("--- Mock Context ---\ndef process_payment():\n    # user_balance = db.get_user(user_id)[\"balance\"]\n    pass")
        else:
            logger.info("no_local_source_found", node="researcher")
            context_blocks.append(
                "--- No local source available for the referenced files. "
                "Diagnose from the stack trace + live context; do not invent file contents. ---")
        
    # 2. Semantic Skills & Codebase Retrieval (Experimental SRA + AST RAG)
    try:
        logger.info("querying_dual_rag_engine", node="researcher", query="crash_log")
        rag = get_rag_engine()
        
        # Skill Retrieval Augmentation
        skill_context = rag.query_skills(search_term=crash_log, top_k=1)
        if skill_context:
            context_blocks.append(skill_context)
            
        # Abstract Syntax Tree Codebase Retrieval
        code_context = rag.query_codebase(search_term=crash_log, top_k=2)
        if code_context:
            context_blocks.append(code_context)
    except Exception as e:
        logger.warning("rag_query_failed", node="researcher", error=str(e))

    # 3. Live observability (Prometheus). Guarded: an observability outage must
    # degrade to "no live metrics", never fail the repair. Skipped entirely when
    # PROMETHEUS_URL is unset (get_metrics_client() returns None).
    try:
        metrics_block = await _gather_live_metrics(state["telemetry"].service_name)
        if metrics_block:
            context_blocks.append(metrics_block)
    except Exception as e:  # noqa: BLE001 - never let metrics enrichment break the graph
        logger.warning("prometheus_query_failed", node="researcher", error=str(e))

    return {"code_context": "\n".join(context_blocks)}


async def _gather_live_metrics(service_name: str) -> str | None:
    """Pull a small standard panel of live metrics for the crashing service and
    render it as an LLM-readable context block. Returns None when observability
    is disabled or nothing is available."""
    from aegis_sre.orchestrator.metrics_tools import format_samples, get_metrics_client

    client = get_metrics_client()
    if client is None:
        return None

    logger.info("querying_live_metrics", node="researcher", service=service_name)
    # `up` proves the scrape target's liveness; the others are best-effort and
    # simply render "(no data)" if the service doesn't export them.
    queries = [
        "up",
        f'up{{job="{service_name}"}}',
        f'rate(http_requests_total{{job="{service_name}",code=~"5.."}}[5m])',
        f'process_resident_memory_bytes{{job="{service_name}"}}',
    ]
    blocks = []
    for q in queries:
        samples = await client.query(q)
        if samples:
            blocks.append(format_samples(q, samples))
    if not blocks:
        return None
    return "--- Live Metrics (Prometheus) ---\n" + "\n".join(blocks)

async def executor_node(state: GraphState) -> GraphState:
    """Produce a Remediation. Dispatches on the planner's triage: crash signals
    get a CodePatch; metric/log signals get an ActionPlan (gated infra action)."""
    iteration = state.get("iteration_count", 0)
    if state.get("signal_kind", "crash") in _ACTION_SIGNALS:
        current = await _generate_action_plan(state, iteration)
    else:
        current = await _generate_code_patch(state, iteration)
    return {"current_patch": current, "iteration_count": iteration + 1, "sandbox_status": "pending"}


def _retry_feedback(state: GraphState, iteration: int) -> str:
    """Feed the reviewer's rejection back on a retry so the model produces a
    different remediation instead of repeating the rejected one."""
    review = state.get("review")
    if iteration > 0 and review is not None and not review.is_safe:
        return (f"\n\n--- PREVIOUS ATTEMPT WAS REJECTED ---\nReviewer feedback: {review.feedback}\n"
                "Produce a DIFFERENT remediation that addresses this feedback.")
    return ""


async def _generate_code_patch(state: GraphState, iteration: int):
    logger.info("generating_patch", node="executor", iteration=iteration + 1)
    executor_model = get_settings().executor_model
    system_prompt = (
        "You are an autonomous SRE. Your job is to fix code that causes crashes. "
        "Analyze the stack trace and the surrounding Code Context. Output a JSON object matching this schema exactly:\n"
        "{'file_path': 'string', 'target_content': 'string', 'replacement_content': 'string', 'root_cause_analysis': 'string', 'explanation': 'string'}"
    )
    user_prompt = (
        f"Crash Log:\n{state['telemetry'].crash_log}\n\n"
        f"Code Context:\n{state.get('code_context', 'No context available.')}"
        + _retry_feedback(state, iteration)
    )
    try:
        content = await chat_json(executor_model, system_prompt, user_prompt)
        current_patch = PatchProposal(**json.loads(content))
        metrics.patches_generated.inc()
        logger.info("patch_generated", node="executor", model=executor_model)
        return current_patch
    except (json.JSONDecodeError, ValidationError) as e:
        logger.error("executor_bad_output", node="executor", error=str(e))
        return None
    except Exception as e:  # noqa: BLE001 - fail closed, never fabricate a patch
        if os.environ.get("AEGIS_ALLOW_MOCK_PATCH", "false").lower() == "true":
            logger.warning("network_error_fallback_mock_DEV_ONLY", node="executor", error=str(e))
            return PatchProposal(
                file_path="main.py", target_content="def process_data(data):\n    pass",
                replacement_content="def process_data(data):\n    if not data:\n        return None\n    pass",
                root_cause_analysis="`data` was None at line 42 when subscripting.",
                explanation="Added null check to prevent NoneType exception.")
        logger.error("executor_llm_call_failed_no_patch", node="executor", error=str(e))
        return None


async def _generate_action_plan(state: GraphState, iteration: int):
    """Generate an ActionPlan for a non-crash signal. Allowed tools come from the
    registry's ACT tools so the model can only propose gateable actions; the plan
    is dry_run=True by default (policy + approval gate live execution)."""
    logger.info("generating_action_plan", node="executor", iteration=iteration + 1)
    from aegis_sre.integrations.tool_registry import get_tool_registry

    executor_model = get_settings().executor_model
    act_tools = [t.name for t in get_tool_registry().gated_tools()]
    system_prompt = (
        "You are an autonomous SRE responding to a live infrastructure alert. Propose a "
        "remediation as a JSON ActionPlan with EXACTLY this schema:\n"
        "{'steps': [{'tool': 'string', 'args': {}, 'description': 'string'}], "
        "'rollback_steps': [{'tool': 'string', 'args': {}, 'description': 'string'}], "
        "'blast_radius': 'low'|'medium'|'high', "
        "'verification': {'query': '<PromQL>', 'comparator': 'lt'|'lte'|'gt'|'gte'|'eq', 'threshold': <number>}, "
        "'root_cause_analysis': 'string', 'explanation': 'string'}\n"
        f"Only use tools from this allowed list: {act_tools or ['k8s.cordon_node','k8s.scale_deployment','job.requeue']}."
    )
    user_prompt = (
        f"Alert / Signal:\n{state['telemetry'].crash_log}\n\n"
        f"Live Context:\n{state.get('code_context', 'No context available.')}"
        + _retry_feedback(state, iteration)
    )
    try:
        content = await chat_json(executor_model, system_prompt, user_prompt)
        plan = ActionPlan(**json.loads(content))
        metrics.patches_generated.inc()
        logger.info("action_plan_generated", node="executor", model=executor_model,
                    steps=len(plan.steps), blast_radius=plan.blast_radius.value)
        return plan
    except (json.JSONDecodeError, ValidationError) as e:
        logger.error("executor_bad_action_plan", node="executor", error=str(e))
        return None
    except Exception as e:  # noqa: BLE001 - fail closed
        logger.error("executor_action_plan_failed", node="executor", error=str(e))
        return None

async def sandbox_node(state: GraphState) -> GraphState:
    logger.info("validating_remediation", node="sandbox")
    remediation = state.get("current_patch")
    if not remediation:
        return {"sandbox_status": "failed"}

    # Code patches need the *real* current source so the patch is applied in
    # context and the full patched file is validated — not the chunk alone.
    # Other remediations (ActionPlan) have no source to fetch; the Validator
    # dry-runs them instead.
    original_source = None
    if isinstance(remediation, CodePatch):
        try:
            vcs = get_vcs_provider()
            original_source = await vcs.fetch_file_content(remediation.file_path)
        except Exception as e:  # noqa: BLE001
            logger.warning("sandbox_source_fetch_failed", node="sandbox",
                           file_path=remediation.file_path, error=str(e))

    # Behavioral reproduction is OPTIONAL and must come from a trusted operator
    # source (env), never from the attacker-influenceable crash telemetry.
    repro_command = os.environ.get("AEGIS_REPRO_COMMAND") or None

    # Type-agnostic gate: CodePatch -> compile/repro, ActionPlan -> dry-run.
    result = await Validator().validate(
        remediation, original_source=original_source, repro_command=repro_command
    )

    if result.success:
        metrics.sandbox_validations.labels(result="success").inc()
        logger.info("sandbox_validation_passed", node="sandbox", kind=result.kind, output=result.output)
        return {"sandbox_status": "success"}
    metrics.sandbox_validations.labels(result="failed").inc()
    logger.error("sandbox_validation_failed", node="sandbox", kind=result.kind, output=result.output)
    return {"sandbox_status": "failed"}

async def reviewer_node(state: GraphState) -> GraphState:
    logger.info("analyzing_security_risks", node="reviewer")
    reviewer_model = get_settings().reviewer_model  # e.g. qwen2.5-coder:7b

    system_prompt = (
        "You are a strict Security Reviewer. Evaluate the proposed code patch for security flaws or logic errors. "
        "Output a JSON object matching this schema exactly:\n"
        "{'is_safe': boolean, 'vulnerability_found': boolean, 'feedback': 'string'}"
    )
    
    patch = state.get("current_patch")
    if not patch:
        return state

    # Describe the remediation polymorphically (CodePatch vs ActionPlan).
    if isinstance(patch, CodePatch):
        change = (f"Proposed Patch for {patch.file_path}:\n"
                  f"Replace:\n{patch.target_content}\nWith:\n{patch.replacement_content}")
    elif isinstance(patch, ActionPlan):
        steps = "\n".join(f"  - {s.tool}({s.args})" for s in patch.steps)
        change = (f"Proposed ActionPlan (blast_radius={patch.blast_radius.value}):\n{steps}\n"
                  f"Rollback steps: {len(patch.rollback_steps)}")
    else:
        change = f"Proposed remediation: {type(patch).__name__}"
    user_prompt = (
        f"Original Signal:\n{state['telemetry'].crash_log}\n\n{change}\n"
        f"Explanation: {patch.explanation}"
    )
    
    try:
        content = await chat_json(reviewer_model, system_prompt, user_prompt)
        review_data = json.loads(content)
        review = SecurityReview(**review_data)
        logger.info("review_completed", node="reviewer", model=reviewer_model)
    except json.JSONDecodeError as e:
        logger.error("invalid_json", node="reviewer", error=str(e))
        review = SecurityReview(is_safe=False, vulnerability_found=False, feedback="Failed to parse Reviewer JSON output.")
    except ValidationError as e:
        logger.error("schema_validation_failed", node="reviewer", error=str(e))
        review = SecurityReview(is_safe=False, vulnerability_found=False, feedback="Failed schema validation for Reviewer output.")
    except Exception as e:
        # FAIL-CLOSED: if the security reviewer LLM is unreachable we CANNOT
        # assert the patch is safe. Previously this defaulted to is_safe=True
        # ("Logic is sound."), which meant an outage auto-approved unreviewed
        # code straight to deploy. Default to is_safe=False so an outage routes
        # to retry/abort instead of a blind deploy.
        logger.error("reviewer_llm_call_failed_failing_closed", node="reviewer", error=str(e))
        review = SecurityReview(
            is_safe=False,
            vulnerability_found=False,
            feedback=f"Reviewer unavailable ({type(e).__name__}); failing closed. Patch NOT auto-approved.",
        )
    
    return {"review": review}

def should_deploy(state: GraphState) -> str:
    review = state.get("review")
    sandbox_status = state.get("sandbox_status")

    if review and review.is_safe and sandbox_status == "success":
        return "deploy"

    should_abort, reason = safety_policy.should_abort(state)
    if should_abort:
        logger.warning("graph_execution_aborted", reason=reason)
        return "fail"

    return "retry"


def deploy_node(state: GraphState) -> GraphState:
    """Terminal node for a validated, approved-ready remediation. Marks the
    incident resolved (so `resolved` is no longer write-only dead state)."""
    patch = state.get("current_patch")
    logger.info("remediation_ready_for_deploy", node="deploy",
                kind=type(patch).__name__ if patch else None)
    return {"resolved": True}

def build_graph(checkpointer=None):
    workflow = StateGraph(GraphState)

    workflow.add_node("planner", planner_node)
    workflow.add_node("researcher", researcher_node)
    workflow.add_node("executor", executor_node)
    workflow.add_node("sandbox", sandbox_node)
    workflow.add_node("reviewer", reviewer_node)
    workflow.add_node("deploy", deploy_node)

    workflow.set_entry_point("planner")
    workflow.add_edge("planner", "researcher")
    workflow.add_edge("researcher", "executor")
    workflow.add_edge("executor", "sandbox")
    workflow.add_edge("sandbox", "reviewer")

    workflow.add_conditional_edges(
        "reviewer",
        should_deploy,
        {
            "deploy": "deploy",
            "fail": END,
            "retry": "executor"
        }
    )
    workflow.add_edge("deploy", END)

    return workflow.compile(checkpointer=checkpointer)
