"""
tests/test_phase_a10.py
-----------------------
Comprehensive Automated Test Suite for Cortex Phase A10 — General Computer Use.

Tests:
1. UI observation schema & serialization
2. Native accessibility element extraction
3. Vision perception fallback
4. Element grounding (ID, role+label, exact, substring, coordinate fallback)
5. Spatial and ordinal grounding ("second item", "button under search")
6. Observation versioning & freshness check
7. Stale observation rejection (fencing stale screens)
8. Pointer actions (move, click, double click, right click)
9. Keyboard actions (keypress, hotkeys)
10. Text entry & sensitive credential masking (zero secret leakage)
11. Scrolling (up/down/left/right)
12. Drag and drop
13. Post-action verification
14. Failed verification triggers re-observation and replan
15. Context continuity & deictic pronoun resolution ("click that", "go back", "the second one")
16. Policy gating on high-impact actions (delete, purchase, security)
17. Approval integration with A9 Control Center
18. Executor device routing & screen affinity (WAITING_FOR_EXECUTOR)
19. Multi-step task checkpointing & resume
20. Capability preference over GUI automation (app.launch > UI typing into Start)
21. No arbitrary shell fallback for missing UI capabilities
22. Safe clipboard read & write
"""

import asyncio
from datetime import datetime, timezone
import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from agent.ui_action_executor import (
    UIActionExecutor,
    set_ui_action_override,
)
from agent.ui_grounder import GroundingResult, UIElementGrounder
from agent.ui_models import UIActionResult, UIElement, UIObservation
from agent.ui_observer import (
    UIObserver,
    set_ui_observer_override,
)
from agent.ui_verifier import UIActionVerifier, VerificationOutcome
from hub.capability_registry import CanonicalCapabilityRegistry
from hub.computer_use_controller import (
    ComputerUseStep,
    ComputerUseTask,
    ComputerUseTaskController,
)
from hub.permissions import COMMAND_TIERS, check_permission
from hub.pipeline.context_assembler import AssembledContext
from hub.pipeline.policy_gate import PolicyGate, PolicyGateResult
from hub.pipeline.semantic_interpreter import SemanticIntent, SemanticInterpreter


@pytest.fixture(autouse=True)
def cleanup_overrides():
    """Ensure test overrides are cleared after each test."""
    yield
    set_ui_action_override(None)
    set_ui_observer_override(None)
    UIActionExecutor.reset()
    UIObserver.reset()
    ComputerUseTaskController.reset()


def make_sample_observation(obs_id="obs_test1", title="Settings", app="Settings", x=100, y=100):
    """Helper to construct a deterministic sample UIObservation."""
    elements = [
        UIElement(
            element_id="el_1",
            role="window",
            label="Settings",
            bounds=[x, y, 1000, 700],
            confidence=1.0,
            enabled=True,
        ),
        UIElement(
            element_id="el_2",
            role="input",
            label="Find a setting",
            text="",
            bounds=[x + 50, y + 50, 300, 35],
            confidence=1.0,
            enabled=True,
        ),
        UIElement(
            element_id="el_3",
            role="button",
            label="Search",
            text="Search",
            bounds=[x + 360, y + 50, 60, 35],
            confidence=1.0,
            enabled=True,
        ),
        UIElement(
            element_id="el_4",
            role="button",
            label="System",
            bounds=[x + 50, y + 110, 200, 40],
            confidence=1.0,
            enabled=True,
        ),
        UIElement(
            element_id="el_5",
            role="button",
            label="Privacy & Security",
            bounds=[x + 50, y + 160, 200, 40],
            confidence=1.0,
            enabled=True,
        ),
        UIElement(
            element_id="el_6",
            role="checkbox",
            label="Dark mode",
            text="off",
            bounds=[x + 350, y + 160, 150, 30],
            confidence=1.0,
            enabled=True,
        ),
        UIElement(
            element_id="el_7",
            role="button",
            label="Save",
            text="Save",
            bounds=[x + 500, y + 600, 100, 40],
            confidence=1.0,
            enabled=True,
        ),
    ]
    return UIObservation(
        observation_id=obs_id,
        device_id="asus",
        timestamp=datetime.now(timezone.utc).isoformat(),
        screen_size={"width": 1920, "height": 1080},
        foreground_app=app,
        window_title=title,
        window_id=12345,
        window_bounds=[x, y, 1000, 700],
        summary=f"Foreground: {app} ('{title}') with {len(elements)} elements.",
        elements=elements,
    )


# ---------------------------------------------------------------------------
# 1. UI Observation Schema & Serialization
# ---------------------------------------------------------------------------
def test_ui_observation_schema():
    obs = make_sample_observation("obs_001")
    d = obs.to_dict()

    assert d["observation_id"] == "obs_001"
    assert d["foreground_app"] == "Settings"
    assert d["screen_size"] == {"width": 1920, "height": 1080}
    assert len(d["elements"]) == 7
    assert d["elements"][6]["label"] == "Save"
    assert d["elements"][6]["role"] == "button"
    assert d["state_hash"] != ""

    # Reconstruct from dict
    restored = UIObservation.from_dict(d)
    assert restored.observation_id == obs.observation_id
    assert restored.state_hash == obs.state_hash
    assert len(restored.elements) == 7
    assert restored.elements[6].bounds == [600, 700, 100, 40]
    assert restored.elements[6].center_point == (650, 720)


# ---------------------------------------------------------------------------
# 2. Native Accessibility Element Extraction
# ---------------------------------------------------------------------------
def test_accessibility_element_extraction():
    observer = UIObserver.get_instance()
    # Mock COM client returning synthetic elements
    mock_el = MagicMock()
    mock_rect = MagicMock()
    mock_rect.left, mock_rect.top, mock_rect.right, mock_rect.bottom = 100, 100, 200, 140
    mock_el.CurrentBoundingRectangle = mock_rect
    mock_el.CurrentName = "Export"
    mock_el.CurrentControlType = 50000  # Button
    mock_el.CurrentIsEnabled = True
    mock_el.CurrentIsOffscreen = False
    mock_el.CurrentIsPassword = False
    mock_el.CurrentIsKeyboardFocusable = True
    mock_el.CurrentHasKeyboardFocus = False
    mock_el.CurrentAutomationId = "btn_export"
    mock_el.CurrentClassName = "Button"
    mock_el.GetCurrentPattern.return_value = None

    mock_array = MagicMock()
    mock_array.Length = 1
    mock_array.GetElement.return_value = mock_el

    mock_uia = MagicMock()
    mock_uia.ElementFromHandle.return_value = MagicMock()
    mock_uia.ElementFromHandle.return_value.FindAll.return_value = mock_array
    mock_uia.CreateTrueCondition.return_value = MagicMock()

    observer._uia_client = mock_uia
    observer._uia_module = MagicMock()

    extracted = observer._extract_accessibility_elements(hwnd=999, screen_size={"width": 1920, "height": 1080})
    assert len(extracted) == 1
    assert extracted[0].role == "button"
    assert extracted[0].label == "Export"
    assert extracted[0].bounds == [100, 100, 100, 40]
    assert extracted[0].confidence == 1.0


# ---------------------------------------------------------------------------
# 3. Vision Perception Fallback
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_vision_fallback_perception():
    observer = UIObserver.get_instance()

    mock_vision_obs = MagicMock()
    mock_vision_obs.regions = [
        {"label": "Custom Graph Control", "bounds": [400, 200, 300, 150], "confidence": 0.88},
        {"label": "Close", "bounds": [900, 50, 30, 30], "confidence": 0.92},
    ]

    with patch("hub.vision_provider.get_vision_provider") as mock_get_vp:
        mock_vp = MagicMock()
        mock_vp.is_available.return_value = True
        mock_vp.analyze_image = AsyncMock(return_value=mock_vision_obs)
        mock_get_vp.return_value = mock_vp

        vis_elements = await observer._extract_vision_fallback_elements(
            image_ref="dummy.png",
            device_id="asus",
            screen_size={"width": 1920, "height": 1080},
            existing_elements=[],
        )

        assert len(vis_elements) == 2
        assert vis_elements[0].label == "Custom Graph Control"
        assert vis_elements[0].bounds == [400, 200, 300, 150]
        assert vis_elements[0].confidence == 0.88
        assert vis_elements[0].metadata.get("source") == "vision"


# ---------------------------------------------------------------------------
# 4. Element Grounding (ID, selector, label, coordinate fallback)
# ---------------------------------------------------------------------------
def test_element_grounding():
    obs = make_sample_observation("obs_ground")

    # 4a. Ground by element_id
    res_id = UIElementGrounder.ground("el_7", obs)
    assert res_id.success is True
    assert res_id.element.label == "Save"
    assert res_id.target_point == (650, 720)
    assert res_id.strategy == "id_lookup"

    # 4b. Ground by exact label
    res_lbl = UIElementGrounder.ground("Save", obs)
    assert res_lbl.success is True
    assert res_lbl.element.element_id == "el_7"
    assert res_lbl.strategy == "exact_label"

    # 4c. Ground by selector string
    res_sel = UIElementGrounder.ground("button[label='System']", obs)
    assert res_sel.success is True
    assert res_sel.element.label == "System"
    assert res_sel.strategy == "role_label"

    # 4d. Ground by substring
    res_sub = UIElementGrounder.ground("Security", obs)
    assert res_sub.success is True
    assert res_sub.element.label == "Privacy & Security"
    assert res_sub.strategy == "substring"

    # 4e. Coordinate fallback
    res_coord = UIElementGrounder.ground(None, obs, coords=(500, 400))
    assert res_coord.success is True
    assert res_coord.target_point == (500, 400)
    assert res_coord.strategy == "coordinate_fallback"


# ---------------------------------------------------------------------------
# 5. Spatial and Ordinal Grounding
# ---------------------------------------------------------------------------
def test_spatial_and_ordinal_grounding():
    obs = make_sample_observation("obs_spatial")

    # Ordinal query: 'second button'
    # Buttons in sample: Search (y=150), System (y=210), Privacy & Security (y=260), Save (y=700)
    res_ord = UIElementGrounder.ground("second button", obs)
    assert res_ord.success is True
    assert res_ord.strategy == "ordinal"
    # Search is 1st button, System is 2nd button
    assert res_ord.element.label == "System"

    # Spatial query: 'button under Search'
    res_rel = UIElementGrounder.ground("button below Search", obs)
    assert res_rel.success is True
    assert res_rel.strategy == "spatial_relative"
    assert res_rel.target_point is not None


# ---------------------------------------------------------------------------
# 6. Observation Freshness & Versioning
# ---------------------------------------------------------------------------
def test_observation_freshness_and_versioning():
    observer = UIObserver.get_instance()
    obs = make_sample_observation("obs_fresh", x=100, y=100)
    observer._store_observation(obs)

    # Current window matches observation exactly
    with patch.object(observer, "_get_active_window_info") as mock_win:
        mock_win.return_value = {
            "window_id": 12345,
            "window_title": "Settings",
            "foreground_app": "Settings",
            "window_bounds": [100, 100, 1000, 700],
        }
        is_fresh, reason = observer.is_observation_fresh("obs_fresh")
        assert is_fresh is True

        # Window moved significantly (bounds change > 60px)
        mock_win.return_value = {
            "window_id": 12345,
            "window_title": "Settings",
            "foreground_app": "Settings",
            "window_bounds": [300, 100, 1000, 700],
        }
        is_fresh, reason = observer.is_observation_fresh("obs_fresh")
        assert is_fresh is False
        assert "moved/resized" in reason


# ---------------------------------------------------------------------------
# 7. Stale Observation Rejection (Fencing)
# ---------------------------------------------------------------------------
def test_stale_observation_rejection():
    executor = UIActionExecutor.get_instance()
    observer = UIObserver.get_instance()

    obs = make_sample_observation("obs_stale", x=100, y=100)
    observer._store_observation(obs)

    # Window title changed to something else
    with patch.object(observer, "_get_active_window_info") as mock_win:
        mock_win.return_value = {
            "window_id": 12345,
            "window_title": "Calculator",
            "foreground_app": "Calculator",
            "window_bounds": [100, 100, 1000, 700],
        }

        # Attempt click with stale observation_id
        res = executor.click({"target": "Save", "observation_id": "obs_stale"})
        assert res["status"] == "failure"
        assert res["error"] == "stale_observation"
        assert "Screen state changed" in res["message"]


# ---------------------------------------------------------------------------
# 8. Pointer Actions (Click, Double Click, Right Click, Move)
# ---------------------------------------------------------------------------
def test_pointer_actions():
    executor = UIActionExecutor.get_instance()
    observer = UIObserver.get_instance()
    obs = make_sample_observation("obs_ptr", x=0, y=0)
    observer._store_observation(obs)

    # Mock user32 pointer methods so no physical cursor jump during test
    with patch.object(executor, "_move_cursor") as mock_move, \
         patch.object(executor, "_send_mouse_event") as mock_mouse, \
         patch.object(observer, "is_observation_fresh", return_value=(True, "Fresh")):

        # 8a. Left click
        res_clk = executor.click({"target": "Save", "observation_id": "obs_ptr"})
        assert res_clk["status"] == "success"
        assert res_clk["target_label"] == "Save"
        assert res_clk["coordinates"] == [550, 620]
        assert mock_move.called
        assert mock_mouse.called

        # 8b. Right click
        res_rclk = executor.click({"target": "Save", "observation_id": "obs_ptr"}, button="right")
        assert res_rclk["status"] == "success"
        assert res_rclk["action"] == "ui.right_click"

        # 8c. Move pointer
        res_mov = executor.move_pointer({"x": 300, "y": 400})
        assert res_mov["status"] == "success"
        assert res_mov["coordinates"] == [300, 400]


# ---------------------------------------------------------------------------
# 9. Keyboard Actions (Keypress & Hotkey)
# ---------------------------------------------------------------------------
def test_keyboard_actions():
    executor = UIActionExecutor.get_instance()

    observer = UIObserver.get_instance()
    obs = make_sample_observation("obs_keys")
    obs.window_id = 42
    observer._store_observation(obs)
    with patch.object(executor, "_send_vk_key") as mock_key, \
         patch.object(observer, "_get_active_window_info", return_value={"window_id": 42}), \
         patch.object(observer, "is_observation_fresh", return_value=(True, "Fresh")):
        res_kp = executor.keypress({"key": "enter", "observation_id": "obs_keys"})
        assert res_kp["status"] == "success"
        assert res_kp["data"]["key"] == "enter"
        assert mock_key.called

        res_hk = executor.hotkey({"keys": ["ctrl", "a"], "observation_id": "obs_keys"})
        assert res_hk["status"] == "success"
        assert res_hk["data"]["combo"] == "ctrl+a"
        assert mock_key.call_count >= 3


# ---------------------------------------------------------------------------
# 10. Text Entry & Credential Masking (Zero Leakage)
# ---------------------------------------------------------------------------
def test_text_entry_and_credential_masking():
    executor = UIActionExecutor.get_instance()
    observer = UIObserver.get_instance()
    obs = make_sample_observation("obs_type")
    observer._store_observation(obs)

    with patch.object(executor, "_send_unicode_text") as mock_type, \
         patch.object(executor, "_move_cursor"), \
         patch.object(executor, "_send_mouse_event"), \
         patch.object(observer, "focus_text_target", return_value=True), \
         patch.object(observer, "text_target_has_focus", return_value=True), \
         patch.object(observer, "is_observation_fresh", return_value=(True, "Fresh")):

        # 10a. Normal text entry
        res_norm = executor.type_text({"text": "Cortex A10 test", "target": "Find a setting", "observation_id": "obs_type"})
        assert res_norm["status"] == "success"
        assert mock_type.called
        assert "Cortex A10 test" not in res_norm["message"]
        assert res_norm["verification_required"] and not res_norm["verified"]

        # 10b. Sensitive credential entry
        secret_password = "SecretPassword!#99"
        res_secret = executor.type_text({
            "text": secret_password,
            "target": "Find a setting",
            "is_credential": True,
            "observation_id": "obs_type",
        })
        assert res_secret["status"] == "success"
        # Verify RAW SECRET is NOT in return payload or message
        assert secret_password not in res_secret["message"]
        assert secret_password not in str(res_secret["data"])
        assert "[CREDENTIAL REDACTED]" in res_secret["message"]
        assert res_secret["data"]["is_credential"] is True


# ---------------------------------------------------------------------------
# 11. Scrolling
# ---------------------------------------------------------------------------
def test_scrolling():
    executor = UIActionExecutor.get_instance()
    with patch.object(executor, "_send_mouse_event") as mock_mouse:
        res = executor.scroll({"direction": "down", "amount": 240})
        assert res["status"] == "success"
        assert res["data"]["direction"] == "down"
        assert res["data"]["amount"] == 240
        assert mock_mouse.called


# ---------------------------------------------------------------------------
# 12. Drag and Drop
# ---------------------------------------------------------------------------
def test_drag_and_drop():
    executor = UIActionExecutor.get_instance()
    with patch.object(executor, "_move_cursor") as mock_move, \
         patch.object(executor, "_send_mouse_event") as mock_mouse:
        res = executor.drag({"from_x": 100, "from_y": 100, "to_x": 400, "to_y": 400})
        assert res["status"] == "success"
        assert res["coordinates"] == [400, 400]
        assert mock_move.call_count >= 10
        assert mock_mouse.called


# ---------------------------------------------------------------------------
# 13. Post-Action Verification
# ---------------------------------------------------------------------------
def test_post_action_verification():
    pre_obs = make_sample_observation("obs_v_pre", title="Home", app="Explorer")
    post_obs = make_sample_observation("obs_v_post", title="Downloads", app="Explorer")

    # 13a. Navigation click verified by title change
    v_nav = UIActionVerifier.verify(
        action="ui.click",
        target_label="Downloads",
        pre_obs=pre_obs,
        post_obs=post_obs,
    )
    assert v_nav.verified is True
    assert v_nav.change_detected is True
    assert v_nav.replan_suggested is False
    assert "updated to 'Downloads'" in v_nav.summary

    # 13b. Click that failed to produce any change -> verified=False, replan_suggested=True
    v_fail = UIActionVerifier.verify(
        action="ui.click",
        target_label="Save",
        pre_obs=pre_obs,
        post_obs=pre_obs,  # No change
    )
    assert v_fail.verified is False
    assert v_fail.replan_suggested is True
    assert "produced no visible UI change" in v_fail.summary


# ---------------------------------------------------------------------------
# 14. Failed Verification Triggers Re-Observation and Replan
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_failed_verification_triggers_replan():
    controller = ComputerUseTaskController.get_instance()
    task = controller.create_task("Open billing section")
    step = ComputerUseStep(step_id="step_1", action="ui.click", target="Billing")
    task.steps.append(step)

    # Mock action executor to return success on click, but post-observation shows no change
    same_obs = make_sample_observation("obs_same")
    with patch("agent.ui_observer.UIObserver.observe", AsyncMock(return_value=same_obs)), \
         patch("agent.ui_action_executor.UIActionExecutor.click", return_value={"status": "success", "action": "ui.click"}):

        res = await controller.execute_task_step(task, step)
        assert res["status"] == "verification_failed"
        assert res["verified"] is False
        assert res["replan_suggested"] is True


# ---------------------------------------------------------------------------
# 15. Context Continuity & Deictic Pronoun Resolution
# ---------------------------------------------------------------------------
def test_context_continuity_references():
    controller = ComputerUseTaskController.get_instance()
    obs = make_sample_observation("obs_ctx")

    task = controller.create_task("Review settings")
    task.last_grounded_element = {"element_id": "el_6", "label": "Dark mode"}

    # 'click that' -> resolves to last grounded element
    target, note = controller.resolve_contextual_target("click that", task, None, obs)
    assert target == "el_6"
    assert "Dark mode" in note

    # 'go back' -> resolves to alt+left hotkey
    act, param = controller.resolve_contextual_target("go back", task, None, obs)
    assert act == "ui.hotkey"
    assert param == "alt+left"

    # 'the second one' -> preserves ordinal query for grounder
    ord_tgt, _ = controller.resolve_contextual_target("the second one", task, None, obs)
    assert ord_tgt == "the second one"


# ---------------------------------------------------------------------------
# 16. Policy Gating on High-Impact Actions
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_policy_gating_high_impact_actions():
    gate = PolicyGate()
    context = MagicMock(spec=AssembledContext)
    context.source_device = "asus"

    # 16a. Safe UI click (e.g. Next page, Save)
    safe_intent = SemanticIntent(
        type="ACTION",
        goal="Click Next Page",
        required_effect="ui_click",
        candidate_capabilities=["ui.click"],
        arguments={"target": "Next Page"},
    )
    res_safe = await gate.evaluate(safe_intent, "asus", context, MagicMock())
    assert res_safe.allowed is True
    assert res_safe.requires_approval is False

    # 16b. High-impact UI click (Delete account)
    risky_intent = SemanticIntent(
        type="ACTION",
        goal="Delete User Account",
        required_effect="ui_click",
        candidate_capabilities=["ui.click"],
        arguments={"target": "Delete Account"},
    )
    res_risky = await gate.evaluate(risky_intent, "asus", context, MagicMock())
    assert res_risky.allowed is True
    assert res_risky.requires_approval is True
    assert "confirmation is required" in res_risky.approval_prompt


# ---------------------------------------------------------------------------
# 17. Human Approval Integration & Resolution
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_approval_integration_and_resolution():
    controller = ComputerUseTaskController.get_instance()
    task = controller.create_task("Perform account purchase")
    step = ComputerUseStep(
        step_id="step_purchase",
        action="ui.click",
        target="Submit Purchase for $84.99",
    )
    task.steps.append(step)

    obs = make_sample_observation("obs_appr")
    with patch("agent.ui_observer.UIObserver.observe", AsyncMock(return_value=obs)):
        res = await controller.execute_task_step(task, step)
        assert res["status"] == "waiting_for_approval"
        assert task.state == "WAITING_FOR_APPROVAL"
        assert task.requires_approval is True

        # Resolve with approve
        resolve_res = controller.resolve_approval(step.step_id, "approve")
        assert resolve_res["status"] == "success"
        assert task.state == "ACTIVE"
        assert task.requires_approval is False


# ---------------------------------------------------------------------------
# 18. Executor Device Routing & Screen Affinity
# ---------------------------------------------------------------------------
def test_executor_device_routing():
    controller = ComputerUseTaskController.get_instance()
    task = controller.create_task("Open Settings on ASUS", executor_device="asus")
    assert task.executor_device == "asus"

    # Cross-device check: Lenovo task
    task_lenovo = controller.create_task("Open App on Lenovo", executor_device="lenovo")
    assert task_lenovo.executor_device == "lenovo"


# ---------------------------------------------------------------------------
# 19. Task Checkpointing & Resume
# ---------------------------------------------------------------------------
def test_task_checkpointing_and_resume():
    controller = ComputerUseTaskController.get_instance()
    task = controller.create_task("Multi-step configuration")
    step1 = ComputerUseStep(step_id="s1", action="ui.click", target="Settings")
    step2 = ComputerUseStep(step_id="s2", action="ui.type", target="Search", args={"text": "Display"})
    task.steps.extend([step1, step2])

    task.current_step_index = 1
    task.last_observation_id = "obs_step1"
    task.action_history.append({"step_id": "s1", "action": "ui.click", "verified": True})

    d = task.to_dict()
    assert d["current_step_index"] == 1
    assert d["last_observation_id"] == "obs_step1"
    assert len(d["action_history"]) == 1
    assert len(d["steps"]) == 2


# ---------------------------------------------------------------------------
# 20. Capability Preference Over GUI Automation
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_capability_preference_over_gui_automation():
    from hub.planner import GoalPlanner
    from hub.goals import Goal, GoalManager

    planner = GoalPlanner.get_instance()
    gm = GoalManager.get_instance()

    # "Open Chrome" should choose app.launch, NOT ui.click(Start) -> type Chrome
    plan_app = planner._reason_from_capabilities(
        request="Open Chrome",
        capabilities=["app.launch", "ui.click", "ui.observe"],
        target_device="asus",
    )
    assert plan_app["steps"][0]["capability"] == "app.launch"

    # "Open Downloads" should choose fs.open or fs.list, NOT GUI automation
    plan_file = planner._reason_from_capabilities(
        request="Open my Calc review PDF and switch to it",
        capabilities=["fs.open", "window.list", "window.focus", "ui.click"],
        target_device="asus",
    )
    assert plan_file["steps"][0]["capability"] == "fs.open"


# ---------------------------------------------------------------------------
# 21. No Arbitrary Shell Fallback for Missing UI Capabilities
# ---------------------------------------------------------------------------
def test_no_arbitrary_shell_fallback():
    from hub.planner import GoalPlanner
    planner = GoalPlanner.get_instance()

    # When UI capability is missing, planner should block truthfully rather than generating powershell
    plan = planner._reason_from_capabilities(
        request="Fabricate a custom plastic hinge",
        capabilities=["app.launch", "fs.open", "shell.run"],
        target_device="asus",
    )
    assert plan.get("unachievable_reason") is not None
    assert len(plan.get("steps", [])) == 0


# ---------------------------------------------------------------------------
# 22. Safe Clipboard Operations
# ---------------------------------------------------------------------------
def test_clipboard_safe_operations():
    executor = UIActionExecutor.get_instance()

    with patch("win32clipboard.OpenClipboard"), \
         patch("win32clipboard.CloseClipboard"), \
         patch("win32clipboard.GetClipboardData", return_value="Cortex Clipboard Data"), \
         patch("win32clipboard.SetClipboardData"), \
         patch("win32clipboard.EmptyClipboard"):

        # Write
        res_w = executor.clipboard_write({"text": "Hello Cortex"})
        assert res_w["status"] == "success"

        # Read
        res_r = executor.clipboard_read()
        assert res_r["status"] == "success"
        assert res_r["content"] == "Cortex Clipboard Data"


# ---------------------------------------------------------------------------
# 23. General Goal Escalation to Computer Use (Pre-A11 Generalization)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_general_goal_escalation_to_computer_use():
    from hub.pipeline.normalizer import InputNormalizer
    from hub.planner import GoalPlanner

    si = SemanticInterpreter()
    gp = GoalPlanner.get_instance()
    ctx = AssembledContext(source_device="asus")
    caps = [
        {"name": "app.launch", "availability": True},
        {"name": "ui.observe", "availability": True},
        {"name": "ui.click", "availability": True},
        {"name": "ui.type", "availability": True},
        {"name": "ui.scroll", "availability": True},
        {"name": "ui.hotkey", "availability": True},
    ]

    general_goals = [
        "change my display to 60 hertz",
        "turn on dark mode",
        "make the text larger",
        "switch the default browser",
        "open Settings and show me my battery usage",
        "find the Bluetooth page",
    ]

    for goal_text in general_goals:
        norm = InputNormalizer.normalize(goal_text)
        intent = await si.interpret(norm, ctx)
        assert intent.type == "GOAL", f"Expected GOAL intent for '{goal_text}', got {intent.type}"
        assert intent.required_effect == "desktop_state_change", f"Expected desktop_state_change for '{goal_text}'"
        assert intent.arguments.get("execution_mode") == "computer_use"
        assert intent.arguments.get("target_device") == "asus"
        assert "success_condition" in intent.arguments

        plan = gp._reason_from_capabilities(request=goal_text, capabilities=caps, target_device="asus")
        steps = [s["capability"] for s in plan.get("steps", [])]
        assert len(steps) >= 3, f"Expected multi-step plan for '{goal_text}', got {steps}"
        assert "app.launch" in steps
        assert "ui.observe" in steps
        assert "ui.click" in steps


# ---------------------------------------------------------------------------
# 24. Truthful Capability Limitation for Non-Desktop Physical / Inquiries
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_truthful_limitation_for_non_desktop_goals():
    from hub.pipeline.normalizer import InputNormalizer
    from hub.planner import GoalPlanner

    si = SemanticInterpreter()
    gp = GoalPlanner.get_instance()
    ctx = AssembledContext(source_device="asus")
    caps = [
        {"name": "app.launch", "availability": True},
        {"name": "ui.observe", "availability": True},
        {"name": "ui.click", "availability": True},
    ]

    # Physical fabrication request
    norm = InputNormalizer.normalize("3d print a phone case")
    intent = await si.interpret(norm, ctx)
    assert intent.required_effect != "desktop_state_change"
    plan = gp._reason_from_capabilities(request="3d print a phone case", capabilities=caps, target_device="asus")
    assert plan.get("unachievable_reason") is not None
    assert len(plan.get("steps", [])) == 0

    # Conversational question
    norm_q = InputNormalizer.normalize("what is the capital of France?")
    intent_q = await si.interpret(norm_q, ctx)
    assert intent_q.type in ("QUESTION", "CONVERSATION")


# ---------------------------------------------------------------------------
# 25. Compound Goal Decomposition (e.g. Notepad Typing without Settings Over-selection)
# -------------------------------------------------------------------
@pytest.mark.asyncio
async def test_compound_goal_decomposition_and_notepad():
    from hub.pipeline.normalizer import InputNormalizer
    from hub.planner import GoalPlanner

    si = SemanticInterpreter()
    gp = GoalPlanner.get_instance()
    ctx = AssembledContext(source_device="asus")
    caps = [
        {"name": "app.launch", "availability": True},
        {"name": "ui.observe", "availability": True},
        {"name": "ui.click", "availability": True},
        {"name": "ui.type", "availability": True},
    ]

    goal_text = "open Notepad and type Cortex physical test."
    norm = InputNormalizer.normalize(goal_text)
    intent = await si.interpret(norm, ctx)
    assert intent.type == "GOAL"
    assert intent.required_effect == "compound_app_computer_use"
    assert intent.arguments.get("app") == "Notepad"
    assert "type" in intent.arguments.get("subgoal", "").lower()

    # Verify plan launches Notepad, NOT Settings!
    plan = gp._reason_from_capabilities(
        request=goal_text,
        capabilities=caps,
        target_device="asus",
        intent_args=intent.arguments,
    )
    steps = plan.get("steps", [])
    assert len(steps) >= 3
    launch_step = steps[0]
    assert launch_step["capability"] == "app.launch"
    assert launch_step["arguments"]["app"] == "Notepad"
    assert launch_step["arguments"]["app"] != "Settings"

    # Verify that the full user request is NEVER passed as a UI target selector
    for step in steps:
        if step["capability"] == "ui.click":
            assert step["arguments"].get("target") != goal_text
            assert step["arguments"].get("target") == "Text Editor"


# ---------------------------------------------------------------------------
# 26. Contextual Computer Use, Grounded Perception, and Corrections
# -------------------------------------------------------------------
@pytest.mark.asyncio
async def test_contextual_computer_use_and_corrections():
    from hub.pipeline.normalizer import InputNormalizer

    si = SemanticInterpreter()
    ctx = AssembledContext(source_device="asus")

    # 1. Contextual Return
    intent_back = await si.interpret(InputNormalizer.normalize("Take me back to the app I was using before."), ctx)
    assert intent_back.required_effect == "switch_previous_app"

    # 2. Grounded Perception
    intent_perceive = await si.interpret(InputNormalizer.normalize("Can you see what I'm doing right now?"), ctx)
    assert intent_perceive.required_effect == "grounded_screen_perception"

    # 3. Verification Query
    intent_change = await si.interpret(InputNormalizer.normalize("Did you change it?"), ctx)
    assert intent_change.required_effect == "check_last_task_status"

    # 4. User Correction
    intent_corr = await si.interpret(InputNormalizer.normalize("I said Notepad, not Settings"), ctx)
    assert intent_corr.required_effect == "user_correction"
    assert intent_corr.arguments.get("intended_app") == "Notepad"
    assert intent_corr.arguments.get("rejected_app") == "Settings"

    # 5. Deictic Refresh Rate
    intent_deictic = await si.interpret(InputNormalizer.normalize("Change it back to 120 Hertz."), ctx)
    assert intent_deictic.required_effect == "desktop_state_change"
    assert intent_deictic.arguments.get("execution_mode") == "computer_use"


