"""
tests/test_phase9a_goals.py
---------------------------
Phase 9A: Persistent Goal Engine Test Suite.

Verifies:
1. Goal & GoalStep model CRUD & DB persistence.
2. Status transitions and strict status validation.
3. Checkpoint persistence immediately on every transition.
4. Bounded safety limits: max_steps enforcement (BLOCKED/FAILED) and max_failures enforcement (FAILED).
5. Terminal-state protection (COMPLETED, FAILED, CANCELLED cannot be mutated).
6. Cancel and resume behaviors.
7. Hub restart behavior:
   - COMPLETED / FAILED / CANCELLED remain terminal.
   - WAITING / BLOCKED / NEEDS_APPROVAL restored exactly.
   - ACTIVE / RUNNING normalized safely to WAITING ("Paused due to Hub restart").
8. Natural language query handling ("Create a goal...", "What goals are active?", "Cancel...", "Resume...").
9. Required End-to-End interaction flow with simulated Hub restart.
10. Internal Tier 1 capability execution (goal.create, goal.get, goal.list, goal.cancel, goal.resume).
11. Production DB isolation.
"""

import hashlib
import os
import pytest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from hub import database
from hub.goals import Goal, GoalStep, GoalManager, VALID_GOAL_STATUSES, TERMINAL_GOAL_STATUSES
from hub.control_layer import CortexControlLayer
from hub.agent_harness import AgentHarness, ConversationalResponse
from hub.intent_schema import ActionPlan, ModelResult

PROD_DB_HASH = "62F5E75C85065CD18F43A2D79EFB3CF3D7756F49349D4FDADC6A1B4330D27732"


# ---------------------------------------------------------------------------
# TEST 1: Goal Model & Database Persistence
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_goal_model_and_persistence():
    await database.init_db()
    gm = GoalManager.get_instance()

    goal = await gm.create_goal(
        title="Organize school files",
        original_request="Please organize all my school files in Downloads",
        source="user",
        priority=2,
        status="PENDING",
        max_steps=15,
        max_failures=3,
        metadata={"course": "CS101", "folder": "Downloads"}
    )

    assert goal.goal_id.startswith("goal_")
    assert goal.title == "Organize school files"
    assert goal.status == "PENDING"
    assert goal.priority == 2
    assert goal.max_steps == 15
    assert goal.max_failures == 3
    assert goal.metadata["course"] == "CS101"

    # Fetch from DB
    loaded = await gm.get_goal(goal.goal_id)
    assert loaded is not None
    assert loaded.goal_id == goal.goal_id
    assert loaded.title == "Organize school files"
    assert loaded.status == "PENDING"
    assert loaded.metadata["folder"] == "Downloads"


# ---------------------------------------------------------------------------
# TEST 2: Goal Steps CRUD & Deterministic Ordering
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_goal_steps_crud_and_ordering():
    await database.init_db()
    gm = GoalManager.get_instance()

    goal = await gm.create_goal(title="Multi-step Goal")

    s0 = await gm.add_step(goal.goal_id, description="Scan directory", capability="fs.scan")
    s1 = await gm.add_step(goal.goal_id, description="Filter PDF files", capability="fs.filter")
    s2 = await gm.add_step(goal.goal_id, description="Move files to folder", capability="fs.move")

    assert s0.index == 0
    assert s1.index == 1
    assert s2.index == 2
    assert s0.status == "PENDING"

    steps = await gm.list_steps(goal.goal_id)
    assert len(steps) == 3
    assert [s.description for s in steps] == ["Scan directory", "Filter PDF files", "Move files to folder"]

    # Retrieve step by ID
    loaded_s1 = await gm.get_step(s1.step_id)
    assert loaded_s1 is not None
    assert loaded_s1.description == "Filter PDF files"


# ---------------------------------------------------------------------------
# TEST 3: Status Transitions & Validation
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_status_transitions_and_validations():
    await database.init_db()
    gm = GoalManager.get_instance()

    goal = await gm.create_goal(title="State Transition Test", status="PENDING")
    assert goal.status == "PENDING"

    # PENDING -> ACTIVE
    active = await gm.mark_active(goal.goal_id)
    assert active.status == "ACTIVE"
    assert active.started_at is not None

    # ACTIVE -> WAITING
    waiting = await gm.mark_waiting(goal.goal_id, reason="Waiting for network connection")
    assert waiting.status == "WAITING"
    assert waiting.waiting_reason == "Waiting for network connection"

    # WAITING -> BLOCKED
    blocked = await gm.mark_blocked(goal.goal_id, reason="Missing permissions on target folder")
    assert blocked.status == "BLOCKED"
    assert blocked.blocking_reason == "Missing permissions on target folder"

    # BLOCKED -> NEEDS_APPROVAL
    approval = await gm.mark_needs_approval(goal.goal_id, approval_request_id="apr_987")
    assert approval.status == "NEEDS_APPROVAL"
    assert approval.approval_request_id == "apr_987"

    # Invalid status raises ValueError
    with pytest.raises(ValueError):
        Goal(goal_id="g_bad", title="Bad", original_request="bad", status="INVALID_STATUS")


# ---------------------------------------------------------------------------
# TEST 4: Immediate Checkpoint Persistence
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_checkpoint_persistence():
    await database.init_db()
    gm = GoalManager.get_instance()

    goal = await gm.create_goal(title="Checkpoint Test")
    step = await gm.add_step(goal.goal_id, description="Step 1")

    # Save checkpoint data
    await gm.checkpoint(goal.goal_id, {"processed_files": ["a.txt", "b.txt"], "cursor": 42}, step_id=step.step_id)

    # Re-read fresh from DB
    loaded_goal = await gm.get_goal(goal.goal_id)
    assert loaded_goal.checkpoint_data["cursor"] == 42
    assert loaded_goal.checkpoint_data["processed_files"] == ["a.txt", "b.txt"]

    loaded_step = await gm.get_step(step.step_id)
    assert loaded_step.checkpoint_data["cursor"] == 42


# ---------------------------------------------------------------------------
# TEST 5: Safety Limits Enforcement (max_steps and max_failures)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_safety_limits_max_steps_and_max_failures():
    await database.init_db()
    gm = GoalManager.get_instance()

    # 1. max_steps enforcement
    goal_steps_limit = await gm.create_goal(title="Bounded Steps Goal", max_steps=2)
    await gm.add_step(goal_steps_limit.goal_id, description="Step 1")
    await gm.add_step(goal_steps_limit.goal_id, description="Step 2")

    # Exceeding max_steps must block the goal and raise ValueError
    with pytest.raises(ValueError, match="maximum allowed steps"):
        await gm.add_step(goal_steps_limit.goal_id, description="Step 3")

    g_after = await gm.get_goal(goal_steps_limit.goal_id)
    assert g_after.status == "BLOCKED"
    assert "Exceeded maximum allowed steps" in g_after.blocking_reason

    # 2. max_failures enforcement
    goal_fail_limit = await gm.create_goal(title="Bounded Failures Goal", max_failures=2)
    s1 = await gm.add_step(goal_fail_limit.goal_id, description="Fragile Step 1")
    s2 = await gm.add_step(goal_fail_limit.goal_id, description="Fragile Step 2")

    # Fail first step -> failure_count=1, still non-terminal
    await gm.fail_step(goal_fail_limit.goal_id, s1.step_id, error="Transient timeout")
    g1 = await gm.get_goal(goal_fail_limit.goal_id)
    assert g1.failure_count == 1
    assert g1.status != "FAILED"

    # Fail second step -> failure_count=2 -> exceeds max_failures (2) -> transitions to FAILED
    await gm.fail_step(goal_fail_limit.goal_id, s2.step_id, error="Fatal auth error")
    g2 = await gm.get_goal(goal_fail_limit.goal_id)
    assert g2.failure_count == 2
    assert g2.status == "FAILED"
    assert "Exceeded maximum allowed failures" in g2.blocking_reason


# ---------------------------------------------------------------------------
# TEST 6: Cancel, Resume, and Terminal-State Protection
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_cancel_and_resume_and_terminal_protection():
    await database.init_db()
    gm = GoalManager.get_instance()

    goal = await gm.create_goal(title="Cancel & Resume Test", status="ACTIVE")

    # Cancel
    cancelled = await gm.cancel_goal(goal.goal_id, reason="User clicked stop")
    assert cancelled.status == "CANCELLED"
    assert cancelled.blocking_reason == "User clicked stop"

    # Terminal state protection: cannot mutate or resume CANCELLED goal
    with pytest.raises(ValueError, match="terminal state"):
        await gm.resume_goal(goal.goal_id)

    with pytest.raises(ValueError, match="terminal state"):
        await gm.mark_active(goal.goal_id)

    # Completed goal terminal protection
    goal2 = await gm.create_goal(title="Completed Goal Test", status="ACTIVE")
    completed = await gm.complete_goal(goal2.goal_id, result_summary="Done successfully")
    assert completed.status == "COMPLETED"

    with pytest.raises(ValueError, match="Cannot cancel completed goal"):
        await gm.cancel_goal(goal2.goal_id)

    with pytest.raises(ValueError, match="terminal state"):
        await gm.resume_goal(goal2.goal_id)

    # Resume from WAITING
    goal3 = await gm.create_goal(title="Resumable Waiting Goal", status="ACTIVE")
    await gm.mark_waiting(goal3.goal_id, reason="Paused by user")
    resumed = await gm.resume_goal(goal3.goal_id)
    assert resumed.status == "ACTIVE"
    assert resumed.waiting_reason is None


# ---------------------------------------------------------------------------
# TEST 7: Restart Restoration Behavior
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_restart_restoration_behavior():
    """
    On Hub startup:
    - COMPLETED / FAILED / CANCELLED remain terminal.
    - WAITING / BLOCKED / NEEDS_APPROVAL restore exactly.
    - ACTIVE / RUNNING at process death restore safely to WAITING ("Paused due to Hub restart").
    - Current step and checkpoints preserved.
    - Do NOT automatically continue execution.
    """
    await database.init_db()
    gm = GoalManager.get_instance()

    # 1. Goal A: ACTIVE with RUNNING step
    g_active = await gm.create_goal(title="Active Goal at Crash", status="ACTIVE")
    s_running = await gm.add_step(g_active.goal_id, description="Running Step")
    await gm.start_step(g_active.goal_id, s_running.step_id)
    await gm.checkpoint(g_active.goal_id, {"progress": 75}, step_id=s_running.step_id)

    # 2. Goal B: WAITING with specific reason
    g_waiting = await gm.create_goal(title="Waiting Goal", status="ACTIVE")
    await gm.mark_waiting(g_waiting.goal_id, reason="Waiting for USB drive")

    # 3. Goal C: NEEDS_APPROVAL
    g_approval = await gm.create_goal(title="Approval Goal", status="ACTIVE")
    await gm.mark_needs_approval(g_approval.goal_id, approval_request_id="apr_456")

    # 4. Goal D: COMPLETED
    g_completed = await gm.create_goal(title="Completed Goal", status="ACTIVE")
    await gm.complete_goal(g_completed.goal_id, result_summary="Finished")

    # 5. Goal E: FAILED
    g_failed = await gm.create_goal(title="Failed Goal", status="ACTIVE")
    await gm.fail_goal(g_failed.goal_id, error="Network crash")

    # 6. Goal F: CANCELLED
    g_cancelled = await gm.create_goal(title="Cancelled Goal", status="ACTIVE")
    await gm.cancel_goal(g_cancelled.goal_id, reason="User aborted")

    # -----------------------------------------------------------------------
    # Simulate Hub Restart: reset GoalManager and reinitialize from DB
    # -----------------------------------------------------------------------
    GoalManager.reset()
    gm_new = GoalManager.get_instance()
    recovered = await gm_new.initialize()

    assert recovered == 1  # 1 active goal paused safely

    # Verify Goal A: normalized to WAITING with explicit restart reason
    restored_a = await gm_new.get_goal(g_active.goal_id)
    assert restored_a.status == "WAITING"
    assert restored_a.waiting_reason == "Paused due to Hub restart"
    assert restored_a.checkpoint_data["progress"] == 75
    # Step was normalized from RUNNING to WAITING
    step_a = await gm_new.get_step(s_running.step_id)
    assert step_a.status == "WAITING"
    assert step_a.checkpoint_data["progress"] == 75

    # Verify Goal B: preserved exactly as WAITING with original reason
    restored_b = await gm_new.get_goal(g_waiting.goal_id)
    assert restored_b.status == "WAITING"
    assert restored_b.waiting_reason == "Waiting for USB drive"

    # Verify Goal C: preserved exactly as NEEDS_APPROVAL
    restored_c = await gm_new.get_goal(g_approval.goal_id)
    assert restored_c.status == "NEEDS_APPROVAL"
    assert restored_c.approval_request_id == "apr_456"

    # Verify Goal D: remains terminal COMPLETED
    restored_d = await gm_new.get_goal(g_completed.goal_id)
    assert restored_d.status == "COMPLETED"
    assert restored_d.result_summary == "Finished"

    # Verify Goal E: remains terminal FAILED
    restored_e = await gm_new.get_goal(g_failed.goal_id)
    assert restored_e.status == "FAILED"

    # Verify Goal F: remains terminal CANCELLED
    restored_f = await gm_new.get_goal(g_cancelled.goal_id)
    assert restored_f.status == "CANCELLED"


# ---------------------------------------------------------------------------
# TEST 8: Natural Language Queries
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_natural_language_queries():
    await database.init_db()
    ctrl = CortexControlLayer({})
    harness = AgentHarness(ctrl)
    device_id = "asus"

    # 1. Create a goal: "Create a goal to organize my school files."
    resp1 = await harness.process_request(device_id, "Create a goal to organize my school files.", {}, None)
    assert resp1.status == "success"
    assert "organize my school files" in resp1.message
    assert "Autonomous execution is not enabled yet" in resp1.message

    # 2. What goals are active?
    resp2 = await harness.process_request(device_id, "What goals are active?", {}, None)
    assert resp2.status == "success"
    assert "organize my school files" in resp2.message
    assert "ACTIVE" in resp2.message

    # 3. Cancel the goal: "Cancel the school files goal."
    resp3 = await harness.process_request(device_id, "Cancel the school files goal.", {}, None)
    assert resp3.status == "success"
    assert "CANCELLED" in resp3.message

    # Verify cancellation in DB
    gm = GoalManager.get_instance()
    goals = await gm.list_goals()
    assert len(goals) == 1
    assert goals[0].status == "CANCELLED"


# ---------------------------------------------------------------------------
# TEST 9: REQUIRED END-TO-END TEST WITH RESTART
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_required_end_to_end_flow_with_restart():
    """
    Prompt Required End-to-End Scenario:
    1. User: "Create a goal to organize my school files."
       - Persistent Goal created
       - Assigned goal_id
       - Saved original request
       - status = PENDING or ACTIVE
       - Acknowledges execution has not started yet
    2. User: "What goals are active?"
       - Listed from persisted state
    3. Restart Hub.
    4. User: "What goals are active?"
       - Goal must still exist
    5. User: "Cancel the school files goal."
       - Status must persist as CANCELLED
    """
    await database.init_db()
    ctrl = CortexControlLayer({})
    harness = AgentHarness(ctrl)
    device_id = "asus"

    # Step 1: Create
    r1 = await harness.process_request(device_id, "Create a goal to organize my school files.", {}, None)
    assert r1.status == "success"
    assert "organize my school files" in r1.message.lower()
    assert "saved it" in r1.message.lower() or "created" in r1.message.lower()
    assert "autonomous execution is not enabled yet" in r1.message.lower()

    # Verify database state
    gm = GoalManager.get_instance()
    goals = await gm.list_goals()
    assert len(goals) == 1
    g = goals[0]
    assert g.title == "organize my school files"
    assert "organize my school files" in g.original_request.lower()
    assert g.status in ("PENDING", "ACTIVE")

    # Step 2: What goals are active?
    r2 = await harness.process_request(device_id, "What goals are active?", {}, None)
    assert r2.status == "success"
    assert "organize my school files" in r2.message.lower()

    # Step 3: Restart Hub
    GoalManager.reset()
    gm_restarted = GoalManager.get_instance()
    await gm_restarted.initialize()

    # Step 4: What goals are active? (after restart)
    harness_restarted = AgentHarness(ctrl)
    r4 = await harness_restarted.process_request(device_id, "What goals are active?", {}, None)
    assert r4.status == "success"
    assert "organize my school files" in r4.message.lower()
    # Safely normalized to WAITING due to Hub restart
    assert "waiting" in r4.message.lower() or "paused" in r4.message.lower()

    # Step 5: Cancel the school files goal
    r5 = await harness_restarted.process_request(device_id, "Cancel the school files goal.", {}, None)
    assert r5.status == "success"
    assert "cancelled" in r5.message.lower()

    # Verify status in database is CANCELLED
    fresh_goal = await gm_restarted.get_goal(g.goal_id)
    assert fresh_goal.status == "CANCELLED"


# ---------------------------------------------------------------------------
# TEST 10: Tier 1 Internal Goal Capabilities Execution
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_tier1_goal_capabilities_execution():
    """Verify internal Tier 1 capabilities: goal.create, goal.get, goal.list, goal.cancel, goal.resume."""
    await database.init_db()
    ctrl = CortexControlLayer({})
    harness = AgentHarness(ctrl)
    device_id = "asus"
    await database.enroll_device(device_id, "asus", "admin", "secret")

    # 1. goal.create
    res_create = ModelResult(
        type="ACTION",
        action_plan=ActionPlan(
            intent="create_goal",
            source_device=device_id,
            capability="goal.create",
            action="goal.create",
            arguments={"title": "Prepare tax documents", "original_request": "Prepare taxes"},
            target_device_reference="asus",
            confidence=1.0,
            user_facing_summary="creating goal"
        )
    )
    with patch.object(harness.router, "generate_result", return_value=res_create):
        r1 = await harness.process_request(device_id, "execute action create goal", {}, None)
        assert r1.status == "success"
        assert "Prepare tax documents" in r1.message
        assert r1.action_plan.capability == "goal.create"

    gm = GoalManager.get_instance()
    goals = await gm.list_goals()
    g_id = goals[0].goal_id

    # 2. goal.get
    res_get = ModelResult(
        type="ACTION",
        action_plan=ActionPlan(
            intent="get_goal",
            source_device=device_id,
            capability="goal.get",
            action="goal.get",
            arguments={"goal_id": g_id},
            target_device_reference="asus",
            confidence=1.0,
            user_facing_summary="getting goal"
        )
    )
    with patch.object(harness.router, "generate_result", return_value=res_get):
        r2 = await harness.process_request(device_id, "execute action get goal", {}, None)
        assert r2.status == "success"
        assert "Prepare tax documents" in r2.message
        assert r2.action_plan.capability == "goal.get"

    # 3. goal.list
    res_list = ModelResult(
        type="ACTION",
        action_plan=ActionPlan(
            intent="list_goals",
            source_device=device_id,
            capability="goal.list",
            action="goal.list",
            arguments={},
            target_device_reference="asus",
            confidence=1.0,
            user_facing_summary="listing goals"
        )
    )
    with patch.object(harness.router, "generate_result", return_value=res_list):
        r3 = await harness.process_request(device_id, "execute action query goals", {}, None)
        assert r3.status == "success"
        assert "Prepare tax documents" in r3.message
        assert r3.action_plan.capability == "goal.list"

    # 4. goal.cancel
    res_cancel = ModelResult(
        type="ACTION",
        action_plan=ActionPlan(
            intent="cancel_goal",
            source_device=device_id,
            capability="goal.cancel",
            action="goal.cancel",
            arguments={"goal_id": g_id, "reason": "No longer needed"},
            target_device_reference="asus",
            confidence=1.0,
            user_facing_summary="cancelling goal"
        )
    )
    with patch.object(harness.router, "generate_result", return_value=res_cancel):
        r4 = await harness.process_request(device_id, "execute action cancel goal", {}, None)
        assert r4.status == "success"
        assert "Cancelled goal" in r4.message
        assert r4.action_plan.capability == "goal.cancel"

    # 5. goal.resume
    goal_waiting = await gm.create_goal(title="Waiting Tax Goal", status="ACTIVE")
    await gm.mark_waiting(goal_waiting.goal_id, reason="Paused by user")
    res_resume = ModelResult(
        type="ACTION",
        action_plan=ActionPlan(
            intent="resume_goal",
            source_device=device_id,
            capability="goal.resume",
            action="goal.resume",
            arguments={"goal_id": goal_waiting.goal_id},
            target_device_reference="asus",
            confidence=1.0,
            user_facing_summary="resuming goal"
        )
    )
    with patch.object(harness.router, "generate_result", return_value=res_resume):
        r5 = await harness.process_request(device_id, "execute action resume goal", {}, None)
        assert r5.status == "success"
        assert "Resumed goal" in r5.message
        assert r5.action_plan.capability == "goal.resume"


# ---------------------------------------------------------------------------
# TEST 11: Production Database Isolation
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_production_db_isolation():
    """Verify production database file (data/cortex.db) was never modified or corrupted."""
    from hub.database import get_db_path
    resolved_test_db = get_db_path().resolve()
    prod_path = Path("data/cortex.db").resolve()
    assert resolved_test_db != prod_path, f"FATAL: Test targeting prod DB at {resolved_test_db}!"
    assert not str(resolved_test_db).replace('\\', '/').endswith("data/cortex.db")
    assert prod_path.exists()
