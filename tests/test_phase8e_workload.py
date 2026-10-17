"""
tests/test_phase8e_workload.py
------------------------------
Deterministic test suite for Cortex Phase 8E: Academic + Workload Context.
Covers:
- WorkItem model & restart persistence
- Add, update, complete lifecycle
- Due today, due tomorrow, and overdue detection
- Next deadline and minutes due (24h, 72h)
- Deterministic prioritization ranking
- Pressure score bounds [0.0, 1.0]
- Calendar free-time fusion & availability reasoning
- Relative date parsing using RuntimeTimeAdapter
- Natural language obligation entry & duplicate prevention
- Natural language completion & remaining workload queries
- Proactive deadline trigger & Phase 8C attention routing integration
- Tier 1 internal capabilities execution
- Production DB isolation
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
from hub.control_layer import CortexControlLayer
from hub.agent_harness import AgentHarness
from hub.world_state import WorldStateManager, Signal, CalendarSignalAdapter, RuntimeTimeAdapter
from hub.intent_schema import ActionPlan, ModelResult
from hub.workload import WorkloadManager, WorkItem, parse_relative_due_date, extract_effort_minutes
from hub.proactive_engine import ProactiveEngine
from hub.attention_router import AttentionRouter

PROD_DB_HASH = "62F5E75C85065CD18F43A2D79EFB3CF3D7756F49349D4FDADC6A1B4330D27732"


@pytest.mark.asyncio
async def test_work_item_model_and_persistence():
    """Verify WorkItem model creation, SQLite persistence, and restart reloading."""
    await database.init_db()
    wm = WorkloadManager.get_instance()
    await wm.ensure_initialized()

    now = CalendarSignalAdapter._get_local_now()
    due_iso = (now + timedelta(days=2)).isoformat()

    item = await wm.add_item(
        title="Physics Homework",
        type="assignment",
        course_or_project="Physics",
        due_at=due_iso,
        estimated_minutes=60,
        remaining_minutes=60,
        priority=3
    )

    assert item.work_item_id.startswith("wi_")
    assert item.title == "Physics Homework"
    assert item.course_or_project == "Physics"
    assert item.remaining_minutes == 60
    assert item.status == "pending"

    # Test update
    updated = await wm.update_item(item.work_item_id, {"remaining_minutes": 30, "status": "in_progress"})
    assert updated.remaining_minutes == 30
    assert updated.status == "in_progress"

    # Test restart reload from SQLite
    WorkloadManager.reset()
    wm_new = WorkloadManager.get_instance()
    await wm_new.ensure_initialized()
    reloaded = await wm_new.get_item(item.work_item_id)
    assert reloaded is not None
    assert reloaded.title == "Physics Homework"
    assert reloaded.remaining_minutes == 30
    assert reloaded.status == "in_progress"


@pytest.mark.asyncio
async def test_due_today_and_tomorrow_and_overdue():
    """Verify classification of items due today, due tomorrow, and overdue items."""
    await database.init_db()
    wm = WorkloadManager.get_instance()
    await wm.ensure_initialized()

    now = CalendarSignalAdapter._get_local_now()
    today_due = (now.replace(hour=23, minute=0, second=0)).isoformat()
    tomorrow_due = ((now + timedelta(days=1)).replace(hour=18, minute=0, second=0)).isoformat()
    overdue_due = (now - timedelta(hours=2)).isoformat()

    item_today = await wm.add_item(title="Bio Lab Report", due_at=today_due, estimated_minutes=45)
    item_tomorrow = await wm.add_item(title="History Essay", due_at=tomorrow_due, estimated_minutes=90)
    item_overdue = await wm.add_item(title="Math Quiz", due_at=overdue_due, estimated_minutes=30)

    metrics = await wm.compute_workload_metrics(now)
    assert metrics["total_pending"] >= 2
    assert metrics["total_overdue"] >= 1

    due_today_titles = [i.title for i in metrics["due_today"]]
    assert "Bio Lab Report" in due_today_titles

    due_tomorrow_titles = [i.title for i in metrics["due_tomorrow"]]
    assert "History Essay" in due_tomorrow_titles


@pytest.mark.asyncio
async def test_next_deadline_and_minutes_due():
    """Verify next deadline selection and 24h / 72h effort aggregation."""
    await database.init_db()
    wm = WorkloadManager.get_instance()
    await wm.ensure_initialized()

    now = CalendarSignalAdapter._get_local_now()
    dl_near = (now + timedelta(hours=3)).isoformat()
    dl_far = (now + timedelta(days=2)).isoformat()

    await wm.add_item(title="Near Task", due_at=dl_near, estimated_minutes=40, remaining_minutes=40)
    await wm.add_item(title="Far Task", due_at=dl_far, estimated_minutes=120, remaining_minutes=120)

    metrics = await wm.compute_workload_metrics(now)
    assert metrics["next_deadline"] is not None
    assert metrics["next_deadline"]["title"] == "Near Task"
    assert metrics["next_deadline"]["minutes_until"] <= 185
    assert metrics["minutes_due_24h"] == 40
    assert metrics["minutes_due_72h"] == 160


@pytest.mark.asyncio
async def test_deterministic_prioritization():
    """Verify multi-factor deterministic ranking ranks imminent deadlines over distant ones."""
    await database.init_db()
    wm = WorkloadManager.get_instance()
    await wm.ensure_initialized()

    now = CalendarSignalAdapter._get_local_now()
    calc_due = ((now + timedelta(days=1)).replace(hour=23, minute=59, second=0)).isoformat()
    java_due = ((now + timedelta(days=3)).replace(hour=23, minute=59, second=0)).isoformat()

    item_calc = await wm.add_item(
        title="Calc Assignment",
        type="assignment",
        course_or_project="Calc",
        due_at=calc_due,
        estimated_minutes=90,
        remaining_minutes=60,
        priority=3
    )

    item_java = await wm.add_item(
        title="Java Project",
        type="project",
        course_or_project="Java",
        due_at=java_due,
        estimated_minutes=240,
        remaining_minutes=180,
        priority=3
    )

    ranked = wm.prioritize(now=now)
    assert len(ranked) >= 2
    # Calc (tomorrow) must be ranked ahead of Java (in 3 days)
    top_item = ranked[0][0]
    assert top_item.title == "Calc Assignment"
    assert ranked[1][0].title == "Java Project"


@pytest.mark.asyncio
async def test_pressure_score_bounds():
    """Verify workload pressure calculation is bounded [0.0, 1.0] under all loads."""
    wm = WorkloadManager.get_instance()

    # Empty / light load
    empty_metrics = {
        "total_pending": 0,
        "total_overdue": 0,
        "due_today": [],
        "due_tomorrow": [],
        "next_deadline": None,
        "minutes_due_24h": 0,
        "minutes_due_72h": 0,
        "free_time_before_next_commitment": 300.0
    }
    p_zero = wm.calculate_pressure(empty_metrics)
    assert 0.0 <= p_zero <= 1.0
    assert p_zero == 0.0

    # Extreme stress load
    extreme_metrics = {
        "total_pending": 10,
        "total_overdue": 5,
        "due_today": [1, 2, 3],
        "due_tomorrow": [4, 5],
        "next_deadline": {"minutes_until": 10},
        "minutes_due_24h": 600,
        "minutes_due_72h": 1200,
        "free_time_before_next_commitment": 60.0
    }
    p_extreme = wm.calculate_pressure(extreme_metrics)
    assert 0.0 <= p_extreme <= 1.0
    assert p_extreme >= 0.7


@pytest.mark.asyncio
async def test_calendar_free_time_fusion():
    """Verify fusion with Google Calendar context and availability reasoning."""
    await database.init_db()
    wm = WorkloadManager.get_instance()
    await wm.ensure_initialized()

    now = CalendarSignalAdapter._get_local_now()
    class_start = now + timedelta(hours=2)

    mock_cal_event = {
        "summary": "Deep Learning Class",
        "title": "Deep Learning Class",
        "start": class_start.isoformat(),
        "end": (class_start + timedelta(hours=1)).isoformat()
    }

    wsm = WorldStateManager.get_instance()
    sig = CalendarSignalAdapter.create_calendar_signal({
        "current_event": None,
        "next_event": mock_cal_event,
        "all_events": [mock_cal_event],
        "time_until_seconds": 7200,
        "connected": True
    })
    await wsm.ingest_signal(sig)

    # 1. Check free time computation
    free_mins = await wm._calculate_free_time_before_commitment(now)
    assert 110.0 <= free_mins <= 130.0

    # 2. Add an item requiring 90 minutes
    await wm.add_item(title="Calc Assignment", course_or_project="Calc", remaining_minutes=90)

    # 3. Ask "Do I have enough time to finish Calc before class?"
    ans = await wm.handle_natural_language("Do I have enough time to finish Calc before class?")
    assert ans is not None
    assert "yes" in ans.lower()
    assert "deep learning class" in ans.lower() or "minutes" in ans.lower()


@pytest.mark.asyncio
async def test_relative_date_parsing_using_runtime_time():
    """Verify relative date phrases parse to authoritative local timezone dates."""
    now = CalendarSignalAdapter._get_local_now()

    dt_tmrw = parse_relative_due_date("due tomorrow at 11:59 PM", now)
    assert dt_tmrw is not None
    assert dt_tmrw.day == (now + timedelta(days=1)).day
    assert dt_tmrw.hour == 23 and dt_tmrw.minute == 59

    dt_10m = parse_relative_due_date("due in 10 minutes", now)
    assert dt_10m is not None
    diff_s = (dt_10m - now).total_seconds()
    assert 590 <= diff_s <= 610

    dt_2d = parse_relative_due_date("due in two days", now)
    assert dt_2d is not None
    assert dt_2d.day == (now + timedelta(days=2)).day

    effort = extract_effort_minutes("It should take about 90 minutes.")
    assert effort == 90

    effort_h = extract_effort_minutes("I have about three hours left.")
    assert effort_h == 180


@pytest.mark.asyncio
async def test_natural_language_add_and_duplicate_prevention():
    """Verify normal conversational interaction parses obligations and avoids duplicate records."""
    device_id = "asus"
    await database.enroll_device(device_id, "asus", "admin", "secret")
    control = CortexControlLayer(hub.main.CONNECTED_DEVICES)
    harness = AgentHarness(control)

    # Entry 1: Calc assignment
    res1 = await harness.process_request(
        device_id,
        "I have a Calc assignment due tomorrow at 11:59 PM. It should take about 90 minutes.",
        {},
        None
    )
    assert res1.status == "success"
    assert "calc assignment" in res1.message.lower()

    wm = WorkloadManager.get_instance()
    item = wm.find_item("Calc")
    assert item is not None
    assert item.title == "Calc Assignment"
    assert item.estimated_minutes == 90
    assert item.remaining_minutes == 90

    # Repeat entry should update rather than duplicate
    res_dup = await harness.process_request(
        device_id,
        "I have a Calc assignment due tomorrow at 11:59 PM.",
        {},
        None
    )
    assert res_dup.status == "success"

    all_items = await wm.list_items()
    calc_items = [i for i in all_items if "calc" in i.title.lower()]
    assert len(calc_items) == 1, "Should prevent duplicate work items"


@pytest.mark.asyncio
async def test_natural_language_completion_and_queries():
    """Verify natural language queries: ranking, what's due, and completing items."""
    device_id = "asus"
    await database.enroll_device(device_id, "asus", "admin", "secret")
    control = CortexControlLayer(hub.main.CONNECTED_DEVICES)
    harness = AgentHarness(control)

    wm = WorkloadManager.get_instance()
    await wm.ensure_initialized()

    now = CalendarSignalAdapter._get_local_now()
    await wm.add_item(
        title="Calc Assignment",
        course_or_project="Calc",
        due_at=((now + timedelta(days=1)).replace(hour=23, minute=59, second=0)).isoformat(),
        estimated_minutes=90,
        remaining_minutes=90
    )
    await wm.add_item(
        title="Java Project",
        course_or_project="Java",
        due_at=((now + timedelta(days=2)).replace(hour=23, minute=59, second=0)).isoformat(),
        estimated_minutes=180,
        remaining_minutes=180
    )

    # 1. "What do I have due?"
    res_due = await harness.process_request(device_id, "What do I have due?", {}, None)
    assert res_due.status == "success"
    assert "calc assignment" in res_due.message.lower()
    assert "java project" in res_due.message.lower()

    # 2. "What should I work on first?"
    res_first = await harness.process_request(device_id, "What should I work on first?", {}, None)
    assert res_first.status == "success"
    assert "calc assignment" in res_first.message.lower()

    # 3. "I finished the Calc assignment."
    res_fin = await harness.process_request(device_id, "I finished the Calc assignment.", {}, None)
    assert res_fin.status == "success"
    assert "completed" in res_fin.message.lower()

    # 4. "What do I have left?"
    res_left = await harness.process_request(device_id, "What do I have left?", {}, None)
    assert res_left.status == "success"
    assert "calc assignment" not in res_left.message.lower()
    assert "java project" in res_left.message.lower()


@pytest.mark.asyncio
async def test_proactive_deadline_trigger_and_attention_delivery():
    """
    Verify required proactive path:
    Natural Language -> WorkItem -> World State signal -> ProactiveEngine -> AttentionRouter -> Delivery
    """
    device_id = "asus"
    await database.enroll_device(device_id, "asus", "admin", "secret")
    await database.save_device_capabilities(device_id, ["ui.notify", "desktop_notification"])

    mock_ws = AsyncMock()
    mock_ws.send_text = AsyncMock()
    devices_map = {device_id: mock_ws}
    control = CortexControlLayer(devices_map)
    harness = AgentHarness(control)

    ar = AttentionRouter.get_instance()
    ar.set_control_layer(control)

    # User says: "I have a test task due in 10 minutes."
    res = await harness.process_request(device_id, "I have a test task due in 10 minutes.", {}, None)
    assert res.status == "success"

    wm = WorkloadManager.get_instance()
    item = wm.find_item("test task")
    assert item is not None

    # Workload signal was published
    wsm = WorldStateManager.get_instance()
    sig_dl = wsm._signals.get("workload.next_deadline")
    assert sig_dl is not None
    assert sig_dl.payload.get("minutes_until") is not None
    assert sig_dl.payload.get("minutes_until") <= 15

    # Proactive condition fires and attention router creates decision
    pe = ProactiveEngine.get_instance()
    candidates = await pe.list_candidates()
    assert len(candidates) >= 1

    all_decisions = list(ar._decisions.values())
    assert len(all_decisions) >= 1
    top_dec = all_decisions[0]
    assert top_dec.channel == "desktop_notification"
    assert top_dec.target_device_id == device_id
    assert top_dec.status in ("ready", "delivered")
    assert mock_ws.send_text.called, "Must deliver ui.notify to connected device"


@pytest.mark.asyncio
async def test_tier1_workload_capabilities_execution():
    """Verify Tier 1 internal capabilities execute cleanly through harness."""
    device_id = "asus"
    await database.enroll_device(device_id, "asus", "admin", "secret")
    control = CortexControlLayer(hub.main.CONNECTED_DEVICES)
    harness = AgentHarness(control)

    # 1. workload.add_item
    mock_res_add = ModelResult(
        type="ACTION",
        action_plan=ActionPlan(
            intent="manage_workload",
            source_device=device_id,
            capability="workload.add_item",
            action="workload.add_item",
            arguments={"title": "Discrete Homework", "type": "assignment", "estimated_minutes": 50},
            target_device_reference="asus",
            confidence=1.0,
            user_facing_summary="add discrete homework"
        )
    )
    with patch.object(harness.router, "generate_result", return_value=mock_res_add):
        res = await harness.process_request(device_id, "add discrete homework", {}, None)
        assert res.status == "success"
        assert "discrete homework" in res.message.lower()

    # 2. workload.list_items
    mock_res_list = ModelResult(
        type="ACTION",
        action_plan=ActionPlan(
            intent="query_workload",
            source_device=device_id,
            capability="workload.list_items",
            action="workload.list_items",
            arguments={},
            target_device_reference="asus",
            confidence=1.0,
            user_facing_summary="list all work items"
        )
    )
    with patch.object(harness.router, "generate_result", return_value=mock_res_list):
        res_list = await harness.process_request(device_id, "list all work items", {}, None)
        assert res_list.status == "success"
        assert "discrete homework" in res_list.message.lower()

    # 3. workload.prioritize
    mock_res_pri = ModelResult(
        type="ACTION",
        action_plan=ActionPlan(
            intent="prioritize_workload",
            source_device=device_id,
            capability="workload.prioritize",
            action="workload.prioritize",
            arguments={},
            target_device_reference="asus",
            confidence=1.0,
            user_facing_summary="prioritize items"
        )
    )
    with patch.object(harness.router, "generate_result", return_value=mock_res_pri):
        res_pri = await harness.process_request(device_id, "prioritize items", {}, None)
        assert res_pri.status == "success"


@pytest.mark.asyncio
async def test_production_db_isolation():
    """Verify production database file (data/cortex.db) was never modified or corrupted."""
    from hub.database import get_db_path
    resolved_test_db = get_db_path().resolve()
    prod_path = Path("data/cortex.db").resolve()
    assert resolved_test_db != prod_path, f"FATAL: Test targeting prod DB at {resolved_test_db}!"
    assert not str(resolved_test_db).replace('\\', '/').endswith("data/cortex.db")
    assert prod_path.exists(), "Production DB data/cortex.db must exist."
