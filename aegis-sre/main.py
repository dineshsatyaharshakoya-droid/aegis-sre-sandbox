import os
import sys
import asyncio
import argparse

# Ensure Python can find the package
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from aegis_sre.orchestrator.schemas import TelemetryEvent
from aegis_sre.orchestrator.graph import build_graph
from aegis_sre.orchestrator.rag_engine import RAGEngine
from aegis_sre.telemetry.logger import logger

MOCK_SRE_SKILLS = [
    {
        "issue_type": "NoneType Error",
        "resolution": "When a 'NoneType' object is not subscriptable error occurs during a data processing or payment pipeline, it indicates that a previous function call or API request returned None instead of a dictionary. The senior SRE pattern to fix this is to wrap the data access in an `if data:` or `if data is not None:` null-check guard clause, returning a safe default or logging the missing data explicitly before attempting to subscript keys."
    },
    {
        "issue_type": "OOM Killed",
        "resolution": "If a Kubernetes pod throws an OutOfMemory (OOM) Killed exception in a data pipeline, the standard SRE resolution is to implement chunking or pagination in the query layer, ensuring that massive datasets are processed in generators rather than loaded fully into RAM at once."
    }
]

def build_graph_mock():
    # Helper to prevent circular imports if needed
    from aegis_sre.orchestrator.graph import build_graph
    return build_graph()

async def run_chaos_crucible():
    logger.info("starting_chaos_crucible", mode="local_test")
    
    # 1. Simulate a crash coming from a podEvent
    mock_crash = TelemetryEvent(
        event_id="CRASH-001",
        service_name="payment-processor-pod-xyz",
        crash_log="""
        Traceback (most recent call last):
          File "main.py", line 42, in process_payment
            user_balance = db.get_user(user_id)["balance"]
        TypeError: 'NoneType' object is not subscriptable
        """,
        metadata={"pod_ip": "10.0.1.5", "namespace": "production"}
    )
    
    logger.info("intercepted_crash", service=mock_crash.service_name, error="TypeError: 'NoneType' object is not subscriptable")
    
    # 2. Ingest Workspace and Skills into VectorDB for SRA + AST RAG
    logger.info("building_ast_rag_index_and_skills_brain")
    rag = RAGEngine(workspace_path=".")
    rag.ingest_workspace()
    rag.ingest_skills(MOCK_SRE_SKILLS)
    
    # 3. Build the LangGraph
    app = build_graph()
    
    # 3. Start the Orchestrator Loop
    initial_state = {
        "telemetry": mock_crash,
        "code_context": None,
        "current_patch": None,
        "sandbox_status": "pending",
        "review": None,
        "iteration_count": 0,
        "resolved": False
    }
    
    logger.info("initializing_autonomous_repair_loop")
    
    final_state = await app.ainvoke(initial_state)
    
    logger.info("repair_complete", status="success")
    
    # Ensure final_state exists and handles dict structure from LangGraph
    if final_state and isinstance(final_state, dict):
        patch = final_state.get('current_patch')
        if patch:
            logger.info("final_patch_proposed", file=patch.file_path, explanation=patch.explanation, diff=patch.replacement_content)
        else:
            logger.info("no_patch_generated")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Aegis SRE Daemon")
    parser.add_argument("--api", action="store_true", help="Start the FastAPI Universal Webhook receiver")
    args = parser.parse_args()
    
    if args.api:
        print("========================================")
        print("🌐 STARTING AEGIS UNIVERSAL WEBHOOK API 🌐")
        print("========================================")
        import uvicorn
        # Run the FastAPI app via Uvicorn
        uvicorn.run("aegis_sre.telemetry.api_receiver:app", host="0.0.0.0", port=8000, reload=False)
    else:
        asyncio.run(run_chaos_crucible())
