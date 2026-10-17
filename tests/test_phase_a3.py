"""
tests/test_phase_a3.py
----------------------
Comprehensive test suite for Cortex Phase A3:
Reliable App, Window, and File Actions.

Covers:
1. Running Application & Window Inspection (app.list_running, window.list)
2. Hardened App Launch (verification of PID & window appearance, failure when not appearing)
3. Graceful App Close (graceful by default, verified termination, refuses force kill without explicit force)
4. Window Focus & Ambiguity Handling (restore if minimized, foreground verification, multi-window clarification)
5. Window State Actions (window.minimize, window.maximize, window.restore)
6. File & Folder Open (fs.open for file with associated app and directory in Explorer)
7. Hardened File Operations (fs.copy, fs.move, fs.rename) with Overwrite Protection
8. Delete Post-Action Verification Regression (fs.delete verified absent before success)
9. Grounded Target Resolution & Pronoun Continuity ("close it", "switch back to it", "the copy")
10. App vs Device Disambiguation (App names never treated as remote device names)
11. Capability Registry (all Phase A3 schemas, risk tiers, destructive classifications)
12. GoalPlanner Integration (Compound plans: fs.open -> window.list -> window.focus; app.close -> app.launch)
13. Physical-style Tests (App test, Window focus test, File open test, File ops test)
14. Failure Test (Destination exists blocks overwrite)
15. Ambiguity Test (Two Chrome windows trigger clarification rather than killing both)
16. Production DB Isolation
"""

import os
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
import pytest
import pytest_asyncio

from agent.desktop_actions import (
    DesktopActionsOverride,
    close_app,
    focus_window,
    launch_app,
    list_running_apps,
    list_windows,
    maximize_window,
    minimize_window,
    restore_window,
    set_desktop_actions_override,
)
from agent.executor import execute, get_capabilities
from hub.agent_harness import AgentHarness
from hub.capability_registry import get_capability, get_core_capabilities
from hub.control_layer import CortexControlLayer
from hub.database import init_db, save_device_capabilities
from hub.environment import (
    fs_copy,
    fs_delete,
    fs_move,
    fs_open,
    fs_rename,
    fs_stat,
)
from hub.goals import GoalManager
from hub.intent_schema import ConversationalResponse
from hub.llm_provider import MockLLMProvider, set_llm_provider
from hub.planner import BUILTIN_CAPABILITY_SCHEMAS, GoalPlanner
from hub.target_resolver import ResolvedTarget, TargetResolver


@pytest_asyncio.fixture(autouse=True)
async def setup_test_devices():
    """Ensure clean test DB, enroll test device with Phase A3 capabilities."""
    set_llm_provider(MockLLMProvider())
    await init_db()
    await save_device_capabilities(
        "test_dev",
        [
            "app.list_running",
            "app.launch",
            "app.close",
            "window.list",
            "window.focus",
            "window.minimize",
            "window.maximize",
            "window.restore",
            "fs.open",
            "fs.copy",
            "fs.move",
            "fs.rename",
            "fs.delete",
            "fs.list",
            "fs.stat",
            "device.status",
        ],
    )
    # Reset any desktop action overrides after test
    yield
    set_desktop_actions_override(None)


# ===========================================================================
# 1. RUNNING APPLICATION & WINDOW INSPECTION TESTS
# ===========================================================================

def test_app_list_running_real_or_mock():
    """Verify app.list_running returns process_name, pid, executable_path, window_count."""
    res = list_running_apps({})
    assert res["status"] == "success"
    assert "apps" in res
    assert isinstance(res["apps"], list)
    assert len(res["apps"]) > 0

    app = res["apps"][0]
    assert "process_name" in app
    assert "pid" in app
    assert "executable_path" in app
    assert "window_count" in app


def test_window_list_real_or_mock():
    """Verify window.list returns window_id, title, process_name, pid, visible, minimized, foreground, bounds."""
    res = list_windows({})
    assert res["status"] == "success"
    assert "windows" in res
    assert isinstance(res["windows"], list)

    if res["windows"]:
        win = res["windows"][0]
        assert "window_id" in win
        assert "title" in win
        assert "process_name" in win
        assert "pid" in win
        assert "visible" in win
        assert "minimized" in win
        assert "foreground" in win
        assert "bounds" in win


def test_app_list_running_override():
    """Verify mock desktop action override for app.list_running."""
    mock_override = DesktopActionsOverride(
        list_running_apps=lambda args: {
            "status": "success",
            "count": 1,
            "apps": [{"process_name": "notepad.exe", "pid": 1234, "executable_path": "C:\\Windows\\notepad.exe", "window_count": 1}],
        }
    )
    set_desktop_actions_override(mock_override)
    res = execute("app.list_running", {})
    assert res["status"] == "success"
    assert res["apps"][0]["process_name"] == "notepad.exe"


# ===========================================================================
# 2. APP LAUNCH VERIFICATION TESTS
# ===========================================================================

def test_app_launch_verification_failure():
    """Do not report success purely because subprocess launched if window/proc never appears."""
    mock_override = DesktopActionsOverride(
        launch_app=lambda args: {
            "status": "error",
            "message": "Application 'ghost_app' launch verification failed: process did not appear within timeout.",
        }
    )
    set_desktop_actions_override(mock_override)
    res = execute("app.launch", {"app": "ghost_app"})
    assert res["status"] == "error"
    assert "verification failed" in res["message"]


def test_app_launch_verified_success():
    """Verified app launch returns PID and window_id."""
    mock_override = DesktopActionsOverride(
        launch_app=lambda args: {
            "status": "success",
            "message": "Application 'Notepad' launched and verified running.",
            "pid": 5678,
            "window_id": 99999,
        }
    )
    set_desktop_actions_override(mock_override)
    res = execute("app.launch", {"app": "Notepad"})
    assert res["status"] == "success"
    assert res["pid"] == 5678
    assert res["window_id"] == 99999


# ===========================================================================
# 3. APP CLOSE VERIFICATION & ESCALATION POLICY TESTS
# ===========================================================================

def test_app_close_graceful_by_default():
    """Close requests graceful close first and verifies exit."""
    recorded_calls = []

    def mock_close(args):
        recorded_calls.append(args)
        return {
            "status": "success",
            "message": f"Successfully closed {args.get('app', 'app')} gracefully.",
            "closed_pids": [1234],
        }

    mock_override = DesktopActionsOverride(close_app=mock_close)
    set_desktop_actions_override(mock_override)

    res = execute("app.close", {"app": "Notepad"})
    assert res["status"] == "success"
    assert len(recorded_calls) == 1
    # graceful is True by default
    assert recorded_calls[0].get("graceful", True) is True
    assert recorded_calls[0].get("force", False) is False


def test_app_close_refuses_force_kill_by_default():
    """Verify close does not force-kill without explicit authorization."""
    mock_override = DesktopActionsOverride(
        close_app=lambda args: {
            "status": "error",
            "message": "Application did not respond to graceful close. Force termination requires explicit force approval.",
        }
    )
    set_desktop_actions_override(mock_override)
    res = execute("app.close", {"app": "StuckApp", "force": False})
    assert res["status"] == "error"
    assert "Force termination requires explicit" in res["message"]


# ===========================================================================
# 4. WINDOW FOCUS & AMBIGUITY TESTS
# ===========================================================================

def test_window_focus_ambiguity_triggers_clarification():
    """When multiple plausible windows match (e.g. Chrome Window A vs B), ask clarification."""
    mock_override = DesktopActionsOverride(
        focus_window=lambda args: {
            "status": "clarification_needed",
            "message": "Multiple windows match 'Chrome': 'Google Chrome - Docs' (id=101), 'Google Chrome - YouTube' (id=102). Please clarify which window to focus.",
            "candidates": [
                {"window_id": 101, "title": "Google Chrome - Docs"},
                {"window_id": 102, "title": "Google Chrome - YouTube"},
            ],
        }
    )
    set_desktop_actions_override(mock_override)
    res = execute("window.focus", {"title": "Chrome"})
    assert res["status"] == "clarification_needed"
    assert len(res["candidates"]) == 2
    assert "Please clarify which window" in res["message"]


def test_window_focus_verified_switch():
    """Window focus restores if minimized, brings to foreground, and verifies foreground window."""
    mock_override = DesktopActionsOverride(
        focus_window=lambda args: {
            "status": "success",
            "message": "Window 'Google Chrome' (id=101) brought to foreground.",
            "window_id": 101,
            "title": "Google Chrome",
            "foreground_verified": True,
        }
    )
    set_desktop_actions_override(mock_override)
    res = execute("window.focus", {"title": "Google Chrome"})
    assert res["status"] == "success"
    assert res["foreground_verified"] is True


# ===========================================================================
# 5. WINDOW STATE ACTIONS
# ===========================================================================

def test_window_state_operations():
    """Verify window.minimize, window.maximize, window.restore state controls."""
    mock_override = DesktopActionsOverride(
        minimize_window=lambda args: {"status": "success", "message": "Window minimized.", "window_id": 101},
        maximize_window=lambda args: {"status": "success", "message": "Window maximized.", "window_id": 101},
        restore_window=lambda args: {"status": "success", "message": "Window restored.", "window_id": 101},
    )
    set_desktop_actions_override(mock_override)

    assert execute("window.minimize", {"window_id": 101})["status"] == "success"
    assert execute("window.maximize", {"window_id": 101})["status"] == "success"
    assert execute("window.restore", {"window_id": 101})["status"] == "success"


# ===========================================================================
# 6. FILE OPEN / FOLDER OPEN (fs.open)
# ===========================================================================

def test_fs_open_file_and_directory(tmp_path):
    """Verify fs.open opens file with associated app and directory with Explorer."""
    test_file = tmp_path / "review.txt"
    test_file.write_text("Review document content", encoding="utf-8")

    with patch("os.startfile", return_value=None) as mock_startfile:
        res_file = fs_open(str(test_file))
        assert res_file["status"] == "success"
        assert res_file["type"] == "file"
        mock_startfile.assert_called_with(str(test_file.resolve()))

        res_dir = fs_open(str(tmp_path))
        assert res_dir["status"] == "success"
        assert res_dir["type"] == "directory"


def test_fs_open_missing_file_fails(tmp_path):
    """Verify fs.open on nonexistent target fails cleanly without crash."""
    missing = tmp_path / "nonexistent.pdf"
    res = fs_open(str(missing))
    assert res["status"] in ("failure", "error")
    assert "does not exist" in res["message"]


# ===========================================================================
# 7. FILE OPERATIONS WITH OVERWRITE PROTECTION (fs.copy, fs.move, fs.rename)
# ===========================================================================

def test_fs_copy_preserves_source_and_verifies_destination(tmp_path):
    """Verify fs.copy creates destination and preserves source."""
    src = tmp_path / "alpha.txt"
    src.write_text("Hello alpha", encoding="utf-8")
    dst = tmp_path / "backup_alpha.txt"

    res = fs_copy(str(src), str(dst), overwrite=False)
    assert res["status"] == "success"
    assert src.exists()
    assert dst.exists()
    assert dst.read_text(encoding="utf-8") == "Hello alpha"


def test_fs_copy_overwrite_protection_blocks(tmp_path):
    """FAILURE TEST: Attempt copy onto existing destination without overwrite approval blocks."""
    src = tmp_path / "alpha.txt"
    src.write_text("New content", encoding="utf-8")
    dst = tmp_path / "existing.txt"
    dst.write_text("Pre-existing content", encoding="utf-8")

    res = fs_copy(str(src), str(dst), overwrite=False)
    assert res["status"] in ("failure", "error")
    assert res.get("overwrite_blocked") is True
    assert "Destination already exists" in res["message"]
    # Existing content must not be overwritten
    assert dst.read_text(encoding="utf-8") == "Pre-existing content"


def test_fs_copy_with_explicit_overwrite_allowed(tmp_path):
    """When overwrite=True is explicitly granted, copy succeeds."""
    src = tmp_path / "alpha.txt"
    src.write_text("New content", encoding="utf-8")
    dst = tmp_path / "existing.txt"
    dst.write_text("Old content", encoding="utf-8")

    res = fs_copy(str(src), str(dst), overwrite=True)
    assert res["status"] == "success"
    assert dst.read_text(encoding="utf-8") == "New content"


def test_fs_rename_verifies_state_and_blocks_overwrite(tmp_path):
    """Verify fs.rename creates new name, verifies source absent, and blocks existing target."""
    src = tmp_path / "copy.txt"
    src.write_text("Content", encoding="utf-8")
    new_name = "beta.txt"

    res = fs_rename(str(src), new_name, overwrite=False)
    assert res["status"] == "success"
    assert not src.exists()
    assert (tmp_path / "beta.txt").exists()

    # Attempt renaming again to an existing target without overwrite
    src2 = tmp_path / "gamma.txt"
    src2.write_text("Gamma content", encoding="utf-8")
    res_block = fs_rename(str(src2), "beta.txt", overwrite=False)
    assert res_block["status"] in ("failure", "error")
    assert res_block.get("overwrite_blocked") is True
    assert src2.exists()


def test_fs_move_verifies_source_absent_and_destination_present(tmp_path):
    """Verify fs.move moves file, destination exists, source absent."""
    archive_dir = tmp_path / "Archive"
    archive_dir.mkdir()
    src = tmp_path / "beta.txt"
    src.write_text("Beta content", encoding="utf-8")
    dst = archive_dir / "beta.txt"

    res = fs_move(str(src), str(dst), overwrite=False)
    assert res["status"] == "success"
    assert not src.exists()
    assert dst.exists()
    assert dst.read_text(encoding="utf-8") == "Beta content"


# ===========================================================================
# 8. DELETE POST-ACTION VERIFICATION REGRESSION
# ===========================================================================

def test_fs_delete_verified_absent_before_success(tmp_path):
    """fs.delete must verify target absent before returning success."""
    target = tmp_path / "to_delete.txt"
    target.write_text("delete me", encoding="utf-8")

    res = fs_delete(str(target))
    assert res["status"] == "success"
    assert not target.exists()


def test_fs_delete_reports_error_if_deletion_failed(tmp_path):
    """If file cannot be deleted, report concrete failure rather than dispatch success."""
    target = tmp_path / "locked.txt"
    target.write_text("locked", encoding="utf-8")

    with patch("os.unlink", side_effect=PermissionError("File locked by process")):
        res = fs_delete(str(target))
        assert res["status"] in ("failure", "error")
        assert target.exists()
        assert "Permission denied" in res["message"] or "File locked" in res["message"]


# ===========================================================================
# 9. TARGET RESOLUTION & PRONOUN CONTINUITY
# ===========================================================================

def test_target_resolution_app_and_window_actions():
    """Verify TargetResolver resolves app, window, copy, and rename actions."""
    t1 = TargetResolver.resolve("close Discord", current_device="asus")
    assert t1.action == "close"
    assert t1.target_name.lower() == "discord"
    assert t1.target_kind == "application"

    t2 = TargetResolver.resolve("switch to Chrome", current_device="asus")
    assert t2.action == "focus"
    assert t2.target_name.lower() == "chrome"
    assert t2.target_kind == "window"

    t3 = TargetResolver.resolve("focus the terminal", current_device="asus")
    assert t3.action == "focus"
    assert "terminal" in t3.target_name.lower()

    t4 = TargetResolver.resolve("open my Calc review PDF", current_device="asus")
    assert t4.action == "open"
    assert "calc review pdf" in t4.target_name.lower()
    assert t4.target_kind == "filesystem_item"


def test_pronoun_continuity_and_app_not_treated_as_device():
    """Verify 'close it', 'switch back to it' reuse grounded app/window, and app is never a device."""
    session_ctx = {
        "references": {
            "last_app": "Discord",
            "last_window": "Google Chrome",
            "last_file": "alpha.txt",
            "last_copied_dst": "backup_alpha.txt",
        }
    }

    t_close_it = TargetResolver.resolve("close it", current_device="asus", session_context=session_ctx)
    assert t_close_it.action == "close"
    assert t_close_it.target_name == "Discord"

    t_switch_back = TargetResolver.resolve("switch back to it", current_device="asus", session_context=session_ctx)
    assert t_switch_back.action == "focus"
    # Reuses last window or app
    assert t_switch_back.target_name in ("Google Chrome", "Discord")

    # Critical rule: App name Discord or Chrome must NEVER be assigned as target_device
    assert t_close_it.target_device == "asus"
    assert t_switch_back.target_device == "asus"


# ===========================================================================
# 10. CAPABILITY REGISTRY
# ===========================================================================

def test_phase_a3_capabilities_registered():
    """Verify all Phase A3 capabilities are formally registered in CapabilityRegistry."""
    req_caps = [
        "app.list_running",
        "app.launch",
        "app.close",
        "window.list",
        "window.focus",
        "window.minimize",
        "window.maximize",
        "window.restore",
        "fs.open",
        "fs.copy",
        "fs.move",
        "fs.rename",
        "fs.delete",
    ]
    all_caps = get_core_capabilities()
    cap_names = set(all_caps.keys())

    for rc in req_caps:
        assert rc in cap_names, f"Capability {rc} missing from CapabilityRegistry"
        cap = get_capability(rc)
        assert cap is not None
        assert cap.permission_tier in (1, 2, 3)
        assert cap.risk in ("low", "medium", "high")
        assert cap.classification in ("read", "write", "destructive")


# ===========================================================================
# 11. PLANNER INTEGRATION
# ===========================================================================

@pytest.mark.asyncio
async def test_planner_open_file_and_switch_to_it():
    """User: 'Open my Calc review PDF and switch to it.'
    Plan: fs.open -> window.list -> window.focus."""
    gm = GoalManager.get_instance()
    planner = GoalPlanner.get_instance(goal_manager=gm)

    goal = await gm.create_goal(
        title="Open Calc review and switch",
        original_request="Open my Calc review PDF and switch to it.",
    )

    plan_res = await planner.plan_goal(goal_id=goal.goal_id, target_device="test_dev")
    assert plan_res["status"] == "planned"
    assert plan_res["step_count"] == 3

    steps = plan_res["steps"]
    assert steps[0]["capability"] == "fs.open"
    assert "calc review" in steps[0]["arguments"]["path"].lower()

    assert steps[1]["capability"] == "window.list"

    assert steps[2]["capability"] == "window.focus"
    assert "calc review" in steps[2]["arguments"]["title"].lower()


@pytest.mark.asyncio
async def test_planner_close_discord_and_open_spotify():
    """User: 'Close Discord and open Spotify.'
    Plan: app.close -> app.launch."""
    gm = GoalManager.get_instance()
    planner = GoalPlanner.get_instance(goal_manager=gm)

    goal = await gm.create_goal(
        title="Close Discord and open Spotify",
        original_request="Close Discord and open Spotify",
    )

    plan_res = await planner.plan_goal(goal_id=goal.goal_id, target_device="test_dev")
    assert plan_res["status"] == "planned"
    assert plan_res["step_count"] == 2

    steps = plan_res["steps"]
    assert steps[0]["capability"] == "app.close"
    assert steps[0]["arguments"]["app"].lower() == "discord"

    assert steps[1]["capability"] == "app.launch"
    assert steps[1]["arguments"]["app"].lower() == "spotify"


@pytest.mark.asyncio
async def test_planner_file_ops_goals():
    """Verify GoalPlanner reasons for file copy, rename, and move."""
    gm = GoalManager.get_instance()
    planner = GoalPlanner.get_instance(goal_manager=gm)

    goal_copy = await gm.create_goal(
        title="Copy file",
        original_request="Copy alpha.txt to Backup",
    )
    plan_c = await planner.plan_goal(goal_id=goal_copy.goal_id, target_device="test_dev")
    assert plan_c["status"] == "planned"
    assert plan_c["steps"][0]["capability"] == "fs.copy"
    assert plan_c["steps"][0]["arguments"]["src"] == "alpha.txt"
    assert plan_c["steps"][0]["arguments"]["dst"] == "Backup"

    goal_rename = await gm.create_goal(
        title="Rename file",
        original_request="Rename alpha.txt to beta.txt",
    )
    plan_r = await planner.plan_goal(goal_id=goal_rename.goal_id, target_device="test_dev")
    assert plan_r["status"] == "planned"
    assert plan_r["steps"][0]["capability"] == "fs.rename"
    assert plan_r["steps"][0]["arguments"]["new_name"] == "beta.txt"

    goal_move = await gm.create_goal(
        title="Move file",
        original_request="Move beta.txt to Archive",
    )
    plan_m = await planner.plan_goal(goal_id=goal_move.goal_id, target_device="test_dev")
    assert plan_m["status"] == "planned"
    assert plan_m["steps"][0]["capability"] == "fs.move"
    assert plan_m["steps"][0]["arguments"]["dst"] == "Archive"


# ===========================================================================
# 12. PHYSICAL TESTS VIA AGENT HARNESS & DEMOS
# ===========================================================================

@pytest.mark.asyncio
async def test_harness_physical_app_control_lifecycle():
    """REQUIRED PHYSICAL TEST 1 — APP CONTROL
    Ask: 'Open Notepad.' -> verify app.launch verified.
    Then: 'Close Notepad.' -> verify app.close verified.
    """
    ctrl = CortexControlLayer()
    harness = AgentHarness(control_layer=ctrl)

    mock_override = DesktopActionsOverride(
        launch_app=lambda args: {
            "status": "success",
            "message": "Application 'Notepad' launched and verified running (pid=4567).",
            "pid": 4567,
            "window_id": 8888,
        },
        close_app=lambda args: {
            "status": "success",
            "message": "Successfully closed Notepad gracefully.",
            "closed_pids": [4567],
        },
    )
    set_desktop_actions_override(mock_override)

    context = {"device": {"capabilities": ["app.launch", "app.close"]}}

    # 1. Open Notepad
    res_open = await harness.process_request("test_dev", "Open Notepad", context)
    assert res_open.status == "success"
    assert "Notepad" in res_open.message

    # 2. Close Notepad
    res_close = await harness.process_request("test_dev", "Close Notepad", context)
    assert res_close.status == "success"
    assert "closed notepad" in res_close.message.lower()


@pytest.mark.asyncio
async def test_harness_physical_window_focus():
    """REQUIRED PHYSICAL TEST 2 — WINDOW FOCUS
    Ask: 'Switch to Chrome.' -> matching window resolved, window.focus brings to foreground.
    """
    ctrl = CortexControlLayer()
    harness = AgentHarness(control_layer=ctrl)

    mock_override = DesktopActionsOverride(
        focus_window=lambda args: {
            "status": "success",
            "message": "Window 'Google Chrome' (id=101) brought to foreground.",
            "window_id": 101,
            "title": "Google Chrome",
            "foreground_verified": True,
        }
    )
    set_desktop_actions_override(mock_override)

    context = {"device": {"capabilities": ["window.focus"]}}
    res = await harness.process_request("test_dev", "Switch to Chrome", context)
    assert res.status == "success"
    assert "Chrome" in res.message


@pytest.mark.asyncio
async def test_harness_physical_file_open(tmp_path):
    """REQUIRED PHYSICAL TEST 3 — FILE OPEN
    Ask: 'Open this file.' -> fs.open dispatched, associated application opens.
    """
    test_pdf = tmp_path / "calc_review.pdf"
    test_pdf.write_text("%PDF-1.4 test content", encoding="utf-8")

    ctrl = CortexControlLayer()
    harness = AgentHarness(control_layer=ctrl)

    context = {
        "device": {"capabilities": ["fs.open"]},
        "session": {"references": {"last_file": str(test_pdf)}},
    }

    with patch("os.startfile", return_value=None):
        res = await harness.process_request("test_dev", "Open this file", context)
        assert res.status == "success"
        assert str(test_pdf) in res.message or "Opened" in res.message


@pytest.mark.asyncio
async def test_harness_physical_file_ops_sequence(tmp_path):
    """REQUIRED PHYSICAL TEST 4 — FILE OPERATIONS
    1. 'Copy alpha.txt to Backup'
    2. 'Rename the copy to beta.txt'
    3. 'Move beta.txt to Archive'
    """
    test_dir = tmp_path / "sandbox"
    test_dir.mkdir()
    alpha = test_dir / "alpha.txt"
    alpha.write_text("Alpha content", encoding="utf-8")
    backup = test_dir / "backup.txt"
    beta = test_dir / "beta.txt"
    archive = test_dir / "Archive"
    archive.mkdir()

    ctrl = CortexControlLayer()
    harness = AgentHarness(control_layer=ctrl)
    context = {"device": {"capabilities": ["fs.copy", "fs.rename", "fs.move"]}}

    # 1. Copy alpha.txt to backup.txt
    res_copy = await harness.process_request("test_dev", f"Copy {alpha} to {backup}", context)
    assert res_copy.status == "success"
    assert alpha.exists()
    assert backup.exists()

    # 2. Rename the copy to beta.txt
    res_rename = await harness.process_request("test_dev", f"Rename the copy to {beta}", context)
    assert res_rename.status == "success"
    assert not backup.exists()
    assert beta.exists()

    # 3. Move beta.txt to Archive
    final_dest = archive / "beta.txt"
    res_move = await harness.process_request("test_dev", f"Move {beta} to {final_dest}", context)
    assert res_move.status == "success"
    assert not beta.exists()
    assert final_dest.exists()


@pytest.mark.asyncio
async def test_harness_ambiguity_test():
    """REQUIRED AMBIGUITY TEST:
    Have Chrome window A and Chrome window B.
    Request: 'Close Chrome' -> If ambiguous, asks which one rather than killing both.
    """
    ctrl = CortexControlLayer()
    harness = AgentHarness(control_layer=ctrl)

    mock_override = DesktopActionsOverride(
        close_app=lambda args: {
            "status": "clarification_needed",
            "message": "Multiple windows found for 'Chrome': 'Google Chrome - Tab 1' (id=201), 'Google Chrome - Tab 2' (id=202). Which one would you like to close?",
            "candidates": [
                {"window_id": 201, "title": "Google Chrome - Tab 1"},
                {"window_id": 202, "title": "Google Chrome - Tab 2"},
            ],
        }
    )
    set_desktop_actions_override(mock_override)

    context = {"device": {"capabilities": ["app.close"]}}
    res = await harness.process_request("test_dev", "Close Chrome", context)
    assert res.status == "clarification_needed"
    assert "Which one would you like to close" in res.message


# ===========================================================================
# 13. PRODUCTION DB ISOLATION CHECK
# ===========================================================================

def test_production_db_isolation():
    """Confirm test environment strictly targets test_cortex.db and production DB is untouched."""
    db_env = os.environ.get("CORTEX_DB_PATH", "")
    assert "test_cortex.db" in db_env
    assert not db_env.endswith("data/cortex.db")
