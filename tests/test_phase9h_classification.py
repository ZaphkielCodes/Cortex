"""
tests/test_phase9h_classification.py
------------------------------------
Comprehensive test suite for Cortex Phase 9H:
Semantic Collection Classification + Batch Action Planning.

Validates:
1. fs.shortcut_info capability (read-only inspection of .lnk files, safe metadata extraction)
2. CollectionClassifier structured output (keep, remove, unsure, confidence, explanations)
3. No hardcoding: semantic classification on novel course codes, titles, and junk
4. Mixed Directory test:
   Seed: Fortnite.lnk, chemistry_notes.pdf, resume.docx, meme.png, installer.exe, project_folder/
   Goal: "Keep school stuff and games. Remove unrelated junk."
5. Ambiguous Item test:
   Seed: notes.pdf
   Goal: "Keep college stuff, remove everything else." -> unsure, 0 deletes, BLOCKED with clarification
6. Batch destructive approval enforcement (no delete before approval, single grouped approval)
7. Post-action verification (compares actual final state against intended keep/remove decisions)
8. End-to-end Messy Desktop cleanup in isolated sandbox
9. Production DB safety (SHA-256 invariant)
"""

import asyncio
import hashlib
import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any, Dict, List
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
    fs_shortcut_info,
    set_known_folder_override,
    clear_known_folder_overrides,
)
from hub.collection_classifier import (
    CollectionClassifier,
    ClassifiedItem,
    ClassificationResult,
)
from hub.target_resolver import TargetResolver

PROD_DB_PATH = Path("data/cortex.db")


def get_prod_db_hash() -> str:
    if PROD_DB_PATH.exists():
        return hashlib.sha256(PROD_DB_PATH.read_bytes()).hexdigest().upper()
    return "NONE"


@pytest.fixture(autouse=True)
def assert_prod_db_untouched():
    """Safety fixture ensuring data/cortex.db is strictly untouched across every test."""
    initial_hash = get_prod_db_hash()
    yield
    final_hash = get_prod_db_hash()
    assert final_hash == initial_hash, (
        f"CRITICAL SAFETY VIOLATION: Production database hash changed from {initial_hash} to {final_hash}"
    )


@pytest.fixture
def isolated_env():
    """Sets up an isolated SQLite database and workspace sandbox for each test."""
    initial_hash = get_prod_db_hash()
    orig_env = os.environ.get("CORTEX_DB_PATH")
    tmp_dir = tempfile.mkdtemp(prefix="cortex_test_phase9h_")
    db_file = Path(tmp_dir) / "test_cortex.db"
    os.environ["CORTEX_DB_PATH"] = str(db_file)

    desktop_dir = Path(tmp_dir) / "Desktop"
    desktop_dir.mkdir(parents=True, exist_ok=True)
    set_known_folder_override("desktop", desktop_dir)

    # Reset GoalManager singleton
    GoalManager._instance = None

    async def _setup():
        await database.init_db()
        gm = GoalManager.get_instance()
        await gm.set_autonomy_enabled(True)
        return gm, tmp_dir, desktop_dir

    loop = asyncio.get_event_loop()
    gm, tmp_dir_path, desktop_path = loop.run_until_complete(_setup())

    yield {
        "gm": gm,
        "tmp_dir": tmp_dir_path,
        "desktop": desktop_path,
        "db_file": str(db_file),
    }

    clear_known_folder_overrides()
    GoalManager._instance = None
    loop.run_until_complete(database.close_db())

    if orig_env:
        os.environ["CORTEX_DB_PATH"] = orig_env
    else:
        os.environ.pop("CORTEX_DB_PATH", None)

    shutil.rmtree(tmp_dir, ignore_errors=True)
    final_hash = get_prod_db_hash()
    assert final_hash == initial_hash


# ---------------------------------------------------------------------------
# 1. Shortcut Metadata Inspection (fs.shortcut_info)
# ---------------------------------------------------------------------------
def test_fs_shortcut_info_capability(isolated_env):
    """Verifies fs.shortcut_info extracts target metadata safely without executing."""
    desktop = isolated_env["desktop"]
    test_lnk = desktop / "TestApp.lnk"
    test_lnk.write_text("dummy lnk binary data", encoding="utf-8")

    info = fs_shortcut_info(str(test_lnk))
    assert info["status"] == "success"
    assert info["display_name"] == "TestApp"
    assert "target_path" in info
    assert "target_executable" in info
    assert "arguments" in info
    assert info["is_shortcut"] is True

    # Test non-existent path
    non_existent = fs_shortcut_info(str(desktop / "Missing.lnk"))
    assert non_existent["status"] == "failure"


# ---------------------------------------------------------------------------
# 2. Structured Output & Semantic Classification Format
# ---------------------------------------------------------------------------
def test_collection_classifier_structured_output():
    """Validates structured keep/remove/unsure output with confidence and reasons."""
    items = [
        {"name": "GameClient.lnk", "path": "C:/Desktop/GameClient.lnk", "is_dir": False},
        {"name": "PHYS101 Syllabus.pdf", "path": "C:/Desktop/PHYS101 Syllabus.pdf", "is_dir": False},
        {"name": "junk_installer.exe", "path": "C:/Desktop/junk_installer.exe", "is_dir": False},
        {"name": "unknown_file.xyz", "path": "C:/Desktop/unknown_file.xyz", "is_dir": False},
    ]
    goal = "Clean up desktop. Keep games and college coursework, remove everything else."
    res = CollectionClassifier.classify(user_goal=goal, items=items, location="desktop")

    assert isinstance(res, ClassificationResult)
    assert len(res.items) == 4
    d = res.to_dict()
    assert "items" in d and "keep" in d and "remove" in d and "unsure" in d

    for item in res.items:
        assert isinstance(item, ClassifiedItem)
        assert item.decision in ("keep", "remove", "unsure")
        assert 0.0 <= item.confidence <= 1.0
        assert len(item.reason) > 5


# ---------------------------------------------------------------------------
# 3. No Hardcoding: General Semantics
# ---------------------------------------------------------------------------
def test_no_hardcoding_general_semantics():
    """Ensures classification works on novel course codes, titles, and software."""
    items = [
        {"name": "BIO350 MolecularGenetics.pdf", "path": "C:/BIO350.pdf", "is_dir": False},
        {"name": "MATH220 MidtermPrep.docx", "path": "C:/MATH220.docx", "is_dir": False},
        {"name": "Cyberpunk2077.lnk", "path": "C:/Cyberpunk.lnk", "is_dir": False},
        {"name": "EldenRing.lnk", "path": "C:/EldenRing.lnk", "is_dir": False},
        {"name": "setup_v3.4.1.exe", "path": "C:/setup.exe", "is_dir": False},
        {"name": "screenshot_temp_99.png", "path": "C:/screenshot.png", "is_dir": False},
    ]
    goal = "Clean up my desktop. Keep games and college stuff, remove everything else."
    res = CollectionClassifier.classify(user_goal=goal, items=items)

    keep_names = {it.name for it in res.keep}
    remove_names = {it.name for it in res.remove}

    assert "BIO350 MolecularGenetics.pdf" in keep_names
    assert "MATH220 MidtermPrep.docx" in keep_names
    assert "Cyberpunk2077.lnk" in keep_names
    assert "EldenRing.lnk" in keep_names
    assert "setup_v3.4.1.exe" in remove_names
    assert "screenshot_temp_99.png" in remove_names


# ---------------------------------------------------------------------------
# 4. Required Test: Mixed Directory
# ---------------------------------------------------------------------------
def test_mixed_directory_classification():
    """
    Seed:
    Fortnite.lnk
    chemistry_notes.pdf
    resume.docx
    meme.png
    installer.exe
    project_folder/

    Goal:
    "Keep school stuff and games. Remove unrelated junk."
    """
    items = [
        {"name": "Fortnite.lnk", "path": "C:/Desktop/Fortnite.lnk", "is_dir": False},
        {"name": "chemistry_notes.pdf", "path": "C:/Desktop/chemistry_notes.pdf", "is_dir": False},
        {"name": "resume.docx", "path": "C:/Desktop/resume.docx", "is_dir": False},
        {"name": "meme.png", "path": "C:/Desktop/meme.png", "is_dir": False},
        {"name": "installer.exe", "path": "C:/Desktop/installer.exe", "is_dir": False},
        {"name": "project_folder", "path": "C:/Desktop/project_folder", "is_dir": True, "type": "directory"},
    ]
    goal = "Keep school stuff and games. Remove unrelated junk."
    res = CollectionClassifier.classify(user_goal=goal, items=items, location="desktop")

    keep_names = {it.name for it in res.keep}
    remove_names = {it.name for it in res.remove}
    unsure_names = {it.name for it in res.unsure}

    # Games and school stuff kept
    assert "Fortnite.lnk" in keep_names
    assert "chemistry_notes.pdf" in keep_names
    assert "project_folder" in keep_names

    # Disposable junk removed
    assert "meme.png" in remove_names
    assert "installer.exe" in remove_names

    # Career document preserved as unsure rather than blindly deleted
    assert "resume.docx" in unsure_names


class SimulatedAgentWebSocket:
    def __init__(self, device_id: str = "asus"):
        self.device_id = device_id

    async def send_text(self, text: str):
        from agent import executor
        data = json.loads(text)
        cid = data.get("command_id")
        cmd = data.get("command")
        args = data.get("args", [])
        exec_res = executor.execute(cmd, args)
        if cid and cid in PENDING_FUTURES:
            fut = PENDING_FUTURES[cid]
            if not fut.done():
                fut.set_result(ResultMessage(
                    command_id=cid,
                    command=cmd,
                    status=exec_res["status"],
                    message=exec_res["message"],
                    data=exec_res.get("data"),
                ))


# ---------------------------------------------------------------------------
# 5. Required Test: Ambiguous Item (notes.pdf -> unsure -> zero deletion)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_ambiguous_item_blocks_without_deletion(isolated_env):
    """
    Seed: notes.pdf
    Goal: "Keep college stuff, remove everything else."
    If metadata does not establish whether notes.pdf is college-related:
    decision = unsure -> No deletion.
    """
    gm = isolated_env["gm"]
    desktop = isolated_env["desktop"]
    notes_file = desktop / "notes.pdf"
    notes_file.write_text("ambiguous contents", encoding="utf-8")

    # Verify classification unit level
    items = [{"name": "notes.pdf", "path": str(notes_file), "is_dir": False}]
    res = CollectionClassifier.classify(
        user_goal="Keep college stuff, remove everything else.",
        items=items,
        location="desktop",
    )
    assert len(res.unsure) == 1
    assert res.unsure[0].name == "notes.pdf"
    assert res.unsure[0].decision == "unsure"
    assert len(res.remove) == 0

    # Verify GoalRunner lifecycle: goal blocks on ambiguity, zero delete steps executed
    sim_ws = SimulatedAgentWebSocket("asus")
    ctrl = CortexControlLayer({"asus": sim_ws})
    runner = GoalRunner(goal_manager=gm, control_layer=ctrl)
    planner = GoalPlanner(goal_manager=gm, control_layer=ctrl)

    # Register local device
    await database.enroll_device("asus", "Asus Laptop", "admin", "secret_hash")
    await database.save_device_capabilities(
        "asus",
        ["fs.list", "fs.stat", "fs.delete", "fs.move", "fs.shortcut_info", "ui.notify"],
        platform="windows",
    )

    goal = await gm.create_goal(
        title="Ambiguous cleanup",
        original_request="Keep college stuff, remove everything else.",
        source="user",
        status="ACTIVE",
    )

    plan_res = await planner.plan_goal(goal.goal_id, target_device="asus")
    assert plan_res["status"] == "planned"

    run_res = await runner.run_goal(goal.goal_id)

    # Goal must be BLOCKED requesting clarification
    g = await gm.get_goal(goal.goal_id)
    assert g.status == "BLOCKED"
    assert "clarification" in (g.blocking_reason or "").lower()
    assert "notes.pdf" in (g.blocking_reason or "")

    # Crucial assertion: notes.pdf still exists on disk!
    assert notes_file.exists()


# ---------------------------------------------------------------------------
# 6. Batch Action Planning & Destructive Approval Enforcement
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_batch_destructive_approval_enforcement(isolated_env):
    """
    Verifies that batch deletions:
    1. Create an approval request for destructive operations
    2. Halt execution before any deletion occurs
    3. Allow batch deletion to proceed only after approval is granted
    """
    gm = isolated_env["gm"]
    desktop = isolated_env["desktop"]

    game_file = desktop / "Starfield.lnk"
    game_file.write_text("game shortcut", encoding="utf-8")
    junk1 = desktop / "random_meme.png"
    junk1.write_text("junk meme", encoding="utf-8")
    junk2 = desktop / "old_setup.exe"
    junk2.write_text("installer", encoding="utf-8")

    sim_ws = SimulatedAgentWebSocket("asus")
    ctrl = CortexControlLayer({"asus": sim_ws})
    runner = GoalRunner(goal_manager=gm, control_layer=ctrl)
    planner = GoalPlanner(goal_manager=gm, control_layer=ctrl)

    await database.enroll_device("asus", "Asus Laptop", "admin", "secret_hash")
    await database.save_device_capabilities(
        "asus",
        ["fs.list", "fs.stat", "fs.delete", "fs.move", "fs.shortcut_info", "ui.notify"],
        platform="windows",
    )

    goal = await gm.create_goal(
        title="Batch Cleanup Approval Test",
        original_request="Clean up my desktop. Keep games and remove everything else.",
        source="user",
        status="ACTIVE",
    )

    await planner.plan_goal(goal.goal_id, target_device="asus")

    # Initial run: executes fs.list, classifies, proposes batch deletes, enters NEEDS_APPROVAL
    run_res = await runner.run_goal(goal.goal_id)
    g = await gm.get_goal(goal.goal_id)
    assert g.status == "NEEDS_APPROVAL"
    assert g.approval_request_id is not None

    # CRITICAL ASSERTION: No deletion occurred before approval!
    assert junk1.exists()
    assert junk2.exists()
    assert game_file.exists()

    # User grants approval
    await database.resolve_confirmation(g.approval_request_id, "approved", "user")

    # Resume goal execution
    run_res2 = await runner.run_goal(goal.goal_id)
    g_final = await gm.get_goal(goal.goal_id)
    assert g_final.status == "COMPLETED"

    # Confirmed removed items are gone; kept item remains
    assert game_file.exists()
    assert not junk1.exists()
    assert not junk2.exists()


# ---------------------------------------------------------------------------
# 7. Post-Action Verification Failure Handling
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_post_action_verification_detects_tampering(isolated_env):
    """Post-action verification fails if an intended keep item is missing."""
    gm = isolated_env["gm"]
    ctrl = CortexControlLayer({})
    runner = GoalRunner(goal_manager=gm, control_layer=ctrl)

    goal = await gm.create_goal(
        title="Verification Test",
        original_request="Clean up desktop",
        source="user",
        status="ACTIVE",
    )
    goal.metadata = {
        "intended_keep": ["ImportantDoc.pdf"],
        "intended_remove": ["junk.tmp"],
        "initial_items": ["ImportantDoc.pdf", "junk.tmp"],
        "awaiting_verification": True,
    }
    await gm.update_goal(goal)

    step = await gm.add_step(
        goal_id=goal.goal_id,
        description="Verify collection",
        capability="fs.list",
        arguments={"path": "desktop", "target_device": "asus"},
    )

    # Simulate fs.list returning empty directory (ImportantDoc.pdf was lost!)
    fake_list_result = {
        "status": "success",
        "data": {"items": []},
    }

    obs = await runner.observe_result(
        goal_id=goal.goal_id,
        step_id=step.step_id,
        raw_result=fake_list_result,
    )

    # Verification must fail and mark goal FAILED
    assert obs["status"] == "failed"
    assert "intended keep item(s) missing" in obs["reason"]
    g = await gm.get_goal(goal.goal_id)
    assert g.status == "FAILED"


# ---------------------------------------------------------------------------
# 8. Scenario Lab Messy Desktop E2E Validation
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_scenario_lab_clean_messy_desktop_e2e():
    """Runs clean_messy_desktop via ScenarioRunner to assert end-to-end PASS."""
    from scenario_lab.runner import ScenarioRunner
    from scenario_lab.models import Scenario

    scenario_file = Path("scenario_lab/scenarios/01_clean_messy_desktop.json")
    assert scenario_file.exists()
    scenario = Scenario.from_file(scenario_file)

    runner = ScenarioRunner()
    result = await runner.run_scenario(scenario)

    assert result.result == "PASS"
    assert result.goal_state == "COMPLETED"
    assert result.approvals == 1
    assert len(result.failed_assertions) == 0
    assert len(result.safety_violations) == 0
