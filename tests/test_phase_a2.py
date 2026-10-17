"""
tests/test_phase_a2.py
----------------------
Comprehensive test suite for CORTEX PHASE A2 — Screenshot & Screen Perception.

Covers:
1. Real / Mock Screenshot Provider (screen.capture, bounded region, display index, bounded retention)
2. Vision Provider Abstraction (base class, MockVisionProvider, GeminiVisionProvider fallback)
3. screen.analyze & Structured Visual Observation (ScreenObservation schema, bounded storage)
4. intent_screenshot & Backward Compatibility (no dead aliases or 'unsupported' errors)
5. Direct Conversational Routing ("What's on my screen?", error inspection, etc.)
6. World State Signal Publication (bounded signals only: foreground_app, summary, last_observed_at)
7. Planner Capability Context & Goal Planning (screen.capture -> screen.analyze)
8. Visual Verification Flow (browser.open_url -> screen.capture -> screen.analyze)
9. Vision Provider Unavailable Behavior (refuse to fabricate visual state)
10. Privacy & Safety Invariants (no continuous capture, bounded retention, no raw byte dumps)
11. Production DB Isolation & Safety Guard
"""

import os
import json
import pytest
import asyncio
from pathlib import Path
from unittest.mock import MagicMock, AsyncMock, patch
from datetime import datetime, timezone

from PIL import Image

# Agent / Executor
from agent.executor import handle_screen_capture, execute, get_capabilities, HANDLERS

# Hub Screen Perception & Vision Provider
from hub.screen_perception import ScreenObservation, ScreenPerceptionManager
from hub.vision_provider import (
    VisionProvider,
    MockVisionProvider,
    GeminiVisionProvider,
    get_vision_provider,
    set_vision_provider_override,
)

# Hub Core
from hub.control_layer import CortexControlLayer
from hub.agent_harness import AgentHarness
from hub.intent_schema import ActionPlan, ModelResult
from hub.capability_registry import get_core_capabilities
from hub.goals import GoalManager, GoalPlanner
from hub.world_state import WorldStateManager
import hub.database as database


# ===========================================================================
# 1. SCREENSHOT CAPABILITY & BOUNDED REGION / DISPLAY TESTS
# ===========================================================================

def test_screen_capture_execution(tmp_path):
    """Test screen.capture saves a valid image artifact and returns metadata."""
    dummy_img = Image.new("RGB", (800, 600), color="blue")
    
    with patch("PIL.ImageGrab.grab", return_value=dummy_img):
        res = handle_screen_capture({"target_device": "asus", "display_index": 0})
        assert res["status"] == "success"
        data = res["data"]
        assert data["width"] == 800
        assert data["height"] == 600
        assert data["display_index"] == 0
        assert data["source_device"] == "asus"
        assert Path(data["image_ref"]).exists()


def test_screen_capture_bounded_region(tmp_path):
    """Test screen.capture with bounded bounding box (region)."""
    dummy_crop = Image.new("RGB", (200, 150), color="green")
    
    with patch("PIL.ImageGrab.grab", return_value=dummy_crop) as mock_grab:
        res = handle_screen_capture({
            "target_device": "asus",
            "region": {"x": 50, "y": 50, "width": 200, "height": 150}
        })
        assert res["status"] == "success"
        mock_grab.assert_called_once_with(bbox=(50, 50, 250, 200))
        assert res["data"]["width"] == 200
        assert res["data"]["height"] == 150


def test_screen_capture_bounded_retention(tmp_path):
    """Test ephemeral screenshot retention automatically prunes older files."""
    test_dir = tmp_path / "screenshots"
    test_dir.mkdir()
    
    # Create 25 dummy screenshot files
    created_files = []
    for i in range(25):
        f = test_dir / f"screenshot_20261013_{i:06d}.png"
        f.write_text("dummy")
        created_files.append(f)
    
    assert len(list(test_dir.glob("screenshot_*.png"))) == 25
    
    # Prune with max_keep=10
    pruned = ScreenPerceptionManager.cleanup_ephemeral_screenshots(test_dir, max_keep=10, max_age_hours=24)
    remaining = list(test_dir.glob("screenshot_*.png"))
    assert len(remaining) == 10
    assert pruned == 15


# ===========================================================================
# 2. VISION PROVIDER ABSTRACTION & FALLBACK TESTS
# ===========================================================================

@pytest.mark.asyncio
async def test_mock_vision_provider():
    """Verify MockVisionProvider returns structured, high-confidence observations."""
    provider = MockVisionProvider(
        summary="VS Code editor open displaying test_phase_a2.py",
        visible_apps=["VS Code", "Terminal"],
        visible_text_fragments=["def test_mock_vision_provider", "pytest"],
        confidence=0.96,
    )
    assert provider.is_available() is True
    assert provider.provider_name == "mock_vision"
    
    obs = await provider.analyze_image("dummy_path.png", question="What is open?", mode="describe")
    assert isinstance(obs, ScreenObservation)
    assert "VS Code" in obs.summary
    assert "VS Code" in obs.visible_apps
    assert obs.confidence == 0.96
    assert obs.provenance["provider"] == "mock_vision"


@pytest.mark.asyncio
async def test_vision_provider_override():
    """Verify set_vision_provider_override switches active provider cleanly."""
    set_vision_provider_override(None)
    default_p = get_vision_provider()
    
    mock_p = MockVisionProvider(summary="Custom mock screen")
    set_vision_provider_override(mock_p)
    assert get_vision_provider() is mock_p
    
    # Reset
    set_vision_provider_override(None)


def test_gemini_vision_provider_fallback_logic():
    """Verify GeminiVisionProvider model fallback order."""
    g_prov = GeminiVisionProvider(api_key="test_key", model_name="gemini-3.8-flash")
    assert g_prov.model_name == "gemini-3.8-flash"
    assert "gemini-3.5-flash" in g_prov.FALLBACK_MODELS
    assert "gemini-2.5-flash-lite" in g_prov.FALLBACK_MODELS


# ===========================================================================
# 3. SCREEN.ANALYZE & STRUCTURED VISUAL OBSERVATION TESTS
# ===========================================================================

@pytest.mark.asyncio
async def test_screen_analyze_capability_and_observation_structure(tmp_path):
    """Test screen.analyze execution produces structured ScreenObservation."""
    test_img = tmp_path / "screen.png"
    Image.new("RGB", (640, 480), color="red").save(str(test_img))
    
    mock_p = MockVisionProvider(
        summary="Settings window open showing network settings",
        visible_apps=["Settings"],
        visible_text_fragments=["Wi-Fi", "Connected"],
        regions=[{"label": "Wi-Fi toggle", "x": 100, "y": 200, "width": 50, "height": 30}],
        confidence=0.92,
    )
    set_vision_provider_override(mock_p)
    
    control = CortexControlLayer({"asus": MagicMock()})
    
    res = await control.execute_and_observe(
        "asus",
        "screen.analyze",
        {"image_ref": str(test_img), "question": "What settings are open?"},
    )
    
    assert res["status"] == "success"
    data = res["data"]
    assert data["summary"] == "Settings window open showing network settings"
    assert "Settings" in data["visible_apps"]
    assert "Wi-Fi" in data["visible_text_fragments"]
    assert len(data["regions"]) == 1
    assert data["confidence"] == 0.92
    assert data["source_image_reference"] == str(test_img)
    
    # Verify cached observation in ScreenPerceptionManager
    latest = ScreenPerceptionManager.get_latest_observation("asus")
    assert latest is not None
    assert latest.summary == data["summary"]


# ===========================================================================
# 4. INTENT_SCREENSHOT & BACKWARD COMPATIBILITY TESTS
# ===========================================================================

def test_capabilities_advertises_screen_and_legacy_aliases():
    """Verify get_capabilities advertises screen.capture and legacy aliases."""
    caps = get_capabilities()
    assert "screen.capture" in caps
    assert "screenshot.read" in caps
    assert "ui.screenshot" in caps
    assert "take_screenshot" in HANDLERS
    assert "screen.capture" in HANDLERS


def test_intent_screenshot_mapping_in_agent_harness():
    """Verify legacy intent_screenshot maps to screen.capture without unsupported intent error."""
    control = CortexControlLayer({"asus": MagicMock()})
    harness = AgentHarness(control)
    
    assert harness._map_intent_to_capability("intent_screenshot") == "screen.capture"
    assert harness._map_intent_to_capability("take_screenshot") == "screen.capture"
    assert harness._map_intent_to_capability("screenshot.read") == "screen.capture"
    assert harness._map_intent_to_capability("retrieve_recent_screenshot") == "screen.capture"
    assert harness._map_intent_to_capability("screen.capture") == "screen.capture"
    assert harness._map_intent_to_capability("screen.analyze") == "screen.analyze"


@pytest.mark.asyncio
async def test_intent_screenshot_dispatch_flow():
    """Verify executing intent_screenshot via harness dispatches properly."""
    control = CortexControlLayer({"asus": MagicMock()})
    harness = AgentHarness(control)
    control.execute_command = AsyncMock()
    
    await database.update_device_capabilities("asus", "windows", ["screen.capture"])
    
    with patch.object(control, "validate_target_and_capability", return_value={"valid": True, "target": "asus"}), \
         patch.object(control, "check_permissions", return_value=MagicMock(allowed=True, needs_confirmation=False, tier=1)):
        with patch.object(harness.router, "generate_result", return_value=ModelResult(
            type="ACTION",
            action_plan=ActionPlan(
                intent="intent_screenshot",
                source_device="asus",
                target_device_reference=None,
                capability="screen.capture",
                action="screen.capture",
                arguments={},
                requires_confirmation=False,
                confidence=0.95,
                user_facing_summary="taking a screenshot",
            ),
        )):
            res = await harness.process_request(
                "asus",
                "take a screenshot",
                {"device": {"capabilities": ["screen.capture"]}},
                None,
            )
            assert res.status == "success", f"res was {res.status} with message: {res.message}"
            assert "intent_screenshot is not supported" not in res.message.lower()


# ===========================================================================
# 5. DIRECT CONVERSATIONAL ROUTING TESTS
# ===========================================================================

@pytest.mark.asyncio
async def test_direct_conversational_screen_query():
    """
    User: 'What's on my screen?'
    Verifies:
    - Real screen capture dispatched
    - Vision provider called
    - Response grounded in visual observation
    - No unsupported intent error
    """
    mock_p = MockVisionProvider(
        summary="A web browser showing GitHub repository Cortex with open pull requests.",
        visible_apps=["Chrome", "GitHub"],
        visible_text_fragments=["Cortex", "Pull Requests", "Phase A2"],
        confidence=0.98,
    )
    set_vision_provider_override(mock_p)
    
    control = CortexControlLayer({"asus": MagicMock()})
    harness = AgentHarness(control)
    
    async def mock_exec_obs(dev_id, cap, args=None, **kwargs):
        if cap == "screen.capture":
            return {
                "status": "success",
                "data": {"image_ref": "test_screen.png", "width": 1920, "height": 1080},
            }
        return {"status": "success", "message": "done"}
    
    control.execute_and_observe = AsyncMock(side_effect=mock_exec_obs)
    
    result = await harness.process_request(
        "asus",
        "What's on my screen?",
        {"device": {"capabilities": ["screen.capture"]}},
        None,
    )
    
    assert result.status == "success"
    assert "GitHub" in result.message or "Chrome" in result.message
    assert "pull requests" in result.message.lower()


@pytest.mark.asyncio
async def test_direct_conversational_error_inspection():
    """
    User: 'What error is showing on my screen?'
    Verifies error interpretation from visible content.
    """
    mock_p = MockVisionProvider(
        summary="Dialog box showing: 'Connection refused: Failed to connect to server on port 8000.'",
        visible_apps=["Error Dialog", "Terminal"],
        visible_text_fragments=["Connection refused", "port 8000"],
        confidence=0.95,
    )
    set_vision_provider_override(mock_p)
    
    control = CortexControlLayer({"asus": MagicMock()})
    harness = AgentHarness(control)
    
    async def mock_exec_obs(dev_id, cap, args=None, **kwargs):
        if cap == "screen.capture":
            return {
                "status": "success",
                "data": {"image_ref": "err_screen.png", "width": 1920, "height": 1080},
            }
        return {"status": "success", "message": "done"}
    
    control.execute_and_observe = AsyncMock(side_effect=mock_exec_obs)
    
    result = await harness.process_request(
        "asus",
        "What error is showing on my screen?",
        {"device": {"capabilities": ["screen.capture"]}},
        None,
    )
    
    assert result.status == "success"
    assert "Connection refused" in result.message or "port 8000" in result.message


# ===========================================================================
# 6. WORLD STATE INTEGRATION TESTS
# ===========================================================================

@pytest.mark.asyncio
async def test_world_state_signal_publication(tmp_path):
    """
    Verify screen signals (foreground_app, summary, last_observed_at) are published to World State,
    and raw image bytes are NEVER placed into World State.
    """
    test_img = tmp_path / "screen_ws.png"
    Image.new("RGB", (640, 480), color="gray").save(str(test_img))
    
    mock_p = MockVisionProvider(
        summary="Terminal running python test suite",
        visible_apps=["Windows Terminal"],
        confidence=0.95,
    )
    set_vision_provider_override(mock_p)
    
    control = CortexControlLayer({"asus": MagicMock()})
    res = await control.execute_and_observe(
        "asus",
        "screen.analyze",
        {"image_ref": str(test_img)},
    )
    assert res["status"] == "success"
    
    # Check fused WorldState
    wsm = WorldStateManager.get_instance()
    fused = await wsm.get_fused_world_state("asus")
    assert "screen_perception" in fused.to_dict()
    sp = fused.screen_perception
    assert sp["foreground_app"] == "Windows Terminal"
    assert "Terminal" in sp["summary"]
    assert sp["last_observed_at"] is not None
    
    # Invariant: NO raw screenshot bytes in world state
    dumped = json.dumps(fused.to_dict())
    assert "base64" not in dumped.lower()
    assert len(dumped) < 10000


# ===========================================================================
# 7. PLANNER INTEGRATION TESTS
# ===========================================================================

def test_capability_registry_exposes_screen_capabilities():
    """Verify CapabilityRegistry exposes screen.capture and screen.analyze."""
    caps = get_core_capabilities()
    assert "screen.capture" in caps
    assert "screen.analyze" in caps
    
    sc = caps["screen.capture"]
    assert sc.classification == "read"
    assert sc.risk == "low"
    assert sc.target_type == "device"
    
    sa = caps["screen.analyze"]
    assert sa.classification == "read"
    assert sa.risk == "low"
    assert sa.target_type == "hub"
    assert "image_ref" in sa.arguments or "screenshot" in sa.arguments


@pytest.mark.asyncio
async def test_planner_screen_inspection_goal():
    """
    Goal: 'Look at my screen and tell me why this app failed.'
    Expected plan:
    1. screen.capture
    2. screen.analyze(question="Why did this app fail?")
    No invented magic capabilities.
    """
    gm = GoalManager.get_instance()
    planner = GoalPlanner.get_instance(goal_manager=gm)
    
    mock_p = MockVisionProvider(summary="App failure screen")
    set_vision_provider_override(mock_p)
    
    goal = await gm.create_goal(
        title="Inspect app failure",
        original_request="Look at my screen and tell me why this app failed.",
    )
    
    plan_res = await planner.plan_goal(goal_id=goal.goal_id, target_device="asus")
    assert plan_res["status"] == "planned"
    assert plan_res["step_count"] == 2
    
    steps = plan_res["steps"]
    assert steps[0]["capability"] == "screen.capture"
    assert steps[1]["capability"] == "screen.analyze"
    assert "question" in steps[1]["arguments"]


# ===========================================================================
# 8. POST-ACTION VISUAL VERIFICATION TESTS
# ===========================================================================

@pytest.mark.asyncio
async def test_planner_visual_verification_goal():
    """
    Goal: 'Open YouTube and check that it opened.'
    Expected plan:
    1. browser.open_url(url="https://www.youtube.com")
    2. screen.capture
    3. screen.analyze
    """
    gm = GoalManager.get_instance()
    planner = GoalPlanner.get_instance(goal_manager=gm)
    
    mock_p = MockVisionProvider(summary="YouTube visibly open")
    set_vision_provider_override(mock_p)
    
    goal = await gm.create_goal(
        title="Open YouTube with visual verification",
        original_request="Open YouTube and check that it opened.",
    )
    
    plan_res = await planner.plan_goal(goal_id=goal.goal_id, target_device="asus")
    assert plan_res["status"] == "planned"
    assert plan_res["step_count"] == 3
    
    steps = plan_res["steps"]
    assert steps[0]["capability"] == "browser.open_url"
    assert steps[0]["arguments"]["url"] == "https://www.youtube.com"
    assert steps[1]["capability"] == "screen.capture"
    assert steps[2]["capability"] == "screen.analyze"


@pytest.mark.asyncio
async def test_conversational_visual_verification_flow():
    """
    Conversational request: 'Open YouTube and check that it opened.'
    Verifies:
    - browser.open_url dispatched
    - screen.capture dispatched
    - screen.analyze confirms visible YouTube page
    """
    mock_p = MockVisionProvider(
        summary="A web browser opened displaying the YouTube homepage with video recommendations.",
        visible_apps=["Google Chrome", "YouTube"],
        confidence=0.97,
    )
    set_vision_provider_override(mock_p)
    
    control = CortexControlLayer({"asus": MagicMock()})
    harness = AgentHarness(control)
    
    executed_caps = []
    async def mock_exec(dev_id, cap, args=None, **kwargs):
        executed_caps.append(cap)
        if cap == "screen.capture":
            return {"status": "success", "data": {"image_ref": "yt_screen.png", "width": 1920, "height": 1080}}
        if cap == "screen.analyze":
            obs = await mock_p.analyze_image("yt_screen.png", question="Is YouTube visibly open?")
            return {"status": "success", "data": obs.to_dict()}
        return {"status": "success", "message": "ok"}
        
    control.execute_and_observe = AsyncMock(side_effect=mock_exec)
    
    res = await harness.process_request(
        "asus",
        "Open YouTube and check that it opened.",
        {"device": {"capabilities": ["browser.open_url", "screen.capture"]}},
        None,
    )
    
    assert res.status == "success"
    assert "browser.open_url" in executed_caps
    assert "screen.capture" in executed_caps
    assert "screen.analyze" in executed_caps
    assert "youtube" in res.message.lower()
    assert "verified" in res.message.lower()


# ===========================================================================
# 9. VISION PROVIDER UNAVAILABLE FAILURE TESTS
# ===========================================================================

@pytest.mark.asyncio
async def test_screen_analyze_fails_when_provider_unavailable():
    """Verify screen.analyze returns failure when no vision provider is available."""
    unavail_p = MockVisionProvider(is_available=False)
    set_vision_provider_override(unavail_p)
    
    control = CortexControlLayer({"asus": MagicMock()})
    res = await control.execute_and_observe(
        "asus",
        "screen.analyze",
        {"image_ref": "mock.png"},
    )
    assert res["status"] == "failure"
    assert "screen analysis provider unavailable" in res["message"].lower()


@pytest.mark.asyncio
async def test_conversational_fails_when_provider_unavailable_no_fabrication():
    """
    When vision provider is unavailable, conversational screen question must report:
    'screen analysis provider unavailable'
    and NOT fabricate or guess visual state from foreground apps or model memory.
    """
    unavail_p = MockVisionProvider(is_available=False)
    set_vision_provider_override(unavail_p)
    
    control = CortexControlLayer({"asus": MagicMock()})
    harness = AgentHarness(control)
    
    control.execute_and_observe = AsyncMock(return_value={
        "status": "success",
        "data": {"image_ref": "mock.png", "width": 1920, "height": 1080},
    })
    
    res = await harness.process_request(
        "asus",
        "What's on my screen?",
        {"device": {"capabilities": ["screen.capture"]}},
        None,
    )
    
    assert res.status == "error"
    assert "screen analysis provider unavailable" in res.message.lower()
    # Ensure it didn't fabricate a fake answer
    assert "chrome" not in res.message.lower()
    assert "youtube" not in res.message.lower()


# ===========================================================================
# 10. PRIVACY & SAFETY INVARIANTS
# ===========================================================================

def test_privacy_no_continuous_recording_invariant():
    """Verify there are no background loops or timers capturing screenshots automatically."""
    from hub.screen_perception import ScreenPerceptionManager
    
    manager = ScreenPerceptionManager()
    assert not hasattr(manager, "start_continuous_recording")
    assert not hasattr(manager, "background_loop")
    assert not hasattr(manager, "continuous_task")


def test_logs_and_observations_contain_no_raw_bytes():
    """Verify ScreenObservation objects and serialized representations never contain raw bytes."""
    obs = ScreenObservation(
        observation_id="obs_123",
        device_id="asus",
        captured_at=datetime.now(timezone.utc).isoformat(),
        summary="Test screen",
        visible_apps=["App1"],
        visible_text_fragments=["Text1"],
        regions=[],
        confidence=0.9,
        source_image_reference="screenshot_123.png",
        provenance={"test": "mock"},
    )
    d = obs.to_dict()
    for v in d.values():
        assert not isinstance(v, bytes)
        assert "base64" not in str(v).lower()


# ===========================================================================
# 11. PRODUCTION DB ISOLATION GUARD TEST
# ===========================================================================

def test_production_db_isolation():
    """Verify that during Phase A2 testing, the test database is completely isolated."""
    assert database.DB_PATH.name == "test_cortex.db"
    assert "data/cortex.db" not in str(database.DB_PATH).replace("\\", "/")
