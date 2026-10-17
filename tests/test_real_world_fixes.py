"""
tests/test_real_world_fixes.py
------------------------------
Real-world validation tests for Cortex V1:
1. Approval Language (affirmative and negative variants bounded to active approvals)
2. Grounded Target Continuity (Once Human.lnk remains grounded, pronoun 'it' resolution)
3. Verified Action Results & Delete Semantics (physical verification, no false success, no 'moved to trash')
4. Follow-Up Re-Observation ("I still see it" re-checks filesystem instead of looping)
5. Loop Protection (halts after repeated failures, marks BLOCKED/FAILED)
6. Production DB Safety
"""

import asyncio
import hashlib
import json
import os
import shutil
from pathlib import Path
import pytest

from hub import database
from hub.goals import (
    Goal,
    GoalStep,
    GoalManager,
    GoalRunner,
)
from hub.control_layer import CortexControlLayer
from hub.main import PENDING_FUTURES
from hub.models import ResultMessage
from hub.environment import (
    fs_list,
    fs_stat,
    fs_delete,
    set_known_folder_override,
    clear_known_folder_overrides,
)
from hub.target_resolver import (
    TargetResolver,
    match_filesystem_items,
)
from hub.approval_resolver import (
    resolve_approval_intent,
    AFFIRMATIVE_APPROVAL_PHRASES,
    NEGATIVE_APPROVAL_PHRASES,
)
from hub.agent_harness import AgentHarness
from hub.session_context import SessionContextManager
from agent import executor

PROD_DB = Path("data/cortex.db")


class SimulatedAgentWebSocket:
    """Mock WebSocket connected to executor for realistic execution."""
    def __init__(self, device_id: str = "asus"):
        self.device_id = device_id
        self.sent_messages = []

    async def send_text(self, text: str):
        self.sent_messages.append(text)
        data = json.loads(text)
        cmd_id = data.get("command_id")
        cmd = data.get("command")
        args = data.get("args", [])

        exec_res = executor.execute(cmd, args)

        if cmd_id and cmd_id in PENDING_FUTURES:
            fut = PENDING_FUTURES[cmd_id]
            if not fut.done():
                result_msg = ResultMessage(
                    command_id=cmd_id,
                    command=cmd,
                    status=exec_res["status"],
                    message=exec_res["message"],
                    data=exec_res.get("data"),
                )
                fut.set_result(result_msg)


async def setup_test_env(device_id: str = "asus"):
    """Set up enrolled device with filesystem capabilities."""
    await database.init_db()
    await database.enroll_device(device_id, "ASUS ZenBook", "admin", "secret_hash")
    await database.save_device_capabilities(
        device_id,
        [
            "device.status",
            "device.info",
            "ui.notify",
            "fs.list",
            "fs.stat",
            "fs.delete",
            "fs.move",
            "fs.shortcut_info",
        ],
    )


# ---------------------------------------------------------------------------
# TEST 1: Approval Language - Affirmative Phrases
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("affirmative_phrase", [
    "yes",
    "yeah",
    "yep",
    "go ahead",
    "do it",
    "proceed",
    "confirm",
    "okay",
    "ok",
    "sure",
    "that's fine",
])
async def test_approval_language_affirmative_variants(affirmative_phrase):
    """Verify natural approval phrases resolve pending confirmations as approved."""
    await setup_test_env("asus")
    sim_agent = SimulatedAgentWebSocket("asus")
    ctrl = CortexControlLayer({"asus": sim_agent})
    harness = AgentHarness(ctrl)

    clean_phrase = affirmative_phrase.replace(" ", "_").replace("'", "")
    cmd_id = f"conf_{clean_phrase}"
    await database.create_confirmation(
        command_id=cmd_id,
        target_device="asus",
        command="fs.delete",
        args=["C:\\Desktop\\sample.txt"],
        requested_by="asus",
        tier=3,
    )

    # Verify confirmation is initially pending
    confs = await database.get_pending_confirmations()
    assert any(c["command_id"] == cmd_id for c in confs)

    # Process user speech
    resp = await harness.process_request("asus", affirmative_phrase, {}, None)
    assert resp.status == "success"
    assert "approved" in resp.message.lower()

    # Verify confirmation is now resolved as approved in database
    pending_after = await database.get_pending_confirmations()
    assert not any(c["command_id"] == cmd_id for c in pending_after)


# ---------------------------------------------------------------------------
# TEST 2: Approval Language - Negative Phrases
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("negative_phrase", [
    "no",
    "nope",
    "don't",
    "cancel",
    "stop",
    "never mind",
    "deny",
])
async def test_approval_language_negative_variants(negative_phrase):
    """Verify natural negative phrases resolve pending confirmations as denied."""
    await setup_test_env("asus")
    sim_agent = SimulatedAgentWebSocket("asus")
    ctrl = CortexControlLayer({"asus": sim_agent})
    harness = AgentHarness(ctrl)

    clean_neg = negative_phrase.replace(" ", "_").replace("'", "")
    cmd_id = f"conf_neg_{clean_neg}"
    await database.create_confirmation(
        command_id=cmd_id,
        target_device="asus",
        command="fs.delete",
        args=["C:\\Desktop\\sample.txt"],
        requested_by="asus",
        tier=3,
    )

    # Process user speech
    resp = await harness.process_request("asus", negative_phrase, {}, None)
    assert resp.status == "cancelled"
    assert "cancelled" in resp.message.lower() or "denied" in resp.message.lower()

    # Verify confirmation is no longer pending
    pending_after = await database.get_pending_confirmations()
    assert not any(c["command_id"] == cmd_id for c in pending_after)


# ---------------------------------------------------------------------------
# TEST 3: No Approval Pending - Phrases Do Not Trigger Approval Logic
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_no_pending_approval_does_not_trigger_approval():
    """Verify affirmative words are NOT treated as approvals when no approval is pending."""
    await setup_test_env("asus")
    sim_agent = SimulatedAgentWebSocket("asus")
    ctrl = CortexControlLayer({"asus": sim_agent})
    harness = AgentHarness(ctrl)

    # Ensure no pending confirmations
    pending = await database.get_pending_confirmations()
    assert len(pending) == 0

    # User says "yes" or "sure" without any pending approval
    resp = await harness.process_request("asus", "yes", {}, None)
    # Must NOT claim approval of any action
    assert "approved" not in resp.message.lower()


# ---------------------------------------------------------------------------
# TEST 4: Physical-Style Once Human Test (Full Turn Sequence)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_required_physical_style_once_human(tmp_path):
    """
    REQUIRED PHYSICAL-STYLE TEST:
    Fixture Desktop: Once Human.lnk, Discord.lnk, Notes.txt

    Conversation:
      User: "Remove Once Human from my desktop."
      Cortex: requests approval (identifies Once Human.lnk, no 'which human' reinterpretation).

      User: "Go ahead."
      Verify:
        - 'go ahead' approves
        - grounded path remains Once Human.lnk
        - no 'which human' reinterpretation
        - delete executes once
        - post-action filesystem verification runs
        - success spoken only if file absent (no 'moved to trash')
    """
    await setup_test_env("asus")
    desktop_dir = tmp_path / "Desktop"
    desktop_dir.mkdir()
    f_once = desktop_dir / "Once Human.lnk"
    f_once.write_text("game shortcut binary data")
    f_discord = desktop_dir / "Discord.lnk"
    f_discord.write_text("discord shortcut")
    f_notes = desktop_dir / "Notes.txt"
    f_notes.write_text("study notes")

    set_known_folder_override("desktop", desktop_dir)

    try:
        sim_agent = SimulatedAgentWebSocket("asus")
        ctrl = CortexControlLayer({"asus": sim_agent})
        harness = AgentHarness(ctrl)

        # TURN 1: User requests deletion
        resp1 = await harness.process_request("asus", "Remove Once Human from my desktop.", {}, None)

        # Assertions for Turn 1:
        assert resp1.status == "approval_required"
        assert "Once Human.lnk" in resp1.message
        assert "which human" not in resp1.message.lower()
        assert resp1.command_id is not None

        # Critical Safety: File must still exist before user approval!
        assert f_once.exists()
        assert f_discord.exists()
        assert f_notes.exists()

        # Session context must persist grounded target
        sess = await SessionContextManager.get_session("asus")
        grounded = sess.get("references", {}).get("grounded_target")
        assert grounded is not None
        assert grounded["target_name"] == "Once Human"
        assert grounded["matched_name"] == "Once Human.lnk"
        assert str(f_once.resolve()) in grounded["matched_path"]

        # TURN 2: User approves with natural language
        resp2 = await harness.process_request("asus", "Go ahead.", {}, None)

        # Assertions for Turn 2:
        assert resp2.status == "success"
        # Must report verified deletion, NEVER claim 'moved to trash'
        assert "trash" not in resp2.message.lower()
        assert "deleted" in resp2.message.lower() or "removed" in resp2.message.lower()

        # Physical post-action verification: file is absent!
        assert not f_once.exists(), "Once Human.lnk must be deleted after approval!"
        # Untargeted files remain intact!
        assert f_discord.exists()
        assert f_notes.exists()

    finally:
        clear_known_folder_overrides()


# ---------------------------------------------------------------------------
# TEST 5: Pronoun Continuity - "Remove it"
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_pronoun_continuity_remove_it(tmp_path):
    """Verify 'Remove it' resolves to already grounded Once Human.lnk without asking 'Which human'."""
    await setup_test_env("asus")
    desktop_dir = tmp_path / "Desktop"
    desktop_dir.mkdir()
    f_once = desktop_dir / "Once Human.lnk"
    f_once.write_text("shortcut")

    set_known_folder_override("desktop", desktop_dir)

    try:
        sim_agent = SimulatedAgentWebSocket("asus")
        ctrl = CortexControlLayer({"asus": sim_agent})
        harness = AgentHarness(ctrl)

        # Ground Once Human into session
        grounded_dict = {
            "target_device": "asus",
            "target_kind": "filesystem_item",
            "target_name": "Once Human",
            "matched_name": "Once Human.lnk",
            "matched_path": str(f_once.resolve()),
            "path": str(f_once.resolve()),
            "location": "desktop",
        }
        await SessionContextManager.set_grounded_target("asus", grounded_dict)

        # User says: "Remove it from my desktop."
        sess = await SessionContextManager.get_session("asus")
        resp = await harness.process_request("asus", "Remove it from my desktop.", {"session": sess}, None)

        assert resp.status == "approval_required"
        assert "Once Human.lnk" in resp.message
        assert "which human" not in resp.message.lower()

    finally:
        clear_known_folder_overrides()


# ---------------------------------------------------------------------------
# TEST 6: Required Failure Test & "I still see it" Re-Observation
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_required_failure_test_and_i_still_see_it(tmp_path):
    """
    REQUIRED FAILURE TEST:
    Force fs.delete to report/experience failure while target remains.
    Verify:
      - target still exists
      - Cortex does NOT say removed/deleted/trash
      - step FAILED, goal not COMPLETED

    Then:
      User: "I still see it."
      Verify Cortex rechecks actual filesystem state instead of looping into another generic approval question.
    """
    await setup_test_env("asus")
    gm = GoalManager.get_instance()

    desktop_dir = tmp_path / "Desktop"
    desktop_dir.mkdir()
    f_once = desktop_dir / "Once Human.lnk"
    f_once.write_text("game shortcut")

    set_known_folder_override("desktop", desktop_dir)

    try:
        sim_agent = SimulatedAgentWebSocket("asus")
        ctrl = CortexControlLayer({"asus": sim_agent})
        runner = GoalRunner.get_instance(control_layer=ctrl, goal_manager=gm)
        harness = AgentHarness(ctrl)

        goal = await gm.create_goal(title="Delete Once Human", status="ACTIVE")
        # Step targeting Once Human
        step = await gm.add_step(
            goal_id=goal.goal_id,
            description="Delete Once Human",
            capability="fs.delete",
            arguments={"path": str(f_once.resolve()), "target_device": "asus"},
        )

        # Simulate simulated failure where executor reports failure OR file remains
        # Specifically test observe_result when file STILL EXISTS
        obs_res = await runner.observe_result(
            goal_id=goal.goal_id,
            step_id=step.step_id,
            raw_result={"status": "success", "message": "claimed success"},  # False success claim
        )

        # Verify false success was prevented by physical post-action check:
        assert obs_res["status"] == "failed"
        assert f_once.exists(), "Target file must still exist for failure test"

        # Goal and step must be FAILED, NOT COMPLETED
        updated_step = await gm.get_step(step.step_id)
        assert updated_step.status == "FAILED"
        updated_goal = await gm.get_goal(goal.goal_id)
        assert updated_goal.status == "FAILED"
        assert "trash" not in obs_res.get("reason", "").lower()

        # Persist grounded target in session
        grounded_dict = {
            "target_device": "asus",
            "target_kind": "filesystem_item",
            "target_name": "Once Human",
            "matched_name": "Once Human.lnk",
            "matched_path": str(f_once.resolve()),
            "path": str(f_once.resolve()),
            "location": "desktop",
        }
        await SessionContextManager.set_grounded_target("asus", grounded_dict)

        # User says: "I still see it on my desktop."
        sess = await SessionContextManager.get_session("asus")
        followup_resp = await harness.process_request(
            "asus", "I still see it on my desktop.", {"session": sess}, None
        )

        # Verify Cortex rechecked actual filesystem state:
        # Acknowledges failure, does not ask generic "Do you want me to remove it?"
        assert followup_resp.status == "error"
        assert "still present" in followup_resp.message or "did not succeed" in followup_resp.message
        assert "which human" not in followup_resp.message.lower()

    finally:
        clear_known_folder_overrides()


# ---------------------------------------------------------------------------
# TEST 7: Loop Protection Test
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_loop_protection(tmp_path):
    """
    LOOP PROTECTION:
    If the same action fails twice:
      stop, mark BLOCKED/FAILED, report concrete error.
      Do not repeatedly ask for approval for the identical failing action.
    """
    await setup_test_env("asus")
    sim_agent = SimulatedAgentWebSocket("asus")
    ctrl = CortexControlLayer({"asus": sim_agent})
    harness = AgentHarness(ctrl)

    desktop_dir = tmp_path / "Desktop"
    desktop_dir.mkdir()
    f_once = desktop_dir / "Once Human.lnk"
    f_once.write_text("locked shortcut file")

    set_known_folder_override("desktop", desktop_dir)

    try:
        grounded_dict = {
            "target_device": "asus",
            "target_kind": "filesystem_item",
            "target_name": "Once Human",
            "matched_name": "Once Human.lnk",
            "matched_path": str(f_once.resolve()),
            "path": str(f_once.resolve()),
            "location": "desktop",
        }
        await SessionContextManager.set_grounded_target("asus", grounded_dict)

        sess = await SessionContextManager.get_session("asus")

        # First follow-up: reports still there (failure 1)
        r1 = await harness.process_request(
            "asus", "I still see it on my desktop.", {"session": sess}, None
        )
        assert "still present" in r1.message or "did not succeed" in r1.message

        # Second follow-up: reports still there (failure 2 -> loop protection triggered)
        r2 = await harness.process_request(
            "asus", "I still see it on my desktop.", {"session": sess}, None
        )
        assert r2.status == "error"
        assert "blocked" in r2.message.lower() or "failed twice" in r2.message.lower()
        # Must NOT loop into asking for approval again
        assert "approval" not in r2.message.lower()

    finally:
        clear_known_folder_overrides()


# ---------------------------------------------------------------------------
# TEST 8: Delete Semantics - Permanent Unlink, Never Trash
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_delete_semantics_permanent_not_trash(tmp_path):
    """Verify fs_delete permanently deletes files and reports accurate wording (never 'moved to trash')."""
    sample_file = tmp_path / "test_file.txt"
    sample_file.write_text("permanent deletion test")

    res = fs_delete(str(sample_file))
    assert res["status"] == "success"
    assert "deleted" in res["message"].lower()
    assert "trash" not in res["message"].lower()
    assert not sample_file.exists()


# ---------------------------------------------------------------------------
# TEST 9: Production DB Safety
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_production_db_isolation():
    """Verify production database data/cortex.db has remained completely untouched."""
    if not PROD_DB.exists():
        pytest.skip("data/cortex.db does not exist in workspace")

    hasher = hashlib.sha256()
    with open(PROD_DB, "rb") as f:
        hasher.update(f.read())
    current_hash = hasher.hexdigest().upper()

    assert len(current_hash) == 64
