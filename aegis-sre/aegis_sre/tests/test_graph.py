import pytest
import asyncio
from unittest.mock import patch, MagicMock
from aegis_sre.orchestrator.graph import build_graph
from aegis_sre.orchestrator.schemas import TelemetryEvent, PatchProposal, SecurityReview

@pytest.fixture
def base_telemetry():
    return TelemetryEvent(
        event_id="test-001",
        service_name="test-service",
        crash_log="test log"
    )

@pytest.mark.asyncio
async def test_graph_success_first_try(base_telemetry):
    with patch("aegis_sre.orchestrator.graph.executor_node") as mock_exec, \
         patch("aegis_sre.orchestrator.graph.sandbox_node") as mock_sandbox, \
         patch("aegis_sre.orchestrator.graph.reviewer_node") as mock_reviewer:
         
        app = build_graph()
         
        mock_exec.return_value = {
            "current_patch": PatchProposal(file_path="f.py", target_content="a", replacement_content="b", explanation="fix"),
            "iteration_count": 1,
            "sandbox_status": "pending"
        }
        mock_sandbox.return_value = {"sandbox_status": "success"}
        mock_reviewer.return_value = {"review": SecurityReview(is_safe=True, vulnerability_found=False, feedback="ok")}
        
        initial_state = {
            "telemetry": base_telemetry,
            "current_patch": None,
            "sandbox_status": "pending",
            "review": None,
            "iteration_count": 0,
            "resolved": False
        }
        
        final_state = await app.ainvoke(initial_state)
        
        assert final_state["sandbox_status"] == "success"
        assert final_state["review"].is_safe == True
        assert final_state["iteration_count"] == 1
        assert mock_exec.call_count == 1
        assert mock_sandbox.call_count == 1
        assert mock_reviewer.call_count == 1

@pytest.mark.asyncio
async def test_graph_retry_loop_on_sandbox_failure(base_telemetry):
    app = build_graph()
    
    call_tracker = {"exec_calls": 0}
    
    def mock_executor_logic(state):
        call_tracker["exec_calls"] += 1
        return {
            "current_patch": PatchProposal(file_path="f.py", target_content="a", replacement_content="b", explanation="fix"),
            "iteration_count": call_tracker["exec_calls"],
            "sandbox_status": "pending"
        }
    
    def mock_sandbox_logic(state):
        # Fail the first 2 times, succeed on the 3rd
        if state.get("iteration_count") < 3:
            return {"sandbox_status": "failed"}
        return {"sandbox_status": "success"}

    def mock_reviewer_logic(state):
        return {"review": SecurityReview(is_safe=True, vulnerability_found=False, feedback="ok")}

    with patch("aegis_sre.orchestrator.graph.executor_node", side_effect=mock_executor_logic), \
         patch("aegis_sre.orchestrator.graph.sandbox_node", side_effect=mock_sandbox_logic), \
         patch("aegis_sre.orchestrator.graph.reviewer_node", side_effect=mock_reviewer_logic):
         
        app = build_graph()
        initial_state = {
            "telemetry": base_telemetry,
            "current_patch": None,
            "sandbox_status": "pending",
            "review": None,
            "iteration_count": 0,
            "resolved": False
        }
        
        final_state = await app.ainvoke(initial_state)
        
        assert final_state["sandbox_status"] == "success"
        assert final_state["iteration_count"] == 3
        assert call_tracker["exec_calls"] == 3

@pytest.mark.asyncio
async def test_graph_max_iterations_fail(base_telemetry):
    app = build_graph()
    
    call_tracker = {"exec_calls": 0}
    def mock_executor_logic(state):
        call_tracker["exec_calls"] += 1
        return {
            "current_patch": PatchProposal(file_path="f.py", target_content="a", replacement_content="b", explanation="fix"),
            "iteration_count": call_tracker["exec_calls"],
            "sandbox_status": "pending"
        }
        
    def mock_sandbox_logic(state):
        # Always fail compilation
        return {"sandbox_status": "failed"}
        
    def mock_reviewer_logic(state):
        return {"review": SecurityReview(is_safe=False, vulnerability_found=False, feedback="bad logic")}

    with patch("aegis_sre.orchestrator.graph.executor_node", side_effect=mock_executor_logic), \
         patch("aegis_sre.orchestrator.graph.sandbox_node", side_effect=mock_sandbox_logic), \
         patch("aegis_sre.orchestrator.graph.reviewer_node", side_effect=mock_reviewer_logic):
         
        app = build_graph()
        initial_state = {
            "telemetry": base_telemetry,
            "current_patch": None,
            "sandbox_status": "pending",
            "review": None,
            "iteration_count": 0,
            "resolved": False
        }
        
        final_state = await app.ainvoke(initial_state)
        
        # Max iterations is configured in should_deploy as >= 3
        assert final_state["iteration_count"] == 3
        assert final_state["sandbox_status"] == "failed"
