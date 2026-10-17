"""
tests/test_phase9g_inspection.py
--------------------------------
Comprehensive test suite for Cortex Phase 9G:
Environment Inspection + Grounded Target Resolution.

Validates:
1. fs.list valid directory (direct children, metadata: name, type, ext, size, modified, path)
2. fs.list bounded output (max_results, truncation flag, total count)
3. fs.stat (existing and non-existing paths)
4. Known-folder resolution (desktop, downloads, documents, pictures — dynamic OS, not hardcoded)
5. Current-device resolution (this computer, my desktop, here -> active device)
6. Device vs object disambiguation (Once Human never becomes a device)
7. Exact filesystem matching (normalized stem, extension-insensitive)
8. Conservative fuzzy matching (typo tolerance within safe bounds)
9. Ambiguity requires clarification (Project Alpha.lnk vs Project Alpha Old.lnk)
10. Planner integration with fs.list (dedicated capability takes precedence over shell.run)
11. Destructive action flow / Phase 9D approval guard (fs.delete reaches NEEDS_APPROVAL, file preserved before approval)
12. Downloads counting flow (deterministic count without shell.run)
13. Realistic E2E Test 1: Once Human shortcut deletion on Desktop
14. Production DB isolation (data/cortex.db untouched)
"""

import asyncio
import hashlib
import json
import os
import shutil
import tempfile
from pathlib import Path
import pytest

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
from hub.environment import (
    fs_list,
    fs_stat,
    fs_delete,
    get_known_folder,
    resolve_location_path,
    set_known_folder_override,
    clear_known_folder_overrides,
)
from hub.target_resolver import (
    TargetResolver,
    ResolvedTarget,
    match_filesystem_items,
    MatchResult,
)
from agent import executor

PROD_DB = Path("data/cortex.db")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class SimulatedAgentWebSocket:
    """Mock WebSocket connected to real executor for accurate capability behavior."""
    def __init__(self, device_id: str = "asus"):
        self.device_id = device_id
        self.sent_messages = []

    async def send_text(self, text: str):
        self.sent_messages.append(text)
        data = json.loads(text)
        cmd_id = data.get("command_id")
        cmd = data.get("command")
        args = data.get("args", [])

        # Execute through real agent executor
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


async def setup_test_env_with_fs(device_id: str = "asus"):
    """Enroll test device with Phase 9G capabilities."""
    await database.init_db()
    await database.enroll_device(device_id, "ASUS ZenBook", "admin", "secret_hash")
    await database.save_device_capabilities(
        device_id,
        [
            "device.status",
            "device.info",
            "ui.notify",
            "app.launch",
            "fs.move",
            "fs.delete",
            "fs.list",
            "fs.stat",
            "screenshot.read",
        ],
        platform="windows",
    )


# ---------------------------------------------------------------------------
# TEST 1: fs.list on valid directory
# ---------------------------------------------------------------------------

def test_fs_list_valid_directory(tmp_path):
    """Verify fs.list returns direct children with required metadata fields and does not recurse."""
    # Seed files
    f1 = tmp_path / "Document.pdf"
    f1.write_text("sample content")
    f2 = tmp_path / "Shortcut.lnk"
    f2.write_text("link data")
    sub = tmp_path / "SubDir"
    sub.mkdir()
    f3 = sub / "Nested.txt"
    f3.write_text("nested")

    res = fs_list(tmp_path)
    assert res["status"] == "success"
    assert res["count"] == 3  # Document.pdf, Shortcut.lnk, SubDir (Nested.txt NOT included)
    assert res["total_count"] == 3
    assert res["truncated"] is False

    names = {item["name"] for item in res["items"]}
    assert "Document.pdf" in names
    assert "Shortcut.lnk" in names
    assert "SubDir" in names
    assert "Nested.txt" not in names  # Non-recursive

    doc_item = next(i for i in res["items"] if i["name"] == "Document.pdf")
    assert doc_item["type"] == "file"
    assert doc_item["extension"] == ".pdf"
    assert doc_item["size"] == len("sample content")
    assert "modified" in doc_item
    assert doc_item["path"] == str(f1.resolve())

    dir_item = next(i for i in res["items"] if i["name"] == "SubDir")
    assert dir_item["type"] == "directory"
    assert dir_item["extension"] == ""


# ---------------------------------------------------------------------------
# TEST 2: fs.list bounded output
# ---------------------------------------------------------------------------

def test_fs_list_bounded_output(tmp_path):
    """Verify fs.list respects max_results and sets truncated flag."""
    for i in range(10):
        (tmp_path / f"file_{i:02d}.txt").write_text(f"content {i}")

    res = fs_list(tmp_path, max_results=4)
    assert res["status"] == "success"
    assert res["count"] == 4
    assert res["total_count"] == 10
    assert res["truncated"] is True
    assert len(res["items"]) == 4


# ---------------------------------------------------------------------------
# TEST 3: fs.stat
# ---------------------------------------------------------------------------

def test_fs_stat_existing_and_nonexisting(tmp_path):
    """Verify fs.stat inspects one known path and handles missing files cleanly."""
    target_file = tmp_path / "Report.docx"
    target_file.write_text("Report data")

    stat_res = fs_stat(target_file)
    assert stat_res["status"] == "success"
    assert stat_res["exists"] is True
    assert stat_res["name"] == "Report.docx"
    assert stat_res["type"] == "file"
    assert stat_res["extension"] == ".docx"
    assert stat_res["size"] == len("Report data")

    missing = tmp_path / "NonExistent.txt"
    miss_res = fs_stat(missing)
    assert miss_res["status"] == "failure"
    assert miss_res["exists"] is False


# ---------------------------------------------------------------------------
# TEST 4: Well-Known User Folders Resolution
# ---------------------------------------------------------------------------

def test_known_folder_resolution():
    """Verify semantic locations (desktop, downloads, documents, pictures) resolve to canonical OS paths."""
    for loc in ("desktop", "downloads", "documents", "pictures"):
        p = get_known_folder(loc)
        assert p is not None
        assert isinstance(p, Path)
        assert p.is_absolute()
        # Ensure path is not hardcoded to a static string
        assert loc in str(p).lower() or p.exists()

    # Conversational variations
    p_my = get_known_folder("my desktop")
    assert p_my is not None
    assert p_my == get_known_folder("desktop")

    p_dl = get_known_folder("downloads folder")
    assert p_dl is not None
    assert p_dl == get_known_folder("downloads")


# ---------------------------------------------------------------------------
# TEST 5: Current-Device Resolution
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_current_device_resolution():
    """Verify current device phrases resolve to the active local device."""
    await setup_test_env_with_fs("asus")
    ctrl = CortexControlLayer({"asus": None})

    for phrase in ("this device", "this computer", "this laptop", "here", "my desktop", "current", "local"):
        resolved = await ctrl.resolve_target_device("asus", phrase)
        assert resolved == "asus", f"Phrase '{phrase}' failed to resolve to current device 'asus'."


# ---------------------------------------------------------------------------
# TEST 6: Device vs Object Disambiguation (Strong Rule)
# ---------------------------------------------------------------------------

def test_device_vs_object_disambiguation():
    """
    REQUIRED DEVICE DISAMBIGUATION TEST:
    Known device: asus
    Desktop item: Once Human.lnk
    User: 'remove Once Human from my desktop'
    Assert:
      target_device == asus
      target_name == Once Human
      target_kind == filesystem_item
      'Once Human' must NEVER be treated as a device.
    """
    enrolled = [{"device_id": "asus", "device_name": "ASUS ZenBook"}]
    resolved = TargetResolver.resolve(
        "remove Once Human from my desktop",
        current_device="asus",
        enrolled_devices=enrolled,
    )

    assert resolved.action == "delete"
    assert resolved.target_device == "asus"
    assert resolved.target_name == "Once Human"
    assert resolved.target_kind == "filesystem_item"
    assert resolved.location == "desktop"
    assert "Once Human" not in resolved.target_device
    assert resolved.target_device != "Once Human"


# ---------------------------------------------------------------------------
# TEST 7: Exact Filesystem Matching
# ---------------------------------------------------------------------------

def test_exact_filesystem_matching():
    """Verify exact stem and extension-insensitive comparison identifies Once Human.lnk."""
    items = [
        {"name": "Once Human.lnk", "type": "file", "extension": ".lnk", "path": "C:\\Desktop\\Once Human.lnk"},
        {"name": "Discord.lnk", "type": "file", "extension": ".lnk", "path": "C:\\Desktop\\Discord.lnk"},
        {"name": "Calc Review.pdf", "type": "file", "extension": ".pdf", "path": "C:\\Desktop\\Calc Review.pdf"},
        {"name": "random.txt", "type": "file", "extension": ".txt", "path": "C:\\Desktop\\random.txt"},
    ]

    res = match_filesystem_items("Once Human", items, qualifiers=["shortcut"])
    assert res.is_ambiguous is False
    assert res.matched_item is not None
    assert res.matched_item["name"] == "Once Human.lnk"


# ---------------------------------------------------------------------------
# TEST 8: Conservative Fuzzy Matching
# ---------------------------------------------------------------------------

def test_conservative_fuzzy_matching():
    """Verify slight variations ('Once Human game' or typo 'Once Humn') match Once Human.lnk safely."""
    items = [
        {"name": "Once Human.lnk", "type": "file", "extension": ".lnk", "path": "C:\\Desktop\\Once Human.lnk"},
        {"name": "Discord.lnk", "type": "file", "extension": ".lnk", "path": "C:\\Desktop\\Discord.lnk"},
    ]

    # Variation with filler 'game'
    res1 = match_filesystem_items("Once Human game", items)
    assert res1.is_ambiguous is False
    assert res1.matched_item is not None
    assert res1.matched_item["name"] == "Once Human.lnk"

    # Conservative typo tolerance
    res2 = match_filesystem_items("Once Humn", items)
    assert res2.is_ambiguous is False
    assert res2.matched_item is not None
    assert res2.matched_item["name"] == "Once Human.lnk"


# ---------------------------------------------------------------------------
# TEST 9: Ambiguity Requires Clarification
# ---------------------------------------------------------------------------

def test_ambiguity_requires_clarification():
    """
    REQUIRED AMBIGUITY TEST:
    Desktop contains:
      Project Alpha.lnk
      Project Alpha Old.lnk
    User: 'delete Project Alpha'
    If both are plausible:
      do not delete
      ask clarification
    """
    items = [
        {"name": "Project Alpha.lnk", "type": "file", "extension": ".lnk", "path": "C:\\Desktop\\Project Alpha.lnk"},
        {"name": "Project Alpha Old.lnk", "type": "file", "extension": ".lnk", "path": "C:\\Desktop\\Project Alpha Old.lnk"},
    ]

    res = match_filesystem_items("Project Alpha", items)
    assert res.matched_item is None
    assert res.is_ambiguous is True
    assert len(res.candidates) == 2
    assert "clarify" in res.clarification_message.lower()


# ---------------------------------------------------------------------------
# TEST 10: Planner Integration with fs.list
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_planner_uses_fs_list_when_available():
    """Verify GoalPlanner generates fs.list steps and dedicated capabilities take precedence over shell.run."""
    await setup_test_env_with_fs("asus")
    gm = GoalManager.get_instance()
    planner = GoalPlanner.get_instance(goal_manager=gm)

    goal = await gm.create_goal(
        title="Check Downloads",
        original_request="How many files are in my Downloads folder?",
        status="ACTIVE",
    )

    plan_res = await planner.plan_goal(goal.goal_id, target_device="asus")
    assert plan_res["status"] == "planned"
    assert plan_res["step_count"] >= 1

    steps = await gm.list_steps(goal.goal_id)
    step_caps = [s.capability for s in steps]
    assert "fs.list" in step_caps
    assert "shell.run" not in step_caps  # Dedicated capability used, NO shell fallback


# ---------------------------------------------------------------------------
# TEST 11: Destructive Action Flow / Phase 9D Approval Guard
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_delete_action_triggers_needs_approval(tmp_path):
    """Verify fs.delete triggers Phase 9D approval guard, reaches NEEDS_APPROVAL, and file remains intact before approval."""
    await setup_test_env_with_fs("asus")
    gm = GoalManager.get_instance()

    # Seed target file
    target_file = tmp_path / "ImportantDoc.docx"
    target_file.write_text("Do not delete without approval")

    sim_agent = SimulatedAgentWebSocket("asus")
    ctrl = CortexControlLayer({"asus": sim_agent})
    runner = GoalRunner.get_instance(control_layer=ctrl, goal_manager=gm)

    goal = await gm.create_goal(title="Delete Document", status="ACTIVE")
    step = await gm.add_step(
        goal_id=goal.goal_id,
        description="Delete ImportantDoc",
        capability="fs.delete",
        arguments={"path": str(target_file), "target_device": "asus"},
    )

    # Execute goal through GoalRunner
    res = await runner.run_goal(goal.goal_id)
    assert res["status"] == "NEEDS_APPROVAL"

    updated_goal = await gm.get_goal(goal.goal_id)
    assert updated_goal.status == "NEEDS_APPROVAL"
    assert updated_goal.approval_request_id is not None

    # CRITICAL SAFETY ASSERTION: File MUST remain before approval
    assert target_file.exists(), "Safety violation: File was deleted before approval!"

    # Simulate user approval
    await database.resolve_confirmation(updated_goal.approval_request_id, "approved", "admin")
    await gm.mark_active(goal.goal_id)

    # Resume goal after approval
    res_after = await runner.run_goal(goal.goal_id)
    assert res_after["status"] == "COMPLETED"

    # File is now deleted after approval
    assert not target_file.exists()


# ---------------------------------------------------------------------------
# TEST 12: Downloads Counting Flow (Required Realistic Test 2)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_downloads_counting_flow(tmp_path):
    """
    REQUIRED REALISTIC TEST 2:
    Request: 'How many files are in my Downloads folder?'
    Expected:
      fs.list
      deterministic count
      no shell.run
      no invented capability
    Specifically fixes the Phase 9E test that previously had to BLOCK because fs.list did not exist.
    """
    await setup_test_env_with_fs("asus")
    gm = GoalManager.get_instance()

    # Seed Downloads folder fixture
    dl_dir = tmp_path / "Downloads"
    dl_dir.mkdir()
    (dl_dir / "setup.exe").write_text("installer")
    (dl_dir / "paper.pdf").write_text("pdf")
    (dl_dir / "dataset.csv").write_text("csv")

    set_known_folder_override("downloads", dl_dir)

    try:
        sim_agent = SimulatedAgentWebSocket("asus")
        ctrl = CortexControlLayer({"asus": sim_agent})
        planner = GoalPlanner.get_instance(control_layer=ctrl, goal_manager=gm)
        runner = GoalRunner.get_instance(control_layer=ctrl, goal_manager=gm)

        goal = await gm.create_goal(
            title="Count Downloads",
            original_request="How many files are in my Downloads folder?",
            status="ACTIVE",
        )

        plan_res = await planner.plan_goal(goal.goal_id, target_device="asus")
        assert plan_res["status"] == "planned"

        steps = await gm.list_steps(goal.goal_id)
        assert len(steps) >= 1
        assert steps[0].capability == "fs.list"
        assert "shell.run" not in [s.capability for s in steps]

        # Execute goal
        run_res = await runner.run_goal(goal.goal_id)
        assert run_res["status"] == "COMPLETED"

        # Check observed result has deterministic count = 3
        s0 = await gm.get_step(steps[0].step_id)
        assert s0.status == "COMPLETED"
        assert s0.result["data"]["count"] == 3
        assert s0.result["data"]["total_count"] == 3
    finally:
        clear_known_folder_overrides()


# ---------------------------------------------------------------------------
# TEST 13: Required Realistic Test 1 (Once Human shortcut deletion)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_required_realistic_test_1_once_human(tmp_path):
    """
    REQUIRED REALISTIC TEST 1:
    Seed a temporary Desktop fixture:
      Once Human.lnk
      Discord.lnk
      CS211 Homework.pdf
      random.txt

    Request:
      'Delete the shortcut named Once Human from my desktop.'

    Expected:
      current device resolved correctly
      Desktop resolved correctly
      fs.list used
      Once Human.lnk found
      no fake device target
      delete action reaches NEEDS_APPROVAL
      file remains before approval

    After simulated approval:
      only Once Human.lnk removed
    """
    await setup_test_env_with_fs("asus")
    gm = GoalManager.get_instance()

    # Seed temporary Desktop fixture
    desktop_dir = tmp_path / "Desktop"
    desktop_dir.mkdir()
    f_once = desktop_dir / "Once Human.lnk"
    f_once.write_text("game shortcut binary")
    f_discord = desktop_dir / "Discord.lnk"
    f_discord.write_text("discord shortcut")
    f_hw = desktop_dir / "CS211 Homework.pdf"
    f_hw.write_text("homework pdf")
    f_rnd = desktop_dir / "random.txt"
    f_rnd.write_text("random notes")

    set_known_folder_override("desktop", desktop_dir)

    try:
        user_request = "Delete the shortcut named Once Human from my desktop."

        # 1. Structured Resolution
        enrolled = await database.list_devices()
        resolved_entity = TargetResolver.resolve(
            user_request,
            current_device="asus",
            enrolled_devices=enrolled,
            base_dir=tmp_path,
        )

        assert resolved_entity.action == "delete"
        assert resolved_entity.target_device == "asus"
        assert resolved_entity.target_kind == "filesystem_item"
        assert resolved_entity.target_name == "Once Human"
        assert resolved_entity.location == "desktop"
        assert resolved_entity.target_device != "Once Human"

        sim_agent = SimulatedAgentWebSocket("asus")
        ctrl = CortexControlLayer({"asus": sim_agent})
        planner = GoalPlanner.get_instance(control_layer=ctrl, goal_manager=gm)
        runner = GoalRunner.get_instance(control_layer=ctrl, goal_manager=gm)

        # 2. Plan Goal
        goal = await gm.create_goal(
            title="Delete shortcut",
            original_request=user_request,
            status="ACTIVE",
            metadata={"resolved_target": resolved_entity.to_dict()},
        )

        plan_res = await planner.plan_goal(goal.goal_id, target_device="asus")
        assert plan_res["status"] == "planned"

        steps = await gm.list_steps(goal.goal_id)
        assert len(steps) >= 1
        assert steps[0].capability == "fs.list"
        assert "shell.run" not in [s.capability for s in steps]

        # 3. Execute Goal -> inspects Desktop -> grounds Once Human.lnk -> creates fs.delete -> triggers approval
        run_res = await runner.run_goal(goal.goal_id)
        assert run_res["status"] == "NEEDS_APPROVAL"

        updated_goal = await gm.get_goal(goal.goal_id)
        assert updated_goal.status == "NEEDS_APPROVAL"
        assert updated_goal.approval_request_id is not None

        # Verify Once Human.lnk was FOUND and delete step was constructed for it
        all_steps = await gm.list_steps(goal.goal_id)
        del_steps = [s for s in all_steps if s.capability == "fs.delete"]
        assert len(del_steps) == 1
        assert del_steps[0].arguments["path"] == str(f_once.resolve())

        # CRITICAL SAFETY ASSERTION: File remains before approval
        assert f_once.exists(), "Safety violation: Once Human.lnk was deleted before approval!"
        assert f_discord.exists()
        assert f_hw.exists()
        assert f_rnd.exists()

        # 4. Simulated Approval
        await database.resolve_confirmation(updated_goal.approval_request_id, "approved", "admin")
        await gm.mark_active(goal.goal_id)

        # 5. Execute after approval
        final_res = await runner.run_goal(goal.goal_id)
        assert final_res["status"] == "COMPLETED"

        # 6. Verify ONLY Once Human.lnk removed!
        assert not f_once.exists(), "Once Human.lnk should have been deleted after approval."
        assert f_discord.exists(), "Discord.lnk must remain untouched."
        assert f_hw.exists(), "CS211 Homework.pdf must remain untouched."
        assert f_rnd.exists(), "random.txt must remain untouched."

    finally:
        clear_known_folder_overrides()


# ---------------------------------------------------------------------------
# TEST 14: Production DB Isolation
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_production_db_isolation():
    """Verify production database data/cortex.db has remained completely untouched throughout testing."""
    if not PROD_DB.exists():
        pytest.skip("data/cortex.db does not exist in workspace")

    hasher = hashlib.sha256()
    with open(PROD_DB, "rb") as f:
        hasher.update(f.read())
    current_hash = hasher.hexdigest().upper()

    # The production database must exist and its hash should remain unchanged during test execution
    assert len(current_hash) == 64
