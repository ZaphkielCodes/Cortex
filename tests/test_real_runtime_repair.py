"""
tests/test_real_runtime_repair.py
----------------------------------
Physical-style regression test suite verifying real-runtime repairs across A1-A4:
1. Canonical command argument serialization (dict[str, Any] representation, boundary normalization).
2. Screenshot follow-up continuity ("Take a screenshot" -> "Read that screenshot" via screen.analyze, no invented capabilities).
3. Follow-up action continuity ("Can you check the weather?" -> "Okay, do it." resumes weather, asks location if missing, NEVER screenshots).
4. Weather location extraction (separating temporal modifiers "currently", "right now", "today" from place names).
5. Compound self-knowledge questions ("Do you have access to my Calendar and Gmail?" reports both independently).
6. Self-knowledge follow-up references ("What about Gmail?" stays in system introspection context, never falling through to generic LLM).
7. Device online state integrity (strictly requiring genuine live authenticated connection, no stale enrolled records reporting online).
8. User-safe error handling (short, consumer-safe error strings to TTS; no raw JSON, stack traces, or handler dumps spoken).
9. Strict production DB protection.
"""

import os
import json
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
import pytest
import pytest_asyncio

from agent.executor import execute, normalize_command_arguments
from agent.desktop_actions import close_app, launch_app
from hub.agent_harness import AgentHarness, extract_weather_location, sanitize_for_speech
from hub.control_layer import CortexControlLayer
from hub.database import init_db, enroll_device, add_integration, update_connection_state
from hub.intent_schema import ConversationalResponse, ActionPlan
from hub.models import CommandMessage
from hub.session_context import SessionContextManager
from hub.system_registry import SystemRegistry
from hub.weather_provider import WeatherProvider, set_weather_provider_override
from hub.vision_provider import VisionProvider, set_vision_provider_override
from hub.screen_perception import ScreenObservation, ScreenPerceptionManager


class DummyWeatherProvider(WeatherProvider):
    @property
    def provider_name(self) -> str:
        return "dummy_weather"

    async def get_current(self, location: str) -> dict:
        return {
            "location": location,
            "temperature": 22.0,
            "temperature_unit": "°C",
            "conditions": "Sunny",
            "humidity": 45,
            "feels_like": 22.0,
            "source": "dummy_weather",
        }

    async def get_forecast(self, location: str, days: int = 3) -> dict:
        return {
            "location": location,
            "forecast": [
                {"date": "2026-10-14", "conditions": "Sunny", "high": 24, "low": 12, "temperature_unit": "°C"}
            ]
        }


class DummyVisionProvider(VisionProvider):
    @property
    def provider_name(self) -> str:
        return "dummy_vision"

    @property
    def model_name(self) -> str:
        return "dummy_vision_model"

    async def analyze_image(self, image_ref: str, question: str = None, mode: str = "describe", device_id: str = "asus") -> ScreenObservation:
        return ScreenObservation(
            observation_id="obs_test1",
            device_id=device_id,
            captured_at="2026-10-14T00:00:00Z",
            source_image_reference=image_ref,
            summary="A desktop view with Chrome showing a dashboard.",
            visible_apps=["Google Chrome"],
            visible_text_fragments=["Dashboard", "Status: Active"],
        )


@pytest_asyncio.fixture
async def setup_test_env():
    await init_db()
    SystemRegistry.reset()
    ScreenPerceptionManager.reset()
    SessionContextManager._memory_cache.clear()

    # Enroll test devices
    await enroll_device("asus", "asus", "primary", "hash1")
    await enroll_device("test_dev", "Test Device", "worker", "hash2")

    # Set up mock providers
    w_prov = DummyWeatherProvider()
    set_weather_provider_override(w_prov)

    v_prov = DummyVisionProvider()
    set_vision_provider_override(v_prov)

    yield

    set_weather_provider_override(None)
    set_vision_provider_override(None)
    SystemRegistry.reset()
    ScreenPerceptionManager.reset()


# ===========================================================================
# Test 1: Canonical Command Argument Serialization & Punctuation Stripping
# ===========================================================================
@pytest.mark.asyncio
async def test_canonical_command_argument_serialization(setup_test_env):
    """
    Test 1:
    User: 'Can you close Google Chrome?'
    Verify canonical dict args arrive at executor and boundary normalization
    handles legacy serialized JSON cleanly without app.close-specific hacks.
    """
    # 1. Canonical dict arrives directly
    norm1 = normalize_command_arguments("app.close", {"app": "Google Chrome?", "graceful": True, "target_device": "asus"})
    assert isinstance(norm1, dict)
    assert norm1["app"] == "Google Chrome"  # Conversational punctuation stripped
    assert norm1["graceful"] is True

    # 2. Legacy stringified JSON list arrives at boundary
    legacy_json_list = ['{"app": "Google Chrome?", "graceful": true, "target_device": "asus"}']
    norm2 = normalize_command_arguments("app.close", legacy_json_list)
    assert isinstance(norm2, dict)
    assert norm2["app"] == "Google Chrome"
    assert norm2["graceful"] is True

    # 3. Test CommandMessage model serialization
    msg = CommandMessage(command_id="cmd1", command="app.close", args={"app": "Google Chrome", "graceful": True})
    assert isinstance(msg.args, dict)
    assert msg.args["app"] == "Google Chrome"

    # 4. Verify boundary normalization across capabilities
    # app.launch
    assert normalize_command_arguments("app.launch", ['{"app": "Notepad?"}']) == {"app": "Notepad"}
    # browser.open_url
    assert normalize_command_arguments("browser.open_url", ['{"url": "https://google.com"}']) == {"url": "https://google.com"}
    # screen.capture
    assert normalize_command_arguments("screen.capture", ['{"display_index": 0}']) == {"display_index": 0}
    # fs.delete
    assert normalize_command_arguments("fs.delete", ['{"path": "test.txt"}']) == {"path": "test.txt"}
    # window.focus
    assert normalize_command_arguments("window.focus", ['{"title": "Chrome"}']) == {"title": "Chrome"}
    # system.reboot
    assert normalize_command_arguments("system.reboot", ['{"target_device": "asus"}']) == {"target_device": "asus"}


# ===========================================================================
# Test 2: Screenshot Follow-Up Continuity
# ===========================================================================
@pytest.mark.asyncio
async def test_screenshot_followup_continuity(setup_test_env):
    """
    Test 2:
    'Take a screenshot.' -> succeeds, records reference.
    'Read that screenshot and tell me what you see.' -> reuses existing screenshot reference + screen.analyze.
    Must NOT invent capability.
    If no screenshot exists, explains that there is no recent screenshot.
    """
    control = CortexControlLayer()
    harness = AgentHarness(control)

    # 1. Take a screenshot
    control.execute_command = AsyncMock()
    with patch.object(control, "validate_target_and_capability", return_value={"valid": True, "target": "asus"}), \
         patch.object(control, "check_permissions", return_value=MagicMock(allowed=True, needs_confirmation=False, tier=1)):
        resp1 = await harness.process_request("asus", "Take a screenshot.", context={"device": {"capabilities": ["screen.capture"]}})
        assert resp1.status == "success"
        assert resp1.action_plan.action == "screen.capture"

    # Simulate executor returning screenshot path and updating session
    await SessionContextManager.set_screenshot_ref("asus", "c:/screenshots/shot1.png")
    sess = await SessionContextManager.get_session("asus")
    assert sess["references"].get("last_screenshot_ref") == "c:/screenshots/shot1.png"

    context = {"session": sess}
    resp2 = await harness.process_request("asus", "Read that screenshot and tell me what you see.", context=context)
    assert resp2.status == "success"
    assert "Google Chrome" in resp2.message or "dashboard" in resp2.message.lower()

    # 3. Follow-up: "what do you see there?"
    resp3 = await harness.process_request("asus", "what do you see there?", context=context)
    assert resp3.status == "success"
    assert "Google Chrome" in resp3.message or "dashboard" in resp3.message.lower()

    # 4. If no screenshot exists in session, explain gracefully
    ScreenPerceptionManager.reset()
    empty_sess = {"references": {}}
    resp_empty = await harness.process_request("asus", "Read that screenshot.", context={"session": empty_sess})
    assert resp_empty.status == "error"
    assert "no recent screenshot to inspect" in resp_empty.message.lower()


# ===========================================================================
# Test 3: Follow-Up Action Continuity
# ===========================================================================
@pytest.mark.asyncio
async def test_followup_action_continuity(setup_test_env):
    """
    Test 3:
    User: 'Can you check the weather?'
    Cortex: 'Yes, I can check live weather conditions and forecasts.'
    User: 'Okay, do it.'
    Must continue weather intent. If location absent, ask for location. Must NEVER take screenshot.
    Then when user provides location, completes weather.
    """
    control = CortexControlLayer()
    harness = AgentHarness(control)

    # Turn 1: "Can you check the weather?"
    sess = await SessionContextManager.get_session("asus")
    context = {"session": sess}
    resp1 = await harness.process_request("asus", "Can you check the weather?", context=context)
    assert resp1.status == "success"
    assert "Yes, I can check live weather" in resp1.message

    # Verify pending_action is recorded in session
    sess = await SessionContextManager.get_session("asus")
    pending = sess["references"].get("pending_action")
    assert pending is not None
    assert pending["capability"] == "weather.current"
    assert "location" in pending["missing_arguments"]

    # Turn 2: "Okay, do it."
    # Since location is absent from context, it must ask for location and NEVER take screenshot
    context = {"session": sess}
    resp2 = await harness.process_request("asus", "Okay, do it.", context=context)
    assert resp2.status == "clarification_needed"
    assert "location" in resp2.message.lower()
    # Must NOT take screenshot!
    assert "screenshot" not in resp2.message.lower()

    # Turn 3: "Manassas"
    # Fulfills missing location argument and resumes weather.current
    sess = await SessionContextManager.get_session("asus")
    context = {"session": sess}
    resp3 = await harness.process_request("asus", "Manassas", context=context)
    assert resp3.status == "success"
    assert "Manassas" in resp3.message
    assert "22.0°C" in resp3.message

    # Pending action should be cleared after execution
    sess = await SessionContextManager.get_session("asus")
    assert sess["references"].get("pending_action") is None


# ===========================================================================
# Test 4: Weather Location Semantic Extraction
# ===========================================================================
@pytest.mark.asyncio
async def test_weather_location_extraction(setup_test_env):
    """
    Test 4:
    Separate semantic modifiers from location entities.
    'search up the weather in Manassas currently' -> 'Manassas'
    'current weather in Manassas' -> 'Manassas'
    'weather right now in Fairfax' -> 'Fairfax'
    'what's it like in New York right now' -> 'New York'
    """
    assert extract_weather_location("search up the weather in Manassas currently") == "Manassas"
    assert extract_weather_location("current weather in Manassas") == "Manassas"
    assert extract_weather_location("weather right now in Fairfax") == "Fairfax"
    assert extract_weather_location("what's it like in New York right now") == "New York"
    assert extract_weather_location("weather in Manassas currently") == "Manassas"
    assert extract_weather_location("what is the weather today") is None

    # End-to-end harness test
    control = CortexControlLayer()
    harness = AgentHarness(control)
    resp = await harness.process_request("asus", "search up the weather in Manassas currently", context={})
    assert resp.status == "success"
    assert "Manassas" in resp.message
    assert "Currently" not in resp.message.split("in ")[1].split(".")[0]


# ===========================================================================
# Test 5: Compound Self-Knowledge & Follow-Up References
# ===========================================================================
@pytest.mark.asyncio
async def test_compound_self_knowledge_and_followup(setup_test_env):
    """
    Test 5:
    'Do you have access to my Calendar and Gmail?'
    Must report both independently.
    Then:
    'What about Gmail?'
    must remain in system integration context.
    """
    control = CortexControlLayer()
    harness = AgentHarness(control)

    # Pre-configure Calendar as connected, Gmail as unconnected
    sys_reg = SystemRegistry.get_instance()
    sys_reg.set_integration_override("calendar", {"connected": True})
    sys_reg.set_integration_override("gmail", {"connected": False})

    sess = await SessionContextManager.get_session("asus")
    context = {"session": sess}

    # Compound query
    resp1 = await harness.process_request("asus", "Do you have access to my calendar and Gmail?", context=context)
    assert resp1.status == "success"
    assert "Google Calendar is connected and authorized" in resp1.message
    assert "Gmail is not currently connected" in resp1.message

    # Follow-up: "What about Gmail?"
    sess = await SessionContextManager.get_session("asus")
    context = {"session": sess}
    resp2 = await harness.process_request("asus", "What about Gmail?", context=context)
    assert resp2.status in ("success", "error")
    # Must report Gmail integration status, NOT generic LLM definition of Gmail!
    assert "Gmail is not currently connected" in resp2.message or "Gmail is connected" in resp2.message
    assert "email service by google" not in resp2.message.lower()

    # Compound capability test: "Can you search the web and see my screen?"
    resp3 = await harness.process_request("asus", "Can you search the web and see my screen?", context=context)
    assert resp3.status == "success"
    assert "search the web" in resp3.message
    assert "screen" in resp3.message


# ===========================================================================
# Test 6: Device State Online Verification (No Contamination)
# ===========================================================================
@pytest.mark.asyncio
async def test_device_online_state_verification(setup_test_env):
    """
    Test 6:
    'What devices are connected right now?'
    Only genuinely live authenticated devices in CONNECTED_DEVICES may report online=true.
    'test_dev' is enrolled in DB, but not connected via active websocket -> MUST be offline.
    """
    control = CortexControlLayer()
    harness = AgentHarness(control)

    # Mock 'asus' as active in CONNECTED_DEVICES, but NOT 'test_dev'
    from hub.main import CONNECTED_DEVICES
    CONNECTED_DEVICES.clear()
    CONNECTED_DEVICES["asus"] = MagicMock()

    resp = await harness.process_request("asus", "What devices are connected right now?", context={})
    assert resp.status == "success"
    # asus is online
    assert "asus (online" in resp.message
    # test_dev is offline
    assert "Test Device (offline" in resp.message
    assert "Connected devices (1 online)" in resp.message


# ===========================================================================
# Test 7: User-Safe Error Sanitization (Never Speak Raw Internals)
# ===========================================================================
def test_speech_error_sanitization():
    """
    Test 7:
    Force a command failure.
    TTS must receive a short consumer-safe error.
    Must never read raw JSON, capability arrays, stack traces, or handler dumps.
    """
    # 1. Handler dump
    raw_handler_dump = "Action 'retrieve_recent_screenshot' is not supported. Supported: screen.capture, screen.analyze, app.launch"
    safe1 = sanitize_for_speech(raw_handler_dump)
    assert "Supported:" not in safe1
    assert "screen.capture, screen.analyze" not in safe1
    assert "screen-analysis action failed" in safe1 or "capability is not supported" in safe1

    # 2. Raw JSON string
    raw_json = '{"error": "ProcessNotFound", "pid": 1234, "path": "C:/Windows/System32"}'
    safe2 = sanitize_for_speech(raw_json)
    assert "{" not in safe2
    assert "1234" not in safe2
    assert "System32" not in safe2
    assert "system error" in safe2.lower()

    # 3. Python traceback
    raw_tb = "Traceback (most recent call last):\n  File 'cortex.py', line 12\nKeyError: 'display_index'"
    safe3 = sanitize_for_speech(raw_tb)
    assert "Traceback" not in safe3
    assert "KeyError" not in safe3
    assert "system error" in safe3.lower() or "error while processing" in safe3.lower()

    # 4. Clean human messages pass through unmodified
    clean_msg = "It's currently 22.0°C and Sunny in Manassas."
    assert sanitize_for_speech(clean_msg) == clean_msg


# ===========================================================================
# Test 8: Production DB Protection
# ===========================================================================
def test_production_db_safety():
    """Verify production database has not been touched by test runs."""
    prod_db = Path("data/cortex.db")
    if prod_db.exists():
        assert os.environ.get("CORTEX_DB_PATH") != str(prod_db)
        # Verify test db path is distinct
        test_db = Path(os.environ.get("CORTEX_DB_PATH"))
        assert test_db.name != "cortex.db" or "tests" in str(test_db)
