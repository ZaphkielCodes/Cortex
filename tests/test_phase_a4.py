"""
tests/test_phase_a4.py
----------------------
Comprehensive test suite for Cortex Phase A4:
Self-Knowledge & System Registry Layer.

Covers:
1. Canonical System Registry (SystemRegistry singleton, aggregation, test reset)
2. Cortex Identity (system.identity, product_name="Cortex", architecture_mode="hybrid", role, current_node)
3. Model / Provider Introspection (system.models, conversational LLM, vision, planning, STT, TTS)
4. Capability Introspection (system.list_capabilities, system.capability_status, live availability)
5. Dynamic Capability Availability (weather disabled/enabled dynamic reflection without code changes)
6. Device Registry View (system.list_devices, system.device_status, enrolled devices, online/offline state)
7. Integration Introspection (system.list_integrations, system.integration_status, OAuth authorization vs API existence)
8. Provider Status & Secret Masking (system.providers, strict omission of secrets and API keys)
9. Limitation / Failure Explanation (system.explain_capability: unsupported, provider unavailable, missing integration)
10. Direct Self-Knowledge Conversational Routing (AgentHarness deterministic interception before generic LLM)
11. Planner Integration (GoalPlanner consumes registry state and refuses steps for unavailable capabilities)
12. Required Physical Test 1: Identity ("What are you?" -> Cortex, not Gemini)
13. Required Physical Test 2: Model ("What model are you using right now?" -> reports configured model, distinguishes Cortex)
14. Required Physical Test 3: Capabilities ("What can you actually do?" -> live Capability Registry)
15. Required Physical Test 4: Availability (Dynamic disable/enable of weather provider)
16. Required Physical Test 5: Devices ("What devices are connected?" -> true enrolled state, no fabricated nodes)
17. Required Physical Test 6: Integrations ("Do you have access to my calendar?", "Do you have Gmail access?")
18. Required Failure Test (Model metadata unavailable -> truthfully reports unavailable/unknown without guessing)
19. Production Database Isolation (data/cortex.db untouched)
"""

import os
from pathlib import Path
from unittest.mock import MagicMock
import pytest
import pytest_asyncio

from agent.executor import execute, get_capabilities
from hub.agent_harness import AgentHarness
from hub.capability_registry import get_capability, get_core_capabilities
from hub.control_layer import CortexControlLayer
from hub.database import (
    init_db,
    enroll_device,
    save_device_capabilities,
    add_integration,
)
from hub.goals import GoalManager
from hub.intent_schema import ConversationalResponse
from hub.llm_provider import MockLLMProvider, set_llm_provider
from hub.planner import GoalPlanner
from hub.system_registry import (
    SystemRegistry,
    PRODUCT_NAME,
    PRODUCT_VERSION,
    ARCHITECTURE_MODE,
    DEFAULT_ROLE,
    DEFAULT_NODE,
)
from hub.weather_provider import (
    WeatherProvider,
    set_weather_provider_override,
)


class DummyDisabledWeatherProvider(WeatherProvider):
    """Simulates a disabled weather provider."""
    @property
    def provider_name(self) -> str:
        return "open-meteo"

    async def get_current(self, location: str, units: str = "metric"):
        raise RuntimeError("Weather provider is currently disabled by administrator.")

    async def get_forecast(self, location: str, units: str = "metric"):
        raise RuntimeError("Weather provider is currently disabled by administrator.")


class DummyActiveWeatherProvider(WeatherProvider):
    """Simulates an active weather provider."""
    @property
    def provider_name(self) -> str:
        return "open-meteo"

    async def get_current(self, location: str, units: str = "metric"):
        return {
            "location": location,
            "temperature": 21.5,
            "temperature_unit": "°C",
            "conditions": "Sunny",
            "provider": "open-meteo",
        }

    async def get_forecast(self, location: str, units: str = "metric"):
        return {
            "location": location,
            "forecast": [{"date": "2026-10-14", "conditions": "Clear", "high": 22, "low": 12}],
            "provider": "open-meteo",
        }


@pytest.fixture(autouse=True)
def setup_phase_a4():
    """Ensure clean SystemRegistry and weather state for every test."""
    SystemRegistry.reset()
    GoalPlanner.reset()
    GoalManager.reset()
    orig_weather_enabled = os.environ.get("CORTEX_WEATHER_ENABLED")
    os.environ["CORTEX_WEATHER_ENABLED"] = "true"
    set_weather_provider_override(DummyActiveWeatherProvider())
    yield
    SystemRegistry.reset()
    GoalPlanner.reset()
    GoalManager.reset()
    if orig_weather_enabled is not None:
        os.environ["CORTEX_WEATHER_ENABLED"] = orig_weather_enabled
    else:
        os.environ.pop("CORTEX_WEATHER_ENABLED", None)
    set_weather_provider_override(None)


# ===========================================================================
# 1. System Identity Tests
# ===========================================================================

def test_system_identity_metadata():
    """Verify system.identity returns canonical Cortex metadata."""
    reg = SystemRegistry.get_instance()
    ident = reg.get_identity(current_node="asus")

    assert ident["product_name"] == "Cortex"
    assert ident["version"] == PRODUCT_VERSION
    assert ident["architecture_mode"] == "hybrid"
    assert ident["role"] == "primary_admin"
    assert ident["current_node"] == "asus"
    assert "Gemini" not in ident["product_name"]
    assert "OpenAI" not in ident["product_name"]


def test_system_identity_executor_dispatch():
    """Verify system.identity is executable via the agent dispatch table."""
    res = execute("system.identity", [])
    assert res["status"] == "success"
    data = res["data"]
    assert data["product_name"] == "Cortex"
    assert data["architecture_mode"] == "hybrid"


@pytest.mark.asyncio
async def test_system_identity_control_layer():
    """Verify system.identity executes cleanly through CortexControlLayer."""
    control = CortexControlLayer({})
    res = await control.execute_and_observe("asus", "system.identity", {})
    assert res["status"] == "success"
    assert res["data"]["product_name"] == "Cortex"


# ===========================================================================
# 2. Model & Provider Introspection Tests
# ===========================================================================

def test_system_models_introspection():
    """Verify system.models reports active providers/models where known."""
    mock_llm = MockLLMProvider()
    set_llm_provider(mock_llm)

    reg = SystemRegistry.get_instance()
    models = reg.get_models()

    assert isinstance(models, list)
    purposes = {m["purpose"]: m for m in models}

    assert "conversational_reasoning" in purposes
    assert "vision" in purposes
    assert "planning" in purposes
    assert "speech_recognition" in purposes
    assert "text_to_speech" in purposes

    # Mock provider reports its name and model
    cr = purposes["conversational_reasoning"]
    assert cr["provider"] == "mock"
    assert cr["model"] == "mock-v1"
    assert cr["availability"] is True

    # Unconfigured modalities report unknown / False
    assert purposes["speech_recognition"]["provider"] == "unknown"
    assert purposes["speech_recognition"]["availability"] is False


def test_system_models_executor_dispatch():
    """Verify system.models is executable via the executor dispatch table."""
    res = execute("system.models", [])
    assert res["status"] == "success"
    assert isinstance(res["data"], list)
    assert len(res["data"]) >= 5


# ===========================================================================
# 3. Capability Introspection Tests
# ===========================================================================

def test_system_list_capabilities():
    """Verify system.list_capabilities enumerates live CapabilityRegistry."""
    reg = SystemRegistry.get_instance()
    caps = reg.list_capabilities()

    cap_names = {c["name"] for c in caps}
    # Phase A1
    assert "web.search" in cap_names
    assert "weather.current" in cap_names
    assert "browser.open_url" in cap_names

    # Phase A2
    assert "screen.capture" in cap_names
    assert "screen.analyze" in cap_names

    # Phase A3
    assert "app.launch" in cap_names
    assert "app.close" in cap_names
    assert "window.list" in cap_names
    assert "window.focus" in cap_names
    assert "fs.copy" in cap_names
    assert "fs.delete" in cap_names

    # Phase A4
    assert "system.identity" in cap_names
    assert "system.models" in cap_names
    assert "system.list_capabilities" in cap_names
    assert "system.capability_status" in cap_names
    assert "system.list_devices" in cap_names
    assert "system.device_status" in cap_names
    assert "system.list_integrations" in cap_names
    assert "system.integration_status" in cap_names
    assert "system.providers" in cap_names
    assert "system.explain_capability" in cap_names


def test_system_capability_status_specific():
    """Verify system.capability_status returns detailed availability and metadata."""
    reg = SystemRegistry.get_instance()

    # Available capability
    st = reg.get_capability_status("fs.copy")
    assert st["all_available"] is True
    assert "fs.copy" in st["capabilities"]
    assert st["capabilities"]["fs.copy"]["available"] is True

    # Multi-capability query
    st_multi = reg.get_capability_status(["screen.capture", "screen.analyze"])
    assert "screen.capture" in st_multi["capabilities"]
    assert "screen.analyze" in st_multi["capabilities"]

    # Unsupported capability
    st_bad = reg.get_capability_status("teleportation.instant")
    assert st_bad["all_available"] is False
    assert st_bad["capabilities"]["teleportation.instant"]["available"] is False
    assert "unsupported or unregistered" in st_bad["capabilities"]["teleportation.instant"]["reason"]


def test_capability_availability_dynamic_weather():
    """
    REQUIRED PHYSICAL TEST 4 — AVAILABILITY:
    Disable weather provider -> capability unavailable.
    Re-enable it -> capability available without code changes.
    """
    reg = SystemRegistry.get_instance()

    # 1. Active provider
    set_weather_provider_override(DummyActiveWeatherProvider())
    st_active = reg.get_capability_status("weather.current")
    assert st_active["capabilities"]["weather.current"]["available"] is True

    # 2. Disable weather provider via environment configuration
    os.environ["CORTEX_WEATHER_ENABLED"] = "false"
    set_weather_provider_override(None)

    st_disabled = reg.get_capability_status("weather.current")
    assert st_disabled["capabilities"]["weather.current"]["available"] is False
    assert "unavailable or disabled" in st_disabled["capabilities"]["weather.current"]["reason"]

    # 3. Re-enable provider without code changes
    os.environ["CORTEX_WEATHER_ENABLED"] = "true"
    set_weather_provider_override(DummyActiveWeatherProvider())

    st_restored = reg.get_capability_status("weather.current")
    assert st_restored["capabilities"]["weather.current"]["available"] is True


# ===========================================================================
# 4. Device Registry View Tests
# ===========================================================================

@pytest.mark.asyncio
async def test_system_list_devices_local_only():
    """When no remote devices enrolled, reports local ASUS node cleanly without fabrication."""
    reg = SystemRegistry.get_instance()
    devices = await reg.list_devices(current_node="asus")

    assert len(devices) >= 1
    local_dev = next((d for d in devices if d["device_id"] == "asus"), None)
    assert local_dev is not None
    assert local_dev["role"] == "primary_admin"
    assert local_dev["online"] is True
    assert local_dev["is_current_device"] is True


@pytest.mark.asyncio
async def test_system_list_devices_enrolled_state():
    """Enrolled devices are reflected accurately with true online/offline state."""
    await enroll_device("macbook_remote", "MacBook Pro", "admin", "mock_secret_hash")
    await save_device_capabilities("macbook_remote", ["app.launch", "screen.capture"])

    reg = SystemRegistry.get_instance()
    # Mock connected map where only 'asus' is connected, macbook is offline
    devices = await reg.list_devices(current_node="asus", connected_devices_map={"asus": MagicMock()})

    mac = next((d for d in devices if d["device_id"] == "macbook_remote"), None)
    assert mac is not None
    assert mac["name"] == "MacBook Pro"
    assert mac["online"] is False  # NOT online in connected map
    assert "app.launch" in mac["advertised_capabilities"]

    # Test device status single lookup
    mac_status = await reg.get_device_status("macbook_remote", current_node="asus")
    assert mac_status is not None
    assert mac_status["device_id"] == "macbook_remote"

    # Nonexistent device lookup
    nonexistent = await reg.get_device_status("unknown_node", current_node="asus")
    assert nonexistent is None


# ===========================================================================
# 5. Integration Introspection Tests
# ===========================================================================

@pytest.mark.asyncio
async def test_system_list_integrations_unauthorized_by_default():
    """Distinguish API support from user authorization."""
    reg = SystemRegistry.get_instance()
    integs = await reg.list_integrations()

    assert isinstance(integs, list)
    keys = {i["key"]: i for i in integs}

    assert "calendar" in keys
    assert "gmail" in keys

    # Gmail is unauthorized by default
    assert keys["gmail"]["connected"] is False
    assert keys["gmail"]["status"] == "not_connected"

    # Test disconnected override on calendar
    reg.set_integration_override("calendar", {"connected": False, "status": "not_connected"})
    integs_overridden = await reg.list_integrations()
    keys_overridden = {i["key"]: i for i in integs_overridden}
    assert keys_overridden["calendar"]["connected"] is False
    assert keys_overridden["calendar"]["status"] == "not_connected"


@pytest.mark.asyncio
async def test_system_integration_status_override():
    """Verify connected status reflects true user authorization state."""
    reg = SystemRegistry.get_instance()

    # Simulate Calendar connected, Gmail disconnected
    reg.set_integration_override("calendar", {
        "connected": True,
        "scopes": ["https://www.googleapis.com/auth/calendar.readonly"],
        "status": "connected",
        "last_verified": "2026-10-13T12:00:00Z",
    })

    c_stat = await reg.get_integration_status("calendar")
    assert c_stat["connected"] is True
    assert c_stat["status"] == "connected"
    assert len(c_stat["scopes"]) > 0

    g_stat = await reg.get_integration_status("gmail")
    assert g_stat["connected"] is False
    assert g_stat["status"] == "not_connected"


# ===========================================================================
# 6. Provider Status & Secret Masking Tests
# ===========================================================================

def test_system_providers_no_secret_exposure():
    """Verify system.providers lists real state without exposing secrets or keys."""
    reg = SystemRegistry.get_instance()
    providers = reg.get_providers()

    assert isinstance(providers, list)
    purposes = {p["purpose"]: p for p in providers}

    assert "web_search" in purposes
    assert "weather" in purposes
    assert "vision" in purposes
    assert "conversational_reasoning" in purposes

    # Strict check: No sensitive keys permitted in output dicts
    forbidden_keys = {"api_key", "secret", "token", "password", "client_secret", "credential"}
    for p in providers:
        keys_present = set(p.keys())
        overlap = keys_present.intersection(forbidden_keys)
        assert len(overlap) == 0, f"Provider dict exposed secret keys: {overlap}"
        # Ensure values don't look like raw API keys
        for k, v in p.items():
            if isinstance(v, str):
                assert not v.startswith("AIzaSy"), f"Possible Google API key in provider {p}"
                assert not v.startswith("sk-"), f"Possible OpenAI API key in provider {p}"


# ===========================================================================
# 7. Limitation & Failure Explanation Tests
# ===========================================================================

@pytest.mark.asyncio
async def test_system_explain_capability():
    """Verify system.explain_capability distinguishes unsupported, missing integration, etc."""
    reg = SystemRegistry.get_instance()

    # 1. Missing integration (Gmail)
    exp_gmail = await reg.explain_capability("Why can't you read Gmail?")
    assert exp_gmail["category"] == "missing_integration"
    assert exp_gmail["can_run"] is False
    assert "not currently connected or authorized" in exp_gmail["explanation"]

    # 2. Unsupported task (Hardware / 3D printing)
    exp_3d = await reg.explain_capability("Can you 3D print a door hinge?")
    assert exp_3d["category"] == "unsupported"
    assert exp_3d["can_run"] is False
    assert "Hardware fabrication" in exp_3d["explanation"]

    # 3. Available capability
    exp_screen = await reg.explain_capability("screen.capture")
    assert exp_screen["category"] == "supported_and_available"
    assert exp_screen["can_run"] is True


# ===========================================================================
# 8. Conversational Routing & Physical Tests
# ===========================================================================

@pytest.mark.asyncio
async def test_required_physical_test_1_identity():
    """
    REQUIRED PHYSICAL TEST 1 — IDENTITY:
    Ask Cortex: 'What are you?'
    Expected:
    - Identifies itself as Cortex
    - Describes itself using runtime product metadata
    - Does NOT say it is Gemini
    """
    control = CortexControlLayer({})
    harness = AgentHarness(control_layer=control)
    context = {"device": {"id": "asus"}, "session": {}}

    resp = await harness.process_request("asus", "What are you?", context, None)
    assert resp.status == "success"
    assert "I am Cortex" in resp.message
    assert "hybrid" in resp.message
    assert "Gemini" not in resp.message
    assert "OpenAI" not in resp.message


@pytest.mark.asyncio
async def test_required_physical_test_2_model():
    """
    REQUIRED PHYSICAL TEST 2 — MODEL:
    Ask: 'What model are you using right now?'
    Expected:
    - Reports actual configured provider/model if known
    - Distinguishes Cortex from provider model
    """
    mock_llm = MockLLMProvider()
    set_llm_provider(mock_llm)

    control = CortexControlLayer({})
    harness = AgentHarness(control_layer=control)
    context = {"device": {"id": "asus"}, "session": {}}

    resp = await harness.process_request("asus", "What model are you using right now?", context, None)
    assert resp.status == "success"
    # Distinguishes Cortex product from provider model
    assert "Cortex" in resp.message
    assert "mock" in resp.message
    assert "mock-v1" in resp.message


@pytest.mark.asyncio
async def test_required_failure_test_model_metadata_unavailable():
    """
    REQUIRED FAILURE TEST:
    Remove/disable runtime model metadata.
    Ask: 'What model are you using?'
    Expected:
    - Cortex answers: model metadata is unavailable / unknown
    - Does NOT guess or hallucinate
    """
    reg = SystemRegistry.get_instance()
    reg.set_model_metadata_override([
        {
            "purpose": "conversational_reasoning",
            "provider": "unknown",
            "model": "unknown",
            "availability": False,
        }
    ])

    control = CortexControlLayer({})
    harness = AgentHarness(control_layer=control)
    context = {"device": {"id": "asus"}, "session": {}}

    resp = await harness.process_request("asus", "What model are you using?", context, None)
    assert resp.status == "success"
    assert "Active model metadata is currently unavailable or unknown" in resp.message
    assert "Gemini" not in resp.message
    assert "GPT" not in resp.message


@pytest.mark.asyncio
async def test_required_physical_test_3_capabilities():
    """
    REQUIRED PHYSICAL TEST 3 — CAPABILITIES:
    Ask: 'What can you actually do?'
    Expected answer derived from live Capability Registry.
    Verify A1-A3 capabilities appear.
    """
    control = CortexControlLayer({})
    harness = AgentHarness(control_layer=control)
    context = {"device": {"id": "asus"}, "session": {}}

    resp = await harness.process_request("asus", "What can you actually do?", context, None)
    assert resp.status == "success"
    # Live capabilities summary includes A1-A3 capabilities
    assert "web search" in resp.message.lower()
    assert "weather" in resp.message.lower()
    assert "screen" in resp.message.lower()
    assert "application" in resp.message.lower()
    assert "file" in resp.message.lower()

    # Structured data contains the actual live list
    assert resp.data is not None
    assert "capabilities" in resp.data
    cap_names = {c["name"] for c in resp.data["capabilities"]}
    assert "web.search" in cap_names
    assert "screen.capture" in cap_names
    assert "app.launch" in cap_names


@pytest.mark.asyncio
async def test_required_physical_test_4_availability_conversational():
    """
    REQUIRED PHYSICAL TEST 4 — AVAILABILITY (Conversational):
    Disable weather provider.
    Ask: 'Can you check the weather?' -> No / currently unavailable.
    Re-enable it and ask again -> Yes without code changes.
    """
    control = CortexControlLayer({})
    harness = AgentHarness(control_layer=control)
    context = {"device": {"id": "asus"}, "session": {}}

    # 1. Disable weather provider
    os.environ["CORTEX_WEATHER_ENABLED"] = "false"
    set_weather_provider_override(None)

    resp_disabled = await harness.process_request("asus", "Can you check the weather?", context, None)
    assert resp_disabled.status == "error"
    assert "weather access is currently unavailable" in resp_disabled.message.lower()

    # 2. Re-enable weather provider
    os.environ["CORTEX_WEATHER_ENABLED"] = "true"
    set_weather_provider_override(DummyActiveWeatherProvider())

    resp_enabled = await harness.process_request("asus", "Can you check the weather?", context, None)
    assert resp_enabled.status == "success"
    assert "yes" in resp_enabled.message.lower()
    assert "weather" in resp_enabled.message.lower()


@pytest.mark.asyncio
async def test_required_physical_test_5_devices():
    """
    REQUIRED PHYSICAL TEST 5 — DEVICES:
    Ask: 'What devices are connected?'
    Cortex should report only known/enrolled runtime device state.
    Do not fabricate offline/online nodes.
    """
    control = CortexControlLayer({})
    harness = AgentHarness(control_layer=control)
    context = {"device": {"id": "asus"}, "session": {}}

    resp = await harness.process_request("asus", "What devices are connected?", context, None)
    assert resp.status == "success"
    assert "Connected devices" in resp.message
    assert "asus" in resp.message.lower()
    assert resp.data is not None
    assert "devices" in resp.data
    # Verify no imaginary devices
    for d in resp.data["devices"]:
        assert d["device_id"] in ("asus", "macbook_remote") or d["trusted"] is True


@pytest.mark.asyncio
async def test_required_physical_test_6_integrations():
    """
    REQUIRED PHYSICAL TEST 6 — INTEGRATIONS:
    Ask:
    'Do you have access to my calendar?'
    'Do you have Gmail access?'
    Responses must reflect actual OAuth/integration state.
    """
    control = CortexControlLayer({})
    harness = AgentHarness(control_layer=control)
    context = {"device": {"id": "asus"}, "session": {}}

    # Live state: Google Calendar has authorized tokens in CredentialStore
    resp_cal = await harness.process_request("asus", "Do you have access to my calendar?", context, None)
    assert resp_cal.status == "success"
    assert "Google Calendar is connected and authorized" in resp_cal.message

    # Live state: Gmail does NOT have authorized tokens
    resp_gmail = await harness.process_request("asus", "Do you have Gmail access?", context, None)
    assert resp_gmail.status == "error"
    assert "Gmail access is not currently connected" in resp_gmail.message

    # Test disconnected override for Calendar
    reg = SystemRegistry.get_instance()
    reg.set_integration_override("calendar", {"connected": False})

    resp_cal_disc = await harness.process_request("asus", "Do you have access to my calendar?", context, None)
    assert resp_cal_disc.status == "error"
    assert "Google Calendar access is not currently connected" in resp_cal_disc.message


@pytest.mark.asyncio
async def test_conversational_limitation_explanation():
    """
    Verify conversational routing intercepts 'Why can't you read Gmail?'
    and answers deterministically without generic LLM fallback.
    """
    control = CortexControlLayer({})
    harness = AgentHarness(control_layer=control)
    context = {"device": {"id": "asus"}, "session": {}}

    resp = await harness.process_request("asus", "Why can't you read Gmail?", context, None)
    assert resp.status == "success"
    assert "Gmail access is not currently connected or authorized" in resp.message
    assert "As an AI language model" not in resp.message


# ===========================================================================
# 9. Planner Self-Knowledge & Availability Integration
# ===========================================================================

@pytest.mark.asyncio
async def test_planner_respects_registry_availability():
    """
    GoalPlanner must consume the same capability availability state.
    When weather provider is disabled, planner refuses to plan weather.current
    and blocks the goal with unachievable reason.
    """
    # 1. Disable weather provider
    os.environ["CORTEX_WEATHER_ENABLED"] = "false"
    set_weather_provider_override(None)

    planner = GoalPlanner.get_instance()
    context_caps = await planner.build_capability_context(target_device="asus")
    cap_names = {c["name"] for c in context_caps}

    # weather.current must NOT be present in available capability context
    assert "weather.current" not in cap_names
    assert "weather.forecast" not in cap_names

    # Attempt to plan a weather goal
    gm = GoalManager.get_instance()
    g = await gm.create_goal(
        title="Weather check",
        original_request="What's the weather today in Boston",
        metadata={"target_device": "asus"},
    )

    plan_res = await planner.plan_goal(g.goal_id, target_device="asus")
    assert plan_res["status"] == "blocked"
    assert "not available" in plan_res["reason"].lower()

    # Check goal status in manager
    updated_g = await gm.get_goal(g.goal_id)
    assert updated_g.status == "BLOCKED"

    # Steps must be empty - planner did NOT plan a fake step
    steps = await gm.list_steps(g.goal_id)
    assert len(steps) == 0

    # 2. Now re-enable weather provider
    os.environ["CORTEX_WEATHER_ENABLED"] = "true"
    set_weather_provider_override(DummyActiveWeatherProvider())

    context_caps_active = await planner.build_capability_context(target_device="asus")
    cap_names_active = {c["name"] for c in context_caps_active}
    assert "weather.current" in cap_names_active

    g2 = await gm.create_goal(
        title="Weather check 2",
        original_request="What's the weather today in Boston",
        metadata={"target_device": "asus"},
    )
    plan_res2 = await planner.plan_goal(g2.goal_id, target_device="asus")
    assert plan_res2["status"] == "planned"
    steps2 = await gm.list_steps(g2.goal_id)
    assert len(steps2) == 1
    assert steps2[0].capability == "weather.current"


# ===========================================================================
# 10. Production DB Isolation Test
# ===========================================================================

def test_production_db_isolation():
    """Verify production database file data/cortex.db is strictly isolated."""
    db_path = os.environ.get("CORTEX_DB_PATH", "")
    assert "test_cortex.db" in db_path
    assert not db_path.endswith("data/cortex.db") and not db_path.endswith("data\\cortex.db")
