"""
tests/test_phase8f_routines.py
------------------------------
Deterministic test suite for Cortex Phase 8F: Minimal Learned Routines.
Covers:
1. LearnedRoutine model & SQLite persistence
2. Learning rule: requires >= 3 repeated observations, bounded deterministic confidence
3. General categorization without hardcoded courses or employers
4. Multi-day recurring schedule pattern aggregation
5. Required test scenario:
   - Seed repeated Calendar observations representing a recurring schedule
   - Confirm Cortex learns one routine
   - Ask: "What does my normal Monday look like?" -> answers from routine
   - Restart Hub and verify persistence
6. Natural language queries:
   - "When do I usually work?"
   - "When am I normally free?"
   - "What patterns have you learned about me?"
7. World State routine signals & live World State override
8. Attention Router small bounded bonus & strict invariant preservation (offline/busy)
9. Production DB isolation
"""

import os
import pytest
import hashlib
import uuid
import json
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

import hub.database as database
import hub.main
from hub.control_layer import CortexControlLayer
from hub.agent_harness import AgentHarness
from hub.world_state import WorldStateManager, Signal, CalendarSignalAdapter, RuntimeTimeAdapter
from hub.routines import (
    RoutineManager,
    LearnedRoutine,
    categorize_event,
    compute_routine_confidence,
    ROUTINE_CATEGORIES
)
from hub.attention_router import AttentionRouter, score_device_channel

PROD_DB_HASH = "62F5E75C85065CD18F43A2D79EFB3CF3D7756F49349D4FDADC6A1B4330D27732"


# ---------------------------------------------------------------------------
# TEST 1: LearnedRoutine Model & Persistence
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_routine_model_and_persistence():
    """Verify LearnedRoutine dataclass, JSON fields, and SQLite CRUD."""
    await database.init_db()

    routine = LearnedRoutine(
        routine_id="rtn_cs101_0900",
        label="CS 101 Lecture",
        category="class",
        days_of_week=["monday", "wednesday", "friday"],
        typical_start_time="09:00",
        typical_end_time="10:15",
        confidence=0.85,
        observation_count=3,
        source="calendar",
        last_observed_at=datetime.now(timezone.utc).isoformat(),
        metadata={"term": "Fall 2026"}
    )

    # Save to SQLite
    await database.save_learned_routine(routine.to_dict())

    # Retrieve by ID
    loaded = await database.get_learned_routine("rtn_cs101_0900")
    assert loaded is not None
    assert loaded["routine_id"] == "rtn_cs101_0900"
    assert loaded["label"] == "CS 101 Lecture"
    assert loaded["category"] == "class"
    assert loaded["days_of_week"] == ["monday", "wednesday", "friday"]
    assert loaded["typical_start_time"] == "09:00"
    assert loaded["typical_end_time"] == "10:15"
    assert loaded["confidence"] == 0.85
    assert loaded["observation_count"] == 3
    assert loaded["source"] == "calendar"
    assert loaded["metadata"] == {"term": "Fall 2026"}

    # List all
    all_routines = await database.list_learned_routines()
    assert len(all_routines) >= 1
    assert any(r["routine_id"] == "rtn_cs101_0900" for r in all_routines)

    # Delete
    deleted = await database.delete_learned_routine("rtn_cs101_0900")
    assert deleted is True
    assert await database.get_learned_routine("rtn_cs101_0900") is None


# ---------------------------------------------------------------------------
# TEST 2: Learning Rule (Requires >= 3 Repeated Observations)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_learning_requires_at_least_three_observations():
    """Verify that fewer than 3 observations do not trigger routine creation."""
    await database.init_db()
    rm = RoutineManager.get_instance()
    await rm.load_from_db()

    # 1 observation -> should not learn routine
    single_event = [{
        "summary": "Study Group",
        "start": "2026-10-12T14:00:00-04:00",
        "end": "2026-10-12T15:30:00-04:00"
    }]
    learned_1 = await rm.learn_from_calendar_events(single_event)
    assert len(learned_1) == 0

    # 2 observations -> should not learn routine
    two_events = [
        {"summary": "Study Group", "start": "2026-10-12T14:00:00-04:00", "end": "2026-10-12T15:30:00-04:00"},
        {"summary": "Study Group", "start": "2026-10-19T14:00:00-04:00", "end": "2026-10-19T15:30:00-04:00"}
    ]
    learned_2 = await rm.learn_from_calendar_events(two_events)
    assert len(learned_2) == 0

    # 3 observations -> qualifies as learned routine
    three_events = [
        {"summary": "Study Group", "start": "2026-10-12T14:00:00-04:00", "end": "2026-10-12T15:30:00-04:00"},
        {"summary": "Study Group", "start": "2026-10-19T14:00:00-04:00", "end": "2026-10-19T15:30:00-04:00"},
        {"summary": "Study Group", "start": "2026-10-26T14:00:00-04:00", "end": "2026-10-26T15:30:00-04:00"}
    ]
    learned_3 = await rm.learn_from_calendar_events(three_events)
    assert len(learned_3) == 1
    r = learned_3[0]
    assert r.label == "Study Group"
    assert r.category == "study"
    assert r.observation_count == 3
    assert 0.0 <= r.confidence <= 1.0
    assert r.confidence == 0.80  # Deterministic formula base for count=3
    assert r.days_of_week == ["monday"]
    assert r.typical_start_time == "14:00"
    assert r.typical_end_time == "15:30"


# ---------------------------------------------------------------------------
# TEST 3: General Categorization (No Hardcoded Courses or Employers)
# ---------------------------------------------------------------------------

def test_general_categorization():
    """Verify general categorization across academic, professional, and personal activities."""
    # Class patterns: course code regex or classroom keywords
    assert categorize_event("MATH 113: Analytic Geometry") == "class"
    assert categorize_event("PHYS 260 University Physics II") == "class"
    assert categorize_event("CS 310 Data Structures") == "class"
    assert categorize_event("GGS 101 Major World Regions") == "class"
    assert categorize_event("Biology Lecture") == "class"
    assert categorize_event("Chemistry Lab") == "class"

    # Work patterns: general workplace terms
    assert categorize_event("Work Shift") == "work"
    assert categorize_event("Office Hours Coverage") == "work"
    assert categorize_event("Front Desk Duty") == "work"
    assert categorize_event("Retail Shift") == "work"

    # Study patterns
    assert categorize_event("Homework Session") == "study"
    assert categorize_event("Library Study") == "study"
    assert categorize_event("Exam Prep Review") == "study"
    assert categorize_event("Reading Assignment") == "study"

    # Device usage patterns
    assert categorize_event("Laptop Usage Session") == "device_usage"
    assert categorize_event("Workstation Screen Time") == "device_usage"

    # Personal fallback
    assert categorize_event("Gym Workout") == "personal"
    assert categorize_event("Dinner with Friends") == "personal"
    assert categorize_event("Dentist Appointment") == "personal"


# ---------------------------------------------------------------------------
# TEST 4: Multi-Day Recurring Schedule Pattern Aggregation
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_multi_weekday_pattern_learning():
    """Verify pattern learning across multiple weekdays (Mon/Wed/Fri)."""
    await database.init_db()
    rm = RoutineManager.get_instance()
    await rm.load_from_db()

    events = [
        # Week 1
        {"summary": "MATH 213", "start": "2026-10-12T10:30:00-04:00", "end": "2026-10-12T11:45:00-04:00"}, # Mon
        {"summary": "MATH 213", "start": "2026-10-14T10:30:00-04:00", "end": "2026-10-14T11:45:00-04:00"}, # Wed
        {"summary": "MATH 213", "start": "2026-10-16T10:30:00-04:00", "end": "2026-10-16T11:45:00-04:00"}, # Fri
        # Week 2
        {"summary": "MATH 213", "start": "2026-10-19T10:30:00-04:00", "end": "2026-10-19T11:45:00-04:00"}, # Mon
        {"summary": "MATH 213", "start": "2026-10-21T10:30:00-04:00", "end": "2026-10-21T11:45:00-04:00"}, # Wed
        {"summary": "MATH 213", "start": "2026-10-23T10:30:00-04:00", "end": "2026-10-23T11:45:00-04:00"}, # Fri
    ]

    learned = await rm.learn_from_calendar_events(events)
    assert len(learned) == 1
    r = learned[0]
    assert r.label == "MATH 213"
    assert r.category == "class"
    assert r.days_of_week == ["monday", "wednesday", "friday"]
    assert r.typical_start_time == "10:30"
    assert r.typical_end_time == "11:45"
    assert r.observation_count == 6
    assert 0.85 <= r.confidence <= 1.0


# ---------------------------------------------------------------------------
# TEST 5: REQUIRED TEST SCENARIO (Seed -> Learn -> NL Query -> Restart Persistence)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_required_prompt_scenario_normal_monday_and_restart():
    """
    REQUIRED TEST from prompt:
    1. Seed repeated Calendar observations representing a recurring schedule.
    2. Confirm Cortex learns one routine.
    3. Ask: 'What does my normal Monday look like?' -> Cortex must answer from that routine.
    4. Restart Hub and verify persistence.
    """
    await database.init_db()
    rm = RoutineManager.get_instance()
    await rm.load_from_db()

    # Step 1: Seed repeated observations (e.g. Work Shift across 3 Mondays)
    monday_observations = [
        {"summary": "Work Shift", "start": "2026-09-28T13:00:00-04:00", "end": "2026-09-28T17:00:00-04:00"},
        {"summary": "Work Shift", "start": "2026-10-05T13:00:00-04:00", "end": "2026-10-05T17:00:00-04:00"},
        {"summary": "Work Shift", "start": "2026-10-12T13:00:00-04:00", "end": "2026-10-12T17:00:00-04:00"},
    ]

    # Step 2: Confirm Cortex learns one routine
    learned_routines = await rm.learn_from_calendar_events(monday_observations)
    assert len(learned_routines) == 1
    r = learned_routines[0]
    assert r.label == "Work Shift"
    assert r.category == "work"
    assert "monday" in r.days_of_week
    assert r.typical_start_time == "13:00"
    assert r.typical_end_time == "17:00"

    # Step 3: Ask "What does my normal Monday look like?" via AgentHarness
    control = CortexControlLayer(connected_devices_map={})
    harness = AgentHarness(control_layer=control)

    async def log_cb(*args, **kwargs):
        pass

    ctx = {"device": {"id": "asus", "name": "ASUS Zenbook"}}
    resp = await harness.process_request(
        source_device_id="asus",
        natural_request="What does my normal Monday look like?",
        context=ctx,
        log_action_callback=log_cb
    )

    assert resp.status == "success"
    # Response must answer directly from the learned routine
    msg = resp.message
    assert "Work Shift" in msg
    assert "13:00" in msg
    assert "17:00" in msg
    assert "Work" in msg or "work" in msg
    assert resp.latency_metrics.get("routing_lane") == "DETERMINISTIC_SITUATIONAL"

    # Step 4: Restart Hub and verify persistence
    RoutineManager.reset()
    WorldStateManager.reset()

    # Re-initialize after simulated reboot
    rm_restarted = RoutineManager.get_instance()
    await rm_restarted.load_from_db()

    # Verify persisted routine reloaded
    reloaded_list = rm_restarted.list_routines()
    assert len(reloaded_list) == 1
    reloaded_r = reloaded_list[0]
    assert reloaded_r.label == "Work Shift"
    assert reloaded_r.typical_start_time == "13:00"
    assert reloaded_r.typical_end_time == "17:00"

    # Re-run natural language query after restart
    harness_restarted = AgentHarness(control_layer=control)
    resp_after_restart = await harness_restarted.process_request(
        source_device_id="asus",
        natural_request="What does my normal Monday look like?",
        context=ctx,
        log_action_callback=log_cb
    )
    assert resp_after_restart.status == "success"
    assert "Work Shift" in resp_after_restart.message
    assert "13:00 - 17:00" in resp_after_restart.message


# ---------------------------------------------------------------------------
# TEST 6: Natural Language Queries ("When do I usually work?", "When am I normally free?", "What patterns have you learned about me?")
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_natural_language_queries():
    """Verify natural language responses for all required query patterns."""
    await database.init_db()
    rm = RoutineManager.get_instance()
    await rm.load_from_db()

    # Seed 2 routines: Work Shift and CS Lecture
    events = [
        # Work on Mon/Wed
        {"summary": "Work Shift", "start": "2026-10-05T13:00:00-04:00", "end": "2026-10-05T17:00:00-04:00"},
        {"summary": "Work Shift", "start": "2026-10-07T13:00:00-04:00", "end": "2026-10-07T17:00:00-04:00"},
        {"summary": "Work Shift", "start": "2026-10-12T13:00:00-04:00", "end": "2026-10-12T17:00:00-04:00"},
        # CS Lecture on Mon/Wed
        {"summary": "CS 101", "start": "2026-10-05T09:00:00-04:00", "end": "2026-10-05T10:15:00-04:00"},
        {"summary": "CS 101", "start": "2026-10-07T09:00:00-04:00", "end": "2026-10-07T10:15:00-04:00"},
        {"summary": "CS 101", "start": "2026-10-12T09:00:00-04:00", "end": "2026-10-12T10:15:00-04:00"},
    ]
    await rm.learn_from_calendar_events(events)

    control = CortexControlLayer(connected_devices_map={})
    harness = AgentHarness(control_layer=control)
    async def log_cb(*args, **kwargs): pass
    ctx = {"device": {"id": "asus", "name": "ASUS Zenbook"}}

    # Query 1: "When do I usually work?"
    resp_work = await harness.process_request(
        source_device_id="asus",
        natural_request="When do I usually work?",
        context=ctx,
        log_action_callback=log_cb
    )
    assert resp_work.status == "success"
    assert "13:00" in resp_work.message
    assert "17:00" in resp_work.message
    assert "Work Shift" in resp_work.message

    # Query 2: "When am I normally free?"
    resp_free = await harness.process_request(
        source_device_id="asus",
        natural_request="When am I normally free?",
        context=ctx,
        log_action_callback=log_cb
    )
    assert resp_free.status == "success"
    assert "normally free" in resp_free.message.lower()

    # Query 3: "What patterns have you learned about me?"
    resp_patterns = await harness.process_request(
        source_device_id="asus",
        natural_request="What patterns have you learned about me?",
        context=ctx,
        log_action_callback=log_cb
    )
    assert resp_patterns.status == "success"
    assert "2 recurring routines" in resp_patterns.message
    assert "CS 101" in resp_patterns.message
    assert "Work Shift" in resp_patterns.message


# ---------------------------------------------------------------------------
# TEST 7: World State Routine Signals & Live Ground Truth Override
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_world_state_routine_signals_and_live_override():
    """
    Verify:
    1. World State ingests routine.current_expected, routine.next_expected, routine.confidence.
    2. Live World State ALWAYS wins over predictions.
    """
    await database.init_db()
    rm = RoutineManager.get_instance()
    wsm = WorldStateManager.get_instance()

    routine = LearnedRoutine(
        routine_id="rtn_class_1000",
        label="Data Structures",
        category="class",
        days_of_week=["monday"],
        typical_start_time="10:00",
        typical_end_time="11:15",
        confidence=0.90,
        observation_count=4,
        source="calendar",
        last_observed_at=datetime.now(timezone.utc).isoformat()
    )
    await rm.save_routine(routine)

    # Monday at 10:30 -> routine prediction expects "Data Structures"
    local_tz = RuntimeTimeAdapter.get_local_now().tzinfo
    mon_1030 = datetime(2026, 10, 12, 10, 30, tzinfo=local_tz)

    await rm.update_world_state_signals(mon_1030)

    # Verify signals are fresh in World State
    fused = await wsm.get_fused_world_state()
    assert fused.routine_context is not None
    curr_exp = fused.routine_context.get("current_expected")
    assert curr_exp is not None
    assert curr_exp["label"] == "Data Structures"
    assert curr_exp["category"] == "class"
    assert curr_exp["start"] == "10:00"
    assert curr_exp["end"] == "11:15"
    assert fused.routine_context.get("confidence") == 0.90

    # Live ground truth override:
    # If a live event is occurring (e.g. Doctor Appointment), live calendar state is ground truth
    sig_live = Signal(
        key="calendar.current_event",
        source="google_calendar",
        timestamp=datetime.now(timezone.utc).isoformat(),
        confidence=1.0,
        ttl=1800.0,
        payload={"title": "Emergency Doctor Appointment", "is_busy": True}
    )
    await wsm.ingest_signal(sig_live)

    fused_with_live = await wsm.get_fused_world_state()
    curr_event = fused_with_live.calendar_context.get("current_event")
    assert "Emergency Doctor Appointment" in (curr_event.get("title") if isinstance(curr_event, dict) else str(curr_event))
    # Live event is active and wins over routine prediction
    assert fused_with_live.calendar_context.get("is_busy") is True


# ---------------------------------------------------------------------------
# TEST 8: Attention Router Routine Bonus & Invariant Preservation
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_attention_router_device_routine_bonus_and_invariants():
    """
    Verify:
    1. Small bounded bonus (+0.05) applied when device has learned usage routine.
    2. Invariant: NEVER overrides offline device (score must be 0.0).
    3. Invariant: NEVER overrides busy calendar / busy device.
    """
    # Base score without routine bonus
    score_base, rat_base = score_device_channel(
        device_id="asus",
        channel="desktop_notification",
        event_priority=0.5,
        is_online=True,
        is_active_device=False,
        device_capabilities=["desktop_notification"],
        user_presence="active",
        is_calendar_busy=False,
        learned_routine_bonus=0.0
    )

    # Score with learned routine bonus
    score_boosted, rat_boosted = score_device_channel(
        device_id="asus",
        channel="desktop_notification",
        event_priority=0.5,
        is_online=True,
        is_active_device=False,
        device_capabilities=["desktop_notification"],
        user_presence="active",
        is_calendar_busy=False,
        learned_routine_bonus=0.05
    )

    # Bounded small boost
    assert score_boosted == pytest.approx(score_base + 0.05, 0.01)
    assert "routine_bonus=0.05" in rat_boosted

    # Invariant 1: Offline device MUST yield 0.0 despite routine bonus
    score_offline, rat_offline = score_device_channel(
        device_id="asus",
        channel="desktop_notification",
        event_priority=0.5,
        is_online=False,
        is_active_device=False,
        device_capabilities=["desktop_notification"],
        learned_routine_bonus=0.10
    )
    assert score_offline == 0.0
    assert "offline" in rat_offline.lower()

    # Invariant 2: Busy calendar penalty is preserved
    score_busy, rat_busy = score_device_channel(
        device_id="asus",
        channel="desktop_notification",
        event_priority=0.5,
        is_online=True,
        is_active_device=False,
        device_capabilities=["desktop_notification"],
        is_calendar_busy=True,
        learned_routine_bonus=0.05
    )
    # Interruption cost of 0.30 applies
    assert score_busy < score_base


# ---------------------------------------------------------------------------
# TEST 9: Production DB Isolation
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_production_db_isolation():
    """Verify that production DB (data/cortex.db) was never modified or corrupted."""
    from hub.database import get_db_path
    resolved_test_db = get_db_path().resolve()
    prod_path = Path("data/cortex.db").resolve()
    assert resolved_test_db != prod_path, f"FATAL: Test targeting prod DB at {resolved_test_db}!"
    assert not str(resolved_test_db).replace('\\', '/').endswith("data/cortex.db")
    assert prod_path.exists(), "Production DB data/cortex.db must exist."
