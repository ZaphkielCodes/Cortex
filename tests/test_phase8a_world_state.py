"""
tests/test_phase8a_world_state.py
---------------------------------
Deterministic test suite for Phase 8A: World State and Context Fusion Foundation.
Tests cover:
- Signal ingestion
- Freshness and TTL expiry
- Confidence handling
- Stale signal rejection
- Device state fusion
- Calendar context
- Location signal
- Foreground activity
- Persistence across restart
- Bounded event history
- Context capabilities execution
- Production DB safety
"""

import pytest
import hashlib
import asyncio
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import AsyncMock

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
    CalendarSignalAdapter,
    LocationSignalAdapter,
    ActivitySignalAdapter,
    TaskSignalAdapter
)


@pytest.mark.asyncio
async def test_signal_ingestion():
    """Verify generic signal ingestion with provenance and freshness metadata."""
    wsm = WorldStateManager.get_instance()
    now = datetime.now(timezone.utc).isoformat()

    sig = Signal(
        key="device.battery:dev_test",
        source="device_agent",
        timestamp=now,
        confidence=0.98,
        ttl=300.0,
        payload={"device_id": "dev_test", "battery_level": 78, "is_charging": False}
    )

    success = await wsm.ingest_signal(sig, record_event=True, event_summary="Battery 78%")
    assert success is True

    fused = await wsm.get_fused_world_state("dev_test")
    assert "dev_test" in fused.battery_states
    assert fused.battery_states["dev_test"]["battery_level"] == 78
    assert fused.battery_states["dev_test"]["is_charging"] is False
    assert fused.freshness_metadata["device.battery:dev_test"]["source"] == "device_agent"


@pytest.mark.asyncio
async def test_freshness_and_ttl_expiry():
    """Verify signals expire when TTL elapses and are excluded from fused state."""
    wsm = WorldStateManager.get_instance()
    # Signal created with TTL of 2 seconds, but timestamped 10 seconds ago
    expired_time = (datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat()

    expired_sig = Signal(
        key="device.foreground:dev_expired",
        source="device_agent",
        timestamp=expired_time,
        confidence=0.95,
        ttl=2.0,
        payload={"device_id": "dev_expired", "app_name": "Slack"}
    )

    # Ingestion of already expired signal is rejected
    rejected = await wsm.ingest_signal(expired_sig)
    assert rejected is False

    # Ingest a signal with 1s TTL, verify initially fresh then expires
    fresh_time = datetime.now(timezone.utc).isoformat()
    short_lived = Signal(
        key="device.temp:dev_1",
        source="test",
        timestamp=fresh_time,
        confidence=0.9,
        ttl=1.0,
        payload={"temp": 42}
    )
    assert await wsm.ingest_signal(short_lived) is True
    assert short_lived.is_fresh() is True

    # Sleep slightly over TTL
    await asyncio.sleep(1.1)
    now_later = datetime.now(timezone.utc)
    assert short_lived.is_fresh(now_later) is False

    # Check that get_fused_world_state filters it out
    fused = await wsm.get_fused_world_state()
    assert "device.temp:dev_1" not in fused.freshness_metadata


@pytest.mark.asyncio
async def test_confidence_handling():
    """Verify confidence tracking and rejection of lower-confidence duplicate signals."""
    wsm = WorldStateManager.get_instance()
    ts = datetime.now(timezone.utc).isoformat()

    high_conf = Signal(
        key="location.current",
        source="gps",
        timestamp=ts,
        confidence=0.95,
        ttl=600.0,
        payload={"label": "Office"}
    )
    assert await wsm.ingest_signal(high_conf) is True

    # Same timestamp but lower confidence -> rejected
    low_conf = Signal(
        key="location.current",
        source="ip_geo",
        timestamp=ts,
        confidence=0.50,
        ttl=600.0,
        payload={"label": "Coffee Shop"}
    )
    assert await wsm.ingest_signal(low_conf) is False

    fused = await wsm.get_fused_world_state()
    assert fused.location_context["label"] == "Office"
    assert fused.confidence >= 0.85


@pytest.mark.asyncio
async def test_stale_signal_rejection():
    """Verify older signals are rejected when newer fresh signals already exist."""
    wsm = WorldStateManager.get_instance()
    t_now = datetime.now(timezone.utc)
    t_newer = t_now.isoformat()
    t_older = (t_now - timedelta(seconds=30)).isoformat()

    newer_sig = Signal(
        key="task.active",
        source="task_orchestrator",
        timestamp=t_newer,
        confidence=1.0,
        ttl=300.0,
        payload={"id": "task_2", "title": "Building Phase 8"}
    )
    assert await wsm.ingest_signal(newer_sig) is True

    # Older signal for same key
    older_sig = Signal(
        key="task.active",
        source="task_orchestrator",
        timestamp=t_older,
        confidence=1.0,
        ttl=300.0,
        payload={"id": "task_1", "title": "Old Task"}
    )
    assert await wsm.ingest_signal(older_sig) is False

    fused = await wsm.get_fused_world_state()
    assert fused.current_task["id"] == "task_2"
    assert fused.current_task["title"] == "Building Phase 8"


@pytest.mark.asyncio
async def test_device_state_fusion():
    """Verify per-device telemetry is accurately fused into device context."""
    device_id = "test_dev_state"
    await database.enroll_device(device_id, "Dev Laptop", "admin", "secret_hash")
    await database.update_connection_state(device_id, "connected")
    hub.main.CONNECTED_DEVICES[device_id] = object()  # Mock online connection

    wsm = WorldStateManager.get_instance()
    wsm.set_active_device(device_id)

    # Ingest battery
    bat_sig = DeviceSignalAdapter.create_battery_signal(device_id, battery_level=92, is_charging=True)
    await wsm.ingest_signal(bat_sig)

    # Ingest network
    net_sig = DeviceSignalAdapter.create_network_signal(device_id, network_state="WiFi: Home_5G", is_connected=True)
    await wsm.ingest_signal(net_sig)

    # Ingest foreground
    fg_sig = DeviceSignalAdapter.create_foreground_signal(device_id, app_name="Visual Studio Code", window_title="cortex.py - Visual Studio Code")
    await wsm.ingest_signal(fg_sig)

    dev_state = await wsm.get_device_state(device_id)
    assert dev_state["device_id"] == device_id
    assert dev_state["device_name"] == "Dev Laptop"
    assert dev_state["is_online"] is True
    assert dev_state["is_authenticated"] is True
    assert dev_state["battery"]["battery_level"] == 92
    assert dev_state["battery"]["is_charging"] is True
    assert dev_state["network"]["network_state"] == "WiFi: Home_5G"
    assert dev_state["foreground_activity"]["app_name"] == "Visual Studio Code"


@pytest.mark.asyncio
async def test_calendar_context():
    """Verify calendar adapter parses current/next events and free/busy window."""
    now = datetime.now(timezone.utc)
    ev_current = {
        "summary": "Team Architecture Sync",
        "start": (now - timedelta(minutes=15)).isoformat(),
        "end": (now + timedelta(minutes=45)).isoformat()
    }
    ev_next = {
        "summary": "Doctor Appointment",
        "start": (now + timedelta(hours=2)).isoformat(),
        "end": (now + timedelta(hours=3)).isoformat()
    }

    parsed = CalendarSignalAdapter.parse_events([ev_current, ev_next], now=now)
    assert parsed["current_event"]["title"] == "Team Architecture Sync"
    assert parsed["next_event"]["title"] == "Doctor Appointment"
    assert "Busy" in parsed["free_busy_window"]
    assert "Team Architecture Sync" in parsed["free_busy_window"]

    # Test free window when no current event
    now_free = now + timedelta(hours=1)  # after team sync, 1h before doctor
    parsed_free = CalendarSignalAdapter.parse_events([ev_current, ev_next], now=now_free)
    assert parsed_free["current_event"] is None
    assert parsed_free["next_event"]["title"] == "Doctor Appointment"
    assert "Free for 1 hour" in parsed_free["free_busy_window"] or "Free for 60 minutes" in parsed_free["free_busy_window"]

    # Ingest as signal
    wsm = WorldStateManager.get_instance()
    cal_sig = CalendarSignalAdapter.create_calendar_signal(parsed)
    await wsm.ingest_signal(cal_sig)

    fused = await wsm.get_fused_world_state()
    assert fused.calendar_context["current_event"]["title"] == "Team Architecture Sync"


@pytest.mark.asyncio
async def test_location_signal_and_situational_query():
    """Verify location signal ingestion and natural language situational query answering."""
    device_id = "test_dev_loc"
    await database.enroll_device(device_id, "Phone", "admin", "secret_hash")
    await database.update_connection_state(device_id, "connected")

    wsm = WorldStateManager.get_instance()
    loc_sig = LocationSignalAdapter.create_location_signal("Home Office", address="742 Evergreen Terrace")
    await wsm.ingest_signal(loc_sig)

    control = CortexControlLayer(hub.main.CONNECTED_DEVICES)
    harness = AgentHarness(control)

    ctx = await ContextManager.get_active_context(device_id, request="Where am I?")
    assert ctx["world_state"]["location"] == "Home Office"

    resp = await harness.process_request(device_id, "Where am I?", ctx, None)
    assert resp.status == "success"
    assert "home office" in resp.message.lower()


@pytest.mark.asyncio
async def test_foreground_activity_and_task_query():
    """Verify foreground activity and active task fusion in conversational responses."""
    device_id = "test_dev_fg"
    await database.enroll_device(device_id, "Laptop", "admin", "secret_hash")
    await database.update_connection_state(device_id, "connected")

    wsm = WorldStateManager.get_instance()
    fg_sig = ActivitySignalAdapter.create_activity_signal(device_id, app_name="PyCharm", window_title="world_state.py - PyCharm")
    task_sig = TaskSignalAdapter.create_task_signal("task_8a", title="Context Fusion Architecture")
    await wsm.ingest_signal(fg_sig)
    await wsm.ingest_signal(task_sig)

    control = CortexControlLayer(hub.main.CONNECTED_DEVICES)
    harness = AgentHarness(control)

    ctx = await ContextManager.get_active_context(device_id, request="What app is open?")
    assert ctx["world_state"]["foreground_app"] == "PyCharm"
    assert ctx["world_state"]["active_task"] == "Context Fusion Architecture"

    resp = await harness.process_request(device_id, "What app is open?", ctx, None)
    assert resp.status == "success"
    assert "pycharm" in resp.message.lower()
    assert "context fusion architecture" in resp.message.lower()


@pytest.mark.asyncio
async def test_persistence_across_restart():
    """Verify world state signals and events survive in-memory restart."""
    wsm = WorldStateManager.get_instance()
    loc_sig = LocationSignalAdapter.create_location_signal("Downtown Library", ttl=1800.0)
    await wsm.ingest_signal(loc_sig, record_event=True, event_summary="Arrived at library")

    # Clear in-memory singleton
    WorldStateManager.reset()
    fresh_wsm = WorldStateManager.get_instance()
    assert len(fresh_wsm._signals) == 0

    # Reload from DB
    await fresh_wsm.load_from_db()
    assert "location.current" in fresh_wsm._signals
    assert fresh_wsm._signals["location.current"].payload["label"] == "Downtown Library"

    events = await fresh_wsm.get_recent_events(10)
    assert len(events) >= 1
    assert any("library" in e["summary"].lower() for e in events)


@pytest.mark.asyncio
async def test_bounded_event_history():
    """Verify context events remain strictly bounded in memory and persistent storage."""
    wsm = WorldStateManager.get_instance()

    # Generate 25 distinct context events
    for i in range(25):
        s = Signal(
            key=f"sensor.event_{i}",
            source="sensor",
            timestamp=datetime.now(timezone.utc).isoformat(),
            confidence=0.9,
            ttl=300.0,
            payload={"index": i}
        )
        await wsm.ingest_signal(s, record_event=True, event_summary=f"Event index {i}")

    # Check bounded in-memory list (<= 20)
    assert len(wsm._recent_events) <= 20

    # Check bounded DB retrieval
    recent = await wsm.get_recent_events(limit=15)
    assert len(recent) == 15


@pytest.mark.asyncio
async def test_bounded_context_capabilities():
    """Verify context.get_world_state, context.update_signal, context.get_device_state, and context.get_recent_events."""
    device_id = "test_dev_caps"
    await database.enroll_device(device_id, "Workstation", "admin", "secret_hash")
    await database.update_connection_state(device_id, "connected")

    control = CortexControlLayer(hub.main.CONNECTED_DEVICES)
    harness = AgentHarness(control)
    wsm = WorldStateManager.get_instance()

    # 1. context.update_signal
    plan_update = ActionPlan(
        intent="update_signal",
        source_device=device_id,
        target_device_reference="this device",
        capability="context.update_signal",
        action="update_signal",
        arguments={"key": "location.current", "value": {"label": "Conference Room B"}, "ttl": 600.0},
        requires_confirmation=False,
        confidence=1.0,
        user_facing_summary="updating location signal"
    )
    harness.router.generate_result = AsyncMock(return_value=ModelResult(type="ACTION", action_plan=plan_update))
    res_up = await harness.process_request(device_id, "Update signal location", {}, None)
    assert res_up.status == "success"
    assert "location.current" in res_up.message

    # 2. context.get_world_state
    plan_ws = ActionPlan(
        intent="get_world_state",
        source_device=device_id,
        target_device_reference="this device",
        capability="context.get_world_state",
        action="get_world_state",
        arguments={},
        requires_confirmation=False,
        confidence=1.0,
        user_facing_summary="getting world state"
    )
    harness.router.generate_result = AsyncMock(return_value=ModelResult(type="ACTION", action_plan=plan_ws))
    res_ws = await harness.process_request(device_id, "Get world state", {}, None)
    assert res_ws.status == "success"
    assert "world state" in res_ws.message.lower()
    assert "conference room b" in res_ws.message.lower()

    # 3. context.get_device_state
    plan_dev = ActionPlan(
        intent="get_device_state",
        source_device=device_id,
        target_device_reference="this device",
        capability="context.get_device_state",
        action="get_device_state",
        arguments={"device_id": device_id},
        requires_confirmation=False,
        confidence=1.0,
        user_facing_summary="getting device state"
    )
    harness.router.generate_result = AsyncMock(return_value=ModelResult(type="ACTION", action_plan=plan_dev))
    res_dev = await harness.process_request(device_id, "Get device state", {}, None)
    assert res_dev.status == "success"
    assert "workstation" in res_dev.message.lower()

    # 4. context.get_recent_events
    plan_ev = ActionPlan(
        intent="get_recent_events",
        source_device=device_id,
        target_device_reference="this device",
        capability="context.get_recent_events",
        action="get_recent_events",
        arguments={"limit": 5},
        requires_confirmation=False,
        confidence=1.0,
        user_facing_summary="getting recent events"
    )
    harness.router.generate_result = AsyncMock(return_value=ModelResult(type="ACTION", action_plan=plan_ev))
    res_ev = await harness.process_request(device_id, "Get recent events", {}, None)
    assert res_ev.status == "success"
    assert "recent context events" in res_ev.message.lower()


@pytest.mark.asyncio
async def test_production_db_safety():
    """Verify that production DB (data/cortex.db) remains 100% untouched and isolated."""
    from hub.database import get_db_path
    resolved_test_db = get_db_path().resolve()
    prod_db_path = Path("data/cortex.db").resolve()
    assert resolved_test_db != prod_db_path, f"FATAL: Test targeting prod DB at {resolved_test_db}!"
    assert not str(resolved_test_db).replace('\\', '/').endswith("data/cortex.db")
    assert prod_db_path.exists(), "Production DB data/cortex.db must exist."
