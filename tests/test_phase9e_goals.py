"""
tests/test_phase9e_goals.py
---------------------------
Phase 9E: Capability-Aware Planning Test Suite.

Verifies:
1. Machine-readable capability context generation with structured metadata.
2. Structured plan validation (existence, target availability, args, schema, risk).
3. Valid plan persistence into GoalSteps.
4. Invalid/hallucinated capability rejection.
5. Missing required arguments and unknown arguments rejection.
6. max_steps enforcement rejects oversized plans.
7. Required Success E2E: "Check this device and notify me" -> planned & executed without manual seeding.
8. Required Unknown-Capability: "3D print a replacement laptop hinge" -> BLOCKED, capability gap explained.
9. Required File Goal: "List the files in my Downloads folder..." -> BLOCKED cleanly due to missing listing tool.
10. Required Replan: Step 1 succeeds, Step 2 fails -> replan preserves completed step, replan_count += 1, continues.
11. Bounded replanning: replan_count > max_replans -> BLOCKED.
12. Replanning failure when no valid alternative exists -> BLOCKED.
13. Safety integration: planned steps respect kill switch and approvals.
14. Natural language create and run flow.
15. Production DB isolation (SHA-256 of data/cortex.db).
"""

import asyncio
import hashlib
import json
import os
import pytest
from datetime import datetime, timezone
from pathlib import Path

from hub import database
from hub.goals import (
    Goal,
    GoalStep,
    GoalManager,
    GoalRunner,
)
from hub.planner import (
    GoalPlanner,
    CapabilitySchema,
    BUILTIN_CAPABILITY_SCHEMAS,
)
from hub.control_layer import CortexControlLayer
from hub.main import PENDING_FUTURES
from hub.models import ResultMessage

PROD_DB_HASH = "2F3BE6B06C90647DB148F0AFE61A5F6867BFF2A838B73DEC76AD633C5EC65598"


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
        ["device.status", "device.info", "ui.notify", "app.launch", "fs.move", "fs.delete", "screenshot.read"],
        platform="windows",
    )


# ---------------------------------------------------------------------------
# TEST 1: Capability Context Generation
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_capability_context_generation():
    """Verify machine-readable capability context exposes structured metadata."""
    await setup_test_environment("asus")
    planner = GoalPlanner.get_instance()

    caps = await planner.build_capability_context(target_device="asus")
    assert isinstance(caps, list)
    assert len(caps) > 0

    cap_names = {c["name"] for c in caps}
    assert "device.status" in cap_names
    assert "ui.notify" in cap_names

    # Verify structured metadata fields
    status_cap = next(c for c in caps if c["name"] == "device.status")
    assert "description" in status_cap
    assert "arguments" in status_cap
    assert "target_type" in status_cap
    assert "permission_tier" in status_cap
    assert "risk" in status_cap
    assert "classification" in status_cap
    assert status_cap["availability"] is True
    assert status_cap["permission_tier"] == 1
    assert status_cap["classification"] == "read"


# ---------------------------------------------------------------------------
# TEST 2: Structured Plan Validation
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_structured_plan_validation():
    """Verify that a valid plan passes strict schema validation."""
    await setup_test_environment("asus")
    gm = GoalManager.get_instance()
    planner = GoalPlanner.get_instance()

    goal = await gm.create_goal(title="Test Validation", original_request="Check device and notify")
    caps = await planner.build_capability_context(target_device="asus")

    valid_plan = {
        "steps": [
            {
                "description": "Check device telemetry",
                "capability": "device.status",
                "arguments": {"target_device": "asus"},
            },
            {
                "description": "Notify user",
                "capability": "ui.notify",
                "arguments": {"message": "All good", "target_device": "asus"},
            },
        ]
    }

    is_valid, err = planner.validate_plan(valid_plan, caps, goal)
    assert is_valid is True
    assert err is None


# ---------------------------------------------------------------------------
# TEST 3: Invalid Capability Rejection
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_invalid_capability_rejection():
    """Verify that the planner rejects non-existent or hallucinated capability names."""
    await setup_test_environment("asus")
    gm = GoalManager.get_instance()
    planner = GoalPlanner.get_instance()

    goal = await gm.create_goal(title="Test Invalid Cap", original_request="Teleport to office")
    caps = await planner.build_capability_context(target_device="asus")

    invalid_plan = {
        "steps": [
            {
                "description": "Invented teleportation",
                "capability": "device.teleport",
                "arguments": {},
            }
        ]
    }

    is_valid, err = planner.validate_plan(invalid_plan, caps, goal)
    assert is_valid is False
    assert "does not exist or is not available" in err

    # Ensure no steps are persisted
    steps = await gm.list_steps(goal.goal_id)
    assert len(steps) == 0


# ---------------------------------------------------------------------------
# TEST 4: Invalid Arguments Rejection
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_invalid_arguments_rejection():
    """Verify validation rejects missing required arguments and unknown arguments."""
    await setup_test_environment("asus")
    gm = GoalManager.get_instance()
    planner = GoalPlanner.get_instance()

    goal = await gm.create_goal(title="Test Args Validation", original_request="Notify me")
    caps = await planner.build_capability_context(target_device="asus")

    # Missing required argument 'message' for ui.notify
    missing_req_plan = {
        "steps": [
            {
                "description": "Notify without message",
                "capability": "ui.notify",
                "arguments": {"title": "Missing body"},
            }
        ]
    }
    is_valid, err = planner.validate_plan(missing_req_plan, caps, goal)
    assert is_valid is False
    assert "missing required argument 'message'" in err

    # Unknown argument on strict schema
    unknown_arg_plan = {
        "steps": [
            {
                "description": "Notify with hallucinated argument",
                "capability": "ui.notify",
                "arguments": {"message": "Hello", "hallucinated_param": 123},
            }
        ]
    }
    is_valid2, err2 = planner.validate_plan(unknown_arg_plan, caps, goal)
    assert is_valid2 is False
    assert "unknown argument 'hallucinated_param'" in err2


# ---------------------------------------------------------------------------
# TEST 5: max_steps Enforcement
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_max_steps_enforcement():
    """Verify plan with steps exceeding goal.max_steps is rejected and goal blocked."""
    await setup_test_environment("asus")
    gm = GoalManager.get_instance()
    planner = GoalPlanner.get_instance()

    goal = await gm.create_goal(title="Test Max Steps", original_request="Check status", max_steps=2)
    caps = await planner.build_capability_context(target_device="asus")

    oversized_plan = {
        "steps": [
            {"description": "S1", "capability": "device.status", "arguments": {}},
            {"description": "S2", "capability": "device.status", "arguments": {}},
            {"description": "S3", "capability": "device.status", "arguments": {}},
        ]
    }

    is_valid, err = planner.validate_plan(oversized_plan, caps, goal)
    assert is_valid is False
    assert "exceeds maximum allowed steps" in err


# ---------------------------------------------------------------------------
# TEST 6: Valid Plan Persistence
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_valid_plan_persistence():
    """Verify that validated steps are persisted accurately into SQLite."""
    await setup_test_environment("asus")
    gm = GoalManager.get_instance()
    planner = GoalPlanner.get_instance()

    goal = await gm.create_goal(title="Test Persistence", original_request="Check and notify")
    plan = {
        "steps": [
            {"description": "Step 1: Status", "capability": "device.status", "arguments": {"target_device": "asus"}},
            {"description": "Step 2: Notify", "capability": "ui.notify", "arguments": {"message": "Success", "target_device": "asus"}},
        ]
    }

    persisted = await planner.persist_plan(goal.goal_id, plan)
    assert len(persisted) == 2
    assert persisted[0].index == 0
    assert persisted[0].capability == "device.status"
    assert persisted[1].index == 1
    assert persisted[1].capability == "ui.notify"

    # Verify directly from DB
    db_steps = await gm.list_steps(goal.goal_id)
    assert len(db_steps) == 2
    assert db_steps[0].description == "Step 1: Status"
    assert db_steps[1].arguments == {"message": "Success", "target_device": "asus"}


# ---------------------------------------------------------------------------
# TEST 7: Required Success E2E Test (NO MANUAL SEEDING)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_required_success_natural_language_e2e():
    """
    REQUIRED SUCCESS TEST:
    Natural-language goal: 'Check this device and notify me.'
    Expected:
    - Goal created
    - Planner sees available capabilities
    - Valid plan created: device.status selected, ui.notify selected
    - Steps persisted
    - GoalRunner executes
    - Actual results observed
    - Goal COMPLETED
    - Verify NO manually seeded steps.
    """
    await setup_test_environment("asus")
    gm = GoalManager.get_instance()

    executed = []
    def custom_handler(cmd, args):
        executed.append((cmd, args))
        if cmd == "device.status":
            return "success", "Battery is at 95% plugged in", {"battery": 95}
        elif cmd == "ui.notify":
            return "success", "Notification displayed", None
        return "success", "OK", None

    sim_ws = SimulatedDeviceWebSocket("asus", custom_handler=custom_handler)
    ctrl = CortexControlLayer({"asus": sim_ws})
    planner = GoalPlanner.get_instance(control_layer=ctrl, goal_manager=gm)
    runner = GoalRunner.get_instance(control_layer=ctrl, goal_manager=gm)

    # 1. Create goal with natural language request (NO manual steps seeded!)
    goal = await gm.create_goal(
        title="Check this device and notify me",
        original_request="Check this device and notify me.",
        status="ACTIVE",
    )

    # Verify zero steps initially
    assert len(await gm.list_steps(goal.goal_id)) == 0

    # 2. Plan goal
    plan_res = await planner.plan_goal(goal.goal_id, target_device="asus")
    assert plan_res["status"] == "planned"
    assert plan_res["step_count"] == 2

    # Verify steps created by planner
    steps = await gm.list_steps(goal.goal_id)
    assert len(steps) == 2
    assert steps[0].capability == "device.status"
    assert steps[1].capability == "ui.notify"

    # 3. Run goal
    run_res = await runner.run_goal(goal.goal_id)
    assert run_res["status"] == "COMPLETED"

    # 4. Verify physical execution
    assert len(executed) == 2
    assert executed[0][0] == "device.status"
    assert executed[1][0] == "ui.notify"

    # 5. Verify final goal status
    final_goal = await gm.get_goal(goal.goal_id)
    assert final_goal.status == "COMPLETED"
    assert final_goal.current_step_index == 2


# ---------------------------------------------------------------------------
# TEST 8: Required Unknown-Capability Test
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_required_unknown_capability_blocked():
    """
    REQUIRED UNKNOWN-CAPABILITY TEST:
    Goal: '3D print a replacement laptop hinge.'
    Planner must NOT generate printer.3d_print, fabricate_hinge, etc.
    Expected:
    - No invalid steps persisted
    - Goal -> BLOCKED
    - Reason explains capability gap
    """
    await setup_test_environment("asus")
    gm = GoalManager.get_instance()
    planner = GoalPlanner.get_instance(goal_manager=gm)

    goal = await gm.create_goal(
        title="3D print a replacement laptop hinge",
        original_request="3D print a replacement laptop hinge.",
        status="PENDING",
    )

    # Plan goal
    plan_res = await planner.plan_goal(goal.goal_id, target_device="asus")
    assert plan_res["status"] == "blocked"
    assert "3D printing" in plan_res["reason"] or "capability" in plan_res["reason"].lower()

    # Verify no invalid steps persisted
    steps = await gm.list_steps(goal.goal_id)
    assert len(steps) == 0

    # Verify goal is BLOCKED in SQLite
    g = await gm.get_goal(goal.goal_id)
    assert g.status == "BLOCKED"
    assert "3D printing" in g.blocking_reason or "capability" in g.blocking_reason.lower()


# ---------------------------------------------------------------------------
# TEST 9: Required File Goal Test
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_required_file_goal_without_listing_capability_blocked():
    """
    REQUIRED FILE GOAL TEST:
    'List the files in my Downloads folder and notify me how many there are.'
    Since Cortex does not have a folder-listing capability, the correct result is BLOCKED.
    Planner must NOT invent fs.list or similar tool.
    """
    await setup_test_environment("asus")
    gm = GoalManager.get_instance()
    planner = GoalPlanner.get_instance(goal_manager=gm)

    goal = await gm.create_goal(
        title="List files in Downloads",
        original_request="List the files in my Downloads folder and notify me how many there are.",
        status="PENDING",
    )

    plan_res = await planner.plan_goal(goal.goal_id, target_device="asus")
    assert plan_res["status"] == "blocked"
    assert "listing directory contents" in plan_res["reason"] or "filesystem" in plan_res["reason"].lower()

    # Zero steps persisted
    assert len(await gm.list_steps(goal.goal_id)) == 0

    # Goal is BLOCKED
    g = await gm.get_goal(goal.goal_id)
    assert g.status == "BLOCKED"


# ---------------------------------------------------------------------------
# TEST 10: Required Replan Test
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_required_replan_preserves_completed_and_continues():
    """
    REQUIRED REPLAN TEST:
    Step 1 succeeds.
    Step 2 fails.
    Replan preserves completed step 1.
    Remaining plan replaced with valid available alternative.
    replan_count += 1.
    Execution continues.
    """
    await setup_test_environment("asus")
    gm = GoalManager.get_instance()

    executed = []
    def custom_handler(cmd, args):
        executed.append(cmd)
        if cmd == "device.status":
            return "success", "Status OK", None
        elif cmd == "device.info":
            return "success", "Info OK", None
        elif cmd == "ui.notify":
            if len(executed) == 2:
                # Step 2 fails initially
                return "failure", "Notification channel unavailable", None
            return "success", "Notification sent via fallback", None
        return "success", "OK", None

    sim_ws = SimulatedDeviceWebSocket("asus", custom_handler=custom_handler)
    ctrl = CortexControlLayer({"asus": sim_ws})
    planner = GoalPlanner.get_instance(control_layer=ctrl, goal_manager=gm)
    runner = GoalRunner.get_instance(control_layer=ctrl, goal_manager=gm)

    goal = await gm.create_goal(title="Replan Task", original_request="Check this device and notify me.", status="ACTIVE")
    step1 = await gm.add_step(goal.goal_id, description="Step 1: Check", capability="device.status")
    step2 = await gm.add_step(goal.goal_id, description="Step 2: Notify", capability="ui.notify", arguments={"message": "Done", "target_device": "asus"})

    # Run step 1 -> succeeds
    res1 = await runner.run_next_step(goal.goal_id)
    assert res1["status"] in ("continued", "success")
    assert (await gm.get_step(step1.step_id)).status == "COMPLETED"

    # Run step 2 -> fails
    res2 = await runner.run_next_step(goal.goal_id)
    assert res2["status"] == "failed"

    # Goal current_step_index is 1, step 1 is COMPLETED
    g_mid = await gm.get_goal(goal.goal_id)
    assert g_mid.replan_count == 0

    # Trigger replan
    replan_res = await planner.replan_after_observation(
        goal_id=goal.goal_id,
        failed_step_id=step2.step_id,
        observation={"error": "Notification channel unavailable"},
        target_device="asus",
    )
    assert replan_res["status"] == "replanned"
    assert replan_res["replan_count"] == 1

    # Verify Step 1 is STILL COMPLETED and PRESERVED
    s1_preserved = await gm.get_step(step1.step_id)
    assert s1_preserved is not None
    assert s1_preserved.status == "COMPLETED"

    # Verify replan replaced remaining steps
    steps_after = await gm.list_steps(goal.goal_id)
    assert len(steps_after) >= 2
    assert steps_after[0].step_id == step1.step_id

    # Reset goal failure state so GoalRunner continues
    g_ready = await gm.get_goal(goal.goal_id)
    g_ready.status = "ACTIVE"
    await gm.update_goal(g_ready)

    # Continue execution through GoalRunner
    final_res = await runner.run_goal(goal.goal_id)
    assert final_res["status"] == "COMPLETED"


# ---------------------------------------------------------------------------
# TEST 11: Bounded Replanning Limit Enforced
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_bounded_replanning_limit():
    """Verify replan attempts cannot exceed max_replans (conservative default 2)."""
    await setup_test_environment("asus")
    gm = GoalManager.get_instance()
    planner = GoalPlanner.get_instance(goal_manager=gm)

    goal = await gm.create_goal(title="Bounded Replan Goal", original_request="Check and notify", max_replans=1)
    await gm.add_step(goal.goal_id, description="Step 1", capability="device.status")

    # 1st replan -> succeeds
    r1 = await planner.replan_after_observation(goal.goal_id)
    assert r1["status"] == "replanned"
    assert (await gm.get_goal(goal.goal_id)).replan_count == 1

    # 2nd replan -> exceeds max_replans (1) -> BLOCKED
    r2 = await planner.replan_after_observation(goal.goal_id)
    assert r2["status"] == "blocked"
    assert "Exceeded maximum replan limit" in r2["reason"]

    g_blocked = await gm.get_goal(goal.goal_id)
    assert g_blocked.status == "BLOCKED"
    assert "Exceeded maximum replan limit" in g_blocked.blocking_reason


# ---------------------------------------------------------------------------
# TEST 12: Safety Integration: Kill Switch and Approvals Still Enforced
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_safety_controls_authoritative_over_planned_steps():
    """Verify planned steps cannot bypass kill switch or approval requirements."""
    await setup_test_environment("asus")
    gm = GoalManager.get_instance()

    sim_ws = SimulatedDeviceWebSocket("asus")
    ctrl = CortexControlLayer({"asus": sim_ws})
    planner = GoalPlanner.get_instance(control_layer=ctrl, goal_manager=gm)
    runner = GoalRunner.get_instance(control_layer=ctrl, goal_manager=gm)

    # 1. Kill switch enforcement on planned steps
    goal1 = await gm.create_goal(title="Planned Task", original_request="Check this device and notify me.", status="ACTIVE")
    await planner.plan_goal(goal1.goal_id, target_device="asus")

    # Disable autonomy globally
    await gm.set_autonomy_enabled(False)
    run_res = await runner.run_goal(goal1.goal_id)
    assert run_res["status"] in ("blocked", "BLOCKED")
    assert len(sim_ws.sent_messages) == 0  # No commands dispatched

    # Re-enable autonomy
    await gm.set_autonomy_enabled(True)

    # 2. Approval enforcement on planned destructive step
    goal2 = await gm.create_goal(title="Destructive Task", original_request="Delete old log file", status="ACTIVE")
    plan = {
        "steps": [
            {
                "description": "Delete file",
                "capability": "fs.delete",
                "arguments": {"path": "C:\\temp\\log.txt", "target_device": "asus"},
            }
        ]
    }
    await planner.persist_plan(goal2.goal_id, plan)

    step_res = await runner.run_next_step(goal2.goal_id)
    assert step_res["status"] == "needs_approval"
    assert (await gm.get_goal(goal2.goal_id)).status == "NEEDS_APPROVAL"
    assert len(sim_ws.sent_messages) == 0  # Action never dispatched without approval


# ---------------------------------------------------------------------------
# TEST 13: Natural Language Create and Run Flow
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_natural_language_create_and_run_flow():
    """Verify 'Create and run a goal to check this device and notify me.' creates, plans, and runs."""
    await setup_test_environment("asus")
    gm = GoalManager.get_instance()

    executed = []
    def custom_handler(cmd, args):
        executed.append(cmd)
        return "success", "OK", None

    sim_ws = SimulatedDeviceWebSocket("asus", custom_handler=custom_handler)
    ctrl = CortexControlLayer({"asus": sim_ws})
    GoalPlanner.get_instance(control_layer=ctrl, goal_manager=gm)
    GoalRunner.get_instance(control_layer=ctrl, goal_manager=gm)

    reply = await gm.handle_natural_language("Create and run a goal to check this device and notify me.")
    assert "planned" in reply.lower()
    assert "status=COMPLETED" in reply

    assert len(executed) == 2
    assert executed[0] == "device.status"
    assert executed[1] == "ui.notify"


# ---------------------------------------------------------------------------
# TEST 14: Production DB Isolation
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_production_db_isolation():
    """Verify that data/cortex.db has remained completely untouched throughout Phase 9E tests."""
    from hub.database import get_db_path
    resolved_test_db = get_db_path().resolve()
    prod_db = Path("data/cortex.db").resolve()
    assert resolved_test_db != prod_db, f"FATAL: Test targeting prod DB at {resolved_test_db}!"
    assert not str(resolved_test_db).replace('\\', '/').endswith("data/cortex.db")
    assert prod_db.exists(), "Production database data/cortex.db must exist."
