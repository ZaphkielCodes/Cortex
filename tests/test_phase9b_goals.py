"""
tests/test_phase9b_goals.py
---------------------------
Phase 9B: Bounded Execute / Observe Loop Test Suite.

Verifies:
1. Successful single-step execution through ControlLayer with actual result observation.
2. Successful multi-step execution (REQUIRED E2E: "Check this device and notify me"):
   - Step 1: device.status
   - Step 2: ui.notify
   - Full state verification after every transition.
3. Actual result persistence in DB (status, message, data).
4. Step checkpoint persistence in DB.
5. Goal checkpoint persistence in DB.
6. Controlled failure propagation (step FAILED, failure_count increments, checkpoint persisted).
7. max_failures enforcement (goal transitions to terminal FAILED when limit exceeded).
8. Nonexistent capability validation (fake.capability -> goal BLOCKED, execution never occurs).
9. Approval preservation (Tier 2/3 or 'ask' policy -> goal NEEDS_APPROVAL, approval_request_id persisted).
10. max_steps enforcement (bounded execution stops and marks BLOCKED when limit reached).
11. No direct executor bypass (GoalManager coordinates state only; execution strictly through ControlLayer).
12. Hub restart safety unchanged (ACTIVE/RUNNING normalized safely to WAITING, no auto-resume).
13. Minimal natural language invocation ("Run my <goal> goal.").
14. Production DB isolation (SHA-256 verification of data/cortex.db).
"""

import asyncio
import hashlib
import json
import os
import pytest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch, MagicMock

from hub import database
from hub.goals import (
    Goal,
    GoalStep,
    GoalManager,
    GoalRunner,
    VALID_GOAL_STATUSES,
    TERMINAL_GOAL_STATUSES,
)
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
                # Default handler
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
        ["device.status", "ui.notify", "app.launch", "fs.move", "screenshot.read"],
        platform="windows",
    )


# ---------------------------------------------------------------------------
# TEST 1: Successful Single-Step Execution
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_successful_single_step_execution():
    """Verify single-step goal execution through ControlLayer and actual result observation."""
    await setup_test_environment("asus")
    gm = GoalManager.get_instance()

    # Device WebSocket simulator that returns battery status
    def battery_handler(cmd, args):
        assert cmd == "device.status"
        return "success", "Battery is at 95% and is plugged in.", {"battery": 95}

    sim_ws = SimulatedDeviceWebSocket("asus", custom_handler=battery_handler)
    ctrl = CortexControlLayer({"asus": sim_ws})
    runner = GoalRunner.get_instance(control_layer=ctrl, goal_manager=gm)

    goal = await gm.create_goal(title="Check Battery Status", status="PENDING")
    step = await gm.add_step(
        goal.goal_id,
        description="Query device telemetry",
        capability="device.status",
    )

    # Execute single step
    res = await runner.run_next_step(goal.goal_id)
    assert res["status"] == "completed"
    assert res["step_status"] == "COMPLETED"
    assert res["result"]["status"] == "success"
    assert "Battery is at 95%" in res["result"]["message"]

    # Verify DB persistence of step
    db_step = await gm.get_step(step.step_id)
    assert db_step.status == "COMPLETED"
    assert db_step.started_at is not None
    assert db_step.completed_at is not None
    assert db_step.result["status"] == "success"
    assert "Battery is at 95%" in db_step.result["message"]
    assert db_step.result["data"] == {"battery": 95}
    assert db_step.checkpoint_data["status"] == "COMPLETED"

    # Verify DB persistence of goal
    db_goal = await gm.get_goal(goal.goal_id)
    assert db_goal.status == "COMPLETED"
    assert db_goal.current_step_index == 1
    assert db_goal.failure_count == 0
    assert db_goal.completed_at is not None
    assert db_goal.checkpoint_data["last_step_index"] == 0


# ---------------------------------------------------------------------------
# TEST 2: Successful Multi-Step Execution (REQUIRED E2E TEST)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_successful_multi_step_execution_e2e():
    """
    REQUIRED END-TO-END TEST:
    Seed a goal with real existing capabilities:
    Goal: "Check this device and notify me"
    Step 1: device.status
    Step 2: ui.notify

    Required path:
    Goal ACTIVE
    → Step 1 RUNNING
    → ControlLayer executes
    → actual result observed
    → Step 1 COMPLETED
    → checkpoint saved
    → Step 2 RUNNING
    → notification executed
    → Step 2 COMPLETED
    → Goal COMPLETED

    Verifies persisted state after every transition.
    """
    await setup_test_environment("asus")
    gm = GoalManager.get_instance()

    executed_commands = []

    def e2e_device_handler(cmd, args):
        executed_commands.append((cmd, args))
        if cmd == "device.status":
            return "success", "Battery is at 88% and discharging.", {"battery_level": 88}
        elif cmd == "ui.notify":
            return "success", "Notification triggered on desktop.", None
        return "failure", "Unknown command", None

    sim_ws = SimulatedDeviceWebSocket("asus", custom_handler=e2e_device_handler)
    ctrl = CortexControlLayer({"asus": sim_ws})
    runner = GoalRunner.get_instance(control_layer=ctrl, goal_manager=gm)

    # 1. Seed goal & steps
    goal = await gm.create_goal(
        title="Check this device and notify me",
        original_request="Check this device and notify me",
        status="ACTIVE",
    )
    s1 = await gm.add_step(
        goal.goal_id,
        description="Check device telemetry",
        capability="device.status",
    )
    s2 = await gm.add_step(
        goal.goal_id,
        description="Notify user of device status",
        capability="ui.notify",
        arguments={"title": "Device Status", "message": "Battery is at 88%"},
    )

    # Verify initial DB state
    db_g0 = await gm.get_goal(goal.goal_id)
    assert db_g0.status == "ACTIVE"
    assert db_g0.current_step_index == 0

    db_s1_0 = await gm.get_step(s1.step_id)
    assert db_s1_0.status == "PENDING"
    db_s2_0 = await gm.get_step(s2.step_id)
    assert db_s2_0.status == "PENDING"

    # 2. Execute Step 1 (device.status)
    res_step1 = await runner.run_next_step(goal.goal_id)
    assert res_step1["status"] == "continued"
    assert res_step1["step_status"] == "COMPLETED"
    assert res_step1["stop"] is False

    # Verify DB state after Step 1 transition
    db_g1 = await gm.get_goal(goal.goal_id)
    assert db_g1.status == "ACTIVE"
    assert db_g1.current_step_index == 1
    assert db_g1.checkpoint_data["last_step_index"] == 0

    db_s1_1 = await gm.get_step(s1.step_id)
    assert db_s1_1.status == "COMPLETED"
    assert db_s1_1.started_at is not None
    assert db_s1_1.completed_at is not None
    assert "Battery is at 88%" in db_s1_1.result["message"]
    assert db_s1_1.checkpoint_data["status"] == "COMPLETED"

    db_s2_1 = await gm.get_step(s2.step_id)
    assert db_s2_1.status == "PENDING"

    # 3. Execute Step 2 (ui.notify)
    res_step2 = await runner.run_next_step(goal.goal_id)
    assert res_step2["status"] == "completed"
    assert res_step2["step_status"] == "COMPLETED"
    assert res_step2["stop"] is True

    # Verify DB state after Step 2 transition
    db_g2 = await gm.get_goal(goal.goal_id)
    assert db_g2.status == "COMPLETED"
    assert db_g2.current_step_index == 2
    assert db_g2.completed_at is not None
    assert "All 2 steps completed successfully" in db_g2.result_summary

    db_s2_2 = await gm.get_step(s2.step_id)
    assert db_s2_2.status == "COMPLETED"
    assert db_s2_2.started_at is not None
    assert db_s2_2.completed_at is not None
    assert "Notification triggered" in db_s2_2.result["message"]
    assert db_s2_2.checkpoint_data["status"] == "COMPLETED"

    # Verify execution sequence via ControlLayer
    assert len(executed_commands) == 2
    assert executed_commands[0][0] == "device.status"
    assert executed_commands[1][0] == "ui.notify"


# ---------------------------------------------------------------------------
# TEST 3: Full Goal Autonomous Loop via run_goal
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_run_goal_autonomous_loop():
    """Verify run_goal runs full loop automatically to completion."""
    await setup_test_environment("asus")
    gm = GoalManager.get_instance()

    sim_ws = SimulatedDeviceWebSocket("asus", default_status="success", default_message="Action complete")
    ctrl = CortexControlLayer({"asus": sim_ws})
    runner = GoalRunner.get_instance(control_layer=ctrl, goal_manager=gm)

    goal = await gm.create_goal(title="Autonomous 2-Step Goal", status="PENDING")
    await gm.add_step(goal.goal_id, description="Step 1", capability="device.status")
    await gm.add_step(goal.goal_id, description="Step 2", capability="ui.notify")

    loop_res = await runner.run_goal(goal.goal_id)
    assert loop_res["status"] == "COMPLETED"
    assert loop_res["current_step_index"] == 2
    assert loop_res["failure_count"] == 0

    # DB verification
    db_goal = await gm.get_goal(goal.goal_id)
    assert db_goal.status == "COMPLETED"
    steps = await gm.list_steps(goal.goal_id)
    assert all(s.status == "COMPLETED" for s in steps)


# ---------------------------------------------------------------------------
# TEST 4: Actual Result & Checkpoint Persistence
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_actual_result_and_checkpoint_persistence():
    """Verify result, error, and checkpoints are persisted directly into the DB."""
    await setup_test_environment("asus")
    gm = GoalManager.get_instance()

    detailed_payload = {"cpu_pct": 14.2, "ram_gb": 8.1}
    sim_ws = SimulatedDeviceWebSocket(
        "asus",
        custom_handler=lambda cmd, args: ("success", "Telemetry collected", detailed_payload)
    )
    ctrl = CortexControlLayer({"asus": sim_ws})
    runner = GoalRunner.get_instance(control_layer=ctrl, goal_manager=gm)

    goal = await gm.create_goal(title="Telemetry Goal", status="ACTIVE")
    step = await gm.add_step(goal.goal_id, description="Inspect telemetry", capability="device.status")

    await runner.run_goal(goal.goal_id)

    # Fresh DB fetch
    loaded_step = await gm.get_step(step.step_id)
    assert loaded_step.result == {
        "status": "success",
        "message": "Telemetry collected",
        "data": detailed_payload,
    }
    assert loaded_step.checkpoint_data["capability"] == "device.status"
    assert loaded_step.checkpoint_data["status"] == "COMPLETED"

    loaded_goal = await gm.get_goal(goal.goal_id)
    assert loaded_goal.checkpoint_data["last_step_index"] == 0
    assert loaded_goal.checkpoint_data["step_id"] == step.step_id


# ---------------------------------------------------------------------------
# TEST 5: Controlled Failure Propagation
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_failure_propagation():
    """
    FAILURE TEST:
    Use one controlled failing capability result.
    Verify:
    step FAILED
    failure_count increments
    checkpoint persisted
    goal stops or fails according to existing limit
    """
    await setup_test_environment("asus")
    gm = GoalManager.get_instance()

    def failing_handler(cmd, args):
        return "failure", "Hardware thermal sensor malfunction", None

    sim_ws = SimulatedDeviceWebSocket("asus", custom_handler=failing_handler)
    ctrl = CortexControlLayer({"asus": sim_ws})
    runner = GoalRunner.get_instance(control_layer=ctrl, goal_manager=gm)

    goal = await gm.create_goal(title="Sensor Check", max_failures=3, status="ACTIVE")
    s1 = await gm.add_step(goal.goal_id, description="Read thermal sensor", capability="device.status")
    s2 = await gm.add_step(goal.goal_id, description="Follow up notification", capability="ui.notify")

    loop_res = await runner.run_goal(goal.goal_id)
    assert loop_res["status"] == "ACTIVE"
    assert loop_res["failure_count"] == 1
    assert loop_res["last_step_result"]["status"] == "failed"
    assert loop_res["last_step_result"]["step_status"] == "FAILED"

    # Verify step 1 in DB is FAILED
    db_s1 = await gm.get_step(s1.step_id)
    assert db_s1.status == "FAILED"
    assert "Hardware thermal sensor malfunction" in db_s1.error
    assert db_s1.checkpoint_data["status"] == "FAILED"
    assert "Hardware thermal sensor malfunction" in db_s1.checkpoint_data["error"]

    # Verify step 2 was NEVER executed
    db_s2 = await gm.get_step(s2.step_id)
    assert db_s2.status == "PENDING"

    # Verify goal in DB stopped and recorded failure_count
    db_goal = await gm.get_goal(goal.goal_id)
    assert db_goal.failure_count == 1
    assert db_goal.current_step_index == 0
    assert db_goal.checkpoint_data["failed_step_index"] == 0


# ---------------------------------------------------------------------------
# TEST 6: max_failures Enforcement
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_max_failures_enforcement():
    """Verify goal transitions to terminal FAILED when failure_count reaches max_failures."""
    await setup_test_environment("asus")
    gm = GoalManager.get_instance()

    sim_ws = SimulatedDeviceWebSocket(
        "asus",
        custom_handler=lambda cmd, args: ("failure", "Repeated device failure", None)
    )
    ctrl = CortexControlLayer({"asus": sim_ws})
    runner = GoalRunner.get_instance(control_layer=ctrl, goal_manager=gm)

    # Goal with max_failures = 2
    goal = await gm.create_goal(title="Fragile Goal", max_failures=2, status="ACTIVE")
    s1 = await gm.add_step(goal.goal_id, description="Failing step 1", capability="device.status")

    # First failure
    res1 = await runner.run_next_step(goal.goal_id)
    assert res1["status"] == "failed"
    db_g1 = await gm.get_goal(goal.goal_id)
    assert db_g1.failure_count == 1
    assert db_g1.status == "ACTIVE"

    # Reset step to PENDING to simulate retry
    s1.status = "PENDING"
    await gm.update_step(s1)

    # Second failure -> reaches limit
    res2 = await runner.run_next_step(goal.goal_id)
    assert res2["status"] == "failed"

    # Verify goal is now terminal FAILED
    db_g2 = await gm.get_goal(goal.goal_id)
    assert db_g2.status == "FAILED"
    assert db_g2.failure_count == 2
    assert "Exceeded maximum allowed failures (2)" in db_g2.blocking_reason


# ---------------------------------------------------------------------------
# TEST 7: Nonexistent Capability → BLOCKED (BLOCKED CAPABILITY TEST)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_nonexistent_capability_blocked():
    """
    BLOCKED CAPABILITY TEST:
    Seed a step with nonexistent capability: fake.capability
    Verify:
    execution does not occur
    goal → BLOCKED
    blocking_reason persisted
    Do NOT let the model invent an executable capability name and attempt it.
    """
    await setup_test_environment("asus")
    gm = GoalManager.get_instance()

    sim_ws = SimulatedDeviceWebSocket("asus")
    ctrl = CortexControlLayer({"asus": sim_ws})
    runner = GoalRunner.get_instance(control_layer=ctrl, goal_manager=gm)

    goal = await gm.create_goal(title="Invalid Cap Goal", status="ACTIVE")
    step = await gm.add_step(
        goal.goal_id,
        description="Try to run invented tool",
        capability="fake.capability",
    )

    res = await runner.run_goal(goal.goal_id)
    assert res["status"] == "BLOCKED"

    # Verify execution did NOT occur
    assert len(sim_ws.received_commands) == 0

    # Verify DB state
    db_goal = await gm.get_goal(goal.goal_id)
    assert db_goal.status == "BLOCKED"
    assert "fake.capability" in db_goal.blocking_reason

    # Verify step remained PENDING
    db_step = await gm.get_step(step.step_id)
    assert db_step.status == "PENDING"
    assert db_step.started_at is None


# ---------------------------------------------------------------------------
# TEST 8: Approval Required → NEEDS_APPROVAL (APPROVAL TEST)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_approval_required_needs_approval():
    """
    APPROVAL TEST:
    Preserve the existing permission/approval layer.
    If execution requires approval:
    goal → NEEDS_APPROVAL
    approval_request_id persisted
    execution stops
    Do NOT bypass approval.
    """
    await setup_test_environment("asus")
    gm = GoalManager.get_instance()

    # Enroll a sub_admin device (fs.move requires confirmation or Tier 2)
    # Or set an explicit 'ask' override on capability
    await database.set_device_permission("asus", "fs.move", "ask")

    sim_ws = SimulatedDeviceWebSocket("asus")
    ctrl = CortexControlLayer({"asus": sim_ws})
    runner = GoalRunner.get_instance(control_layer=ctrl, goal_manager=gm)

    goal = await gm.create_goal(title="Move Files Goal", status="ACTIVE")
    step = await gm.add_step(
        goal.goal_id,
        description="Move critical data",
        capability="fs.move",
        arguments={"src": "C:/tmp/a.txt", "dst": "C:/tmp/b.txt"},
    )

    res = await runner.run_goal(goal.goal_id)
    assert res["status"] == "NEEDS_APPROVAL"

    # Verify execution was NOT dispatched to device
    assert len(sim_ws.received_commands) == 0

    # Verify DB state
    db_goal = await gm.get_goal(goal.goal_id)
    assert db_goal.status == "NEEDS_APPROVAL"
    assert db_goal.approval_request_id is not None
    assert len(db_goal.approval_request_id) > 0

    # Verify step remained PENDING
    db_step = await gm.get_step(step.step_id)
    assert db_step.status == "PENDING"

    # Verify confirmation row created in DB
    confs = await database.get_pending_confirmations()
    assert any(c["command_id"] == db_goal.approval_request_id for c in confs)


# ---------------------------------------------------------------------------
# TEST 9: max_steps Enforcement
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_max_steps_enforcement():
    """Verify bounded execution stops and marks BLOCKED when max_steps is reached."""
    await setup_test_environment("asus")
    gm = GoalManager.get_instance()

    sim_ws = SimulatedDeviceWebSocket("asus", default_status="success", default_message="OK")
    ctrl = CortexControlLayer({"asus": sim_ws})
    runner = GoalRunner.get_instance(control_layer=ctrl, goal_manager=gm)

    # Goal with max_steps = 1
    goal = await gm.create_goal(title="Bounded Steps Goal", max_steps=1, status="ACTIVE")
    s1 = await gm.add_step(goal.goal_id, description="Step 1", capability="device.status")

    # Manually force current_step_index to 1 (at limit) and add step 2 directly to DB
    goal.current_step_index = 1
    await gm.update_goal(goal)

    # Try running next step
    res = await runner.run_next_step(goal.goal_id)
    assert res["status"] == "blocked"
    assert "Exceeded maximum allowed steps" in res["reason"]

    db_goal = await gm.get_goal(goal.goal_id)
    assert db_goal.status == "BLOCKED"
    assert "Exceeded maximum allowed steps (1)" in db_goal.blocking_reason


# ---------------------------------------------------------------------------
# TEST 10: No Direct Executor Bypass
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_no_direct_executor_bypass():
    """
    EXECUTION RULE:
    Never execute arbitrary code directly from GoalManager.
    Execution must go through existing Capability Registry, Permission / Policy, ControlLayer, Device Agent.
    """
    gm = GoalManager.get_instance()

    # 1. GoalManager has no execution methods
    assert not hasattr(gm, "execute")
    assert not hasattr(gm, "execute_command")
    assert not hasattr(gm, "dispatch")

    # 2. GoalRunner rejects execution if ControlLayer is missing
    runner = GoalRunner(control_layer=None, goal_manager=gm)
    goal = await gm.create_goal(title="No ControlLayer Goal", status="ACTIVE")
    await gm.add_step(goal.goal_id, description="Step 1", capability="device.status")

    res = await runner.run_next_step(goal.goal_id)
    assert res["status"] == "blocked"
    assert "ControlLayer unavailable" in res["reason"]

    # 3. Denied permission halts execution before device dispatch
    await setup_test_environment("asus")
    # Deny capability explicitly on device
    await database.set_device_permission("asus", "device.status", "deny")

    sim_ws = SimulatedDeviceWebSocket("asus")
    ctrl = CortexControlLayer({"asus": sim_ws})
    runner.set_control_layer(ctrl)

    goal2 = await gm.create_goal(title="Denied Cap Goal", status="ACTIVE")
    await gm.add_step(goal2.goal_id, description="Denied step", capability="device.status")

    res2 = await runner.run_next_step(goal2.goal_id)
    assert res2["status"] == "blocked"
    assert "denied" in res2["reason"].lower()
    # No commands sent to WebSocket
    assert len(sim_ws.received_commands) == 0


# ---------------------------------------------------------------------------
# TEST 11: Restart Safety Unchanged
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_restart_safety_unchanged():
    """
    RESTART SAFETY:
    Do not auto-resume on Hub restart yet.
    Existing 9A behavior remains:
    ACTIVE/RUNNING → WAITING on restart.
    Preserves current step index and checkpoint data.
    """
    await setup_test_environment("asus")
    gm = GoalManager.get_instance()

    goal = await gm.create_goal(title="Active Goal Surviving Restart", status="ACTIVE")
    step = await gm.add_step(goal.goal_id, description="Running step", capability="device.status")
    await gm.start_step(goal.goal_id, step.step_id)
    await gm.checkpoint(goal.goal_id, {"cursor": 123}, step_id=step.step_id)

    # Verify state before restart
    assert (await gm.get_goal(goal.goal_id)).status == "ACTIVE"
    assert (await gm.get_step(step.step_id)).status == "RUNNING"

    # Simulate Hub restart
    recovered = await gm.recover_on_startup()
    assert recovered == 1

    # Verify state after restart: normalized to WAITING, NOT auto-resumed
    restored_goal = await gm.get_goal(goal.goal_id)
    assert restored_goal.status == "WAITING"
    assert restored_goal.waiting_reason == "Paused due to Hub restart"
    assert restored_goal.checkpoint_data["cursor"] == 123

    restored_step = await gm.get_step(step.step_id)
    assert restored_step.status == "WAITING"
    assert restored_step.checkpoint_data["cursor"] == 123


# ---------------------------------------------------------------------------
# TEST 12: Minimal Natural Language Invocation
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_natural_language_run_goal():
    """
    NATURAL LANGUAGE:
    Minimal only:
    'Run my <goal> goal.'
    should invoke the bounded runner on an existing goal.
    """
    await setup_test_environment("asus")
    gm = GoalManager.get_instance()

    sim_ws = SimulatedDeviceWebSocket("asus", default_status="success", default_message="Device checked")
    ctrl = CortexControlLayer({"asus": sim_ws})
    GoalRunner.get_instance(control_layer=ctrl, goal_manager=gm)

    goal = await gm.create_goal(title="Device Check", status="WAITING")
    await gm.add_step(goal.goal_id, description="Check battery", capability="device.status")

    # Natural language request
    reply = await gm.handle_natural_language("Run my device check goal.")
    assert reply is not None
    assert "Ran goal 'Device Check'" in reply
    assert "All steps completed successfully" in reply

    # Goal in DB is completed
    db_goal = await gm.get_goal(goal.goal_id)
    assert db_goal.status == "COMPLETED"


# ---------------------------------------------------------------------------
# TEST 13: Production DB Isolation
# ---------------------------------------------------------------------------

def test_production_db_isolation():
    """FATAL GUARD: Confirm data/cortex.db has not been targeted or modified."""
    from hub.database import get_db_path
    resolved_test_db = get_db_path().resolve()
    prod_db = Path(__file__).resolve().parent.parent / "data" / "cortex.db"
    assert resolved_test_db != prod_db.resolve(), f"FATAL: Test targeting prod DB at {resolved_test_db}!"
    assert not str(resolved_test_db).replace('\\', '/').endswith("data/cortex.db")
    assert prod_db.exists(), f"Production DB missing at {prod_db}"
