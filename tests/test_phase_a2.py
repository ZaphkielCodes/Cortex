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
    """Obsolete alias promotion is replaced by canonical IDs and rejection of NL aliases."""
    harness = AgentHarness(CortexControlLayer())
    for alias in ["intent_screenshot","take_screenshot","retrieve_recent_screenshot"]:
        assert harness._map_intent_to_capability(alias) == ""
    assert harness._map_intent_to_capability("screen.capture") == "screen.capture"
    assert harness._map_intent_to_capability("screen.analyze") == "screen.analyze"



@pytest.mark.asyncio
async def test_intent_screenshot_dispatch_flow():
    """An explicit canonical decision dispatches capture once and preserves its reference."""
    harness, ctrl, model = scripted_harness([call("screen.capture")], [{"status":"success","data":{"image_ref":"fixture.png"}}])
    resp = await harness.process_request("asus", "take a screenshot", {})
    assert resp.status == "success" and resp.data["image_ref"] == "fixture.png"
    assert ctrl.execute_and_observe.await_args.args[1] == "screen.capture"
    assert ctrl.execute_and_observe.await_count == 1



# ===========================================================================
# 5. DIRECT CONVERSATIONAL ROUTING TESTS
# ===========================================================================

@pytest.mark.asyncio
async def test_direct_conversational_screen_query(monkeypatch):
    """Actual analysis provider boundary receives the explicit image and question."""
    provider = MockVisionProvider(summary="A browser showing GitHub Cortex pull requests.",visible_apps=["Chrome"],confidence=.98)
    provider.locality = "local_device"
    set_vision_provider_override(provider)
    provider.analyze_image = AsyncMock(wraps=provider.analyze_image)
    harness, _ = await native_harness(monkeypatch, EvidenceModel("screen.analyze",{"image_ref":"fixture.png","question":"What's on my screen?"}))
    resp = await harness.process_request("asus", "What's on my screen?", {})
    assert resp.status == "success" and "GitHub" in resp.message
    assert "pull requests" in resp.message
    assert provider.analyze_image.await_args.kwargs["image_ref"] == "fixture.png"



@pytest.mark.asyncio
async def test_direct_conversational_error_inspection(monkeypatch):
    """Visible errors reach interpretation as evidence, not guessed foreground state."""
    provider = MockVisionProvider(summary="Connection refused on port 8000.",visible_text_fragments=["Connection refused","8000"])
    provider.locality = "local_device"
    set_vision_provider_override(provider)
    harness, _ = await native_harness(monkeypatch, EvidenceModel("screen.analyze",{"image_ref":"error.png","question":"What error?"}))
    resp = await harness.process_request("asus", "What error is showing?", {})
    assert resp.status == "success"
    assert "Connection refused" in resp.message and "8000" in resp.message
    assert resp.data["source_image_reference"] == "error.png"



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
    from model_contract_fixtures import PlanModel, plan_step
    planner = GoalPlanner.get_instance(goal_manager=gm, model_router=PlanModel([plan_step("screen.capture"),plan_step("screen.analyze",image_ref="fixture.png",question="Why did this app fail?")]))
    
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
    from model_contract_fixtures import PlanModel, plan_step
    planner = GoalPlanner.get_instance(goal_manager=gm, model_router=PlanModel([plan_step("browser.open_url",url="https://www.youtube.com"),plan_step("screen.capture"),plan_step("screen.analyze",image_ref="fixture.png",question="Is YouTube visible?")]))
    
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
    """Verification requires a second observation after the launch, with retained image ID."""
    provider = MockVisionProvider(summary="YouTube visibly open")
    provider.locality = "local_device"
    set_vision_provider_override(provider)
    harness, ctrl, model = scripted_harness([
        dict(call("browser.open_url",url="https://www.youtube.com"),continue_after_result=True,
             success_condition=[{"source":"capability","capability":"screen.analyze","path":["data","summary"],
                                 "operator":"equals","value":"YouTube is visibly open"}]),
        dict(call("screen.capture"),continue_after_result=True),
        call("screen.analyze",image_ref="yt_screen.png",question="Is YouTube open?"),
        {"decision":"RESPOND","response_text":"YouTube is verified open."}],
        [{"status":"success","data":{"opened":True}},
         {"status":"success","data":{"image_ref":"yt_screen.png"}},
         {"status":"success","data":{"summary":"YouTube is visibly open","source_image_reference":"yt_screen.png"}}],answer="YouTube is verified open.")
    res = await harness.process_request("asus", "Open YouTube and check that it opened.", {})
    assert res.status == "success" and "verified" in res.message
    assert [c.args[1] for c in ctrl.execute_and_observe.await_args_list] == ["browser.open_url","screen.capture","screen.analyze"]
    assert ctrl.execute_and_observe.await_args.args[2]["image_ref"] == "yt_screen.png"
    assert "yt_screen.png" in model.calls[2][1]["system_prompt"]



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
async def test_conversational_fails_when_provider_unavailable_no_fabrication(monkeypatch):
    """An unavailable vision capability must stop without fabricating visible contents."""
    provider = MockVisionProvider(is_available=False)
    provider.locality = "local_device"
    set_vision_provider_override(provider)
    harness, ctrl = await native_harness(monkeypatch, EvidenceModel("screen.analyze",{"image_ref":"fixture.png"}))
    ctrl.execute_and_observe = AsyncMock(wraps=ctrl.execute_and_observe)
    resp = await harness.process_request("asus", "What's on my screen?", {})
    assert resp.status in {"unsupported","error"}
    assert "Chrome" not in resp.message and "YouTube" not in resp.message
    assert "unavailable" in resp.message.lower()
    assert "summary" not in (resp.data or {})



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

from model_contract_fixtures import native_harness, EvidenceModel, scripted_harness, call
