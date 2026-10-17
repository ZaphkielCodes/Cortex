"""
tests/test_phase8b_proactive.py
-------------------------------
Deterministic test suite for Cortex Phase 8B:
Proactive Context + Contextual Event Engine.

Tests:
1. False -> True transition
2. Deduplication: True -> True (no duplicate)
3. Reset: True -> False
4. Retrigger after reset (False -> True later)
5. Waiting task dependency (waiting -> resumable, no auto execution)
6. Contextual reminder (composite time + location)
7. Priority scoring (deterministic bounds [0.0, 1.0])
8. Expiration (condition expiry)
9. Persistence across restart (DB rehydration)
10. Stale signal ignored (rejection of expired signals)
11. Multiple unrelated conditions
12. Capabilities (register, evaluate, list, get, dismiss)
13. Natural-language registration
14. Required real end-to-end scenario (NL -> 100 -> 94 -> 93 -> 97 -> 94)
15. DB safety
"""

import pytest
import hashlib
import asyncio
from datetime import datetime, timezone, timedelta
from pathlib import Path

import hub.database as database
from hub.control_layer import CortexControlLayer
from hub.agent_harness import AgentHarness
from hub.context import ContextManager
from hub.world_state import Signal, WorldStateManager, DeviceSignalAdapter
from hub.proactive_engine import (
    Condition,
    ProactiveEvent,
    ProactiveEngine,
    calculate_priority,
)


@pytest.mark.asyncio
async def test_false_to_true_transition():
    """Verify false -> true transition creates exactly one proactive candidate event."""
    engine = ProactiveEngine.get_instance()
    wsm = WorldStateManager.get_instance()

    # 1. Register condition: battery < 20
    cond = await engine.register_condition(
        signal_key="device.battery:asus",
        operator="less_than",
        target_value=20,
        description="Battery below 20%"
    )
    assert cond.current_truth_state is False

    # 2. Ingest battery 80% (false)
    sig_80 = DeviceSignalAdapter.create_battery_signal("asus", 80, False)
    await wsm.ingest_signal(sig_80)
    candidates = await engine.list_candidates()
    assert len(candidates) == 0
    assert cond.current_truth_state is False

    # 3. Ingest battery 15% (false -> true)
    sig_15 = DeviceSignalAdapter.create_battery_signal("asus", 15, False)
    await wsm.ingest_signal(sig_15)

    candidates = await engine.list_candidates()
    assert len(candidates) == 1
    ev = candidates[0]
    assert ev["status"] == "candidate"
    assert ev["condition_id"] == cond.condition_id
    assert ev["trigger_condition"]["actual_value"] == 15
    assert 0.0 <= ev["priority"] <= 1.0
    assert cond.current_truth_state is True


@pytest.mark.asyncio
async def test_deduplication_true_to_true():
    """Verify true -> true state produces no duplicate candidate event."""
    engine = ProactiveEngine.get_instance()
    wsm = WorldStateManager.get_instance()

    cond = await engine.register_condition(
        signal_key="device.battery:asus",
        operator="less_than",
        target_value=20,
        description="Battery below 20%"
    )

    # Ingest 15% -> 1st event
    await wsm.ingest_signal(DeviceSignalAdapter.create_battery_signal("asus", 15, False))
    candidates_1 = await engine.list_candidates()
    assert len(candidates_1) == 1

    # Ingest 14% -> True -> True: deduplicate
    await wsm.ingest_signal(DeviceSignalAdapter.create_battery_signal("asus", 14, False))
    candidates_2 = await engine.list_candidates()
    assert len(candidates_2) == 1  # No duplicate!

    # Ingest 10% -> True -> True: deduplicate
    await wsm.ingest_signal(DeviceSignalAdapter.create_battery_signal("asus", 10, False))
    candidates_3 = await engine.list_candidates()
    assert len(candidates_3) == 1


@pytest.mark.asyncio
async def test_reset_true_to_false():
    """Verify true -> false resets truth state without creating new events."""
    engine = ProactiveEngine.get_instance()
    wsm = WorldStateManager.get_instance()

    cond = await engine.register_condition(
        signal_key="device.battery:asus",
        operator="less_than",
        target_value=20,
        description="Battery below 20%"
    )

    # Ingest 15% -> True
    await wsm.ingest_signal(DeviceSignalAdapter.create_battery_signal("asus", 15, False))
    assert cond.current_truth_state is True
    candidates_1 = await engine.list_candidates()
    assert len(candidates_1) == 1

    # Ingest 60% -> False (reset)
    await wsm.ingest_signal(DeviceSignalAdapter.create_battery_signal("asus", 60, False))
    assert cond.current_truth_state is False
    candidates_2 = await engine.list_candidates()
    assert len(candidates_2) == 1  # Still 1 candidate, no new event


@pytest.mark.asyncio
async def test_retrigger_after_reset():
    """Verify retriggering creates a new event after a reset cycle."""
    engine = ProactiveEngine.get_instance()
    wsm = WorldStateManager.get_instance()

    cond = await engine.register_condition(
        signal_key="device.battery:asus",
        operator="less_than",
        target_value=20,
        description="Battery below 20%"
    )

    # 1. 15% -> Event 1
    await wsm.ingest_signal(DeviceSignalAdapter.create_battery_signal("asus", 15, False))
    assert len(await engine.list_candidates()) == 1

    # 2. 60% -> Reset
    await wsm.ingest_signal(DeviceSignalAdapter.create_battery_signal("asus", 60, False))
    assert cond.current_truth_state is False

    # 3. 18% -> Event 2 (retrigger)
    await wsm.ingest_signal(DeviceSignalAdapter.create_battery_signal("asus", 18, False))
    assert cond.current_truth_state is True
    candidates = await engine.list_candidates()
    assert len(candidates) == 2


@pytest.mark.asyncio
async def test_waiting_task_dependency():
    """Verify task dependency condition marks task resumable without executing it."""
    engine = ProactiveEngine.get_instance()
    wsm = WorldStateManager.get_instance()

    # Create a waiting task
    task_id = "task_dep_test_1"
    task_data = {
        "id": task_id,
        "title": "Sync Backup",
        "type": "instant",
        "status": "waiting",
        "command": "fs.move",
        "context": {"waiting_for": "device.online:asus"}
    }
    await database.add_task(task_data)

    # Register task dependency condition
    cond = await engine.register_task_dependency(
        task_id=task_id,
        waiting_for_signal="device.online:asus",
        target_value=True,
        description="Sync Backup waiting for ASUS online"
    )

    # Initially task is waiting
    t_before = await database.get_task(task_id)
    assert t_before["status"] == "waiting"

    # ASUS becomes online
    now = datetime.now(timezone.utc).isoformat()
    online_sig = Signal(
        key="device.online:asus",
        source="device_agent",
        timestamp=now,
        confidence=1.0,
        ttl=300.0,
        payload={"is_online": True}
    )
    await wsm.ingest_signal(online_sig)

    # Candidate event created for task dependency
    cands = await engine.list_candidates()
    assert len(cands) == 1
    ev = cands[0]
    assert ev["related_task_id"] == task_id
    assert ev["event_type"] == "task_dependency"

    # Task is marked resumable in database!
    t_after = await database.get_task(task_id)
    assert t_after["status"] == "resumable"


@pytest.mark.asyncio
async def test_contextual_reminder():
    """Verify composite condition (location + time) creates contextual reminder event."""
    engine = ProactiveEngine.get_instance()
    wsm = WorldStateManager.get_instance()

    now = datetime.now(timezone.utc)
    cur_hour = now.hour

    # Create composite condition: current hour AND location == home
    cond = await engine.register_condition(
        condition_type="contextual_reminder",
        clauses=[
            {"signal": "time.hour", "operator": "greater_than_or_equal", "value": cur_hour},
            {"signal": "location.label", "operator": "equals", "value": "home"}
        ],
        combination="AND",
        description="Home after current hour"
    )

    # Location is office -> condition is false
    office_sig = Signal(
        key="location.current",
        source="location_provider",
        timestamp=now.isoformat(),
        confidence=0.9,
        ttl=300.0,
        payload={"label": "office", "type": "workplace"}
    )
    await wsm.ingest_signal(office_sig)
    assert len(await engine.list_candidates()) == 0

    # Location becomes home -> both clauses true -> creates event
    home_sig = Signal(
        key="location.current",
        source="location_provider",
        timestamp=now.isoformat(),
        confidence=0.9,
        ttl=300.0,
        payload={"label": "home", "type": "place"}
    )
    await wsm.ingest_signal(home_sig)

    cands = await engine.list_candidates()
    assert len(cands) == 1
    assert cands[0]["event_type"] == "contextual_reminder"


def test_priority_scoring():
    """Verify priority scoring is deterministic and strictly bounded in [0.0, 1.0]."""
    # 1. Critical low battery
    prio_crit = calculate_priority(
        condition_type="signal_threshold",
        trigger_signal_key="device.battery:asus",
        actual_value=10,
        target_value=20,
        signal_confidence=1.0,
        signal_freshness=1.0,
        deadline_seconds=300
    )
    assert 0.0 <= prio_crit <= 1.0
    assert prio_crit > 0.70

    # 2. Mild threshold (battery drops below 95%)
    prio_mild = calculate_priority(
        condition_type="signal_threshold",
        trigger_signal_key="device.battery:asus",
        actual_value=94,
        target_value=95,
        signal_confidence=0.9,
        signal_freshness=0.8
    )
    assert 0.0 <= prio_mild <= 1.0
    assert prio_mild < prio_crit

    # 3. Bounds check on extreme inputs
    p_high = calculate_priority("task", "sig", 0, 0, 1.5, 2.0, "urgent", 10)
    p_low = calculate_priority("task", "sig", 100, 100, 0.0, 0.0, None, None)
    assert 0.0 <= p_high <= 1.0
    assert 0.0 <= p_low <= 1.0


@pytest.mark.asyncio
async def test_expiration():
    """Verify expired conditions are skipped and marked inactive."""
    engine = ProactiveEngine.get_instance()
    wsm = WorldStateManager.get_instance()

    past_time = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    cond = await engine.register_condition(
        signal_key="device.battery:asus",
        operator="less_than",
        target_value=50,
        expires_at=past_time,
        description="Expired condition"
    )

    # Ingest signal that would satisfy condition
    await wsm.ingest_signal(DeviceSignalAdapter.create_battery_signal("asus", 30, False))

    # Condition was expired -> no event
    assert len(await engine.list_candidates()) == 0
    assert cond.is_active is False


@pytest.mark.asyncio
async def test_persistence_across_restart():
    """Verify conditions and truth states survive Hub restart and rehydrate from DB."""
    engine = ProactiveEngine.get_instance()
    wsm = WorldStateManager.get_instance()

    cond = await engine.register_condition(
        signal_key="device.battery:asus",
        operator="less_than",
        target_value=30,
        description="Persisted battery check"
    )

    # Trigger event
    await wsm.ingest_signal(DeviceSignalAdapter.create_battery_signal("asus", 25, False))
    assert cond.current_truth_state is True
    assert len(await engine.list_candidates()) == 1

    # Simulate Hub restart
    ProactiveEngine.reset()
    new_engine = ProactiveEngine.get_instance()
    assert len(new_engine._conditions) == 0

    await new_engine.load_from_db()
    assert cond.condition_id in new_engine._conditions
    rehydrated_cond = new_engine._conditions[cond.condition_id]
    assert rehydrated_cond.current_truth_state is True
    assert rehydrated_cond.description == "Persisted battery check"

    cands = await new_engine.list_candidates()
    assert len(cands) == 1


@pytest.mark.asyncio
async def test_stale_signal_ignored():
    """Verify signals with expired TTL are ignored and do not trigger conditions."""
    engine = ProactiveEngine.get_instance()
    wsm = WorldStateManager.get_instance()

    cond = await engine.register_condition(
        signal_key="device.battery:asus",
        operator="less_than",
        target_value=50,
        description="Fresh battery check"
    )

    # Create stale signal (timestamp 10 minutes ago, TTL 300s)
    old_time = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
    stale_sig = Signal(
        key="device.battery:asus",
        source="device_agent",
        timestamp=old_time,
        confidence=1.0,
        ttl=300.0,
        payload={"battery_level": 25}
    )

    # Ingest stale signal -> rejected or ignored
    success = await wsm.ingest_signal(stale_sig)
    assert success is False

    # Force direct evaluate -> no event
    evs = await engine.evaluate()
    assert len(evs) == 0
    assert len(await engine.list_candidates()) == 0


@pytest.mark.asyncio
async def test_multiple_unrelated_conditions():
    """Verify only the matching condition transitions when a specific signal changes."""
    engine = ProactiveEngine.get_instance()
    wsm = WorldStateManager.get_instance()

    cond_bat = await engine.register_condition(
        signal_key="device.battery:asus",
        operator="less_than",
        target_value=20,
        description="Battery condition"
    )
    cond_loc = await engine.register_condition(
        signal_key="location.label",
        operator="equals",
        target_value="gym",
        description="Location condition"
    )

    # Update only battery
    await wsm.ingest_signal(DeviceSignalAdapter.create_battery_signal("asus", 15, False))

    assert cond_bat.current_truth_state is True
    assert cond_loc.current_truth_state is False

    cands = await engine.list_candidates()
    assert len(cands) == 1
    assert cands[0]["condition_id"] == cond_bat.condition_id


@pytest.mark.asyncio
async def test_capabilities():
    """Verify internal proactive capabilities (register, evaluate, list, get, dismiss)."""
    engine = ProactiveEngine.get_instance()
    wsm = WorldStateManager.get_instance()

    # 1. Register
    cond = await engine.register_condition(
        signal_key="device.battery:asus",
        operator="less_than",
        target_value=40
    )
    assert cond.condition_id.startswith("cond_")

    # 2. Trigger
    await wsm.ingest_signal(DeviceSignalAdapter.create_battery_signal("asus", 35, False))

    # 3. List candidates
    cands = await engine.list_candidates()
    assert len(cands) == 1
    ev_id = cands[0]["event_id"]

    # 4. Get event
    ev = await engine.get_event(ev_id)
    assert ev is not None
    assert ev["event_id"] == ev_id

    # 5. Dismiss
    ok = await engine.dismiss(ev_id)
    assert ok is True
    ev_after = await engine.get_event(ev_id)
    assert ev_after["status"] == "dismissed"
    assert len(await engine.list_candidates()) == 0


@pytest.mark.asyncio
async def test_natural_language_registration():
    """Verify natural-language requests register structured conditions through AgentHarness."""
    # Enroll test device
    dev_id = "asus"
    await database.enroll_device(dev_id, "ASUS Laptop", "admin", "hash123")

    connected_map = {dev_id: None}
    control_layer = CortexControlLayer(connected_map)
    harness = AgentHarness(control_layer=control_layer)
    engine = ProactiveEngine.get_instance()

    # 1. "Remind me when my laptop comes online."
    res1 = await harness.process_request(
        source_device_id=dev_id,
        natural_request="Remind me when my laptop comes online.",
        context={"device": {"id": dev_id, "capabilities": ["proactive.register_condition"]}},
        log_action_callback=None
    )
    assert res1.status == "success"
    assert "laptop comes online" in res1.message.lower()

    # 2. "Remind me when I'm home after 5 PM."
    res2 = await harness.process_request(
        source_device_id=dev_id,
        natural_request="Remind me when I'm home after 5 PM.",
        context={"device": {"id": dev_id, "capabilities": ["proactive.register_condition"]}},
        log_action_callback=None
    )
    assert res2.status == "success"
    assert "home after" in res2.message.lower()

    # Check registered conditions in ProactiveEngine
    conds = await engine.list_candidates()
    assert len(engine._conditions) >= 2


@pytest.mark.asyncio
async def test_required_real_end_to_end_scenario():
    """
    Required Phase 8B End-to-End Test:
    User: "Let me know when my battery drops below 95 percent."
    Cortex: registers condition
    World State signal crossing:
    100 -> 94: exactly one proactive candidate event is created
    93: no duplicate event
    97: condition resets
    94 again: a new event is created
    """
    dev_id = "asus"
    await database.enroll_device(dev_id, "ASUS Laptop", "admin", "hash123")
    connected_map = {dev_id: None}
    control_layer = CortexControlLayer(connected_map)
    harness = AgentHarness(control_layer=control_layer)

    wsm = WorldStateManager.get_instance()
    wsm.set_active_device(dev_id)
    engine = ProactiveEngine.get_instance()

    # 1. User says: "Let me know when my battery drops below 95 percent."
    res = await harness.process_request(
        source_device_id=dev_id,
        natural_request="Let me know when my battery drops below 95 percent.",
        context={"device": {"id": dev_id, "capabilities": ["proactive.register_condition"]}},
        log_action_callback=None
    )
    assert res.status == "success"
    assert "battery drops below 95%" in res.message.lower() or "95" in res.message

    # Verify condition registered
    matching_conds = [c for c in engine._conditions.values() if "battery" in c.signal_key]
    assert len(matching_conds) >= 1
    cond = matching_conds[0]
    assert cond.target_value == 95
    assert cond.operator == "less_than"
    assert cond.current_truth_state is False

    # 2. Update World State battery signal: 100%
    sig_100 = DeviceSignalAdapter.create_battery_signal(dev_id, 100, False)
    await wsm.ingest_signal(sig_100)
    cands_100 = await engine.list_candidates()
    assert len(cands_100) == 0
    assert cond.current_truth_state is False

    # 3. Update battery signal crossing: 100 -> 94%
    sig_94 = DeviceSignalAdapter.create_battery_signal(dev_id, 94, False)
    await wsm.ingest_signal(sig_94)

    # Expected: exactly one proactive candidate event is created
    cands_94 = await engine.list_candidates()
    assert len(cands_94) == 1
    ev1 = cands_94[0]
    assert ev1["status"] == "candidate"
    assert ev1["trigger_condition"]["actual_value"] == 94
    assert cond.current_truth_state is True

    # 4. Send another 93 signal
    # Expected: no duplicate event
    sig_93 = DeviceSignalAdapter.create_battery_signal(dev_id, 93, False)
    await wsm.ingest_signal(sig_93)

    cands_93 = await engine.list_candidates()
    assert len(cands_93) == 1  # Exactly 1 event, deduplicated!
    assert cands_93[0]["event_id"] == ev1["event_id"]

    # 5. Send 97 signal
    # Expected: condition resets
    sig_97 = DeviceSignalAdapter.create_battery_signal(dev_id, 97, False)
    await wsm.ingest_signal(sig_97)

    assert cond.current_truth_state is False
    cands_97 = await engine.list_candidates()
    assert len(cands_97) == 1  # Still 1 candidate in history, reset occurred

    # 6. Send 94 signal again
    # Expected: a new event is created
    sig_94_again = DeviceSignalAdapter.create_battery_signal(dev_id, 94, False)
    await wsm.ingest_signal(sig_94_again)

    assert cond.current_truth_state is True
    cands_retrigger = await engine.list_candidates()
    assert len(cands_retrigger) == 2  # New event created!
    ev2 = cands_retrigger[0]
    assert ev2["event_id"] != ev1["event_id"]


@pytest.mark.asyncio
async def test_db_safety():
    """Verify that test operations strictly operated on isolated scratch DB."""
    from hub.database import get_db_path
    resolved_test_db = get_db_path().resolve()
    prod_path = Path("data/cortex.db").resolve()
    assert resolved_test_db != prod_path, f"FATAL: Test targeting prod DB at {resolved_test_db}!"
    assert not str(resolved_test_db).replace('\\', '/').endswith("data/cortex.db")
    assert prod_path.exists()
