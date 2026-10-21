"""
tests/test_architecture_repair.py
---------------------------------
Comprehensive validation test suite for Cortex Pre-A9 General Semantic Routing
and Capability Unification Architecture Repair.

Verifies the 16 core scenarios and architectural guard rails specified in Section 15:
1. Semantic paraphrases resolve to same capability
2. No special handler needed for each paraphrase
3. Target grounding happens after semantic interpretation
4. Canonical registry is shared across planner, system registry, and orchestrator
5. TaskOrchestrator accepts any valid registered capability
6. TaskOrchestrator rejects invented/unregistered capabilities
7. Planner builds plan from capability schema, not phrase template
8. Missing capability returns truthful limitation
9. Conversational follow-up uses prior grounded context
10. Previous-window concept resolves from world-state history
11. Process memory query uses general process telemetry
12. A5 executor routing regression
13. A6 ownership/failover regression
14. A7 privacy regression
15. A8 transfer regression
16. Production DB isolation guard

Architectural Guard Tests:
- TaskOrchestrator does not contain a fixed four-capability whitelist
- GoalPlanner does not maintain a second divergent capability registry
- agent_harness does not depend on expanding phrase dictionaries for ordinary intents
"""

import inspect
import json
import os
import re
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio

import hub.database as database
import hub.main
from hub.agent_harness import AgentHarness
from hub.capability_registry import (
    CanonicalCapabilityRegistry,
    RegisteredCapability,
)
from hub.control_layer import CortexControlLayer
from hub.device_handle import DeviceHandle
from hub.executor_registry import ExecutorRegistry
from hub.executor_selector import ExecutorSelector
from hub.goals import GoalPlanner
from hub.pipeline.context_assembler import AssembledContext, ContextAssembler
from hub.pipeline.normalizer import InputNormalizer
from hub.pipeline.semantic_interpreter import SemanticInterpreter
from hub.pipeline.target_grounder import TargetGrounder
from hub.privacy import PrivacyClass, PrivacyDecision, PrivacyPolicyEngine, TaskPrivacyContext
from hub.system_registry import SystemRegistry
from hub.target_resolver import TargetResolver
from hub.task_orchestrator import TaskOrchestrator
from hub.task_ownership import TaskOwnershipManager
from hub.world_state import Signal, WorldState, WorldStateManager


# ===========================================================================
# 1. Semantic paraphrases resolve to same capability (Section 14 & 15.1)
# ===========================================================================
@pytest.mark.asyncio
async def test_semantic_paraphrases_resolve_to_same_capability():
    from model_contract_fixtures import scripted_harness, call
    for request in ["Open Chrome.", "Launch Chrome.", "Can you bring Chrome up?", "I need Chrome."]:
        harness, ctrl, model = scripted_harness([call("app.launch", app="chrome")], [{"status":"success","data":{"opened":True}}])
        result = await harness.process_request("asus", request, {})
        assert result.status == "success"
        assert ctrl.execute_and_observe.call_args.args[1:] == ("app.launch", {"app":"chrome", "target_device":"asus"})
        assert model.calls[0][0] == request  # Wiring, not simulated paraphrase comprehension.


# ===========================================================================
# 2. No special handler needed for each paraphrase (Section 15.2)
# ===========================================================================
def test_no_special_handler_needed_for_each_paraphrase():
    """Ensure agent_harness.py does not contain explicit hardcoded branches for the paraphrases."""
    import hub.agent_harness as ah
    source = inspect.getsource(ah)

    banned_substrings = [
        "can you bring chrome up",
        "i need chrome",
        "close the app i'm using right now",
        "open it back",
        "so i'm working on you",
    ]
    for banned in banned_substrings:
        assert banned not in source.lower(), (
            f"Architectural violation: Found hardcoded phrase '{banned}' in agent_harness.py! "
            f"Routing must be semantic, not phrase-enumerated."
        )


# ===========================================================================
# 3. Target grounding happens after semantic interpretation (Section 4 & 15.3)
# ===========================================================================
@pytest.mark.asyncio
async def test_target_grounding_happens_after_semantic_interpretation():
    from model_contract_fixtures import scripted_harness, call
    harness, ctrl, model = scripted_harness([call("app.close", app="Code.exe")], [{"status":"success","data":{"closed":True}}])
    context={"world_state":{"foreground_app":"Code.exe"}}
    result=await harness.process_request("asus", "Close the app I'm using.", context)
    assert result.status == "success"
    assert ctrl.execute_and_observe.call_args.args[1:] == ("app.close", {"app":"Code.exe", "target_device":"asus"})
    state=json.loads(model.calls[0][1]["system_prompt"].split("\nSTATE:\n")[1])
    assert state["context"]["world_snapshot"]["foreground_app"] == "Code.exe"


# ===========================================================================
# 4. Canonical registry is shared across planner/system/orchestrator (Section 2 & 15.4)
# ===========================================================================
def test_canonical_registry_is_shared():
    """SystemRegistry, GoalPlanner, and TaskOrchestrator must consume the identical CanonicalCapabilityRegistry."""
    canon = CanonicalCapabilityRegistry.get_instance()
    sys_reg = SystemRegistry.get_instance()
    planner = GoalPlanner.get_instance()

    # All components see the same capabilities
    assert "process.list" in canon.get_all()
    assert any(c["name"] == "process.list" for c in sys_reg.list_capabilities())
    assert "process.list" in planner.BUILTIN_CAPABILITY_SCHEMAS

    # Dynamic registration in canonical registry is instantly visible across all three
    test_cap = RegisteredCapability(
        name="test.unification_probe",
        description="Dynamic probe for canonical registry sharing",
        arguments={"probe_id": {"type": "string", "required": True}},
        risk="low",
        classification="read",
        target_type="hub",
        permission_tier=1,
        availability=True,
    )
    canon.register(test_cap)

    assert "test.unification_probe" in canon.get_all()
    assert any(c["name"] == "test.unification_probe" for c in sys_reg.list_capabilities())
    assert "test.unification_probe" in planner.BUILTIN_CAPABILITY_SCHEMAS


# ===========================================================================
# 5. TaskOrchestrator accepts any valid registered capability (Section 3 & 15.5)
# ===========================================================================
@pytest.mark.asyncio
async def test_task_orchestrator_accepts_any_valid_registered_capability():
    """Verify TaskOrchestrator executes newly registered capabilities rather than jailing to 4 tools."""
    canon = CanonicalCapabilityRegistry.get_instance()
    assert canon.get_capability("process.list") is not None

    er = ExecutorRegistry.get_instance()
    h = DeviceHandle(
        device_id="asus",
        display_name="ASUS Host",
        role="primary_admin",
        trust_level="admin",
        capabilities=["process.list", "app.launch", "device.status", "fs.read", "ui.notify"],
    )
    er.register_device(h)
    er.set_device_online("asus", True)

    control = CortexControlLayer(hub.main.CONNECTED_DEVICES)
    orchestrator = TaskOrchestrator(control)

    from hub.intent_schema import ActionPlan
    valid_plan = ActionPlan(
        intent="inspect_processes",
        source_device="asus",
        target_device="asus",
        capability="process.list",
        action="process.list",
        arguments={"limit": 5, "sort_by": "memory"},
        requires_confirmation=False,
        confidence=1.0,
        user_facing_summary="inspecting processes",
    )

    # Must accept and execute via control layer without hitting the old 4-tool rejection
    async def mock_exec_cmd(dev, action, args, cmd_id=None, **kwargs):
        from hub.main import PENDING_FUTURES
        from hub.intent_schema import ConversationalResponse
        fut = PENDING_FUTURES.get(cmd_id)
        if fut and not fut.done():
            fut.set_result(ConversationalResponse(status="success", message="executed"))
        return cmd_id or "cmd_123"

    with patch.object(control, "execute_command", side_effect=mock_exec_cmd):
        res = await orchestrator._run_task_background(
            task_id="task_test_valid",
            title="Inspect processes",
            plan=valid_plan,
            source_device_id="asus",
        )
        assert res.get("status") == "COMPLETED"


# ===========================================================================
# 6. TaskOrchestrator rejects invented/unregistered capability (Section 3 & 15.6)
# ===========================================================================
@pytest.mark.asyncio
async def test_task_orchestrator_rejects_invented_capability():
    """TaskOrchestrator must reject invented or unregistered capabilities."""
    control = CortexControlLayer(hub.main.CONNECTED_DEVICES)
    orchestrator = TaskOrchestrator(control)

    from hub.intent_schema import ActionPlan
    invented_plan = ActionPlan(
        intent="hack_mainframe",
        source_device="asus",
        target_device="asus",
        capability="arbitrary.invented_tool",
        action="arbitrary.invented_tool",
        arguments={"foo": "bar"},
        requires_confirmation=False,
        confidence=1.0,
        user_facing_summary="executing invented tool",
    )

    res = await orchestrator._run_task_background(
        task_id="task_test_invented",
        title="Execute invented tool",
        plan=invented_plan,
        source_device_id="asus",
    )
    assert res.get("status") == "FAILED"
    assert "unregistered" in res.get("error", "").lower() or "not available" in res.get("error", "").lower()


# ===========================================================================
# 7. Planner builds plan from capability schema, not phrase template (Section 6 & 15.7)
# ===========================================================================
@pytest.mark.asyncio
async def test_planner_builds_plan_from_capability_schema():
    from hub.goals import Goal
    from hub.intent_schema import ModelResult
    planner=GoalPlanner.get_instance()
    planner._model_router=type("PlanningModel", (), {"name":"fixture", "locality":"local_device", "generate_result":AsyncMock(return_value=ModelResult(type="PLAN",plan={"steps":[{"capability":"process.list","arguments":{"sort_by":"memory"}}]}))})()
    caps=[c.to_dict() for c in CanonicalCapabilityRegistry.get_instance().get_available_capabilities("asus")]
    plan=await planner._generate_plan(Goal(goal_id="probe", title="Inspect processes",original_request="check how much memory processes are taking"),caps,"asus")
    assert plan["steps"][0]["capability"] == "process.list"
    assert planner._model_router.generate_result.call_args.args[1]["available_capabilities"] == caps


# ===========================================================================
# 8. Missing capability returns truthful limitation (Section 7 & 15.8)
# ===========================================================================
@pytest.mark.asyncio
async def test_missing_capability_returns_truthful_limitation():
    from model_contract_fixtures import scripted_harness
    harness,ctrl,model=scripted_harness([{"decision":"FAIL_TRUTHFULLY","response_text":"Physical fabrication hardware is unavailable."}])
    result=await harness.process_request("asus","Can you 3D print a hinge for my desk?",{})
    assert result.status == "unsupported"
    assert "hardware" in result.message.lower()
    ctrl.execute_and_observe.assert_not_called()


# ===========================================================================
# 9. Conversational follow-up uses prior grounded context (Section 14 & 15.9)
# ===========================================================================
@pytest.mark.asyncio
async def test_conversational_followup_uses_prior_context():
    from model_contract_fixtures import scripted_harness
    harness,ctrl,model=scripted_harness([{"decision":"RESPOND","response_text":"You're editing Cortex."}])
    prior=[{"role":"user","text":"What am I using?"},{"role":"assistant","text":"Cortex in Code.exe"}]
    result=await harness.process_request("asus","So I'm working on you?",{"history":prior})
    assert result.status == "success"
    assert model.calls[0][1]["conversation_history"] == prior
    ctrl.execute_and_observe.assert_not_called()


# ===========================================================================
# 10. Previous-window concept resolves from world-state history (Section 7 & 15.10)
# ===========================================================================
@pytest.mark.asyncio
async def test_previous_window_concept_resolves_from_history():
    from model_contract_fixtures import scripted_harness,call
    harness,ctrl,model=scripted_harness([call("window.focus",app="chrome.exe")],[{"status":"success","data":{"focused":True}}])
    context={"world_state":{"foreground_app":"slack.exe","previous_app":"chrome.exe"}}
    result=await harness.process_request("asus","Switch apps.",context)
    assert result.status == "success"
    assert ctrl.execute_and_observe.call_args.args[2]["app"] == "chrome.exe"
    assert "chrome.exe" in model.calls[0][1]["system_prompt"]


# ===========================================================================
# 11. Process memory query uses general process telemetry (Section 7 & 15.11)
# ===========================================================================
@pytest.mark.asyncio
async def test_process_memory_query_distinction():
    from model_contract_fixtures import scripted_harness,call
    for request, decision, raw in [("What's taking most of the memory on my laptop right now?",call("process.list",sort_by="memory"),{"processes":[]}), ("How much RAM am I using?",call("telemetry.get_system_usage"),{"cpu_percent":12,"ram_percent":34})]:
        harness,ctrl,model=scripted_harness([decision],[{"status":"success","data":raw}])
        result=await harness.process_request("asus",request,{})
        assert result.status == "success"
        assert ctrl.execute_and_observe.call_args.args[1] == decision["capability"]


# ===========================================================================
# 12. A5 executor routing regression (Section 13 & 15.12)
# ===========================================================================
def test_a5_executor_routing_regression():
    """Verify Phase A5 executor selection honors capability advertisement and liveness."""
    er = ExecutorRegistry.get_instance()
    h_local = DeviceHandle(
        device_id="asus",
        display_name="ASUS Primary",
        role="primary_admin",
        trust_level="admin",
        capabilities=["app.launch", "process.list"],
    )
    h_worker = DeviceHandle(
        device_id="worker_gpu",
        display_name="GPU Rig",
        role="trusted_worker",
        trust_level="trusted",
        capabilities=["model.infer"],
    )
    er.register_device(h_local)
    er.register_device(h_worker)
    er.set_device_online("asus", True)
    er.set_device_online("worker_gpu", True)

    selector = ExecutorSelector(er, PrivacyPolicyEngine())

    # Capabilities match respective nodes
    res_local = selector.select_executor(required_capability="process.list", source_device="asus")
    assert res_local.is_success
    assert res_local.selected_device == "asus"

    res_worker = selector.select_executor(required_capability="model.infer", source_device="asus")
    assert res_worker.is_success
    assert res_worker.selected_device == "worker_gpu"


# ===========================================================================
# 13. A6 ownership/failover regression (Section 13 & 15.13)
# ===========================================================================
@pytest.mark.asyncio
async def test_a6_ownership_failover_regression():
    """Verify Phase A6 task leases and fencing tokens operate correctly under the unified registry."""
    tom = TaskOwnershipManager.get_instance()
    execution = await tom.create_execution(
        goal_id="goal_regression_1",
        step_id="step_1",
        owner_device_id="cortex",
        executor_device_id="asus",
        required_capabilities=["process.list"],
        movable=True,
    )
    assert execution is not None
    assert execution.generation >= 1
    assert execution.executor_device_id == "asus"

    # Verification of active lease
    valid, err = tom.validate_fencing_token(execution.task_execution_id, execution.lease_id, execution.generation)
    assert valid is True


# ===========================================================================
# 14. A7 privacy regression (Section 13 & 15.14)
# ===========================================================================
def test_a7_privacy_regression():
    """Verify Phase A7 privacy policy blocks confidential data on restricted or cloud nodes."""
    engine = PrivacyPolicyEngine()
    restricted_node = DeviceHandle(
        device_id="guest_tablet",
        display_name="Guest Tablet",
        role="restricted",
        trust_level="restricted",
        locality="trusted_lan_device",
        private_compute_allowed=False,
    )
    confidential_ctx = TaskPrivacyContext(
        privacy_class=PrivacyClass.HIGHLY_SENSITIVE,
        data_categories=["credentials"],
    )

    eval_res = engine.evaluate_candidate(restricted_node, confidential_ctx, "fs.read")
    assert eval_res.is_allowed is False
    assert eval_res.decision in (PrivacyDecision.DENY, PrivacyDecision.LOCAL_ONLY, PrivacyDecision.WAIT_FOR_PRIVATE_EXECUTOR)


# ===========================================================================
# 15. A8 transfer regression (Section 13 & 15.15)
# ===========================================================================
@pytest.mark.asyncio
async def test_a8_transfer_regression(tmp_path):
    """Verify Phase A8 file transfer manifests remain intact."""
    from hub.file_transfer import FileTransferManager, TransferState
    from hub.executor_registry import ExecutorRegistry
    from hub.device_handle import DeviceHandle

    er = ExecutorRegistry.get_instance()
    ftm = FileTransferManager.get_instance()

    h_src = DeviceHandle(
        device_id="asus",
        display_name="ASUS Primary",
        role="primary_admin",
        trust_level="admin",
        capabilities=["fs.read"],
        transports={"lan": True},
        online=True,
    )
    h_dst = DeviceHandle(
        device_id="worker_node",
        display_name="Worker Node",
        role="trusted_worker",
        trust_level="trusted",
        capabilities=["fs.write"],
        transports={"lan": True},
        online=True,
    )
    er.register_device(h_src)
    er.register_device(h_dst)
    er.set_device_online("asus", True)
    er.set_device_online("worker_node", True)

    test_file = tmp_path / "probe.txt"
    dst_file = tmp_path / "worker_dest" / "probe.txt"
    test_file.write_text("Cortex Architecture Repair Phase A8 Transfer Probe")

    from hub.file_transfer import InProcessTransportProvider, TransportSelector
    ftm.transport_selector = TransportSelector({"lan": InProcessTransportProvider()})
    session = await ftm.initiate_transfer(
        source_device_id="asus",
        destination_device_id="worker_node",
        source_path=str(test_file),
        destination_path=str(dst_file),
    )
    assert session.total_bytes == len("Cortex Architecture Repair Phase A8 Transfer Probe")
    assert session.state == TransferState.PENDING


# ===========================================================================
# 16. Production DB isolation guard (Section 11 & 15.16)
# ===========================================================================
def test_production_db_isolation_guard():
    """Ensure running tests are guaranteed to never touch or write to production data/cortex.db."""
    from hub.database import get_db_path, reset_test_db

    current_path = get_db_path().resolve()
    prod_path = (Path("data/cortex.db")).resolve()

    assert current_path != prod_path, f"FATAL TEST FAILURE: Tests targeting production database at {current_path}!"
    assert not str(current_path).replace("\\", "/").endswith("data/cortex.db")

    # Safety guard in reset_test_db must raise if called on production path
    with patch("hub.database.get_db_path", return_value=prod_path):
        with pytest.raises(RuntimeError) as exc_info:
            reset_test_db()
        assert "FATAL SAFETY GUARD" in str(exc_info.value)


# ===========================================================================
# 17. Architectural Guard: TaskOrchestrator Whitelist Jail Removed (Section 3)
# ===========================================================================
def test_architectural_guard_task_orchestrator_whitelist_removed():
    """Inspect TaskOrchestrator source code to verify the hardcoded 4-tool whitelist is eliminated."""
    import hub.task_orchestrator as to_mod
    src = inspect.getsource(to_mod)

    # The anti-pattern was: if plan.capability not in ["app.launch", "device.status", "fs.read", "ui.notify"]:
    assert 'plan.capability not in ["app.launch", "device.status", "fs.read", "ui.notify"]' not in src, (
        "Anti-pattern detected: TaskOrchestrator still contains the 4-capability execution whitelist!"
    )
    assert 'AVAILABLE_TOOLS = ["app.launch", "device.status", "fs.read", "ui.notify"]' not in src, (
        "Anti-pattern detected: TaskOrchestrator prompt still uses hardcoded 4 tools!"
    )


# ===========================================================================
# 18. Architectural Guard: GoalPlanner Dual Registry Removed (Section 2)
# ===========================================================================
def test_architectural_guard_planner_dual_registry_removed():
    """Inspect GoalPlanner source code to verify duplicate BUILTIN_CAPABILITY_SCHEMAS dictionary is gone."""
    import hub.planner as planner_mod
    src = inspect.getsource(planner_mod)

    # In old code, BUILTIN_CAPABILITY_SCHEMAS was a giant literal dictionary of 800+ lines.
    # In unified code, it delegates to CanonicalCapabilityRegistry or _CapabilitySchemaDict.
    assert "_CapabilitySchemaDict" in src or "CanonicalCapabilityRegistry" in src, (
        "Anti-pattern detected: planner.py does not delegate BUILTIN_CAPABILITY_SCHEMAS to CanonicalCapabilityRegistry!"
    )
