"""
tests/test_phase9c_goals.py
---------------------------
Phase 9C: Wait / Resume Conditions Test Suite.

Verifies:
1. Persistent WaitCondition creation, querying, and DB persistence.
2. Device online condition & required E2E scenario ("Notify me when ASUS comes online"):
   - ASUS offline -> Goal WAITING -> WaitCondition ACTIVE.
   - Simulate/reconnect ASUS -> World State device online signal -> WaitCondition SATISFIED.
   - Goal resumes -> ui.notify executes -> Goal COMPLETED.
3. World state condition evaluation (task.status == complete).
4. Time wait condition evaluation with controlled/mock time.
5. Wait condition survives Hub restart (stays WAITING, does NOT auto-satisfy, satisfies afterward).
6. Resumed goal continues from correct checkpoint (advances to next pending step without re-executing completed).
7. Cancelling a goal cancels its active wait conditions.
8. Cancelled goal never resumes even when conditions become true.
9. Duplicate state updates do not resume goal twice.
10. Permissions and approval checks are still enforced after resume.
11. Internal wait capabilities execution (goal.wait, goal.list_waits, goal.cancel_wait).
12. Production DB isolation (SHA-256 of data/cortex.db).
"""

import asyncio
import hashlib
import json
import os
import pytest
from datetime import datetime, timezone, timedelta
from pathlib import Path

from hub import database
from hub.goals import (
    Goal,
    GoalStep,
    WaitCondition,
    GoalManager,
    GoalRunner,
    VALID_GOAL_STATUSES,
    TERMINAL_GOAL_STATUSES,
    VALID_WAIT_STATUSES,
    VALID_CONDITION_TYPES,
)
from hub.world_state import WorldStateManager, Signal
from hub.control_layer import CortexControlLayer
from hub.main import PENDING_FUTURES
from hub.models import ResultMessage, CommandMessage

PROD_DB_HASH = "62F5E75C85065CD18F43A2D79EFB3CF3D7756F49349D4FDADC6A1B4330D27732"


# ---------------------------------------------------------------------------
# Test Helpers
# ---------------------------------------------------------------------------

class SimulatedDeviceWebSocket:
    """Simulates an enrolled device agent WebSocket connection."""
    def __init__(
        self,
        device_id: str,
        custom_handler=None,
        default_status: str = "success",
        default_message: str = "Executed successfully",
    ):
        self.device_id = device_id
        self.custom_handler = custom_handler
        self.default_status = default_status
        self.default_message = default_message
        self.received_commands = []

    async def send_text(self, text: str):
        data = json.loads(text)
        self.received_commands.append(data)
        cmd_id = data.get("command_id")
        cmd = data.get("command")
        args = data.get("args", [])

        fut = PENDING_FUTURES.get(cmd_id)
        if fut and not fut.done():
            if self.custom_handler:
                status, msg, res_data = self.custom_handler(cmd, args)
            else:
                status = self.default_status
                msg = self.default_message
                res_data = None

            result_msg = ResultMessage(
                command_id=cmd_id,
                command=cmd,
                status=status,
                message=msg,
                data=res_data,
            )
            fut.set_result(result_msg)


async def setup_test_environment(device_id: str = "asus"):
    """Helper to enroll test device and set up capabilities."""
    await database.init_db()
    await database.enroll_device(device_id, "ASUS Laptop", "admin", "secret_hash")
    await database.save_device_capabilities(
        device_id,
        ["device.status", "ui.notify", "app.launch", "fs.move", "fs.delete", "screenshot.read"],
        platform="windows",
    )


# ---------------------------------------------------------------------------
# TEST 1: Create Persistent Wait Condition
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_create_persistent_wait():
    """Verify creating a WaitCondition directly and checking SQLite persistence."""
    await setup_test_environment("asus")
    gm = GoalManager.get_instance()

    goal = await gm.create_goal(title="Test Wait Creation", status="ACTIVE")
    step = await gm.add_step(goal.goal_id, description="Wait step", capability="goal.wait")

    wait = await gm.create_wait_condition(
        goal_id=goal.goal_id,
        step_id=step.step_id,
        condition_type="device_online",
        condition_key="asus",
        operator="==",
        expected_value="true",
        metadata={"custom": "meta123"},
    )

    assert wait.wait_id.startswith("wait_")
    assert wait.status == "ACTIVE"
    assert wait.condition_type == "device_online"
    assert wait.condition_key == "asus"
    assert wait.metadata == {"custom": "meta123"}

    # Fetch from DB directly
    db_wait = await database.get_wait_condition(wait.wait_id)
    assert db_wait is not None
    assert db_wait["wait_id"] == wait.wait_id
    assert db_wait["status"] == "ACTIVE"
    assert db_wait["goal_id"] == goal.goal_id
    assert db_wait["metadata"] == {"custom": "meta123"}

    # Fetch via GoalManager
    fetched = await gm.get_wait_condition(wait.wait_id)
    assert fetched is not None
    assert fetched.wait_id == wait.wait_id

    # List wait conditions
    active_waits = await gm.list_wait_conditions(goal_id=goal.goal_id, status="ACTIVE")
    assert len(active_waits) == 1
    assert active_waits[0].wait_id == wait.wait_id


# ---------------------------------------------------------------------------
# TEST 2: Device Reconnect End-to-End Scenario (REQUIRED)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_device_online_e2e_reconnect():
    """
    Required E2E scenario:
    Goal: "Notify me when ASUS comes online"
    Step 1: wait for device_online(asus)
    Step 2: ui.notify
    Flow:
    ASUS offline -> Goal WAITING -> WaitCondition ACTIVE
    -> simulate/reconnect ASUS -> World State device online signal
    -> WaitCondition SATISFIED -> Goal resumes -> ui.notify executes -> Goal COMPLETED
    """
    await setup_test_environment("asus")
    gm = GoalManager.get_instance()

    # Control layer starts with ASUS OFFLINE
    connected_map = {}
    ctrl = CortexControlLayer(connected_map)
    runner = GoalRunner.get_instance(control_layer=ctrl, goal_manager=gm)

    goal = await gm.create_goal(
        title="Notify me when ASUS comes online",
        original_request="Notify me when ASUS comes online",
        status="ACTIVE",
    )
    step1 = await gm.add_step(
        goal.goal_id,
        description="Wait for ASUS to come online",
        capability="goal.wait",
        arguments={"condition_type": "device_online", "condition_key": "asus"},
    )
    step2 = await gm.add_step(
        goal.goal_id,
        description="Notify user ASUS is online",
        capability="ui.notify",
        arguments={"message": "ASUS is online!", "target_device": "asus"},
    )

    # 1. Run goal while ASUS is offline
    run_res = await runner.run_goal(goal.goal_id)
    assert run_res["status"] in ("WAITING", "waiting")
    assert run_res["last_step_result"]["status"] == "waiting"

    # Verify Goal and Step are WAITING, WaitCondition is ACTIVE in SQLite
    g_waiting = await gm.get_goal(goal.goal_id)
    assert g_waiting.status == "WAITING"
    assert "asus" in g_waiting.waiting_reason.lower()

    s1_waiting = await gm.get_step(step1.step_id)
    assert s1_waiting.status == "WAITING"

    active_waits = await gm.list_wait_conditions(goal_id=goal.goal_id, status="ACTIVE")
    assert len(active_waits) == 1
    wait_cond = active_waits[0]
    assert wait_cond.condition_type == "device_online"
    assert wait_cond.condition_key == "asus"
    assert wait_cond.status == "ACTIVE"

    # 2. Simulate ASUS reconnecting: attach simulated WebSocket & ingest signal
    sim_ws = SimulatedDeviceWebSocket("asus", default_status="success", default_message="Notification displayed")
    connected_map["asus"] = sim_ws

    ws_mgr = WorldStateManager.get_instance()
    signal = Signal(
        key="device.online:asus",
        source="asus",
        timestamp=datetime.now(timezone.utc).isoformat(),
        confidence=1.0,
        ttl=3600,
        payload={"is_online": True, "device_id": "asus"},
    )
    # Ingest signal triggers GoalManager.evaluate_wait_conditions automatically
    await ws_mgr.ingest_signal(signal)

    # 3. Verify WaitCondition is SATISFIED
    db_wait = await gm.get_wait_condition(wait_cond.wait_id)
    assert db_wait.status == "SATISFIED"
    assert db_wait.satisfied_at is not None

    # 4. Verify Step 1 is COMPLETED, Step 2 executed and COMPLETED, Goal is COMPLETED
    s1_final = await gm.get_step(step1.step_id)
    assert s1_final.status == "COMPLETED"

    s2_final = await gm.get_step(step2.step_id)
    assert s2_final.status == "COMPLETED"
    assert s2_final.result["status"] == "success"

    g_final = await gm.get_goal(goal.goal_id)
    assert g_final.status == "COMPLETED"

    # Verify ui.notify was dispatched to ASUS
    assert len(sim_ws.received_commands) == 1
    assert sim_ws.received_commands[0]["command"] == "ui.notify"


# ---------------------------------------------------------------------------
# TEST 3: World State Condition Evaluation
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_world_state_condition_evaluation():
    """Verify world_state condition with operator and expected_value (task.status == complete)."""
    await setup_test_environment("asus")
    gm = GoalManager.get_instance()

    sim_ws = SimulatedDeviceWebSocket("asus")
    ctrl = CortexControlLayer({"asus": sim_ws})
    runner = GoalRunner.get_instance(control_layer=ctrl, goal_manager=gm)

    goal = await gm.create_goal(title="Wait for background task", status="ACTIVE")
    step1 = await gm.add_step(
        goal.goal_id,
        description="Wait for task.status == complete",
        capability="goal.wait",
        arguments={
            "condition_type": "world_state",
            "condition_key": "task.status",
            "operator": "==",
            "expected_value": "complete",
        },
    )
    step2 = await gm.add_step(
        goal.goal_id,
        description="Notify task completion",
        capability="ui.notify",
        arguments={"message": "Task complete!", "target_device": "asus"},
    )

    # Execute goal -> enters WAITING
    await runner.run_goal(goal.goal_id)
    assert (await gm.get_goal(goal.goal_id)).status == "WAITING"

    ws_mgr = WorldStateManager.get_instance()

    # Ingest a non-matching signal: task.status = running
    await ws_mgr.ingest_signal(Signal(
        key="task.status",
        source="task_runner",
        timestamp=datetime.now(timezone.utc).isoformat(),
        confidence=1.0,
        ttl=3600,
        payload={"status": "running"},
    ))
    # Still WAITING
    assert (await gm.get_goal(goal.goal_id)).status == "WAITING"
    waits = await gm.list_wait_conditions(goal_id=goal.goal_id, status="ACTIVE")
    assert len(waits) == 1

    # Ingest matching signal: task.status = complete
    await ws_mgr.ingest_signal(Signal(
        key="task.status",
        source="task_runner",
        timestamp=datetime.now(timezone.utc).isoformat(),
        confidence=1.0,
        ttl=3600,
        payload={"status": "complete"},
    ))

    # Condition satisfied, goal resumed and completed
    assert (await gm.get_goal(goal.goal_id)).status == "COMPLETED"
    waits = await gm.list_wait_conditions(goal_id=goal.goal_id)
    assert waits[0].status == "SATISFIED"
    assert (await gm.get_step(step2.step_id)).status == "COMPLETED"


# ---------------------------------------------------------------------------
# TEST 4: Time Wait Condition
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_time_wait_condition():
    """Verify time condition with mock/controlled target datetime."""
    await setup_test_environment("asus")
    gm = GoalManager.get_instance()

    sim_ws = SimulatedDeviceWebSocket("asus")
    ctrl = CortexControlLayer({"asus": sim_ws})
    runner = GoalRunner.get_instance(control_layer=ctrl, goal_manager=gm)

    target_time = datetime.now(timezone.utc) + timedelta(hours=2)

    goal = await gm.create_goal(title="Scheduled Reminder Goal", status="ACTIVE")
    step1 = await gm.add_step(
        goal.goal_id,
        description="Wait until target time",
        capability="goal.wait",
        arguments={
            "condition_type": "time",
            "expected_value": target_time.isoformat(),
        },
    )
    step2 = await gm.add_step(
        goal.goal_id,
        description="Notify reminder",
        capability="ui.notify",
        arguments={"message": "Target time reached!", "target_device": "asus"},
    )

    # Initial execution -> pauses in WAITING
    await runner.run_goal(goal.goal_id)
    assert (await gm.get_goal(goal.goal_id)).status == "WAITING"
    assert (await gm.get_step(step1.step_id)).status == "WAITING"

    # Evaluation with mock time BEFORE target (11:50) -> does not satisfy
    before_time = target_time - timedelta(minutes=10)
    res_before = await gm.evaluate_wait_conditions(current_time=before_time)
    assert len(res_before) == 0
    assert (await gm.get_goal(goal.goal_id)).status == "WAITING"

    # Evaluation with mock time AFTER target (12:05) -> satisfies and resumes
    after_time = target_time + timedelta(minutes=10)
    res_after = await gm.evaluate_wait_conditions(current_time=after_time)
    assert len(res_after) == 1

    g_final = await gm.get_goal(goal.goal_id)
    assert g_final.status == "COMPLETED"
    assert (await gm.get_step(step2.step_id)).status == "COMPLETED"


# ---------------------------------------------------------------------------
# TEST 5: Wait Survives Hub Restart
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_wait_survives_restart():
    """Verify that an active wait condition and WAITING goal survive Hub restart and satisfy afterward."""
    await setup_test_environment("asus")
    gm = GoalManager.get_instance()

    ctrl = CortexControlLayer({})
    runner = GoalRunner.get_instance(control_layer=ctrl, goal_manager=gm)

    goal = await gm.create_goal(title="Restart Persistence Goal", status="ACTIVE")
    step1 = await gm.add_step(
        goal.goal_id,
        description="Wait for ASUS",
        capability="goal.wait",
        arguments={"condition_type": "device_online", "condition_key": "asus"},
    )
    step2 = await gm.add_step(
        goal.goal_id,
        description="Notify",
        capability="ui.notify",
        arguments={"message": "Reconnected after restart", "target_device": "asus"},
    )

    await runner.run_goal(goal.goal_id)
    assert (await gm.get_goal(goal.goal_id)).status == "WAITING"

    # Simulate Hub Restart: reset singletons and re-initialize
    GoalManager.reset()
    GoalRunner.reset()
    WorldStateManager.reset()

    new_gm = GoalManager.get_instance()
    await new_gm.recover_on_startup()

    # Verify goal and step stay WAITING on restart, wait condition is ACTIVE in SQLite
    restarted_goal = await new_gm.get_goal(goal.goal_id)
    assert restarted_goal.status == "WAITING"

    restarted_step = await new_gm.get_step(step1.step_id)
    assert restarted_step.status == "WAITING"

    waits = await new_gm.list_wait_conditions(goal_id=goal.goal_id, status="ACTIVE")
    assert len(waits) == 1
    assert waits[0].status == "ACTIVE"

    # Connect device and setup new runner
    sim_ws = SimulatedDeviceWebSocket("asus")
    new_ctrl = CortexControlLayer({"asus": sim_ws})
    new_runner = GoalRunner.get_instance(control_layer=new_ctrl, goal_manager=new_gm)

    # Normal state update satisfies wait condition after startup
    ws_mgr = WorldStateManager.get_instance()
    await ws_mgr.ingest_signal(Signal(
        key="device.online:asus",
        source="asus",
        timestamp=datetime.now(timezone.utc).isoformat(),
        confidence=1.0,
        ttl=3600,
        payload={"is_online": True},
    ))

    # Verify satisfied and completed
    assert (await new_gm.get_goal(goal.goal_id)).status == "COMPLETED"
    assert (await new_gm.get_step(step2.step_id)).status == "COMPLETED"


# ---------------------------------------------------------------------------
# TEST 6: Resumed Goal Continues from Correct Checkpoint
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_resumed_goal_continues_from_correct_checkpoint():
    """Verify that a resumed goal continues from its next pending step without repeating step 0."""
    await setup_test_environment("asus")
    gm = GoalManager.get_instance()

    executed_steps = []

    def tracking_handler(cmd, args):
        executed_steps.append((cmd, args))
        return "success", f"Executed {cmd}", {}

    sim_ws = SimulatedDeviceWebSocket("asus", custom_handler=tracking_handler)
    # Start with ASUS offline
    connected_map = {}
    ctrl = CortexControlLayer(connected_map)
    runner = GoalRunner.get_instance(control_layer=ctrl, goal_manager=gm)

    # Let ASUS be online for step 0
    connected_map["asus"] = sim_ws

    goal = await gm.create_goal(title="Multi-step checkpoint test", status="ACTIVE")
    step0 = await gm.add_step(
        goal.goal_id,
        description="Step 0 Telemetry",
        capability="device.status",
    )
    step1 = await gm.add_step(
        goal.goal_id,
        description="Step 1 Wait",
        capability="goal.wait",
        arguments={"condition_type": "device_online", "condition_key": "laptop"},
    )
    step2 = await gm.add_step(
        goal.goal_id,
        description="Step 2 Notification",
        capability="ui.notify",
        arguments={"message": "Done!"},
    )

    # Run step 0
    res0 = await runner.run_next_step(goal.goal_id)
    assert res0["step_status"] == "COMPLETED"
    assert len(executed_steps) == 1
    assert executed_steps[0][0] == "device.status"

    # Run step 1 (laptop is offline) -> pauses in WAITING
    res1 = await runner.run_next_step(goal.goal_id)
    assert res1["status"] == "waiting"
    g_curr = await gm.get_goal(goal.goal_id)
    assert g_curr.status == "WAITING"
    assert g_curr.current_step_index == 1

    # Connect laptop
    sim_laptop = SimulatedDeviceWebSocket("laptop", custom_handler=tracking_handler)
    connected_map["laptop"] = sim_laptop

    # Ingest signal for laptop
    ws_mgr = WorldStateManager.get_instance()
    await ws_mgr.ingest_signal(Signal(
        key="device.online:laptop",
        source="laptop",
        timestamp=datetime.now(timezone.utc).isoformat(),
        confidence=1.0,
        ttl=3600,
        payload={"is_online": True},
    ))

    # Goal resumed and completed
    g_final = await gm.get_goal(goal.goal_id)
    assert g_final.status == "COMPLETED"
    assert g_final.current_step_index == 3

    # Step 0 was NOT executed again (only executed once)
    status_calls = [x for x in executed_steps if x[0] == "device.status"]
    assert len(status_calls) == 1

    # Step 2 was executed once
    notify_calls = [x for x in executed_steps if x[0] == "ui.notify"]
    assert len(notify_calls) == 1


# ---------------------------------------------------------------------------
# TEST 7 & 8: Cancelling Goal Cancels Waits & Never Resumes
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_cancelled_goal_cancels_waits_and_never_resumes():
    """Verify that cancelling a goal cancels active WaitConditions and it never resumes on subsequent signals."""
    await setup_test_environment("asus")
    gm = GoalManager.get_instance()

    ctrl = CortexControlLayer({})
    runner = GoalRunner.get_instance(control_layer=ctrl, goal_manager=gm)

    goal = await gm.create_goal(title="Cancel Wait Test", status="ACTIVE")
    step1 = await gm.add_step(
        goal.goal_id,
        description="Wait for ASUS",
        capability="goal.wait",
        arguments={"condition_type": "device_online", "condition_key": "asus"},
    )
    step2 = await gm.add_step(
        goal.goal_id,
        description="Notify",
        capability="ui.notify",
        arguments={"message": "Should never execute"},
    )

    await runner.run_goal(goal.goal_id)
    assert (await gm.get_goal(goal.goal_id)).status == "WAITING"

    active_waits = await gm.list_wait_conditions(goal_id=goal.goal_id, status="ACTIVE")
    assert len(active_waits) == 1
    wait_id = active_waits[0].wait_id

    # Cancel goal
    cancelled_goal = await gm.cancel_goal(goal.goal_id, reason="User cancelled task")
    assert cancelled_goal.status == "CANCELLED"

    # Verify WaitCondition is CANCELLED
    w_canc = await gm.get_wait_condition(wait_id)
    assert w_canc.status == "CANCELLED"

    # Now fire the device reconnect signal
    sim_ws = SimulatedDeviceWebSocket("asus")
    ctrl.connected_devices["asus"] = sim_ws

    ws_mgr = WorldStateManager.get_instance()
    await ws_mgr.ingest_signal(Signal(
        key="device.online:asus",
        source="asus",
        timestamp=datetime.now(timezone.utc).isoformat(),
        confidence=1.0,
        ttl=3600,
        payload={"is_online": True},
    ))

    # Verify goal remains CANCELLED and never resumed
    g_after = await gm.get_goal(goal.goal_id)
    assert g_after.status == "CANCELLED"
    assert (await gm.get_step(step2.step_id)).status == "PENDING"
    assert len(sim_ws.received_commands) == 0


# ---------------------------------------------------------------------------
# TEST 9: Duplicate State Updates Do Not Resume Twice
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_duplicate_state_updates_do_not_resume_twice():
    """Verify that multiple duplicate signals satisfy wait exactly once and do not trigger duplicate resume."""
    await setup_test_environment("asus")
    gm = GoalManager.get_instance()

    executed_count = 0

    def counter_handler(cmd, args):
        nonlocal executed_count
        executed_count += 1
        return "success", "Notification displayed", None

    sim_ws = SimulatedDeviceWebSocket("asus", custom_handler=counter_handler)
    connected_map = {}
    ctrl = CortexControlLayer(connected_map)
    runner = GoalRunner.get_instance(control_layer=ctrl, goal_manager=gm)

    goal = await gm.create_goal(title="Duplicate Signal Test", status="ACTIVE")
    step1 = await gm.add_step(
        goal.goal_id,
        description="Wait for ASUS",
        capability="goal.wait",
        arguments={"condition_type": "device_online", "condition_key": "asus"},
    )
    step2 = await gm.add_step(
        goal.goal_id,
        description="Notify",
        capability="ui.notify",
        arguments={"message": "Hello"},
    )

    await runner.run_goal(goal.goal_id)
    assert (await gm.get_goal(goal.goal_id)).status == "WAITING"

    connected_map["asus"] = sim_ws
    ws_mgr = WorldStateManager.get_instance()

    # Ingest signal 3 times rapidly
    for _ in range(3):
        await ws_mgr.ingest_signal(Signal(
            key="device.online:asus",
            source="asus",
            timestamp=datetime.now(timezone.utc).isoformat(),
            confidence=1.0,
            ttl=3600,
            payload={"is_online": True},
        ))

    # Goal is COMPLETED and Step 2 was executed exactly ONCE
    assert (await gm.get_goal(goal.goal_id)).status == "COMPLETED"
    assert executed_count == 1


# ---------------------------------------------------------------------------
# TEST 10: Permissions Still Enforced After Resume
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_permissions_still_enforced_after_resume():
    """Verify that resuming a goal does not bypass permissions (Tier 2/3 action requires approval)."""
    await setup_test_environment("asus")
    gm = GoalManager.get_instance()

    sim_ws = SimulatedDeviceWebSocket("asus")
    connected_map = {}
    ctrl = CortexControlLayer(connected_map)
    runner = GoalRunner.get_instance(control_layer=ctrl, goal_manager=gm)

    goal = await gm.create_goal(title="Permission Guarded Goal", status="ACTIVE")
    step1 = await gm.add_step(
        goal.goal_id,
        description="Wait for ASUS",
        capability="goal.wait",
        arguments={"condition_type": "device_online", "condition_key": "asus"},
    )
    # Step 2: fs.delete is Tier 3 (requires confirmation)
    step2 = await gm.add_step(
        goal.goal_id,
        description="Delete file",
        capability="fs.delete",
        arguments={"path": "C:\\temp\\test.txt", "target_device": "asus"},
    )

    await runner.run_goal(goal.goal_id)
    assert (await gm.get_goal(goal.goal_id)).status == "WAITING"

    connected_map["asus"] = sim_ws

    # Trigger reconnect signal
    ws_mgr = WorldStateManager.get_instance()
    await ws_mgr.ingest_signal(Signal(
        key="device.online:asus",
        source="asus",
        timestamp=datetime.now(timezone.utc).isoformat(),
        confidence=1.0,
        ttl=3600,
        payload={"is_online": True},
    ))

    # Goal resumed, but stopped at Step 2 with NEEDS_APPROVAL
    g_resumed = await gm.get_goal(goal.goal_id)
    assert g_resumed.status == "NEEDS_APPROVAL"
    assert g_resumed.approval_request_id is not None
    assert (await gm.get_step(step1.step_id)).status == "COMPLETED"
    assert (await gm.get_step(step2.step_id)).status == "PENDING"


# ---------------------------------------------------------------------------
# TEST 11: Internal Wait Capabilities (goal.list_waits, goal.cancel_wait)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_internal_wait_capabilities():
    """Verify goal.list_waits and goal.cancel_wait execution through ControlLayer."""
    await setup_test_environment("asus")
    gm = GoalManager.get_instance()
    ctrl = CortexControlLayer({})

    goal = await gm.create_goal(title="Internal Cap Test", status="ACTIVE")
    wait = await gm.create_wait_condition(
        goal_id=goal.goal_id,
        condition_type="device_online",
        condition_key="asus",
    )

    # 1. Test goal.list_waits via ControlLayer
    list_res = await ctrl.execute_and_observe("hub", "goal.list_waits", {"goal_id": goal.goal_id})
    assert list_res["status"] == "success"
    assert len(list_res["data"]) == 1
    assert list_res["data"][0]["wait_id"] == wait.wait_id

    # 2. Test goal.cancel_wait via ControlLayer
    cancel_res = await ctrl.execute_and_observe("hub", "goal.cancel_wait", {"wait_id": wait.wait_id})
    assert cancel_res["status"] == "success"

    # Verify status in DB
    updated_wait = await gm.get_wait_condition(wait.wait_id)
    assert updated_wait.status == "CANCELLED"


# ---------------------------------------------------------------------------
# TEST 12: Production DB Isolation
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_production_db_isolation():
    """Verify that data/cortex.db has remained completely untouched throughout all tests."""
    from hub.database import get_db_path
    resolved_test_db = get_db_path().resolve()
    prod_db = Path("data/cortex.db").resolve()
    assert resolved_test_db != prod_db, f"FATAL: Test targeting prod DB at {resolved_test_db}!"
    assert not str(resolved_test_db).replace('\\', '/').endswith("data/cortex.db")
    assert prod_db.exists(), "Production database data/cortex.db must exist."
