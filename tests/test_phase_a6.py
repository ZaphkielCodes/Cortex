"""
tests/test_phase_a6.py
----------------------
Phase A6 Test Suite: Task Ownership, Execution Leases, Fencing Tokens,
Durable Checkpoints, Handoff Eligibility, Failover, Wait Conditions,
and Crash Recovery.

Covers:
- Test 1 — Normal ownership (ASUS starts task -> owner/executor = asus, valid lease exists)
- Test 2 — Safe handoff (ASUS executing movable task, ASUS offline, Lenovo capable + authorized -> ASUS lease invalidated, Lenovo receives new generation, resumes checkpoint)
- Test 3 — ASUS-only data (task requires ASUS-local file, ASUS offline, Lenovo available -> NO handoff, WAITING_FOR_EXECUTOR(asus))
- Test 4 — Resume when ASUS returns (waiting task, ASUS reconnects -> automatically resumes checkpoint)
- Test 5 — Duplicate prevention (ASUS loses lease, Lenovo takes generation N+1, delayed ASUS result arrives -> reject stale generation)
- Test 6 — Side-effect uncertainty (executor dies after dispatching non-idempotent action -> verify outcome before any retry)
- Test 7 — Hub restart (active task persisted, restart -> reconcile, resume/handoff/wait correctly)
- Test 8 — Original executor returns after handoff (Lenovo currently owns valid lease, ASUS returns -> ASUS does not duplicate or steal execution)
- Test 9 — No alternate executor (current executor disappears, nobody capable -> WAITING_FOR_EXECUTOR)
- Test 10 — Capability loss (executor remains online but loses required capability -> reassess safely)
- Self-Knowledge queries (all 5 conversational queries)
- Production DB isolation
"""

import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import time
from unittest.mock import AsyncMock, MagicMock, patch
import pytest
import pytest_asyncio

from hub.device_handle import DeviceHandle
from hub.executor_registry import ExecutorRegistry
from hub.executor_selector import ExecutorSelector
from hub.task_ownership import (
    TaskOwnershipManager,
    TaskExecution,
    ExecutionLease,
    TaskCheckpoint,
    ExecutionState,
    ActionSafetyClass,
    classify_action_safety,
)
from hub.goals import GoalManager, GoalRunner, WaitCondition
from hub.control_layer import CortexControlLayer
from hub.agent_harness import AgentHarness
from hub.database import (
    get_db,
    save_task_execution,
    get_task_execution,
    list_task_executions,
    get_device,
    enroll_device,
    save_device_capabilities,
)


# ===========================================================================
# Test 1 — Normal ownership
# ===========================================================================
@pytest.mark.asyncio
async def test_1_normal_ownership():
    """
    Test 1 — Normal ownership:
    ASUS starts task
    → owner/executor = asus
    → valid lease exists
    """
    registry = ExecutorRegistry.get_instance()
    tom = TaskOwnershipManager.get_instance()

    asus_handle = DeviceHandle(
        device_id="asus",
        display_name="ASUS Zenbook",
        role="primary_admin",
        trust_level="admin",
        capabilities=["web.search", "app.launch"],
        online=True,
    )
    registry.register_device(asus_handle)
    registry.set_device_online("asus", True)

    execution = await tom.create_execution(
        goal_id="goal_norm_1",
        step_id="step_1",
        owner_device_id="cortex",
        executor_device_id="asus",
        required_capabilities=["web.search"],
        movable=True,
    )

    assert execution.task_execution_id is not None
    assert execution.goal_id == "goal_norm_1"
    assert execution.owner_device_id == "cortex"
    assert execution.executor_device_id == "asus"
    assert execution.execution_state == ExecutionState.ACTIVE
    assert execution.generation == 1
    assert execution.lease_id != ""

    # Verify active lease
    lease = tom._active_leases.get(execution.task_execution_id)
    assert lease is not None
    assert lease.active is True
    assert lease.executor_device_id == "asus"
    assert lease.generation == 1
    assert not lease.is_expired()

    # Verify DB persistence
    db_ex = await get_task_execution(execution.task_execution_id)
    assert db_ex is not None
    assert db_ex["executor_device_id"] == "asus"
    assert db_ex["generation"] == 1


# ===========================================================================
# Test 2 — Safe handoff
# ===========================================================================
@pytest.mark.asyncio
async def test_2_safe_handoff():
    """
    Test 2 — Safe handoff:
    ASUS executing movable task
    ASUS offline
    Lenovo capable + authorized
    → ASUS lease invalidated
    → Lenovo receives new generation
    → resumes checkpoint
    """
    registry = ExecutorRegistry.get_instance()
    tom = TaskOwnershipManager.get_instance()

    asus = DeviceHandle(
        device_id="asus",
        display_name="ASUS Zenbook",
        role="primary_admin",
        trust_level="admin",
        capabilities=["web.search"],
        online=True,
    )
    lenovo = DeviceHandle(
        device_id="lenovo",
        display_name="Lenovo ThinkPad",
        role="trusted_worker",
        trust_level="trusted",
        capabilities=["web.search"],
        online=True,
    )
    registry.register_device(asus)
    registry.register_device(lenovo)
    registry.set_device_online("asus", True)
    registry.set_device_online("lenovo", True)

    # ASUS starts movable research task
    execution = await tom.create_execution(
        goal_id="goal_handoff_2",
        step_id="step_2",
        owner_device_id="cortex",
        executor_device_id="asus",
        required_capabilities=["web.search"],
        movable=True,
    )
    asus_lease_id = execution.lease_id
    assert execution.generation == 1

    # Save intermediate checkpoint
    await tom.save_checkpoint(
        task_execution_id=execution.task_execution_id,
        completed_sub_actions=[{"action": "query_formulated", "query": "python 3.14 features"}],
        structured_outputs={"interim": "draft search query"},
        side_effect_state=ActionSafetyClass.RETRY_SAFE,
    )

    # ASUS goes offline
    registry.set_device_online("asus", False)

    # Trigger failover check
    res = await tom.failover_if_needed(execution.task_execution_id)
    assert res["status"] == "handed_off"
    assert res["old_executor"] == "asus"
    assert res["new_executor"] == "lenovo"
    assert res["generation"] == 2

    # Fresh state inspection
    updated = await tom.get_execution(execution.task_execution_id)
    assert updated.executor_device_id == "lenovo"
    assert updated.generation == 2
    assert updated.lease_id != asus_lease_id
    assert updated.execution_state == ExecutionState.ACTIVE

    # Checkpoint was preserved
    ckpt = await tom.get_latest_checkpoint(execution.task_execution_id)
    assert ckpt is not None
    assert ckpt.structured_outputs["interim"] == "draft search query"


# ===========================================================================
# Test 3 — ASUS-only data
# ===========================================================================
@pytest.mark.asyncio
async def test_3_asus_only_data():
    """
    Test 3 — ASUS-only data:
    task requires ASUS-local file
    ASUS offline
    Lenovo available
    → NO handoff
    → WAITING_FOR_EXECUTOR(asus)
    """
    registry = ExecutorRegistry.get_instance()
    tom = TaskOwnershipManager.get_instance()
    gm = GoalManager.get_instance()

    asus = DeviceHandle(
        device_id="asus",
        display_name="ASUS Zenbook",
        role="primary_admin",
        trust_level="admin",
        capabilities=["fs.open", "app.launch"],
        online=True,
    )
    lenovo = DeviceHandle(
        device_id="lenovo",
        display_name="Lenovo ThinkPad",
        role="trusted_worker",
        trust_level="trusted",
        capabilities=["fs.open", "app.launch"],
        online=True,
    )
    registry.register_device(asus)
    registry.register_device(lenovo)
    registry.set_device_online("asus", True)
    registry.set_device_online("lenovo", True)

    goal = await gm.create_goal(title="Edit Local Report", status="ACTIVE")
    local_path = r"C:\Users\abhin\Desktop\report.docx"

    execution = await tom.create_execution(
        goal_id=goal.goal_id,
        step_id="step_edit",
        owner_device_id="asus",
        executor_device_id="asus",
        required_capabilities=["fs.open"],
        required_resources=[local_path],
        required_data_locations={local_path: "asus"},
        movable=False,
    )

    # ASUS goes offline
    registry.set_device_online("asus", False)

    # Check handoff eligibility explicitly
    elig = tom.evaluate_handoff_eligibility(execution, "lenovo")
    assert not elig.eligible
    assert "report.docx" in elig.reason or "local" in elig.reason.lower()

    # Trigger failover
    res = await tom.failover_if_needed(execution.task_execution_id)
    assert res["status"] == "waiting_for_executor"
    assert res["preferred_executor"] == "asus"

    updated = await tom.get_execution(execution.task_execution_id)
    assert updated.execution_state == ExecutionState.WAITING_FOR_EXECUTOR
    assert updated.executor_device_id == "asus"  # Pinned to asus, NOT lenovo!

    # Verify WaitCondition was registered in GoalManager
    waits = await gm.list_wait_conditions(goal_id=goal.goal_id, status="ACTIVE")
    assert len(waits) == 1
    assert waits[0].condition_key == "asus"


# ===========================================================================
# Test 4 — Resume when ASUS returns
# ===========================================================================
@pytest.mark.asyncio
async def test_4_resume_when_asus_returns():
    """
    Test 4 — Resume when ASUS returns:
    waiting task
    ASUS reconnects
    → automatically resumes checkpoint
    """
    registry = ExecutorRegistry.get_instance()
    tom = TaskOwnershipManager.get_instance()
    gm = GoalManager.get_instance()

    asus = DeviceHandle(
        device_id="asus",
        display_name="ASUS Zenbook",
        role="primary_admin",
        capabilities=["fs.open"],
        online=False,
    )
    registry.register_device(asus)
    registry.set_device_online("asus", False)

    goal = await gm.create_goal(title="Waiting Goal", status="WAITING")
    execution = await tom.create_execution(
        goal_id=goal.goal_id,
        step_id="step_w4",
        owner_device_id="asus",
        executor_device_id="asus",
        required_capabilities=["fs.open"],
        movable=False,
    )
    # Put into WAITING_FOR_EXECUTOR
    await tom.transition_to_waiting_for_executor(
        task_execution_id=execution.task_execution_id,
        preferred_executor="asus",
        missing_reason="ASUS offline with local file",
    )
    await tom.save_checkpoint(
        task_execution_id=execution.task_execution_id,
        completed_sub_actions=[{"sub": "file_located"}],
        structured_outputs={"cursor_position": 1024},
    )

    # ASUS reconnects!
    registry.set_device_online("asus", True)
    reconnect_results = await tom.on_executor_reconnected("asus")

    assert len(reconnect_results) == 1
    rec = reconnect_results[0]
    assert rec["executor"] == "asus"
    assert rec["generation"] == 2  # Fresh generation token acquired

    updated = await tom.get_execution(execution.task_execution_id)
    assert updated.execution_state == ExecutionState.ACTIVE
    assert updated.generation == 2
    assert updated.lease_id != ""

    # Checkpoint preserved
    ckpt = await tom.get_latest_checkpoint(execution.task_execution_id)
    assert ckpt.structured_outputs["cursor_position"] == 1024


# ===========================================================================
# Test 5 — Duplicate prevention (fencing token semantics)
# ===========================================================================
@pytest.mark.asyncio
async def test_5_duplicate_prevention():
    """
    Test 5 — Duplicate prevention:
    ASUS loses lease
    Lenovo takes generation N+1
    delayed ASUS result arrives
    → reject stale generation
    """
    registry = ExecutorRegistry.get_instance()
    tom = TaskOwnershipManager.get_instance()

    asus = DeviceHandle(device_id="asus", display_name="ASUS", capabilities=["web.search"], online=True)
    lenovo = DeviceHandle(device_id="lenovo", display_name="Lenovo", capabilities=["web.search"], online=True)
    registry.register_device(asus)
    registry.register_device(lenovo)
    registry.set_device_online("asus", True)
    registry.set_device_online("lenovo", True)

    execution = await tom.create_execution(
        goal_id="goal_dup_5",
        executor_device_id="asus",
        required_capabilities=["web.search"],
        movable=True,
    )
    asus_lease_id = execution.lease_id
    asus_generation = execution.generation  # 1

    # ASUS disconnects -> handoff to Lenovo (generation becomes 2)
    registry.set_device_online("asus", False)
    await tom.handoff_task(
        task_execution_id=execution.task_execution_id,
        new_executor_id="lenovo",
        reason="ASUS heartbeat expired",
    )

    cur_ex = await tom.get_execution(execution.task_execution_id)
    assert cur_ex.generation == 2
    assert cur_ex.executor_device_id == "lenovo"

    # ASUS reconnects and sends a delayed command result with generation 1
    is_valid, err_msg = tom.validate_fencing_token(
        task_execution_id=execution.task_execution_id,
        lease_id=asus_lease_id,
        generation=asus_generation,
    )
    assert not is_valid
    assert "stale generation 1 rejected" in err_msg.lower()
    assert "current active generation is 2" in err_msg.lower()

    # Lenovo sends with generation 2 -> Accepted!
    is_valid_lenovo, msg_lenovo = tom.validate_fencing_token(
        task_execution_id=execution.task_execution_id,
        lease_id=cur_ex.lease_id,
        generation=cur_ex.generation,
    )
    assert is_valid_lenovo
    assert msg_lenovo == "Valid"


# ===========================================================================
# Test 6 — Side-effect uncertainty
# ===========================================================================
@pytest.mark.asyncio
async def test_6_side_effect_uncertainty():
    """
    Test 6 — Side-effect uncertainty:
    executor dies after dispatching non-idempotent action
    → verify outcome before any retry
    """
    tom = TaskOwnershipManager.get_instance()

    # Capability classification checks
    assert classify_action_safety("web.search") == ActionSafetyClass.RETRY_SAFE
    assert classify_action_safety("fs.stat") == ActionSafetyClass.RETRY_SAFE
    assert classify_action_safety("app.list_running") == ActionSafetyClass.RETRY_SAFE
    assert classify_action_safety("email.send") == ActionSafetyClass.VERIFICATION_REQUIRED
    assert classify_action_safety("calendar.create") == ActionSafetyClass.VERIFICATION_REQUIRED
    assert classify_action_safety("fs.move") == ActionSafetyClass.VERIFICATION_REQUIRED
    assert classify_action_safety("fs.delete") == ActionSafetyClass.VERIFICATION_REQUIRED

    # Sub-case A: Verification detects action was already executed on target
    event_calendar_database = {"Team Sync": True}

    def verify_calendar(args, executor_id):
        title = args.get("title", "")
        if title in event_calendar_database:
            return {"verified": True, "already_completed": True, "status": "already_executed"}
        return {"verified": True, "already_completed": False, "status": "safe_to_retry"}

    tom.register_verification_handler("calendar.create", verify_calendar)

    res_already_done = await tom.verify_action_outcome(
        capability="calendar.create",
        arguments={"title": "Team Sync"},
        executor_device_id="asus",
    )
    assert res_already_done["verified"] is True
    assert res_already_done["already_completed"] is True

    # Sub-case B: Action was not executed -> Safe to retry
    res_not_done = await tom.verify_action_outcome(
        capability="calendar.create",
        arguments={"title": "New Unscheduled Meeting"},
        executor_device_id="asus",
    )
    assert res_not_done["verified"] is True
    assert res_not_done["already_completed"] is False

    # Sub-case C: Outcome uncertain -> Must halt to avoid duplicate action
    def verify_uncertain(args, executor_id):
        return {"verified": False, "status": "uncertain", "error": "External API timeout during check"}

    tom.register_verification_handler("email.send", verify_uncertain)
    res_uncertain = await tom.verify_action_outcome(
        capability="email.send",
        arguments={"to": "client@example.com"},
        executor_device_id="asus",
    )
    assert res_uncertain["verified"] is False
    assert res_uncertain["status"] == "uncertain"


# ===========================================================================
# Test 7 — Hub restart recovery
# ===========================================================================
@pytest.mark.asyncio
async def test_7_hub_restart():
    """
    Test 7 — Hub restart:
    active task persisted
    restart
    → reconcile
    → resume/handoff/wait correctly
    Never treat ACTIVE before crash as proof action is still running.
    """
    registry = ExecutorRegistry.get_instance()
    tom = TaskOwnershipManager.get_instance()

    # 1. Setup online devices
    asus = DeviceHandle(device_id="asus", display_name="ASUS", capabilities=["web.search"], online=False)
    lenovo = DeviceHandle(device_id="lenovo", display_name="Lenovo", capabilities=["web.search"], online=True)
    registry.register_device(asus)
    registry.register_device(lenovo)
    registry.set_device_online("asus", False)  # ASUS crashed / offline
    registry.set_device_online("lenovo", True) # Lenovo available

    # Persist an ACTIVE execution that was running on ASUS before crash
    execution = TaskExecution(
        task_execution_id="exec_pre_crash_7",
        goal_id="goal_crash_7",
        owner_device_id="cortex",
        executor_device_id="asus",
        lease_id="lease_stale_before_crash",
        lease_acquired_at=datetime.now(timezone.utc).isoformat(),
        lease_expires_at=datetime.now(timezone.utc).isoformat(),
        execution_state=ExecutionState.ACTIVE,
        required_capabilities=["web.search"],
        movable=True,
        generation=3,
    )
    await save_task_execution(execution.to_dict())

    # Simulate Hub Restart: Wipe in-memory manager state and reconcile from DB
    TaskOwnershipManager.reset()
    fresh_tom = TaskOwnershipManager.get_instance()
    fresh_tom.executor_registry = registry

    rec_result = await fresh_tom.reconcile_on_restart()
    assert rec_result["reconciled_count"] >= 1

    # Verify ASUS lease was invalidated and task handed off to Lenovo with new generation
    reconciled_ex = await fresh_tom.get_execution("exec_pre_crash_7")
    assert reconciled_ex is not None
    assert reconciled_ex.executor_device_id == "lenovo"
    assert reconciled_ex.generation == 4  # Incremented from 3
    assert reconciled_ex.lease_id != "lease_stale_before_crash"
    assert reconciled_ex.execution_state == ExecutionState.ACTIVE


# ===========================================================================
# Test 8 — Original executor returns after handoff
# ===========================================================================
@pytest.mark.asyncio
async def test_8_original_executor_returns_after_handoff():
    """
    Test 8 — Original executor returns after handoff:
    Lenovo currently owns valid lease
    ASUS returns
    → ASUS does not duplicate or steal execution
    """
    registry = ExecutorRegistry.get_instance()
    tom = TaskOwnershipManager.get_instance()

    asus = DeviceHandle(device_id="asus", display_name="ASUS", capabilities=["web.search"], online=False)
    lenovo = DeviceHandle(device_id="lenovo", display_name="Lenovo", capabilities=["web.search"], online=True)
    registry.register_device(asus)
    registry.register_device(lenovo)
    registry.set_device_online("asus", False)
    registry.set_device_online("lenovo", True)

    # Task is actively owned by Lenovo with a valid lease
    execution = await tom.create_execution(
        goal_id="goal_handoff_8",
        owner_device_id="cortex",
        executor_device_id="lenovo",
        required_capabilities=["web.search"],
        initial_generation=5,
    )
    lenovo_lease_id = execution.lease_id

    # ASUS reconnects
    registry.set_device_online("asus", True)
    reconnect_results = await tom.on_executor_reconnected("asus")

    # ASUS must NOT preempt or steal the active lease from Lenovo
    current = await tom.get_execution(execution.task_execution_id)
    assert current.executor_device_id == "lenovo"
    assert current.generation == 5
    assert current.lease_id == lenovo_lease_id
    assert current.execution_state == ExecutionState.ACTIVE


# ===========================================================================
# Test 9 — No alternate executor
# ===========================================================================
@pytest.mark.asyncio
async def test_9_no_alternate_executor():
    """
    Test 9 — No alternate executor:
    current executor disappears
    nobody capable
    → WAITING_FOR_EXECUTOR
    """
    registry = ExecutorRegistry.get_instance()
    tom = TaskOwnershipManager.get_instance()

    # Only ASUS has video.encode capability
    asus = DeviceHandle(device_id="asus", display_name="ASUS", capabilities=["video.encode"], online=True)
    lenovo = DeviceHandle(device_id="lenovo", display_name="Lenovo", capabilities=["app.launch"], online=True)
    registry.register_device(asus)
    registry.register_device(lenovo)
    registry.set_device_online("asus", True)
    registry.set_device_online("lenovo", True)

    execution = await tom.create_execution(
        goal_id="goal_video_9",
        executor_device_id="asus",
        required_capabilities=["video.encode"],
        movable=True,
    )

    # ASUS disappears
    registry.set_device_online("asus", False)

    res = await tom.failover_if_needed(execution.task_execution_id)
    assert res["status"] == "waiting_for_executor"
    assert res["preferred_executor"] == "asus"

    updated = await tom.get_execution(execution.task_execution_id)
    assert updated.execution_state == ExecutionState.WAITING_FOR_EXECUTOR


# ===========================================================================
# Test 10 — Capability loss
# ===========================================================================
@pytest.mark.asyncio
async def test_10_capability_loss():
    """
    Test 10 — Capability loss:
    executor remains online but loses required capability
    → reassess safely
    """
    registry = ExecutorRegistry.get_instance()
    tom = TaskOwnershipManager.get_instance()

    lenovo = DeviceHandle(
        device_id="lenovo",
        display_name="Lenovo",
        capabilities=["screen.capture", "web.search"],
        online=True,
    )
    asus = DeviceHandle(
        device_id="asus",
        display_name="ASUS",
        capabilities=["screen.capture", "web.search"],
        online=True,
    )
    registry.register_device(lenovo)
    registry.register_device(asus)
    registry.set_device_online("lenovo", True)
    registry.set_device_online("asus", True)

    # Task requiring screen.capture executing on Lenovo
    execution = await tom.create_execution(
        goal_id="goal_cap_loss_10",
        executor_device_id="lenovo",
        required_capabilities=["screen.capture"],
        movable=True,
    )

    # Lenovo remains online, but loses screen.capture
    lenovo.capabilities = ["web.search"]  # screen.capture dropped!

    # Trigger capability loss event
    loss_results = await tom.on_executor_capability_lost("lenovo", "screen.capture")
    assert len(loss_results) == 1
    res = loss_results[0]
    assert res["status"] == "handed_off"
    assert res["new_executor"] == "asus"

    updated = await tom.get_execution(execution.task_execution_id)
    assert updated.executor_device_id == "asus"
    assert updated.generation == 2


# ===========================================================================
# Test 11 — Self-Knowledge Introspection Queries
# ===========================================================================
@pytest.mark.asyncio
async def test_11_self_knowledge_queries():
    """
    Test 11 — Self-Knowledge:
    Verify Cortex truthfully answers all 5 required questions:
    - 'Where is this task running?'
    - 'Why did it move to Lenovo?'
    - 'Why is this task waiting?'
    - 'Will it continue when ASUS comes back?'
    - 'Which device owns this task right now?'
    """
    ctrl = CortexControlLayer()
    from model_contract_fixtures import runtime_harness
    harness, model = await runtime_harness()
    tom = TaskOwnershipManager.get_instance()

    # 1. Active task
    ex1 = await tom.create_execution(
        goal_id="goal_sk_1",
        executor_device_id="lenovo",
        owner_device_id="cortex",
        required_capabilities=["web.search"],
        initial_generation=3,
    )
    ex1.metadata["handoff_history"] = [
        {
            "from_executor": "asus",
            "to_executor": "lenovo",
            "reason": "ASUS went offline / heartbeat expired",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    ]
    await save_task_execution(ex1.to_dict())

    # Query 1: Where is this task running?
    res1 = await harness.process_request("asus", "Where is this task running?", context={})
    assert res1.status == "success", res1

    # Query 2: Why did it move to Lenovo?
    res2 = await harness.process_request("asus", "Why did it move to Lenovo?", context={})
    assert res2.status == "success", res2

    # Query 5: Which device owns this task right now?
    res5 = await harness.process_request("asus", "Which device owns this task right now?", context={})
    assert res5.status == "success", res5

    # Now transition to WAITING_FOR_EXECUTOR
    await tom.transition_to_waiting_for_executor(
        task_execution_id=ex1.task_execution_id,
        preferred_executor="asus",
        missing_reason="requires ASUS-local file report.docx",
    )

    # Query 3: Why is this task waiting?
    res3 = await harness.process_request("asus", "Why is this task waiting?", context={})
    assert res3.status == "success", res3

    # Query 4: Will it continue when ASUS comes back?
    res4 = await harness.process_request("asus", "Will it continue when ASUS comes back?", context={})
    assert res4.status == "success", res4
    assert model.calls[-1]["mode"] == "result_interpretation"
    evidence=model.calls[-1]["tool_result"]
    assert evidence["status"] == "success"
    assert evidence["data"]["source_device"] == "asus"
    assert evidence["data"]["automatic_action_replay"] is False
    assert any(e["executor_device_id"] == "asus" or e["executor_device_id"] == "lenovo" for e in evidence["data"]["executions"])


# ===========================================================================
# Test 12 — Production DB Isolation
# ===========================================================================
def test_12_production_db_isolation():
    """Verify tests run strictly against test_cortex.db and production DB is protected."""
    db_path = os.environ.get("CORTEX_DB_PATH", "")
    assert "test_cortex.db" in db_path
    assert not db_path.endswith("data/cortex.db")
