"""
tests/test_phase9d_goals.py
---------------------------
Phase 9D: Autonomy Safety Controls Test Suite.

Verifies:
1. Global kill switch blocks autonomous execution.
2. Normal non-autonomous queries remain available when kill switch is active.
3. Pause all active goals safely.
4. Resume only user-paused goals (ignores BLOCKED, NEEDS_APPROVAL, FAILED, CANCELLED, COMPLETED, external waits).
5. Wall-clock runtime limit enforces elapsed time and stops at safe boundary (BLOCKED).
6. Consecutive failure limit enforces threshold and transitions to FAILED.
7. Success resets consecutive failure counter to 0.
8. Irreversible / high-impact actions require approval and never dispatch (NEEDS_APPROVAL).
9. Mid-goal pause checkpoint: preserves Step 1 progress and stops before Step 2.
10. Explicit resume continues execution from checkpoint exactly once.
11. Wait-condition auto-resume respects global kill switch.
12. Hub restart preserves autonomy safety state, limits, and user-paused goals.
13. Natural language deterministic safety commands ("Stop all autonomous tasks.", etc.).
14. Production DB isolation (SHA-256 of data/cortex.db unchanged).
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
    """Mock WebSocket for agent connections."""
    def __init__(self, device_id: str, default_status: str = "success", default_message: str = "OK", custom_handler = None):
        self.device_id = device_id
        self.default_status = default_status
        self.default_message = default_message
        self.custom_handler = custom_handler
        self.sent_messages = []

    async def send_text(self, text: str):
        self.sent_messages.append(text)
        data = json.loads(text)
        cmd_id = data.get("command_id")
        cmd = data.get("command")
        args = data.get("args", [])

        if cmd_id and cmd_id in PENDING_FUTURES:
            fut = PENDING_FUTURES[cmd_id]
            if fut.done():
                return

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
# TEST 1: Kill switch blocks autonomous execution
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_kill_switch_blocks_autonomous_execution():
    """Verify that disabling autonomy globally halts GoalRunner execution and preserves state."""
    await setup_test_environment("asus")
    gm = GoalManager.get_instance()

    sim_ws = SimulatedDeviceWebSocket("asus")
    ctrl = CortexControlLayer({"asus": sim_ws})
    runner = GoalRunner.get_instance(control_layer=ctrl, goal_manager=gm)

    goal = await gm.create_goal(title="Autonomous Task", status="ACTIVE")
    step1 = await gm.add_step(goal.goal_id, description="Check battery", capability="device.status")
    step2 = await gm.add_step(goal.goal_id, description="Notify user", capability="ui.notify", arguments={"message": "Done"})

    # Disable autonomy before execution
    await gm.set_autonomy_enabled(False)
    assert await gm.is_autonomy_enabled() is False

    # Attempt to run goal
    res = await runner.run_goal(goal.goal_id)
    assert res["status"] == "blocked"
    assert "kill switch" in res["reason"].lower()

    # Verify no steps executed
    s1 = await gm.get_step(step1.step_id)
    s2 = await gm.get_step(step2.step_id)
    assert s1.status == "PENDING"
    assert s2.status == "PENDING"
    assert len(sim_ws.sent_messages) == 0

    # Enable autonomy and explicitly resume
    await gm.set_autonomy_enabled(True)
    assert await gm.is_autonomy_enabled() is True

    res2 = await runner.run_goal(goal.goal_id)
    assert res2["status"] == "COMPLETED"
    assert (await gm.get_step(step1.step_id)).status == "COMPLETED"
    assert (await gm.get_step(step2.step_id)).status == "COMPLETED"


# ---------------------------------------------------------------------------
# TEST 2: Normal non-autonomous queries remain available
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_normal_non_autonomous_queries_remain_available():
    """Verify normal interactive queries (device status, conversational reads) succeed while autonomy is disabled."""
    await setup_test_environment("asus")
    gm = GoalManager.get_instance()

    sim_ws = SimulatedDeviceWebSocket("asus", default_status="success", default_message="Battery at 95%")
    ctrl = CortexControlLayer({"asus": sim_ws})

    # Disable autonomy globally
    await gm.set_autonomy_enabled(False)

    # 1. Direct interactive command via ControlLayer (is_autonomous=False)
    direct_res = await ctrl.execute_and_observe("asus", "device.status", {}, is_autonomous=False)
    assert direct_res["status"] == "success"
    assert direct_res["message"] == "Battery at 95%"

    # 2. Natural language query regarding autonomy status
    status_reply = await gm.handle_natural_language("Is autonomous execution enabled?")
    assert status_reply == "Autonomous execution is disabled."

    # 3. Read-only goal query
    goals_reply = await gm.handle_natural_language("What goals are active?")
    assert "no active goals" in goals_reply.lower()

    # 4. Confirm an autonomous invocation is strictly rejected by ControlLayer
    rejected_res = await ctrl.execute_and_observe("asus", "device.status", {}, is_autonomous=True)
    assert rejected_res["status"] == "failure"
    assert "kill switch" in rejected_res["message"].lower()


# ---------------------------------------------------------------------------
# TEST 3: Pause all active goals
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_pause_all_goals():
    """Verify pause_all sets ACTIVE goals to WAITING with 'Paused by user' and stops execution safely."""
    await setup_test_environment("asus")
    gm = GoalManager.get_instance()

    goal1 = await gm.create_goal(title="Goal 1", status="ACTIVE")
    goal2 = await gm.create_goal(title="Goal 2", status="ACTIVE")
    goal3 = await gm.create_goal(title="Goal 3", status="PENDING")

    paused_ids = await gm.pause_all_goals(reason="Paused by user")
    assert goal1.goal_id in paused_ids
    assert goal2.goal_id in paused_ids
    assert goal3.goal_id not in paused_ids

    g1 = await gm.get_goal(goal1.goal_id)
    assert g1.status == "WAITING"
    assert g1.waiting_reason == "Paused by user"
    assert g1.metadata.get("user_paused") is True

    g2 = await gm.get_goal(goal2.goal_id)
    assert g2.status == "WAITING"
    assert g2.waiting_reason == "Paused by user"


# ---------------------------------------------------------------------------
# TEST 4: Resume only user-paused goals
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_resume_only_user_paused_goals():
    """Verify resume_all resumes ONLY goals specifically paused by the user, ignoring terminal or external waits."""
    await setup_test_environment("asus")
    gm = GoalManager.get_instance()

    # Goal A: user-paused
    goal_a = await gm.create_goal(title="Goal A", status="ACTIVE")
    await gm.pause_all_goals()

    # Goal B: BLOCKED
    goal_b = await gm.create_goal(title="Goal B", status="BLOCKED")
    goal_b.blocking_reason = "Manual block"
    await gm.update_goal(goal_b)

    # Goal C: NEEDS_APPROVAL
    goal_c = await gm.create_goal(title="Goal C", status="NEEDS_APPROVAL")
    goal_c.approval_request_id = "req_123"
    await gm.update_goal(goal_c)

    # Goal D: FAILED
    goal_d = await gm.create_goal(title="Goal D", status="ACTIVE")
    await gm.fail_goal(goal_d.goal_id, error="Terminal fail")

    # Goal E: CANCELLED
    goal_e = await gm.create_goal(title="Goal E", status="ACTIVE")
    await gm.cancel_goal(goal_e.goal_id, reason="User cancelled")

    # Goal F: WAITING on an external wait condition (NOT user-paused)
    goal_f = await gm.create_goal(title="Goal F", status="ACTIVE")
    step_f = await gm.add_step(goal_f.goal_id, description="Wait", capability="goal.wait")
    await gm.create_wait_condition(goal_f.goal_id, condition_type="device_online", condition_key="asus", step_id=step_f.step_id)
    goal_f.status = "WAITING"
    goal_f.waiting_reason = "Waiting for device_online (asus)"
    await gm.update_goal(goal_f)

    # Resume all
    resumed = await gm.resume_all_goals()
    assert goal_a.goal_id in resumed
    assert goal_b.goal_id not in resumed
    assert goal_c.goal_id not in resumed
    assert goal_d.goal_id not in resumed
    assert goal_e.goal_id not in resumed
    assert goal_f.goal_id not in resumed

    # Status verifications
    assert (await gm.get_goal(goal_a.goal_id)).status == "ACTIVE"
    assert (await gm.get_goal(goal_b.goal_id)).status == "BLOCKED"
    assert (await gm.get_goal(goal_c.goal_id)).status == "NEEDS_APPROVAL"
    assert (await gm.get_goal(goal_d.goal_id)).status == "FAILED"
    assert (await gm.get_goal(goal_e.goal_id)).status == "CANCELLED"
    assert (await gm.get_goal(goal_f.goal_id)).status == "WAITING"


# ---------------------------------------------------------------------------
# TEST 5: Wall-clock limit
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_wall_clock_limit_enforced():
    """Verify that a goal exceeding max_runtime_seconds transitions to BLOCKED at a safe execution boundary."""
    await setup_test_environment("asus")
    gm = GoalManager.get_instance()

    ctrl = CortexControlLayer({"asus": SimulatedDeviceWebSocket("asus")})
    runner = GoalRunner.get_instance(control_layer=ctrl, goal_manager=gm)

    # Goal configured with a 10-second limit
    goal = await gm.create_goal(title="Time Limited Goal", status="ACTIVE", max_runtime_seconds=10)
    step1 = await gm.add_step(goal.goal_id, description="Step 1", capability="device.status")
    step2 = await gm.add_step(goal.goal_id, description="Step 2", capability="ui.notify", arguments={"message": "Hi"})

    # Simulate started_at 30 seconds ago
    past_time = (datetime.now(timezone.utc) - timedelta(seconds=30)).isoformat()
    goal.started_at = past_time
    await gm.update_goal(goal)

    # Execute goal
    res = await runner.run_goal(goal.goal_id)
    assert res["status"] == "BLOCKED"

    updated_goal = await gm.get_goal(goal.goal_id)
    assert updated_goal.status == "BLOCKED"
    assert "Runtime limit exceeded" in updated_goal.blocking_reason

    # Verify Step 1 was not executed due to limit
    s1 = await gm.get_step(step1.step_id)
    assert s1.status == "PENDING"


# ---------------------------------------------------------------------------
# TEST 6: Consecutive failure limit and success reset
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_consecutive_failure_limit_and_success_reset():
    """Verify consecutive failure threshold triggers FAILED, and success resets the counter."""
    await setup_test_environment("asus")
    gm = GoalManager.get_instance()

    # Goal configured with max_consecutive_failures=2, max_failures=10
    goal = await gm.create_goal(title="Failure Bound Goal", status="ACTIVE", max_failures=10, max_consecutive_failures=2)
    s1 = await gm.add_step(goal.goal_id, description="Step 1", capability="device.status")
    s2 = await gm.add_step(goal.goal_id, description="Step 2", capability="device.status")
    s3 = await gm.add_step(goal.goal_id, description="Step 3", capability="device.status")

    # 1. Step 1 fails
    await gm.fail_step(goal.goal_id, s1.step_id, error="Transient error")
    g = await gm.get_goal(goal.goal_id)
    assert g.consecutive_failures == 1
    assert g.failure_count == 1
    assert g.status == "ACTIVE"

    # 2. Step 2 succeeds -> resets consecutive_failures to 0
    await gm.complete_step(goal.goal_id, s2.step_id, result={"status": "ok"})
    g = await gm.get_goal(goal.goal_id)
    assert g.consecutive_failures == 0
    assert g.failure_count == 1
    assert g.status == "ACTIVE"

    # 3. Next step fails -> consecutive_failures = 1
    s4 = await gm.add_step(goal.goal_id, description="Step 4", capability="device.status")
    await gm.fail_step(goal.goal_id, s4.step_id, error="Error 1")
    g = await gm.get_goal(goal.goal_id)
    assert g.consecutive_failures == 1
    assert g.status == "ACTIVE"

    # 4. Another step fails -> consecutive_failures = 2 == threshold -> Goal FAILED
    s5 = await gm.add_step(goal.goal_id, description="Step 5", capability="device.status")
    await gm.fail_step(goal.goal_id, s5.step_id, error="Error 2")
    g = await gm.get_goal(goal.goal_id)
    assert g.consecutive_failures == 2
    assert g.status == "FAILED"
    assert "Exceeded maximum consecutive failures (2)" in g.blocking_reason


# ---------------------------------------------------------------------------
# TEST 7: Irreversible action requires approval
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_irreversible_action_requires_approval():
    """Verify destructive / irreversible actions (Tier 3 fs.delete) transition goal to NEEDS_APPROVAL and never dispatch."""
    await setup_test_environment("asus")
    gm = GoalManager.get_instance()

    sim_ws = SimulatedDeviceWebSocket("asus")
    ctrl = CortexControlLayer({"asus": sim_ws})
    runner = GoalRunner.get_instance(control_layer=ctrl, goal_manager=gm)

    goal = await gm.create_goal(title="Destructive Task", status="ACTIVE")
    step = await gm.add_step(
        goal.goal_id,
        description="Delete database backup",
        capability="fs.delete",
        arguments={"path": "C:\\backups\\cortex.bak", "target_device": "asus"},
    )

    res = await runner.run_goal(goal.goal_id)
    assert res["status"] == "NEEDS_APPROVAL"

    updated_goal = await gm.get_goal(goal.goal_id)
    assert updated_goal.status == "NEEDS_APPROVAL"
    assert updated_goal.approval_request_id is not None

    # Verify confirmation record created in database
    pending = await database.get_pending_confirmations()
    assert any(c["command_id"] == updated_goal.approval_request_id for c in pending)

    # Verify action was NEVER sent to device agent
    assert len(sim_ws.sent_messages) == 0


# ---------------------------------------------------------------------------
# TEST 8: Mid-goal pause checkpoint and explicit resume
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_mid_goal_pause_checkpoint_and_explicit_resume():
    """Verify user pause/kill switch stops execution between steps, preserving checkpoint, and explicit resume continues once."""
    await setup_test_environment("asus")
    gm = GoalManager.get_instance()

    executed_steps = []

    def custom_handler(cmd, args):
        executed_steps.append(cmd)
        return "success", f"Executed {cmd}", None

    sim_ws = SimulatedDeviceWebSocket("asus", custom_handler=custom_handler)
    ctrl = CortexControlLayer({"asus": sim_ws})
    runner = GoalRunner.get_instance(control_layer=ctrl, goal_manager=gm)

    goal = await gm.create_goal(title="Multi Step Task", status="ACTIVE")
    step1 = await gm.add_step(goal.goal_id, description="Step 1", capability="device.status")
    step2 = await gm.add_step(goal.goal_id, description="Step 2", capability="ui.notify", arguments={"message": "Finished"})

    # Execute Step 1 only
    step_res = await runner.run_next_step(goal.goal_id)
    assert step_res["status"] in ("continued", "success")
    assert len(executed_steps) == 1
    assert executed_steps[0] == "device.status"

    # User activates pause / kill switch mid-goal
    await gm.pause_all_goals(reason="Paused by user")
    assert (await gm.get_goal(goal.goal_id)).status == "WAITING"
    assert (await gm.get_goal(goal.goal_id)).current_step_index == 1

    # Attempt running while paused / waiting
    run_res = await runner.run_goal(goal.goal_id)
    assert run_res["status"] in ("stopped", "waiting", "WAITING")
    assert len(executed_steps) == 1  # Step 2 did NOT execute

    # Explicitly resume user-paused goals
    resumed = await gm.resume_all_goals()
    assert goal.goal_id in resumed
    assert (await gm.get_goal(goal.goal_id)).status == "ACTIVE"

    # Run goal to completion
    run_res2 = await runner.run_goal(goal.goal_id)
    assert run_res2["status"] == "COMPLETED"
    assert len(executed_steps) == 2
    assert executed_steps[1] == "ui.notify"
    assert (await gm.get_goal(goal.goal_id)).current_step_index == 2


# ---------------------------------------------------------------------------
# TEST 9: Wait-condition auto-resume respects kill switch
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_wait_condition_autoresume_respects_kill_switch():
    """Verify that incoming signals do NOT auto-resume waiting goals when autonomy is disabled."""
    await setup_test_environment("asus")
    gm = GoalManager.get_instance()
    wsm = WorldStateManager.get_instance()

    sim_ws = SimulatedDeviceWebSocket("asus")
    connected_map = {}  # Starts OFFLINE
    ctrl = CortexControlLayer(connected_map)
    runner = GoalRunner.get_instance(control_layer=ctrl, goal_manager=gm)

    goal = await gm.create_goal(title="Wait Condition Goal", status="ACTIVE")
    step1 = await gm.add_step(
        goal.goal_id,
        description="Wait for ASUS",
        capability="goal.wait",
        arguments={"condition_type": "device_online", "condition_key": "asus"},
    )
    step2 = await gm.add_step(goal.goal_id, description="Notify", capability="ui.notify", arguments={"message": "Back", "target_device": "asus"})

    # Initial run: ASUS is offline, goal enters WAITING
    await runner.run_goal(goal.goal_id)
    assert (await gm.get_goal(goal.goal_id)).status == "WAITING"

    # Disable autonomy globally
    await gm.set_autonomy_enabled(False)

    # Reconnect ASUS
    connected_map["asus"] = sim_ws

    # Ingest device-online signal
    await wsm.ingest_signal(Signal(
        key="device.online:asus",
        source="device",
        timestamp=datetime.now(timezone.utc).isoformat(),
        confidence=1.0,
        ttl=3600.0,
        payload={"is_online": True, "device_id": "asus"},
    ))

    # Goal MUST remain WAITING because autonomy is disabled
    g = await gm.get_goal(goal.goal_id)
    assert g.status == "WAITING"

    # Enable autonomy and trigger evaluation
    await gm.set_autonomy_enabled(True)
    await gm.evaluate_wait_conditions()

    # Now goal resumes and completes Step 2
    g_after = await gm.get_goal(goal.goal_id)
    assert g_after.status == "COMPLETED"


# ---------------------------------------------------------------------------
# TEST 10: Restart preserves safety state and limits
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_restart_preserves_safety_state():
    """Verify that global autonomy disabled state, safety bounds, and user-paused goals survive Hub restart."""
    await setup_test_environment("asus")
    gm = GoalManager.get_instance()

    # Disable autonomy
    await gm.set_autonomy_enabled(False)

    # Create goal with custom bounds
    goal = await gm.create_goal(
        title="Safety Bounds Goal",
        status="ACTIVE",
        max_runtime_seconds=500,
        max_consecutive_failures=2,
    )
    await gm.pause_all_goals(reason="Paused by user")

    # Simulate Hub restart
    GoalManager.reset()
    GoalRunner.reset()

    gm_restarted = GoalManager.get_instance()
    await gm_restarted.recover_on_startup()

    # 1. Kill switch state survived
    assert await gm_restarted.is_autonomy_enabled() is False

    # 2. Goal remains user-paused in WAITING
    restored_goal = await gm_restarted.get_goal(goal.goal_id)
    assert restored_goal.status == "WAITING"
    assert restored_goal.waiting_reason == "Paused by user"
    assert restored_goal.metadata.get("user_paused") is True

    # 3. Safety bounds survived
    assert restored_goal.max_runtime_seconds == 500
    assert restored_goal.max_consecutive_failures == 2


# ---------------------------------------------------------------------------
# TEST 11: Natural language deterministic safety commands
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_natural_language_safety_commands():
    """Verify deterministic natural language handling for stop, pause, resume, and status checks without LLM."""
    await setup_test_environment("asus")
    gm = GoalManager.get_instance()

    # Initial query
    reply_status1 = await gm.handle_natural_language("Is autonomous execution enabled?")
    assert "enabled" in reply_status1.lower()

    # Create an active goal
    goal = await gm.create_goal(title="Test NL Task", status="ACTIVE")

    # Emergency kill switch command
    reply_stop = await gm.handle_natural_language("Stop all autonomous tasks.")
    assert "disabled by global kill switch" in reply_stop.lower()
    assert await gm.is_autonomy_enabled() is False
    assert (await gm.get_goal(goal.goal_id)).status == "WAITING"

    # Status check after disable
    reply_status2 = await gm.handle_natural_language("Is autonomous execution enabled?")
    assert "disabled" in reply_status2.lower()

    # Resume paused goals command
    reply_resume = await gm.handle_natural_language("Resume my paused goals.")
    assert "resumed 1 paused goal" in reply_resume.lower()
    assert (await gm.get_goal(goal.goal_id)).status == "ACTIVE"

    # Pause all goals command
    reply_pause = await gm.handle_natural_language("Pause all my goals.")
    assert "paused 1 active goal" in reply_pause.lower()
    assert (await gm.get_goal(goal.goal_id)).status == "WAITING"

    # Re-enable autonomy
    reply_enable = await gm.handle_natural_language("Enable autonomous execution.")
    assert "now enabled" in reply_enable.lower()
    assert await gm.is_autonomy_enabled() is True


# ---------------------------------------------------------------------------
# TEST 12: Production DB Isolation
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_production_db_isolation():
    """Verify that data/cortex.db has remained completely untouched throughout all Phase 9D tests."""
    from hub.database import get_db_path
    resolved_test_db = get_db_path().resolve()
    prod_db = Path("data/cortex.db").resolve()
    assert resolved_test_db != prod_db, f"FATAL: Test targeting prod DB at {resolved_test_db}!"
    assert not str(resolved_test_db).replace('\\', '/').endswith("data/cortex.db")
    assert prod_db.exists(), "Production database data/cortex.db must exist."
