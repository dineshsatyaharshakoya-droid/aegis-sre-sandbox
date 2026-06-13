import os
import re
import json
from typing import TypedDict
from langgraph.graph import StateGraph, END
from pydantic import ValidationError

from aegis_sre.config import get_settings
from aegis_sre.orchestrator.llm import chat_json
from aegis_sre.orchestrator.schemas import TelemetryEvent, PatchProposal, SecurityReview
from aegis_sre.orchestrator.vcs_provider import get_vcs_provider
from aegis_sre.orchestrator.sandbox_engine import get_sandbox_engine
from aegis_sre.orchestrator.rag_engine import RAGEngine
from aegis_sre.orchestrator.safety import safety_policy
from aegis_sre.telemetry.logger import logger
from aegis_sre.telemetry import metrics

_rag_engine_instance = None

def get_rag_engine() -> RAGEngine:
    global _rag_engine_instance
    if _rag_engine_instance is None:
        _rag_engine_instance = RAGEngine(workspace_path=".")
    return _rag_engine_instance

class GraphState(TypedDict):
    telemetry: TelemetryEvent
    code_context: str | None
    current_patch: PatchProposal | None
    sandbox_status: str
    review: SecurityReview | None
    iteration_count: int
    resolved: bool

def planner_node(state: GraphState) -> GraphState:
    logger.info("analyzing_telemetry", node="planner", service_name=state['telemetry'].service_name)
    return state

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
        logger.info("no_files_found_using_mock", node="researcher")
        context_blocks.append("--- Mock Context ---\ndef process_payment():\n    # user_balance = db.get_user(user_id)[\"balance\"]\n    pass")
        
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
        
    return {"code_context": "\n".join(context_blocks)}

async def executor_node(state: GraphState) -> GraphState:
    iteration = state.get("iteration_count", 0)
    logger.info("generating_patch", node="executor", iteration=iteration + 1)
    
    # Executor/reasoning model (e.g. hermes3:8b on the local OpenAI-compatible endpoint).
    executor_model = get_settings().executor_model

    system_prompt = (
        "You are an autonomous SRE. Your job is to fix code that causes crashes. "
        "Analyze the stack trace and the surrounding Code Context. Output a JSON object matching this schema exactly:\n"
        "{'file_path': 'string', 'target_content': 'string', 'replacement_content': 'string', 'root_cause_analysis': 'string', 'explanation': 'string'}"
    )
    user_prompt = (
        f"Crash Log:\n{state['telemetry'].crash_log}\n\n"
        f"Code Context:\n{state.get('code_context', 'No context available.')}"
    )

    # On a retry, feed the reviewer's rejection reason and the rejected patch
    # back to the model. Without this the executor regenerated blind and tended
    # to reproduce the same rejected patch, burning every retry on identical
    # output before the safety policy aborted.
    previous_review = state.get("review")
    previous_patch = state.get("current_patch")
    if iteration > 0 and previous_review is not None and not previous_review.is_safe:
        rejected = (
            f"\n\n--- PREVIOUS ATTEMPT WAS REJECTED ---\n"
            f"Reviewer feedback: {previous_review.feedback}\n"
        )
        if previous_patch is not None:
            rejected += (
                f"Rejected replacement for {previous_patch.file_path}:\n"
                f"{previous_patch.replacement_content}\n"
            )
        rejected += "Produce a DIFFERENT patch that addresses this feedback."
        user_prompt += rejected

    try:
        content = await chat_json(executor_model, system_prompt, user_prompt)
        patch_data = json.loads(content)
        current_patch = PatchProposal(**patch_data)
        metrics.patches_generated.inc()
        logger.info("patch_generated", node="executor", model=executor_model)
    except json.JSONDecodeError as e:
        logger.error("invalid_json", node="executor", error=str(e))
        current_patch = None
    except ValidationError as e:
        logger.error("schema_validation_failed", node="executor", error=str(e))
        current_patch = None
    except Exception as e:
        # FAIL-CLOSED: an infrastructure error (network/timeout/rate-limit) must
        # NOT fabricate a patch. Returning a hardcoded mock here previously let a
        # fake `main.py` null-check flow downstream and potentially be deployed.
        # Only emit the demo patch when explicitly running in dev/demo mode.
        if os.environ.get("AEGIS_ALLOW_MOCK_PATCH", "false").lower() == "true":
            logger.warning("network_error_fallback_mock_DEV_ONLY", node="executor", error=str(e))
            current_patch = PatchProposal(
                file_path="main.py",
                target_content="def process_data(data):\n    pass",
                replacement_content="def process_data(data):\n    if not data:\n        return None\n    pass",
                root_cause_analysis="The `data` object was None at line 42 when subscripting.",
                explanation="Added null check to prevent NoneType exception."
            )
        else:
            logger.error("executor_llm_call_failed_no_patch", node="executor", error=str(e))
            current_patch = None
    
    return {
        "current_patch": current_patch,
        "iteration_count": iteration + 1,
        "sandbox_status": "pending"
    }

async def sandbox_node(state: GraphState) -> GraphState:
    logger.info("compiling_and_testing", node="sandbox")
    patch = state.get("current_patch")
    if not patch:
        return {"sandbox_status": "failed"}

    # Fetch the *real* current source so the patch is applied in context and the
    # full patched file is what gets validated — not the replacement chunk alone.
    original_source = None
    try:
        vcs = get_vcs_provider()
        original_source = await vcs.fetch_file_content(patch.file_path)
    except Exception as e:  # noqa: BLE001
        logger.warning("sandbox_source_fetch_failed", node="sandbox", file_path=patch.file_path, error=str(e))

    # Behavioral reproduction is OPTIONAL and must come from a trusted operator
    # source (env), never from the attacker-influenceable crash telemetry.
    repro_command = os.environ.get("AEGIS_REPRO_COMMAND") or None

    engine = get_sandbox_engine()
    success, output = await engine.compile_and_test(
        patch, original_source=original_source, repro_command=repro_command
    )

    if success:
        metrics.sandbox_validations.labels(result="success").inc()
        logger.info("sandbox_validation_passed", node="sandbox", output=output)
        return {"sandbox_status": "success"}
    metrics.sandbox_validations.labels(result="failed").inc()
    logger.error("sandbox_validation_failed", node="sandbox", output=output)
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
        
    user_prompt = (
        f"Original Crash Log:\n{state['telemetry'].crash_log}\n\n"
        f"Proposed Patch for {patch.file_path}:\n"
        f"Replace:\n{patch.target_content}\n"
        f"With:\n{patch.replacement_content}\n"
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

def build_graph(checkpointer=None):
    workflow = StateGraph(GraphState)
    
    workflow.add_node("planner", planner_node)
    workflow.add_node("researcher", researcher_node)
    workflow.add_node("executor", executor_node)
    workflow.add_node("sandbox", sandbox_node)
    workflow.add_node("reviewer", reviewer_node)
    
    workflow.set_entry_point("planner")
    workflow.add_edge("planner", "researcher")
    workflow.add_edge("researcher", "executor")
    workflow.add_edge("executor", "sandbox")
    workflow.add_edge("sandbox", "reviewer")
    
    workflow.add_conditional_edges(
        "reviewer",
        should_deploy,
        {
            "deploy": END,
            "fail": END,
            "retry": "executor"
        }
    )
    
    return workflow.compile(checkpointer=checkpointer)
