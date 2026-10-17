"""
tests/test_phase8c_attention.py
-------------------------------
Deterministic test suite for Cortex Phase 8C:
Attention Router + Delivery Decision Foundation.

Tests:
1. Active device preferred
2. Offline device excluded
3. Fallback device selection
4. Unsupported channel excluded
5. Busy state causes defer
6. Urgent event can override defer threshold
7. No eligible devices -> queued
8. Reevaluation on context change
9. Deterministic scoring
10. Persistence across restart
11. Expired events not delivered
12. No duplicate decisions
13. Phase 8B event integration
14. Attention capabilities execution
15. Required real end-to-end scenario (NL -> 95% -> battery 94 -> router -> offline -> online reevaluate)
16. DB safety
"""

import pytest
import hashlib
import asyncio
from datetime import datetime, timezone, timedelta
from pathlib import Path

import hub.database as database
from hub.control_layer import CortexControlLayer
from hub.agent_harness import AgentHarness
from hub.world_state import Signal, WorldStateManager, DeviceSignalAdapter
from hub.proactive_engine import Condition, ProactiveEvent, ProactiveEngine
from hub.attention_router import (
    DeliveryDecision,
    AttentionRouter,
    score_device_channel,
)


@pytest.mark.asyncio
async def test_active_device_preferred():
    """Verify active interacting device receives higher score and is preferred."""
    router = AttentionRouter.get_instance()
    pe = ProactiveEngine.get_instance()
    wsm = WorldStateManager.get_instance()

    # Enroll two devices: asus (active laptop) and desktop
    await database.enroll_device("asus", "ASUS Laptop", "admin", "hash1")
    await database.enroll_device("desktop", "Workstation PC", "admin", "hash2")
    await database.save_device_capabilities("asus", ["ui.notify", "desktop_notification"])
    await database.save_device_capabilities("desktop", ["ui.notify", "desktop_notification"])

    # Set asus as the active interacting device
    wsm.set_active_device("asus")

    # Ingest heartbeat signals marking both online
    now = datetime.now(timezone.utc).isoformat()
    await wsm.ingest_signal(Signal(key="device.online:asus", source="test", timestamp=now, payload={"is_online": True}))
    await wsm.ingest_signal(Signal(key="device.online:desktop", source="test", timestamp=now, payload={"is_online": True}))

    # Create proactive event
    ev = ProactiveEvent(
        event_id="pe_test_pref_1",
        event_type="signal_threshold",
        condition_id="cond_test_1",
        priority=0.5,
        created_at=now
    )
    pe._events[ev.event_id] = ev

    decision = await router.route_event(ev)
    assert decision is not None
    assert decision.target_device_id == "asus"
    assert decision.timing == "now"
    assert decision.status == "ready"


@pytest.mark.asyncio
async def test_offline_device_excluded():
    """Verify offline devices receive score 0.0 and are excluded."""
    router = AttentionRouter.get_instance()
    pe = ProactiveEngine.get_instance()
    wsm = WorldStateManager.get_instance()

    await database.enroll_device("asus", "ASUS Laptop", "admin", "hash1")
    await database.enroll_device("phone", "User Phone", "admin", "hash2")
    await database.save_device_capabilities("asus", ["ui.notify", "desktop_notification"])
    await database.save_device_capabilities("phone", ["ui.notify", "desktop_notification"])

    wsm.set_active_device("phone")

    # Only phone is online; asus has is_online = False
    now = datetime.now(timezone.utc).isoformat()
    await wsm.ingest_signal(Signal(key="device.online:phone", source="test", timestamp=now, payload={"is_online": True}))
    await wsm.ingest_signal(Signal(key="device.online:asus", source="test", timestamp=now, payload={"is_online": False}))

    ev = ProactiveEvent(
        event_id="pe_test_off_1",
        event_type="signal_threshold",
        condition_id="cond_test_1",
        priority=0.6,
        created_at=now
    )
    pe._events[ev.event_id] = ev

    decision = await router.route_event(ev)
    assert decision is not None
    assert decision.target_device_id == "phone"
    assert decision.timing == "now"


@pytest.mark.asyncio
async def test_fallback_device_selection():
    """Verify fallback to next eligible online device when preferred device is offline."""
    router = AttentionRouter.get_instance()
    pe = ProactiveEngine.get_instance()
    wsm = WorldStateManager.get_instance()

    # asus is preferred (active), but goes offline
    await database.enroll_device("asus", "ASUS Laptop", "admin", "hash1")
    await database.enroll_device("desktop", "Workstation PC", "admin", "hash2")
    await database.save_device_capabilities("asus", ["ui.notify", "desktop_notification"])
    await database.save_device_capabilities("desktop", ["ui.notify", "desktop_notification"])

    wsm.set_active_device("asus")
    now = datetime.now(timezone.utc).isoformat()
    # asus offline, desktop online
    await wsm.ingest_signal(Signal(key="device.online:asus", source="test", timestamp=now, payload={"is_online": False}))
    await wsm.ingest_signal(Signal(key="device.online:desktop", source="test", timestamp=now, payload={"is_online": True}))

    ev = ProactiveEvent(
        event_id="pe_test_fallback_1",
        event_type="signal_threshold",
        condition_id="cond_test_1",
        priority=0.5,
        created_at=now
    )
    pe._events[ev.event_id] = ev

    decision = await router.route_event(ev)
    assert decision is not None
    assert decision.target_device_id == "desktop"


@pytest.mark.asyncio
async def test_unsupported_channel_excluded():
    """Verify channels not supported by device capability registry are excluded."""
    await database.enroll_device("screenless_dev", "Audio Speaker", "admin", "hash1")
    # Only supports voice, not desktop_notification
    await database.save_device_capabilities("screenless_dev", ["voice", "audio.output"])

    score_notif, rat_notif = score_device_channel(
        device_id="screenless_dev",
        channel="desktop_notification",
        event_priority=0.5,
        is_online=True,
        is_active_device=True,
        device_capabilities=["voice", "audio.output"]
    )
    assert score_notif == 0.0
    assert "does not support" in rat_notif

    score_voice, rat_voice = score_device_channel(
        device_id="screenless_dev",
        channel="voice",
        event_priority=0.5,
        is_online=True,
        is_active_device=True,
        device_capabilities=["voice", "audio.output"]
    )
    assert score_voice > 0.0


@pytest.mark.asyncio
async def test_busy_state_causes_defer():
    """Verify calendar busy state causes low-priority event to defer delivery."""
    router = AttentionRouter.get_instance()
    pe = ProactiveEngine.get_instance()
    wsm = WorldStateManager.get_instance()

    await database.enroll_device("asus", "ASUS Laptop", "admin", "hash1")
    await database.save_device_capabilities("asus", ["ui.notify", "desktop_notification"])
    wsm.set_active_device("asus")

    now = datetime.now(timezone.utc).isoformat()
    await wsm.ingest_signal(Signal(key="device.online:asus", source="test", timestamp=now, payload={"is_online": True}))

    # Ingest busy calendar signal
    await wsm.ingest_signal(Signal(
        key="calendar.current",
        source="calendar_adapter",
        timestamp=now,
        payload={"is_busy": True, "current_event": {"title": "Important Meeting"}}
    ))

    # Low priority proactive event (priority 0.4 < 0.75)
    ev = ProactiveEvent(
        event_id="pe_test_defer_1",
        event_type="signal_threshold",
        condition_id="cond_test_1",
        priority=0.4,
        created_at=now
    )
    pe._events[ev.event_id] = ev

    decision = await router.route_event(ev)
    assert decision is not None
    assert decision.timing == "deferred"
    assert decision.status == "deferred"
    assert "meeting" in decision.reason.lower() or "busy" in decision.reason.lower()


@pytest.mark.asyncio
async def test_urgent_event_can_override_defer_threshold():
    """Verify high-priority event (>= 0.75) overrides calendar busy deferral."""
    router = AttentionRouter.get_instance()
    pe = ProactiveEngine.get_instance()
    wsm = WorldStateManager.get_instance()

    await database.enroll_device("asus", "ASUS Laptop", "admin", "hash1")
    await database.save_device_capabilities("asus", ["ui.notify", "desktop_notification"])
    wsm.set_active_device("asus")

    now = datetime.now(timezone.utc).isoformat()
    await wsm.ingest_signal(Signal(key="device.online:asus", source="test", timestamp=now, payload={"is_online": True}))
    await wsm.ingest_signal(Signal(
        key="calendar.current",
        source="calendar_adapter",
        timestamp=now,
        payload={"is_busy": True, "current_event": {"title": "Executive Review"}}
    ))

    # Urgent proactive event (e.g. battery critical, priority 0.90)
    ev = ProactiveEvent(
        event_id="pe_test_urgent_1",
        event_type="signal_threshold",
        condition_id="cond_test_1",
        priority=0.90,
        created_at=now
    )
    pe._events[ev.event_id] = ev

    decision = await router.route_event(ev)
    assert decision is not None
    assert decision.timing == "now"
    assert decision.status == "ready"
    assert "urgent" in decision.reason.lower() or decision.score > 0.0


@pytest.mark.asyncio
async def test_no_eligible_devices_queued():
    """Verify that when no eligible devices are online, event is queued/deferred."""
    router = AttentionRouter.get_instance()
    pe = ProactiveEngine.get_instance()
    wsm = WorldStateManager.get_instance()

    await database.enroll_device("asus", "ASUS Laptop", "admin", "hash1")
    # All devices offline
    now = datetime.now(timezone.utc).isoformat()
    await wsm.ingest_signal(Signal(key="device.online:asus", source="test", timestamp=now, payload={"is_online": False}))

    ev = ProactiveEvent(
        event_id="pe_test_queued_1",
        event_type="signal_threshold",
        condition_id="cond_test_1",
        priority=0.5,
        created_at=now
    )
    pe._events[ev.event_id] = ev

    decision = await router.route_event(ev)
    assert decision is not None
    assert decision.status == "deferred"
    assert decision.channel == "queued"
    assert decision.target_device_id is None


@pytest.mark.asyncio
async def test_reevaluation_on_context_change():
    """Verify deferred decision reevaluates to ready when device comes online."""
    router = AttentionRouter.get_instance()
    pe = ProactiveEngine.get_instance()
    wsm = WorldStateManager.get_instance()

    await database.enroll_device("asus", "ASUS Laptop", "admin", "hash1")
    await database.save_device_capabilities("asus", ["ui.notify", "desktop_notification"])

    now = datetime.now(timezone.utc).isoformat()
    # 1. ASUS is initially offline
    await wsm.ingest_signal(Signal(key="device.online:asus", source="test", timestamp=now, payload={"is_online": False}))

    ev = ProactiveEvent(
        event_id="pe_test_reeval_1",
        event_type="signal_threshold",
        condition_id="cond_test_1",
        priority=0.5,
        created_at=now
    )
    pe._events[ev.event_id] = ev

    dec = await router.route_event(ev)
    assert dec.status == "deferred"

    # 2. ASUS comes online (triggers automatic reevaluation in WorldState)
    await wsm.ingest_signal(Signal(key="device.online:asus", source="test", timestamp=now, payload={"is_online": True}))

    # 3. Decision is now ready
    assert dec.status == "ready"
    assert dec.target_device_id == "asus"
    assert dec.timing == "now"


def test_deterministic_scoring():
    """Verify score_device_channel produces deterministic, bounded [0.0, 1.0] outputs."""
    score1, _ = score_device_channel("dev1", "desktop_notification", 0.5, True, True, ["ui.notify"])
    score2, _ = score_device_channel("dev1", "desktop_notification", 0.5, True, True, ["ui.notify"])
    assert score1 == score2
    assert 0.0 <= score1 <= 1.0

    # Bounds on extreme arguments
    score_min, _ = score_device_channel("dev1", "desktop_notification", 0.0, False, False, [])
    assert score_min == 0.0
    score_max, _ = score_device_channel("dev1", "desktop_notification", 1.0, True, True, ["ui.notify"])
    assert 0.0 <= score_max <= 1.0


@pytest.mark.asyncio
async def test_persistence_across_restart():
    """Verify DeliveryDecisions survive restart and rehydrate from DB."""
    router = AttentionRouter.get_instance()
    pe = ProactiveEngine.get_instance()
    wsm = WorldStateManager.get_instance()

    await database.enroll_device("asus", "ASUS Laptop", "admin", "hash1")
    await database.save_device_capabilities("asus", ["ui.notify", "desktop_notification"])
    wsm.set_active_device("asus")

    now = datetime.now(timezone.utc).isoformat()
    await wsm.ingest_signal(Signal(key="device.online:asus", source="test", timestamp=now, payload={"is_online": True}))

    ev = ProactiveEvent(
        event_id="pe_persist_1",
        event_type="signal_threshold",
        condition_id="cond_test_1",
        priority=0.5,
        created_at=now
    )
    pe._events[ev.event_id] = ev

    decision = await router.route_event(ev)
    dec_id = decision.decision_id

    # Simulate restart
    AttentionRouter.reset()
    new_router = AttentionRouter.get_instance()
    assert len(new_router._decisions) == 0

    await new_router.load_from_db()
    assert dec_id in new_router._decisions
    rehydrated = new_router._decisions[dec_id]
    assert rehydrated.target_device_id == "asus"
    assert rehydrated.proactive_event_id == "pe_persist_1"


@pytest.mark.asyncio
async def test_expired_events_not_delivered():
    """Verify that expired proactive events result in expired decision status."""
    router = AttentionRouter.get_instance()
    pe = ProactiveEngine.get_instance()

    past_time = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    ev = ProactiveEvent(
        event_id="pe_exp_1",
        event_type="signal_threshold",
        condition_id="cond_test_1",
        priority=0.5,
        expires_at=past_time
    )
    pe._events[ev.event_id] = ev

    decision = await router.route_event(ev)
    assert decision is not None
    assert decision.status == "expired"


@pytest.mark.asyncio
async def test_no_duplicate_decisions():
    """Verify routing the same proactive event multiple times returns the existing decision."""
    router = AttentionRouter.get_instance()
    pe = ProactiveEngine.get_instance()
    wsm = WorldStateManager.get_instance()

    await database.enroll_device("asus", "ASUS Laptop", "admin", "hash1")
    await database.save_device_capabilities("asus", ["ui.notify", "desktop_notification"])
    wsm.set_active_device("asus")

    now = datetime.now(timezone.utc).isoformat()
    await wsm.ingest_signal(Signal(key="device.online:asus", source="test", timestamp=now, payload={"is_online": True}))

    ev = ProactiveEvent(
        event_id="pe_no_dup_1",
        event_type="signal_threshold",
        condition_id="cond_test_1",
        priority=0.5,
        created_at=now
    )
    pe._events[ev.event_id] = ev

    dec1 = await router.route_event(ev)
    dec2 = await router.route_event(ev)

    assert dec1.decision_id == dec2.decision_id
    assert len(router._decisions) == 1


@pytest.mark.asyncio
async def test_phase8b_event_integration():
    """Verify Phase 8B proactive event automatically invokes AttentionRouter upon ingestion."""
    engine = ProactiveEngine.get_instance()
    router = AttentionRouter.get_instance()
    wsm = WorldStateManager.get_instance()

    await database.enroll_device("asus", "ASUS Laptop", "admin", "hash1")
    await database.save_device_capabilities("asus", ["ui.notify", "desktop_notification"])
    wsm.set_active_device("asus")

    # Register condition
    cond = await engine.register_condition(
        signal_key="device.battery:asus",
        operator="less_than",
        target_value=50,
        description="Battery below 50%"
    )

    # Ingest signal crossing 50% threshold
    await wsm.ingest_signal(DeviceSignalAdapter.create_battery_signal("asus", 40, False))

    # Check that candidate event was created AND routed to a DeliveryDecision
    cands = await engine.list_candidates()
    assert len(cands) == 1
    ev_id = cands[0]["event_id"]

    pending_decisions = await router.list_pending()
    assert len(pending_decisions) >= 1
    matched = [d for d in pending_decisions if d["proactive_event_id"] == ev_id]
    assert len(matched) == 1
    assert matched[0]["target_device_id"] == "asus"
    assert matched[0]["status"] == "ready"


@pytest.mark.asyncio
async def test_attention_capabilities():
    """Verify internal attention.* capabilities (route_event, get_decision, list_pending, reevaluate, cancel)."""
    router = AttentionRouter.get_instance()
    pe = ProactiveEngine.get_instance()
    wsm = WorldStateManager.get_instance()

    await database.enroll_device("asus", "ASUS Laptop", "admin", "hash1")
    await database.save_device_capabilities("asus", ["ui.notify", "desktop_notification"])
    wsm.set_active_device("asus")

    now = datetime.now(timezone.utc).isoformat()
    await wsm.ingest_signal(Signal(key="device.online:asus", source="test", timestamp=now, payload={"is_online": True}))

    ev = ProactiveEvent(
        event_id="pe_cap_test_1",
        event_type="signal_threshold",
        condition_id="cond_test_1",
        priority=0.5,
        created_at=now
    )
    pe._events[ev.event_id] = ev

    # 1. attention.route_event
    dec = await router.route_event(ev)
    assert dec is not None
    dec_id = dec.decision_id

    # 2. attention.get_decision
    fetched = await router.get_decision(dec_id)
    assert fetched is not None
    assert fetched["decision_id"] == dec_id

    # 3. attention.list_pending
    pending = await router.list_pending()
    assert len(pending) >= 1

    # 4. attention.reevaluate
    ready = await router.reevaluate()
    assert isinstance(ready, list)

    # 5. attention.cancel
    ok = await router.cancel(dec_id)
    assert ok is True
    cancelled = await router.get_decision(dec_id)
    assert cancelled["status"] == "cancelled"


@pytest.mark.asyncio
async def test_required_real_end_to_end_scenario():
    """
    REQUIRED END-TO-END TEST:
    User says: "Let me know when my battery drops below 95 percent."
    Battery: 100 -> 94
    Required chain:
    natural language -> proactive condition -> world state change -> proactive event -> attention router -> delivery decision.
    For ASUS active/online:
    target_device = asus, channel = desktop_notification, timing = now, status = ready
    Then test:
    asus offline -> expected: status = deferred/queued
    Then:
    asus online again -> expected: reevaluation -> ready delivery decision
    """
    dev_id = "asus"
    await database.enroll_device(dev_id, "ASUS Laptop", "admin", "hash123")
    await database.save_device_capabilities(dev_id, ["ui.notify", "desktop_notification", "proactive.register_condition"])

    connected_map = {dev_id: None}
    control_layer = CortexControlLayer(connected_map)
    harness = AgentHarness(control_layer=control_layer)

    wsm = WorldStateManager.get_instance()
    wsm.set_active_device(dev_id)
    pe = ProactiveEngine.get_instance()
    router = AttentionRouter.get_instance()

    # 1. Natural language condition registration
    res = await harness.process_request(
        source_device_id=dev_id,
        natural_request="Let me know when my battery drops below 95 percent.",
        context={"device": {"id": dev_id, "capabilities": ["proactive.register_condition", "ui.notify"]}},
        log_action_callback=None
    )
    assert res.status == "success"

    # Mark asus online initially
    now = datetime.now(timezone.utc).isoformat()
    await wsm.ingest_signal(Signal(key=f"device.online:{dev_id}", source="test", timestamp=now, payload={"is_online": True}))

    # 2. Ingest 100% battery (no event)
    await wsm.ingest_signal(DeviceSignalAdapter.create_battery_signal(dev_id, 100, False))
    assert len(await pe.list_candidates()) == 0

    # 3. Battery drops: 100 -> 94%
    # Triggers condition -> creates proactive event -> AttentionRouter automatically creates DeliveryDecision
    await wsm.ingest_signal(DeviceSignalAdapter.create_battery_signal(dev_id, 94, False))

    cands = await pe.list_candidates()
    assert len(cands) == 1
    ev1 = cands[0]

    dec1 = await router.route_event(ev1["event_id"])
    assert dec1 is not None
    assert dec1.target_device_id == dev_id
    assert dec1.channel == "desktop_notification"
    assert dec1.timing == "now"
    assert dec1.status == "ready"

    # 4. Test: ASUS goes offline
    # Ingest offline signal for ASUS
    await wsm.ingest_signal(Signal(key=f"device.online:{dev_id}", source="test", timestamp=now, payload={"is_online": False}))

    # Reset router decision for a new event to simulate routing when asus is offline
    ev_offline = ProactiveEvent(
        event_id="pe_battery_offline_test",
        event_type="signal_threshold",
        condition_id="cond_offline_test",
        priority=0.5,
        created_at=now
    )
    pe._events[ev_offline.event_id] = ev_offline

    dec_offline = await router.route_event(ev_offline)
    assert dec_offline is not None
    assert dec_offline.status == "deferred"
    assert dec_offline.channel == "queued"
    assert dec_offline.target_device_id is None

    # 5. Test: ASUS comes online again (triggers reevaluation in WorldState)
    await wsm.ingest_signal(Signal(key=f"device.online:{dev_id}", source="test", timestamp=now, payload={"is_online": True}))

    assert dec_offline.status == "ready"
    assert dec_offline.target_device_id == dev_id
    assert dec_offline.channel == "desktop_notification"


@pytest.mark.asyncio
async def test_db_safety():
    """Verify that test operations strictly operated on isolated scratch DB."""
    from hub.database import get_db_path
    resolved_test_db = get_db_path().resolve()
    prod_path = Path("data/cortex.db").resolve()
    assert resolved_test_db != prod_path, f"FATAL: Test targeting prod DB at {resolved_test_db}!"
    assert not str(resolved_test_db).replace('\\', '/').endswith("data/cortex.db")
    assert prod_path.exists()
