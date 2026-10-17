"""
tests/test_phase8d_live_context.py
----------------------------------
Deterministic test suite for Phase 8D: Live Personal Context Inputs.
Covers:
- Calendar disconnected handling
- Calendar connected state & structured event parsing
- Current event / next event parsing
- Busy / free state calculation
- time_until_next_event (numeric seconds payload)
- Stale calendar data ignored
- Telemetry RAM, CPU, Disk
- Hardware metadata & device identity
- Missing telemetry fallback
- Natural-language calendar routing
- Natural-language telemetry routing
- World State ingestion & compact summary
- Phase 8B calendar proactive condition integration
- Phase 8C attention routing & desktop delivery integration
- Restart persistence
- Production DB isolation
- Tier 1 internal capabilities execution
"""

import os
import pytest
import hashlib
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

import hub.database as database
import hub.main
from hub.intent_schema import ActionPlan, ModelResult
from hub.control_layer import CortexControlLayer
from hub.agent_harness import AgentHarness
from hub.context import ContextManager
from hub.world_state import (
    Signal,
    WorldState,
    WorldStateManager,
    DeviceSignalAdapter,
    CalendarSignalAdapter
)
from hub.proactive_engine import ProactiveEngine, Condition
from hub.attention_router import AttentionRouter, DeliveryDecision


PROD_DB_HASH = "62F5E75C85065CD18F43A2D79EFB3CF3D7756F49349D4FDADC6A1B4330D27732"


import pytest_asyncio


@pytest_asyncio.fixture(autouse=True, scope="function")
async def isolate_phase8d_db(tmp_path):
    """Ensure every test executes on an isolated scratch DB, leaving production DB 100% untouched."""
    try:
        await database.close_db()
    except Exception:
        pass
    db_file = tmp_path / f"test_phase8d_{uuid.uuid4().hex[:8]}.db"
    old_env = os.environ.get("CORTEX_DB_PATH")
    os.environ["CORTEX_DB_PATH"] = str(db_file)
    await database.init_db()

    # Reset singletons
    WorldStateManager.reset()
    ProactiveEngine.reset()
    AttentionRouter.reset()

    # Ensure asus device is registered in connected devices map for local routing
    hub.main.CONNECTED_DEVICES["asus"] = AsyncMock()

    yield db_file

    try:
        await database.close_db()
    except Exception:
        pass
    hub.main.CONNECTED_DEVICES.clear()

    if old_env is not None:
        os.environ["CORTEX_DB_PATH"] = old_env
    else:
        os.environ.pop("CORTEX_DB_PATH", None)


@pytest.mark.asyncio
async def test_calendar_disconnected_handling():
    """Verify that when Google Calendar is disconnected, Cortex reports status gracefully without hallucinating."""
    conn_info = await CalendarSignalAdapter.get_connection_status()
    assert conn_info["connected"] is False
    assert conn_info["status"] in ("disconnected", "unavailable")

    # Generate signals in disconnected state
    signals = await CalendarSignalAdapter.fetch_and_create_signals()
    conn_sig = next(s for s in signals if s.key == "calendar.connected")
    assert conn_sig.payload["connected"] is False

    wsm = WorldStateManager.get_instance()
    for s in signals:
        await wsm.ingest_signal(s)

    fused = await wsm.get_fused_world_state("asus")
    assert fused.calendar_context["connected"] is False

    control = CortexControlLayer(hub.main.CONNECTED_DEVICES)
    harness = AgentHarness(control)
    await database.enroll_device("asus", "asus", "admin", "secret")

    # NL query when disconnected
    resp = await harness.process_request("asus", "What's on my calendar right now?", {}, None)
    assert resp.status == "success"
    assert "google calendar" in resp.message.lower()
    assert "don't have anything on your calendar right now" in resp.message.lower()


@pytest.mark.asyncio
async def test_calendar_connected_state_and_event_parsing():
    """Verify structured parsing of live/mock events into current/next/time_until signals."""
    now = datetime(2026, 10, 8, 14, 0, 0, tzinfo=timezone.utc)

    mock_events = [
        {
            "summary": "Live AI Architecture Review",
            "start": {"dateTime": "2026-10-08T13:30:00+00:00"},
            "end": {"dateTime": "2026-10-08T14:30:00+00:00"},
            "location": "Boardroom A"
        },
        {
            "summary": "Product Strategy Sync",
            "start": {"dateTime": "2026-10-08T14:45:00+00:00"},
            "end": {"dateTime": "2026-10-08T15:30:00+00:00"},
            "location": "Zoom"
        }
    ]

    signals = await CalendarSignalAdapter.fetch_and_create_signals(raw_events=mock_events, now=now)
    sig_map = {s.key: s for s in signals}

    # Verify atomic signals
    assert "calendar.connected" in sig_map
    assert sig_map["calendar.connected"].payload["connected"] is True

    assert "calendar.current_event" in sig_map
    assert sig_map["calendar.current_event"].payload["title"] == "Live AI Architecture Review"

    assert "calendar.current_event.start" in sig_map
    assert sig_map["calendar.current_event.start"].payload["value"] == "2026-10-08T13:30:00+00:00"

    assert "calendar.current_event.end" in sig_map
    assert sig_map["calendar.current_event.end"].payload["value"] == "2026-10-08T14:30:00+00:00"

    assert "calendar.current_event.title" in sig_map
    assert sig_map["calendar.current_event.title"].payload["value"] == "Live AI Architecture Review"

    assert "calendar.next_event" in sig_map
    assert sig_map["calendar.next_event"].payload["title"] == "Product Strategy Sync"

    assert "calendar.next_event.start" in sig_map
    assert sig_map["calendar.next_event.start"].payload["value"] == "2026-10-08T14:45:00+00:00"

    assert "calendar.next_event.end" in sig_map
    assert sig_map["calendar.next_event.end"].payload["value"] == "2026-10-08T15:30:00+00:00"

    assert "calendar.next_event.title" in sig_map
    assert sig_map["calendar.next_event.title"].payload["value"] == "Product Strategy Sync"

    # Numeric payload for proactive condition evaluation
    assert "calendar.time_until_next_event" in sig_map
    time_sig = sig_map["calendar.time_until_next_event"]
    assert time_sig.payload["value"] == 45 * 60  # 2700 seconds (14:00 to 14:45)
    assert time_sig.payload["minutes"] == 45
    assert "45 minutes" in time_sig.payload["formatted"]

    assert "calendar.busy" in sig_map
    assert sig_map["calendar.busy"].payload["value"] is True

    assert "calendar.free_until" in sig_map
    assert sig_map["calendar.free_until"].payload["value"] == "2026-10-08T14:30:00+00:00"


@pytest.mark.asyncio
async def test_calendar_busy_free_window():
    """Verify busy/free calculations when user is currently free but has an upcoming event."""
    now = datetime(2026, 10, 8, 10, 0, 0, tzinfo=timezone.utc)
    mock_events = [
        {
            "summary": "Team Demo",
            "start": {"dateTime": "2026-10-08T10:30:00+00:00"},
            "end": {"dateTime": "2026-10-08T11:00:00+00:00"}
        }
    ]

    parsed = CalendarSignalAdapter.parse_events(mock_events, now=now)
    assert parsed["current_event"] is None
    assert parsed["next_event"]["title"] == "Team Demo"
    assert parsed["busy"] is False
    assert parsed["free_until"] == "2026-10-08T10:30:00+00:00"
    assert parsed["time_until_seconds"] == 1800
    assert parsed["time_until_minutes"] == 30


@pytest.mark.asyncio
async def test_stale_calendar_data_ignored():
    """Verify expired calendar signals are ignored by ProactiveEngine."""
    wsm = WorldStateManager.get_instance()
    expired_time = (datetime.now(timezone.utc) - timedelta(seconds=120)).isoformat()

    stale_sig = Signal(
        key="calendar.time_until_next_event",
        source="google_calendar",
        timestamp=expired_time,
        confidence=0.95,
        ttl=10.0,
        payload={"value": 300, "seconds": 300}
    )

    # Ingestion of expired signal must be rejected
    ok = await wsm.ingest_signal(stale_sig)
    assert ok is False


@pytest.mark.asyncio
async def test_telemetry_cpu_ram_disk():
    """Verify real local OS retrieval of CPU, RAM, and Disk metrics via DeviceSignalAdapter."""
    telem = DeviceSignalAdapter.inspect_system_telemetry("asus")
    assert telem["cpu_percent"] >= 0.0
    assert telem["ram_percent"] > 0.0
    assert telem["ram_used_gb"] > 0.0
    assert telem["ram_total_gb"] > 0.0
    assert telem["disk_percent"] > 0.0
    assert telem["disk_free_gb"] > 0.0
    assert telem["disk_total_gb"] > 0.0

    signals = DeviceSignalAdapter.create_telemetry_signals("asus", telemetry=telem)
    sig_keys = {s.key for s in signals}

    required_keys = [
        "device.cpu_percent:asus",
        "device.ram_percent:asus",
        "device.ram_used_gb:asus",
        "device.ram_total_gb:asus",
        "device.disk_percent:asus",
        "device.disk_free_gb:asus",
        "device.disk_total_gb:asus",
        "device.hostname:asus",
        "device.os:asus",
        "device.manufacturer:asus",
        "device.model:asus",
        "device.processor:asus",
        "device.gpu:asus"
    ]
    for rk in required_keys:
        assert rk in sig_keys, f"Missing telemetry signal key: {rk}"

    wsm = WorldStateManager.get_instance()
    for s in signals:
        await wsm.ingest_signal(s)

    fused = await wsm.get_fused_world_state("asus")
    assert fused.system_telemetry["cpu_percent"] == telem["cpu_percent"]
    assert fused.system_telemetry["ram_percent"] == telem["ram_percent"]
    assert fused.system_telemetry["ram_used_gb"] == telem["ram_used_gb"]
    assert fused.system_telemetry["disk_free_gb"] == telem["disk_free_gb"]


@pytest.mark.asyncio
async def test_hardware_metadata_and_device_identity():
    """Verify hardware metadata (manufacturer, model, processor, GPU) is retrieved and not hardcoded."""
    telem = DeviceSignalAdapter.inspect_system_telemetry("asus")
    assert telem["hostname"] != ""
    assert telem["os"] != ""
    assert telem["manufacturer"] != ""
    assert telem["model"] != ""
    assert telem["processor"] != ""
    assert telem["gpu"] != ""

    if os.name == "nt":
        # On Windows ASUS host, check plausible hardware string formats
        assert "Windows" in telem["os"]


@pytest.mark.asyncio
async def test_missing_telemetry_fallback():
    """Verify graceful handling when telemetry is queried for an unknown or offline remote device."""
    wsm = WorldStateManager.get_instance()
    state = await wsm.get_device_state("non_enrolled_remote")
    assert "error" in state
    assert "not enrolled" in state["error"]


@pytest.mark.asyncio
async def test_natural_language_telemetry_routing():
    """Verify natural language queries for RAM, CPU, Disk, GPU, Processor, Model, and Specs return live telemetry."""
    device_id = "asus"
    await database.enroll_device(device_id, "asus", "admin", "secret")
    control = CortexControlLayer(hub.main.CONNECTED_DEVICES)
    harness = AgentHarness(control)

    wsm = WorldStateManager.get_instance()
    for s in DeviceSignalAdapter.create_telemetry_signals(device_id):
        await wsm.ingest_signal(s)

    # 1. RAM query
    res_ram = await harness.process_request(device_id, "How much RAM am I using?", {}, None)
    assert res_ram.status == "success"
    assert "ram" in res_ram.message.lower()
    assert "gb" in res_ram.message.lower()

    # 2. CPU query
    res_cpu = await harness.process_request(device_id, "What's my CPU usage?", {}, None)
    assert res_cpu.status == "success"
    assert "cpu usage" in res_cpu.message.lower()
    assert "%" in res_cpu.message

    # 3. Disk query
    res_disk = await harness.process_request(device_id, "How much disk space do I have left?", {}, None)
    assert res_disk.status == "success"
    assert "disk space" in res_disk.message.lower()
    assert "gb free" in res_disk.message.lower()

    # 4. GPU query
    res_gpu = await harness.process_request(device_id, "What GPU do I have?", {}, None)
    assert res_gpu.status == "success"
    assert "gpu" in res_gpu.message.lower()

    # 5. Processor query
    res_proc = await harness.process_request(device_id, "What CPU do I have?", {}, None)
    assert res_proc.status == "success"
    assert "processor" in res_proc.message.lower() or "cpu" in res_proc.message.lower()

    # 6. Specs query
    res_specs = await harness.process_request(device_id, "Tell me my laptop specs.", {}, None)
    assert res_specs.status == "success"
    assert "laptop specs" in res_specs.message.lower()
    assert "cpu:" in res_specs.message.lower()
    assert "gpu:" in res_specs.message.lower()

    # 7. Model query
    res_mod = await harness.process_request(device_id, "What laptop model is this?", {}, None)
    assert res_mod.status == "success"
    assert "model" in res_mod.message.lower()

    # 8. Brand query
    res_brand = await harness.process_request(device_id, "What brand is my laptop?", {}, None)
    assert res_brand.status == "success"
    assert "brand" in res_brand.message.lower()


@pytest.mark.asyncio
async def test_natural_language_calendar_routing():
    """Verify natural language calendar questions return real event data when available."""
    device_id = "asus"
    await database.enroll_device(device_id, "asus", "admin", "secret")
    control = CortexControlLayer(hub.main.CONNECTED_DEVICES)
    harness = AgentHarness(control)

    now = datetime.now(timezone.utc)
    local_now = CalendarSignalAdapter._get_local_now()
    sync_start = local_now.replace(hour=15, minute=30, second=0, microsecond=0)
    if sync_start <= local_now:
        sync_start += timedelta(days=1)
    mock_events = [
        {
            "summary": "Deep Learning Class",
            "start": {"dateTime": now.isoformat()},
            "end": {"dateTime": (now + timedelta(hours=1, minutes=30)).isoformat()}
        },
        {
            "summary": "Cortex Engineering Sync",
            "start": {"dateTime": sync_start.isoformat()},
            "end": {"dateTime": (sync_start + timedelta(hours=1)).isoformat()}
        }
    ]

    wsm = WorldStateManager.get_instance()
    signals = await CalendarSignalAdapter.fetch_and_create_signals(raw_events=mock_events, now=now)
    for s in signals:
        await wsm.ingest_signal(s)

    # 1. Current event
    res_cur = await harness.process_request(device_id, "What's on my calendar right now?", {}, None)
    assert res_cur.status == "success"
    assert "deep learning class" in res_cur.message.lower()

    # 2. Next event
    res_nxt = await harness.process_request(device_id, "What's my next event?", {}, None)
    assert res_nxt.status == "success"
    assert "cortex engineering sync" in res_nxt.message.lower()

    # 3. Next class
    res_class = await harness.process_request(device_id, "When is my next class?", {}, None)
    assert res_class.status == "success"
    assert "deep learning class" in res_class.message.lower()

    # 4. Free after 3 PM (event at 15:30 overlaps after 3 PM)
    res_free = await harness.process_request(device_id, "Am I free after 3 PM?", {}, None)
    assert res_free.status == "success"
    assert "not completely free after 3 pm" in res_free.message.lower()
    assert "cortex engineering sync" in res_free.message.lower()

    # 5. Time until next event
    res_until = await harness.process_request(device_id, "How long until my next event?", {}, None)
    assert res_until.status == "success"
    assert "cortex engineering sync" in res_until.message.lower()
    assert "hour" in res_until.message.lower() or "minute" in res_until.message.lower()


@pytest.mark.asyncio
async def test_world_state_ingestion_and_compact_summary():
    """Verify World State fusion incorporates telemetry and calendar context into compact LLM context."""
    wsm = WorldStateManager.get_instance()
    now = datetime.now(timezone.utc)
    mock_events = [{
        "summary": "One-on-One",
        "start": {"dateTime": (now + timedelta(minutes=10)).isoformat()},
        "end": {"dateTime": (now + timedelta(minutes=40)).isoformat()}
    }]

    for s in await CalendarSignalAdapter.fetch_and_create_signals(raw_events=mock_events, now=now):
        await wsm.ingest_signal(s)

    for s in DeviceSignalAdapter.create_telemetry_signals("asus"):
        await wsm.ingest_signal(s)

    compact = await wsm.get_compact_world_summary("asus")
    assert "calendar_current_event" in compact
    assert "system_telemetry" in compact
    assert compact["system_telemetry"]["cpu_percent"] is not None
    assert compact["system_telemetry"]["ram_used_gb"] is not None


@pytest.mark.asyncio
async def test_8b_calendar_condition_integration():
    """Verify natural-language calendar reminder registration and ProactiveEngine threshold trigger."""
    device_id = "asus"
    await database.enroll_device(device_id, "asus", "admin", "secret")
    control = CortexControlLayer(hub.main.CONNECTED_DEVICES)
    harness = AgentHarness(control)

    # 1. Natural language registration
    req = "Remind me 15 minutes before my next event."
    resp = await harness.process_request(device_id, req, {}, None)
    assert resp.status == "success"
    assert "remind you 15 minutes before" in resp.message.lower()

    engine = ProactiveEngine.get_instance()
    conds = await engine.list_conditions()
    assert len(conds) == 1
    cond = conds[0]
    assert cond.signal_key == "calendar.time_until_next_event"
    assert cond.target_value == 900  # 15 * 60 seconds
    assert cond.operator == "less_than_or_equal"

    wsm = WorldStateManager.get_instance()
    now = datetime.now(timezone.utc)

    # 2. Next event is 30 minutes away (1800s > 900s) -> Condition remains False
    sig_far = Signal(
        key="calendar.time_until_next_event",
        source="google_calendar",
        timestamp=now.isoformat(),
        confidence=0.95,
        ttl=300.0,
        payload={"value": 1800, "seconds": 1800, "time_until_seconds": 1800, "minutes": 30}
    )
    await wsm.ingest_signal(sig_far)
    evs_far = await engine.evaluate()
    assert len(evs_far) == 0

    # 3. Next event is 10 minutes away (600s <= 900s) -> False -> True transition -> ProactiveEvent!
    sig_near = Signal(
        key="calendar.time_until_next_event",
        source="google_calendar",
        timestamp=(now + timedelta(seconds=1)).isoformat(),
        confidence=0.95,
        ttl=300.0,
        payload={"value": 600, "seconds": 600, "time_until_seconds": 600, "minutes": 10}
    )
    await wsm.ingest_signal(sig_near)
    candidates = await engine.list_candidates()
    assert len(candidates) >= 1
    cal_ev = candidates[-1]
    assert cal_ev["condition_id"] == cond.condition_id


@pytest.mark.asyncio
async def test_8c_attention_and_delivery_integration():
    """Verify that a proactive event from a calendar threshold is routed to a ready desktop notification."""
    device_id = "asus"
    await database.enroll_device(device_id, "asus", "admin", "secret")
    await database.update_connection_state(device_id, "connected")

    router = AttentionRouter.get_instance()

    # Create proactive event triggered by calendar condition
    cal_event = {
        "event_id": f"ev_cal_{uuid.uuid4().hex[:8]}",
        "event_type": "signal_threshold",
        "source": "proactive_engine",
        "condition_id": "cond_cal_reminder",
        "trigger_condition": {
            "signal_key": "calendar.time_until_next_event",
            "operator": "less_than_or_equal",
            "target_value": 900
        },
        "context_snapshot": {
            "calendar.next_event": "Project Standup",
            "time_until": "10 minutes"
        },
        "priority": 0.85,
        "confidence": 1.0,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat(),
        "status": "candidate"
    }

    decision = await router.route_event(cal_event)
    assert decision.target_device_id == "asus"
    assert decision.channel == "desktop_notification"
    assert decision.timing == "now"
    assert decision.status == "ready"


@pytest.mark.asyncio
async def test_restart_behavior():
    """Verify that telemetry and calendar signals survive in-memory reset via DB rehydration."""
    wsm = WorldStateManager.get_instance()
    signals = DeviceSignalAdapter.create_telemetry_signals("asus")
    for s in signals:
        await wsm.ingest_signal(s)

    # In-memory reset
    WorldStateManager.reset()
    fresh_wsm = WorldStateManager.get_instance()
    assert len(fresh_wsm._signals) == 0

    # Load from DB
    await fresh_wsm.load_from_db()
    assert "device.cpu_percent:asus" in fresh_wsm._signals
    assert "device.ram_percent:asus" in fresh_wsm._signals
    assert "device.model:asus" in fresh_wsm._signals


@pytest.mark.asyncio
async def test_tier1_capabilities_execution():
    """Verify direct invocation of Tier 1 calendar and telemetry capabilities."""
    device_id = "asus"
    await database.enroll_device(device_id, "asus", "admin", "secret")
    control = CortexControlLayer(hub.main.CONNECTED_DEVICES)
    harness = AgentHarness(control)

    # 1. telemetry.get_system_usage
    plan_usage = ActionPlan(
        intent="telemetry_get_system_usage",
        source_device=device_id,
        target_device_reference="this device",
        capability="telemetry.get_system_usage",
        action="telemetry_get_system_usage",
        arguments={},
        requires_confirmation=False,
        confidence=1.0,
        user_facing_summary="inspecting system usage"
    )
    harness.router.generate_result = AsyncMock(return_value=ModelResult(type="ACTION", action_plan=plan_usage))
    res_usage = await harness.process_request(device_id, "get system usage", {}, None)
    assert res_usage.status == "success"
    assert "cpu:" in res_usage.message.lower()
    assert "ram:" in res_usage.message.lower()

    # 2. telemetry.get_hardware_info
    plan_hw = ActionPlan(
        intent="telemetry_get_hardware_info",
        source_device=device_id,
        target_device_reference="this device",
        capability="telemetry.get_hardware_info",
        action="telemetry_get_hardware_info",
        arguments={},
        requires_confirmation=False,
        confidence=1.0,
        user_facing_summary="inspecting hardware info"
    )
    harness.router.generate_result = AsyncMock(return_value=ModelResult(type="ACTION", action_plan=plan_hw))
    res_hw = await harness.process_request(device_id, "get hardware info", {}, None)
    assert res_hw.status == "success"
    assert "hardware info:" in res_hw.message.lower()

    # 3. calendar.status
    plan_cal_st = ActionPlan(
        intent="calendar_status",
        source_device=device_id,
        target_device_reference="this device",
        capability="calendar.status",
        action="calendar_status",
        arguments={},
        requires_confirmation=False,
        confidence=1.0,
        user_facing_summary="checking calendar status"
    )
    harness.router.generate_result = AsyncMock(return_value=ModelResult(type="ACTION", action_plan=plan_cal_st))
    res_st = await harness.process_request(device_id, "check calendar status", {}, None)
    assert res_st.status == "success"
    assert "google calendar" in res_st.message.lower()


@pytest.mark.asyncio
async def test_calendar_arbitrary_monday_query_outside_cache():
    """Verify that standalone query 'How is my Monday's schedule looking?' outside startup cache performs on-demand fetch for Monday 00:00 to Tuesday 00:00 local."""
    device_id = "asus"
    await database.enroll_device(device_id, "asus", "admin", "secret")
    control = CortexControlLayer(hub.main.CONNECTED_DEVICES)
    harness = AgentHarness(control)

    mock_tc = {
        "local_datetime": "2026-10-10T14:00:00-04:00",
        "local_date": "2026-10-10",
        "local_time": "14:00:00",
        "local_time_12h": "02:00 PM",
        "timezone": "EDT",
        "utc_offset": "UTC-04:00",
        "utc_offset_seconds": -14400,
        "day_of_week": "Saturday",
        "tomorrow_date": "2026-10-11",
        "tomorrow_name": "Sunday",
        "utc_datetime": "2026-10-10T18:00:00+00:00",
    }
    mock_local_now = datetime(2026, 10, 10, 14, 0, 0, tzinfo=timezone(timedelta(hours=-4)))

    wsm = WorldStateManager.get_instance()
    sat_event = [{
        "summary": "Saturday Groceries",
        "start": {"dateTime": "2026-10-10T15:00:00-04:00"},
        "end": {"dateTime": "2026-10-10T16:00:00-04:00"}
    }]
    for s in await CalendarSignalAdapter.fetch_and_create_signals(raw_events=sat_event):
        await wsm.ingest_signal(s)

    monday_items = [
        {
            "summary": "Walmart OGP",
            "start": {"dateTime": "2026-10-12T14:00:00-04:00"},
            "end": {"dateTime": "2026-10-12T19:00:00-04:00"},
            "location": "Store 1234"
        }
    ]

    mock_execute = AsyncMock(return_value={"items": monday_items})
    mock_conn = AsyncMock(return_value={"connected": True, "status": "connected", "account_id": "primary"})

    with patch("hub.world_state.RuntimeTimeAdapter.get_runtime_time_context", return_value=mock_tc), \
         patch("hub.world_state.CalendarSignalAdapter._get_local_now", return_value=mock_local_now), \
         patch("hub.world_state.CalendarSignalAdapter.get_connection_status", mock_conn), \
         patch("hub.integrations.calendar.CalendarIntegration.execute_action", mock_execute):

        resp = await harness.process_request(device_id, "How is my Monday's schedule looking?", {}, None)

        assert resp.status == "success"
        assert "walmart ogp" in resp.message.lower()
        assert "monday" in resp.message.lower()

        mock_execute.assert_called_once()
        args, kwargs = mock_execute.call_args
        assert args[0] == "primary"
        assert args[1] == "calendar.read"
        assert kwargs["time_min"].startswith("2026-10-12T00:00:00")
        assert kwargs["time_max"].startswith("2026-10-13T00:00:00")

        # Verify World State default cache was not modified with out-of-cache event
        raw_events_cached = wsm._signals.get("calendar.raw_events").payload.get("events", [])
        assert all("walmart ogp" not in ev.get("summary", "").lower() for ev in raw_events_cached)


@pytest.mark.asyncio
async def test_calendar_arbitrary_explicit_date_and_all_day_outside_cache():
    """Verify explicit date query 'What do I have October 18?' fetches Oct 18 00:00 to Oct 19 00:00 and formats all-day events."""
    device_id = "asus"
    await database.enroll_device(device_id, "asus", "admin", "secret")
    control = CortexControlLayer(hub.main.CONNECTED_DEVICES)
    harness = AgentHarness(control)

    mock_tc = {
        "local_datetime": "2026-10-10T14:00:00-04:00",
        "local_date": "2026-10-10",
        "local_time": "14:00:00",
        "local_time_12h": "02:00 PM",
        "timezone": "EDT",
        "utc_offset": "UTC-04:00",
        "utc_offset_seconds": -14400,
        "day_of_week": "Saturday",
        "tomorrow_date": "2026-10-11",
        "tomorrow_name": "Sunday",
        "utc_datetime": "2026-10-10T18:00:00+00:00",
    }
    mock_local_now = datetime(2026, 10, 10, 14, 0, 0, tzinfo=timezone(timedelta(hours=-4)))

    wsm = WorldStateManager.get_instance()
    for s in await CalendarSignalAdapter.fetch_and_create_signals(raw_events=[]):
        await wsm.ingest_signal(s)

    oct18_items = [
        {
            "summary": "Hackathon 2026",
            "start": {"date": "2026-10-18"},
            "end": {"date": "2026-10-19"},
            "location": "Boston Convention Center"
        }
    ]

    mock_execute = AsyncMock(return_value={"items": oct18_items})
    mock_conn = AsyncMock(return_value={"connected": True, "status": "connected", "account_id": "primary"})

    with patch("hub.world_state.RuntimeTimeAdapter.get_runtime_time_context", return_value=mock_tc), \
         patch("hub.world_state.CalendarSignalAdapter._get_local_now", return_value=mock_local_now), \
         patch("hub.world_state.CalendarSignalAdapter.get_connection_status", mock_conn), \
         patch("hub.integrations.calendar.CalendarIntegration.execute_action", mock_execute):

        resp = await harness.process_request(device_id, "What do I have October 18?", {}, None)

        assert resp.status == "success"
        assert "hackathon 2026" in resp.message.lower()
        assert "(all day)" in resp.message.lower()
        assert "october 18" in resp.message.lower()

        mock_execute.assert_called_once()
        args, kwargs = mock_execute.call_args
        assert kwargs["time_min"].startswith("2026-10-18T00:00:00")
        assert kwargs["time_max"].startswith("2026-10-19T00:00:00")


@pytest.mark.asyncio
async def test_calendar_arbitrary_queries_tuesday_and_next_friday():
    """Verify 'What do I have Tuesday?' and 'What's my schedule next Friday?' resolve and fetch live slices."""
    device_id = "asus"
    await database.enroll_device(device_id, "asus", "admin", "secret")
    control = CortexControlLayer(hub.main.CONNECTED_DEVICES)
    harness = AgentHarness(control)

    mock_tc = {
        "local_datetime": "2026-10-10T14:00:00-04:00",
        "local_date": "2026-10-10",
        "local_time": "14:00:00",
        "local_time_12h": "02:00 PM",
        "timezone": "EDT",
        "utc_offset": "UTC-04:00",
        "utc_offset_seconds": -14400,
        "day_of_week": "Saturday",
        "tomorrow_date": "2026-10-11",
        "tomorrow_name": "Sunday",
        "utc_datetime": "2026-10-10T18:00:00+00:00",
    }
    mock_local_now = datetime(2026, 10, 10, 14, 0, 0, tzinfo=timezone(timedelta(hours=-4)))

    wsm = WorldStateManager.get_instance()
    for s in await CalendarSignalAdapter.fetch_and_create_signals(raw_events=[]):
        await wsm.ingest_signal(s)

    mock_execute = AsyncMock(side_effect=[
        {"items": [{"summary": "Tuesday Sync", "start": {"dateTime": "2026-10-13T11:00:00-04:00"}, "end": {"dateTime": "2026-10-13T12:00:00-04:00"}}]},
        {"items": [{"summary": "Friday Demo", "start": {"dateTime": "2026-10-16T15:00:00-04:00"}, "end": {"dateTime": "2026-10-16T16:00:00-04:00"}}]},
    ])
    mock_conn = AsyncMock(return_value={"connected": True, "status": "connected", "account_id": "primary"})

    with patch("hub.world_state.RuntimeTimeAdapter.get_runtime_time_context", return_value=mock_tc), \
         patch("hub.world_state.CalendarSignalAdapter._get_local_now", return_value=mock_local_now), \
         patch("hub.world_state.CalendarSignalAdapter.get_connection_status", mock_conn), \
         patch("hub.integrations.calendar.CalendarIntegration.execute_action", mock_execute):

        resp_tue = await harness.process_request(device_id, "What do I have Tuesday?", {}, None)
        assert resp_tue.status == "success"
        assert "tuesday sync" in resp_tue.message.lower()
        call1_kwargs = mock_execute.call_args_list[0][1]
        assert call1_kwargs["time_min"].startswith("2026-10-13T00:00:00")
        assert call1_kwargs["time_max"].startswith("2026-10-14T00:00:00")

        resp_fri = await harness.process_request(device_id, "What's my schedule next Friday?", {}, None)
        assert resp_fri.status == "success"
        assert "friday demo" in resp_fri.message.lower()
        call2_kwargs = mock_execute.call_args_list[1][1]
        assert call2_kwargs["time_min"].startswith("2026-10-16T00:00:00")
        assert call2_kwargs["time_max"].startswith("2026-10-17T00:00:00")


@pytest.mark.asyncio
async def test_calendar_followup_arbitrary_monday_outside_cache():
    """Verify conversational follow-up 'What about Monday?' inherits calendar topic and fetches live slice."""
    device_id = "asus"
    await database.enroll_device(device_id, "asus", "admin", "secret")
    control = CortexControlLayer(hub.main.CONNECTED_DEVICES)
    harness = AgentHarness(control)

    mock_tc = {
        "local_datetime": "2026-10-10T14:00:00-04:00",
        "local_date": "2026-10-10",
        "local_time": "14:00:00",
        "local_time_12h": "02:00 PM",
        "timezone": "EDT",
        "utc_offset": "UTC-04:00",
        "utc_offset_seconds": -14400,
        "day_of_week": "Saturday",
        "tomorrow_date": "2026-10-11",
        "tomorrow_name": "Sunday",
        "utc_datetime": "2026-10-10T18:00:00+00:00",
    }
    mock_local_now = datetime(2026, 10, 10, 14, 0, 0, tzinfo=timezone(timedelta(hours=-4)))

    wsm = WorldStateManager.get_instance()
    for s in await CalendarSignalAdapter.fetch_and_create_signals(raw_events=[]):
        await wsm.ingest_signal(s)

    mock_execute = AsyncMock(return_value={"items": [{"summary": "Monday Sprint Planning", "start": {"dateTime": "2026-10-12T09:00:00-04:00"}, "end": {"dateTime": "2026-10-12T10:00:00-04:00"}}]})
    mock_conn = AsyncMock(return_value={"connected": True, "status": "connected", "account_id": "primary"})

    session_context = {"session": {"references": {"last_calendar_topic": "schedule_2026-10-10"}}}

    with patch("hub.world_state.RuntimeTimeAdapter.get_runtime_time_context", return_value=mock_tc), \
         patch("hub.world_state.CalendarSignalAdapter._get_local_now", return_value=mock_local_now), \
         patch("hub.world_state.CalendarSignalAdapter.get_connection_status", mock_conn), \
         patch("hub.integrations.calendar.CalendarIntegration.execute_action", mock_execute):

        resp = await harness.process_request(device_id, "What about Monday?", session_context, None)

        assert resp.status == "success"
        assert "monday sprint planning" in resp.message.lower()
        mock_execute.assert_called_once()
        kwargs = mock_execute.call_args[1]
        assert kwargs["time_min"].startswith("2026-10-12T00:00:00")
        assert kwargs["time_max"].startswith("2026-10-13T00:00:00")


@pytest.mark.asyncio
async def test_production_db_isolation():
    """Verify that the production database file (data/cortex.db) was never touched or modified."""
    from hub.database import get_db_path
    resolved_test_db = get_db_path().resolve()
    prod_path = Path("data/cortex.db").resolve()
    assert resolved_test_db != prod_path, f"FATAL: Test targeting prod DB at {resolved_test_db}!"
    assert not str(resolved_test_db).replace('\\', '/').endswith("data/cortex.db")
    assert prod_path.exists(), "Production DB data/cortex.db must exist."
