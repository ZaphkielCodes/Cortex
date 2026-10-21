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
from hub.agent_harness import AgentHarness, sanitize_for_speech
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
    await enroll_device("asus", "asus", "admin", "hash1")
    await enroll_device("test_dev", "Test Device", "sub_admin", "hash2")

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
async def test_screenshot_followup_continuity(setup_test_env,monkeypatch):
    """A captured reference survives two follow-ups; absent references require clarification."""
    from model_contract_fixtures import native_harness, SequenceEvidenceModel, call
    from hub.vision_provider import get_vision_provider
    get_vision_provider().locality="local_device"
    model=SequenceEvidenceModel([call("screen.capture"),
        call("screen.analyze",image_ref="shot1.png"),call("screen.analyze",image_ref="shot1.png"),
        {"decision":"ASK_CLARIFICATION","response_text":"Which screenshot should I inspect?"}])
    harness,ctrl=await native_harness(monkeypatch,model)
    original=ctrl.execute_and_observe
    async def dispatch(device,cap,args,**kwargs):
        if cap=="screen.capture":return {"status":"success","data":{"image_ref":"shot1.png"}}
        return await original(device,cap,args,**kwargs)
    ctrl.execute_and_observe=AsyncMock(side_effect=dispatch)
    first=await harness.process_request("asus","Take a screenshot.",{})
    assert first.status=="success" and first.data["image_ref"]=="shot1.png"
    await SessionContextManager.update_session("asus","Take a screenshot.",first.model_dump())
    for request in ["Read that screenshot and tell me what you see.","what do you see there?"]:
        sess=await SessionContextManager.get_session("asus")
        resp=await harness.process_request("asus",request,{"session":sess})
        assert resp.status=="success" and resp.data["source_image_reference"]=="shot1.png"
        assert "Chrome" in resp.message
    assert "shot1.png" in model.calls[2]["system_prompt"]
    empty=await harness.process_request("asus","Read that screenshot.",{"session":{"references":{}}})
    assert empty.status=="clarification_needed"
    assert [c.args[1] for c in ctrl.execute_and_observe.await_args_list]==["screen.capture","screen.analyze","screen.analyze"]



# ===========================================================================
# Test 3: Follow-Up Action Continuity
# ===========================================================================
@pytest.mark.asyncio
async def test_followup_action_continuity(setup_test_env,monkeypatch):
    """History bridges offer, missing-location clarification and exact tool execution."""
    from model_contract_fixtures import native_harness, SequenceEvidenceModel, call
    from hub.weather_provider import MockWeatherProvider
    set_weather_provider_override(MockWeatherProvider())
    model=SequenceEvidenceModel([
        {"decision":"RESPOND","response_text":"I can check live weather if you give me a location."},
        {"decision":"ASK_CLARIFICATION","response_text":"Which location?"},
        call("weather.current",location="Manassas")])
    harness,ctrl=await native_harness(monkeypatch,model)
    ctrl.execute_and_observe=AsyncMock(wraps=ctrl.execute_and_observe)
    for index,request in enumerate(["Can you check the weather?","Okay, do it.","Manassas"]):
        context={"session":await SessionContextManager.get_session("asus")}
        resp=await harness.process_request("asus",request,context)
        if index==1:
            assert resp.status=="clarification_needed"
            assert ctrl.execute_and_observe.await_count==0
            assert "Can you check the weather?" in str(model.calls[-1]["conversation_history"])
        await SessionContextManager.update_session("asus",request,resp.model_dump())
    assert resp.status=="success" and resp.data["temperature"]==22.0
    assert "Manassas" in resp.data["location"]
    assert [c.args[1] for c in ctrl.execute_and_observe.await_args_list]==["weather.current"]
    assert ctrl.execute_and_observe.await_args.args[2]["location"]=="Manassas"



# ===========================================================================
# Test 4: Weather Location Semantic Extraction
# ===========================================================================
def test_weather_wording_and_locations_reach_the_model_unchanged():
    from hub.pipeline.normalizer import InputNormalizer
    for text in ["Weather near São Paulo rn?", "Forecast for somewhere I mentioned earlier", "Don't open Weather"]:
        assert InputNormalizer.normalize(text).raw_text == text


@pytest.mark.asyncio
async def test_compound_self_knowledge_and_followup(setup_test_env,monkeypatch):
    """Independent integration facts and prior context survive follow-up queries."""
    from model_contract_fixtures import native_harness, SequenceEvidenceModel, call
    reg=SystemRegistry.get_instance()
    reg.set_integration_override("calendar",{"connected":True})
    reg.set_integration_override("gmail",{"connected":False})
    model=SequenceEvidenceModel([call("system.list_integrations"),
        call("system.integration_status",integration="gmail"),
        {"decision":"CALL_CAPABILITY","capability":"system.capability_status","arguments":{"capability":"web.search,screen.analyze"}}])
    harness,ctrl=await native_harness(monkeypatch,model)
    first=await harness.process_request("asus","Do you have access to my calendar and Gmail?",{})
    assert first.status=="success"
    rows={r["key"]:r for r in first.data["result"]}
    assert rows["calendar"]["connected"] is True and rows["gmail"]["connected"] is False
    await SessionContextManager.update_session("asus","Do you have access to my calendar and Gmail?",first.model_dump())
    second=await harness.process_request("asus","What about Gmail?",{"session":await SessionContextManager.get_session("asus")})
    assert second.status=="success" and second.data["connected"] is False
    assert "calendar and Gmail" in str(model.calls[2]["conversation_history"])
    third=await harness.process_request("asus","Can you search the web and see my screen?",{})
    assert third.status=="success"
    assert set(third.data["capabilities"])=={"web.search","screen.analyze"}



# ===========================================================================
# Test 6: Device State Online Verification (No Contamination)
# ===========================================================================
@pytest.mark.asyncio
async def test_device_online_state_verification(setup_test_env,monkeypatch):
    """Enrolled offline devices remain offline; a model sees authenticated live state."""
    from model_contract_fixtures import native_harness, EvidenceModel
    harness,ctrl=await native_harness(monkeypatch,EvidenceModel("system.list_devices"))
    resp=await harness.process_request("asus","What devices are connected right now?",{})
    assert resp.status=="success"
    devices={r["device_id"]:r for r in resp.data["result"]}
    assert devices["asus"]["online"] is True
    assert devices["test_dev"]["online"] is False
    assert set(devices)=={"asus","test_dev"}



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
