"""
tests/test_phase_a5.py
----------------------
Phase A5 Test Suite: Distributed Device Handles & Executor Registry

Covers:
- DeviceHandle persistence and schema migration
- Capability advertisement & reconnect refreshes
- Heartbeat / liveness updates and timeout transitions
- ExecutorRegistry and live connection management
- Deterministic ExecutorSelector priority and trust enforcement
- Target ownership routing
- SystemRegistry distributed device introspection
- Self-knowledge conversational queries
- GoalPlanner executor-aware capability context
- All 8 Required Physical-Style Tests
- Production DB isolation
"""

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch
from pathlib import Path
import pytest
import pytest_asyncio

from hub.device_handle import (
    DeviceHandle,
    normalize_role,
    normalize_trust_level,
    normalize_device_type,
    default_transports,
)
from hub.executor_registry import ExecutorRegistry
from hub.executor_selector import ExecutorSelector, SelectionResult
from hub.system_registry import SystemRegistry
from hub.control_layer import CortexControlLayer
from agent.desktop_actions import (
    set_desktop_actions_override,
    DesktopActionsOverride,
)
from hub.agent_harness import AgentHarness, ConversationalResponse
from hub.planner import GoalPlanner
from hub.database import (
    get_db,
    enroll_device,
    get_device,
    list_devices as db_list_devices,
    update_device_heartbeat,
)


# ===========================================================================
# Physical-Style Test 1: Local Default
# ===========================================================================
@pytest.mark.asyncio
async def test_physical_1_local_default(monkeypatch):
    """
    REQUIRED PHYSICAL-STYLE TEST 1 — LOCAL DEFAULT
    Setup: asus online, capable of app.launch.
    Request: 'Open Notepad.'
    Expected:
        selected executor = asus
        reason = source/current capable device
    """
    # Migration: model decisions and enrollment are explicit; OS execution remains overridden.
    from test_agent_turn_runtime import ScriptedModel, call
    from unittest.mock import patch
    await enroll_device("asus", "ASUS test source", "admin", "test-hash")
    from hub.database import update_device_capabilities
    await update_device_capabilities("asus", "windows", ["app.launch", "fs.open", "screen.capture"])
    await update_device_capabilities("lenovo", "windows", ["app.launch", "fs.open"])
    monkeypatch.setenv("CORTEX_ENABLE_HUB_DESKTOP", "1")
    monkeypatch.setenv("CORTEX_HUB_DEVICE_ID", "asus")
    ctrl = CortexControlLayer()
    harness = AgentHarness(control_layer=ctrl, model_router=ScriptedModel([call('app.launch', **{'app': 'Notepad'})], answer='Notepad opened.'))
    registry = ExecutorRegistry.get_instance()

    launched = []
    mock_override = DesktopActionsOverride(
        launch_app=lambda args: (
            launched.append(args),
            {
                "status": "success",
                "message": "Application 'Notepad' launched and verified running (pid=1234).",
                "pid": 1234,
                "window_id": 5678,
            }
        )[1],
    )
    set_desktop_actions_override(mock_override)

    # Register asus in registry
    asus_handle = DeviceHandle(
        device_id="asus",
        display_name="ASUS Zenbook",
        role="primary_admin",
        trust_level="admin",
        device_type="laptop",
        platform="windows",
        capabilities=["app.launch", "app.close", "screen.capture", "fs.open"],
    )
    registry.register_device(asus_handle)
    registry.set_device_online("asus", True)

    selector = ExecutorSelector(registry)
    sel = selector.select_executor(
        required_capability="app.launch",
        requested_device=None,
        source_device="asus",
        owning_device=None,
    )
    assert sel.is_success
    assert sel.selected_device == "asus"
    assert "source/current" in sel.reason.lower() or "asus" in sel.reason.lower()

    # Verify conversational dispatch end-to-end
    context = {"device": {"capabilities": ["app.launch"]}}
    res = await harness.process_request("asus", "Open Notepad", context)
    assert res.status == "success", res
    assert len(launched) == 1
    assert "Notepad" in res.message


# ===========================================================================
# Physical-Style Test 2: Explicit Remote Device
# ===========================================================================
@pytest.mark.asyncio
async def test_physical_2_explicit_remote_device():
    """
    REQUIRED PHYSICAL-STYLE TEST 2 — EXPLICIT REMOTE DEVICE
    Simulate/enroll Lenovo as online with: app.launch.
    Request: 'Open Spotify on Lenovo.'
    Expected:
        selected executor = lenovo
        Do not dispatch to ASUS.
    """
    # Migration: model decisions and enrollment are explicit; OS execution remains overridden.
    from test_agent_turn_runtime import ScriptedModel, call
    from unittest.mock import patch
    await enroll_device("asus", "ASUS test source", "admin", "test-hash")
    from hub.database import update_device_capabilities
    await update_device_capabilities("asus", "windows", ["app.launch", "fs.open", "screen.capture"])
    await update_device_capabilities("lenovo", "windows", ["app.launch", "fs.open"])
    ctrl = CortexControlLayer()
    harness = AgentHarness(control_layer=ctrl, model_router=ScriptedModel([call('app.launch', **{'app': 'Spotify', 'target_device': 'lenovo'})], answer='Spotify launched on Lenovo ThinkPad.'))
    registry = ExecutorRegistry.get_instance()

    # Enrol & connect Lenovo
    lenovo_handle = DeviceHandle(
        device_id="lenovo",
        display_name="Lenovo ThinkPad",
        role="trusted_worker",
        trust_level="trusted",
        device_type="laptop",
        platform="windows",
        capabilities=["app.launch", "fs.open"],
    )
    registry.register_device(lenovo_handle)
    registry.set_device_online("lenovo", True)

    asus_handle = DeviceHandle(
        device_id="asus",
        display_name="ASUS Zenbook",
        role="primary_admin",
        trust_level="admin",
        capabilities=["app.launch", "screen.capture"],
    )
    registry.register_device(asus_handle)
    registry.set_device_online("asus", True)

    # 1. Deterministic Selector Verification
    selector = ExecutorSelector(registry)
    sel = selector.select_executor(
        required_capability="app.launch",
        requested_device="lenovo",
        source_device="asus",
    )
    assert sel.is_success
    assert sel.selected_device == "lenovo"
    assert sel.selected_device != "asus"

    # 2. Conversational flow dispatch targeting Lenovo
    # Mock remote execution observation for Lenovo
    with patch.object(ctrl, "execute_and_observe", new=AsyncMock(return_value={
        "status": "success",
        "message": "Spotify launched on Lenovo ThinkPad.",
    })) as mock_exec:
        res = await harness.process_request("asus", "Open Spotify on Lenovo", context={})
        assert res.status == "success", res
        assert "Spotify" in res.message
        # Verify call was routed to Lenovo, not ASUS
        mock_exec.assert_called_once()
        target_dispatched = mock_exec.call_args[0][0]
        assert target_dispatched == "lenovo"


# ===========================================================================
# Physical-Style Test 3: Device Capability Mismatch
# ===========================================================================
@pytest.mark.asyncio
async def test_physical_3_device_capability_mismatch():
    """
    REQUIRED PHYSICAL-STYLE TEST 3 — DEVICE CAPABILITY MISMATCH
    Lenovo online but without: screen.capture.
    Request: 'Take a screenshot on Lenovo.'
    Expected:
        blocked / unsupported on target device.
        Do not reroute to ASUS because the user explicitly requested Lenovo.
    """
    registry = ExecutorRegistry.get_instance()

    # Lenovo has only app.launch, NO screen.capture
    lenovo_handle = DeviceHandle(
        device_id="lenovo",
        display_name="Lenovo ThinkPad",
        role="trusted_worker",
        trust_level="trusted",
        capabilities=["app.launch"],
    )
    registry.register_device(lenovo_handle)
    registry.set_device_online("lenovo", True)

    # ASUS HAS screen.capture
    asus_handle = DeviceHandle(
        device_id="asus",
        display_name="ASUS Zenbook",
        role="primary_admin",
        trust_level="admin",
        capabilities=["screen.capture", "app.launch"],
    )
    registry.register_device(asus_handle)
    registry.set_device_online("asus", True)

    selector = ExecutorSelector(registry)
    sel = selector.select_executor(
        required_capability="screen.capture",
        requested_device="lenovo",
        source_device="asus",
    )
    assert not sel.is_success
    assert sel.status == "capability_mismatch"
    assert sel.selected_device is None
    # STRICT: Never silently fallback to ASUS
    assert sel.selected_device != "asus"

    # End-to-end conversational check
    # Migration: model decisions and enrollment are explicit; OS execution remains overridden.
    from test_agent_turn_runtime import ScriptedModel, call
    from unittest.mock import patch
    await enroll_device("asus", "ASUS test source", "admin", "test-hash")
    from hub.database import update_device_capabilities
    await update_device_capabilities("asus", "windows", ["app.launch", "fs.open", "screen.capture"])
    await update_device_capabilities("lenovo", "windows", ["app.launch", "fs.open"])
    ctrl = CortexControlLayer()
    harness = AgentHarness(control_layer=ctrl, model_router=ScriptedModel([call('screen.capture', **{'target_device': 'lenovo'})]*4, answer='unused'))
    res = await harness.process_request("asus", "Take a screenshot on Lenovo", context={})
    assert res.status in {"denied", "error"}
    assert "does not support" in res.message or "unsupported" in res.message.lower()
    assert "lenovo" in res.message.lower()


# ===========================================================================
# Physical-Style Test 4: Offline Device
# ===========================================================================
@pytest.mark.asyncio
async def test_physical_4_offline_device(monkeypatch):
    """
    REQUIRED PHYSICAL-STYLE TEST 4 — OFFLINE DEVICE
    Lenovo enrolled but heartbeat expired / offline.
    Request: 'Open Chrome on Lenovo.'
    Expected:
        target device unavailable/offline.
        no dispatch.
        Do NOT silently execute on ASUS.
    """
    registry = ExecutorRegistry.get_instance()

    lenovo_handle = DeviceHandle(
        device_id="lenovo",
        display_name="Lenovo ThinkPad",
        role="trusted_worker",
        trust_level="trusted",
        capabilities=["app.launch"],
    )
    registry.register_device(lenovo_handle)
    # Ensure offline
    registry.set_device_online("lenovo", False)

    # ASUS is online and capable
    asus_handle = DeviceHandle(
        device_id="asus",
        display_name="ASUS Zenbook",
        role="primary_admin",
        capabilities=["app.launch"],
    )
    registry.register_device(asus_handle)
    registry.set_device_online("asus", True)

    selector = ExecutorSelector(registry)
    sel = selector.select_executor(
        required_capability="app.launch",
        requested_device="lenovo",
        source_device="asus",
    )
    assert not sel.is_success
    assert sel.status == "device_offline"
    assert sel.selected_device is None

    # End-to-end conversational check
    # Migration: model decisions and enrollment are explicit; OS execution remains overridden.
    from test_agent_turn_runtime import ScriptedModel, call
    from unittest.mock import patch
    await enroll_device("asus", "ASUS test source", "admin", "test-hash")
    from hub.database import update_device_capabilities
    await update_device_capabilities("asus", "windows", ["app.launch", "fs.open", "screen.capture"])
    await update_device_capabilities("lenovo", "windows", ["app.launch", "fs.open"])
    ctrl = CortexControlLayer()
    harness = AgentHarness(control_layer=ctrl, model_router=ScriptedModel([call('app.launch', **{'app': 'Chrome', 'target_device': 'lenovo'})], answer='unused'))
    from hub.agent_turn import AgentTurn
    monkeypatch.setattr(AgentTurn, "ACK_SECONDS", .01)
    res = await harness.process_request("asus", "Open Chrome on Lenovo", context={})
    assert res.status == "in_progress"
    from hub.database import get_task
    await asyncio.sleep(.02)
    assert (await get_task(res.data["task_id"]))["status"] == "WAITING"
    job=AgentTurn._running[res.data["task_id"]]
    job.cancel()
    await asyncio.gather(job,return_exceptions=True)


# ===========================================================================
# Physical-Style Test 5: Capability Discovery
# ===========================================================================
@pytest.mark.asyncio
async def test_physical_5_capability_discovery():
    """
    REQUIRED PHYSICAL-STYLE TEST 5 — CAPABILITY DISCOVERY
    Ask: 'Which devices can capture my screen?'
    Cortex should enumerate only online/known devices actually advertising screen.capture.
    """
    registry = ExecutorRegistry.get_instance()

    asus_handle = DeviceHandle(
        device_id="asus",
        display_name="ASUS Zenbook",
        role="primary_admin",
        capabilities=["screen.capture", "app.launch"],
    )
    registry.register_device(asus_handle)
    registry.set_device_online("asus", True)

    lenovo_handle = DeviceHandle(
        device_id="lenovo",
        display_name="Lenovo ThinkPad",
        role="trusted_worker",
        capabilities=["app.launch"],  # NO screen.capture
    )
    registry.register_device(lenovo_handle)
    registry.set_device_online("lenovo", True)

    phone_handle = DeviceHandle(
        device_id="pixel",
        display_name="Pixel 9 Pro",
        role="standard",
        capabilities=["screen.capture"],
    )
    registry.register_device(phone_handle)
    registry.set_device_online("pixel", False)  # Offline!

    # Registry inquiry
    capable_online = registry.get_capable_devices("screen.capture", online_only=True)
    assert len(capable_online) == 1
    assert capable_online[0].device_id == "asus"

    # Conversational inquiry
    # Migration: model decisions and enrollment are explicit; OS execution remains overridden.
    from test_agent_turn_runtime import ScriptedModel, call
    from unittest.mock import patch
    await enroll_device("asus", "ASUS test source", "admin", "test-hash")
    from hub.database import update_device_capabilities
    await update_device_capabilities("asus", "windows", ["app.launch", "fs.open", "screen.capture"])
    await update_device_capabilities("lenovo", "windows", ["app.launch", "fs.open"])
    ctrl = CortexControlLayer()
    harness = AgentHarness(control_layer=ctrl, model_router=ScriptedModel([call('system.device_status', **{'device_id': 'asus'})], answer='ASUS Zenbook supports screen.capture.'))
    res = await harness.process_request("asus", "Which devices can capture my screen?", context={})
    assert res.status == "success", res
    assert "ASUS Zenbook" in res.message or "asus" in res.message.lower()
    assert "Lenovo" not in res.message
    assert "Pixel" not in res.message  # Offline device excluded


# ===========================================================================
# Physical-Style Test 6: Target Ownership
# ===========================================================================
@pytest.mark.asyncio
async def test_physical_6_target_ownership(tmp_path, monkeypatch):
    """
    REQUIRED PHYSICAL-STYLE TEST 6 — TARGET OWNERSHIP
    Register: file C:\\Users\\...\\document.pdf owned by asus.
    Ask: 'Open that file.'
    Expected: executor = asus, even if another capable device (Lenovo) is online.
    """
    registry = ExecutorRegistry.get_instance()
    test_pdf = tmp_path / "document.pdf"
    test_pdf.write_text("sample content", encoding="utf-8")

    # Both ASUS and Lenovo support fs.open and are online
    asus_handle = DeviceHandle(
        device_id="asus",
        display_name="ASUS Zenbook",
        role="primary_admin",
        capabilities=["fs.open"],
    )
    registry.register_device(asus_handle)
    registry.set_device_online("asus", True)

    lenovo_handle = DeviceHandle(
        device_id="lenovo",
        display_name="Lenovo ThinkPad",
        role="trusted_worker",
        capabilities=["fs.open"],
    )
    registry.register_device(lenovo_handle)
    registry.set_device_online("lenovo", True)

    selector = ExecutorSelector(registry)
    sel = selector.select_executor(
        required_capability="fs.open",
        requested_device=None,
        source_device="asus",
        owning_device="asus",
    )
    assert sel.is_success
    assert sel.selected_device == "asus"
    assert "owned by 'asus'" in sel.reason

    # Conversational continuity check with last_file reference
    # Migration: model decisions and enrollment are explicit; OS execution remains overridden.
    from test_agent_turn_runtime import ScriptedModel, call
    from unittest.mock import patch
    await enroll_device("asus", "ASUS test source", "admin", "test-hash")
    from hub.database import update_device_capabilities
    await update_device_capabilities("asus", "windows", ["app.launch", "fs.open", "screen.capture"])
    await update_device_capabilities("lenovo", "windows", ["app.launch", "fs.open"])
    monkeypatch.setenv("CORTEX_ENABLE_HUB_DESKTOP", "1")
    monkeypatch.setenv("CORTEX_HUB_DEVICE_ID", "asus")
    ctrl = CortexControlLayer()
    harness = AgentHarness(control_layer=ctrl, model_router=ScriptedModel([call("fs.open", path=str(test_pdf))], answer='The file is open.'))
    context = {
        "session": {
            "references": {"last_file": str(test_pdf)},
            "grounded_target": {"owning_device": "asus", "path": str(test_pdf)},
        }
    }
    with patch("os.startfile", return_value=None):
        res = await harness.process_request("asus", "Open that file", context=context)
        assert res.status == "success", res


# ===========================================================================
# Physical-Style Test 7: Heartbeat Timeout
# ===========================================================================
@pytest.mark.asyncio
async def test_physical_7_heartbeat_timeout():
    """
    REQUIRED PHYSICAL-STYLE TEST 7 — HEARTBEAT TIMEOUT
    Simulate active device.
    Stop heartbeats.
    Verify: online -> offline after configured timeout. No stale online=true.
    """
    registry = ExecutorRegistry.get_instance()
    # Configure short timeout for test
    registry.set_heartbeat_timeout(0.2)

    dev_handle = DeviceHandle(
        device_id="test_node",
        display_name="Test Node",
        role="trusted_worker",
        capabilities=["app.launch"],
    )
    registry.register_device(dev_handle)
    registry.on_device_connected("test_node", websocket=MagicMock())

    # Initially online
    assert registry.is_device_online("test_node") is True

    # Send heartbeat
    registry.on_heartbeat("test_node", state="idle", active_task_count=0)
    assert registry.is_device_online("test_node") is True

    # Wait for timeout to expire
    await asyncio.sleep(0.3)

    # Must transition to offline
    assert registry.is_device_online("test_node") is False

    # Resume heartbeat -> transitions back to online
    registry.on_heartbeat("test_node", state="active", active_task_count=1)
    assert registry.is_device_online("test_node") is True


# ===========================================================================
# Physical-Style Test 8: Trust Restriction
# ===========================================================================
@pytest.mark.asyncio
async def test_physical_8_trust_restriction():
    """
    REQUIRED PHYSICAL-STYLE TEST 8 — TRUST RESTRICTION
    Register restricted executor advertising: fs.delete.
    Attempt destructive action.
    Expected:
        selector rejects executor due to trust/policy.
        Technical capability alone must not grant execution authority.
    """
    registry = ExecutorRegistry.get_instance()

    # Restricted device advertising fs.delete
    restricted_handle = DeviceHandle(
        device_id="kiosk",
        display_name="Public Kiosk",
        role="restricted",
        trust_level="restricted",
        capabilities=["fs.delete", "app.launch"],
    )
    registry.register_device(restricted_handle)
    registry.set_device_online("kiosk", True)

    selector = ExecutorSelector(registry)

    # 1. Non-destructive action should be permitted
    sel_safe = selector.select_executor(
        required_capability="app.launch",
        requested_device="kiosk",
        source_device="asus",
        permission_tier=1,
        classification="read",
    )
    assert sel_safe.is_success
    assert sel_safe.selected_device == "kiosk"

    # 2. Destructive action (tier >= 3 or classification='destructive') MUST be rejected
    sel_destr = selector.select_executor(
        required_capability="fs.delete",
        requested_device="kiosk",
        source_device="asus",
        permission_tier=3,
        classification="destructive",
    )
    assert not sel_destr.is_success
    assert sel_destr.status == "trust_denied"
    assert sel_destr.selected_device is None
    assert "restricted" in sel_destr.reason.lower()


# ===========================================================================
# Unit Test: DeviceHandle Persistence & Migration
# ===========================================================================
@pytest.mark.asyncio
async def test_device_handle_persistence_and_migration():
    """Verify DeviceHandle persistence, DB schema migration, and ASUS role migration."""
    # 1. Simulate existing legacy ASUS enrollment with role='admin'
    await enroll_device("asus", "ASUS Zenbook", role="admin", secret_hash="hash123")
    from hub.database import _migrate_schema_updates
    await _migrate_schema_updates()
    asus_db = await get_device("asus")
    assert asus_db is not None
    assert asus_db["role"] == "admin"
    assert asus_db["trust_level"] == "admin"
    assert asus_db["device_type"] == "unknown"

    # 2. Enroll a new device with full Phase A5 fields
    transports = {"websocket": True, "lan": True, "ble": False, "wifi_direct": False}
    meta = {"location": "living_room"}

    await enroll_device(
        device_id="node_x",
        device_name="Worker Node X",
        role="trusted_worker",
        secret_hash="secret_hash_x",
        trust_level="trusted",
        device_type="server",
        platform="linux",
        transports=transports,
        metadata=meta,
    )
    from hub.database import update_device_capabilities
    await update_device_capabilities("node_x", "linux", ["app.launch", "fs.open"])

    rec = await get_device("node_x")
    assert rec is not None
    assert rec["role"] == "trusted_worker"
    assert rec["trust_level"] == "trusted"
    assert rec["device_type"] == "server"

    # Reconstruct DeviceHandle
    handle = DeviceHandle.from_db_row(rec)
    assert handle.device_id == "node_x"
    assert handle.transports["websocket"] is True
    assert handle.transports["ble"] is False
    assert handle.metadata.get("location") == "living_room"


# ===========================================================================
# Unit Test: Reconnect Capability Refresh
# ===========================================================================
@pytest.mark.asyncio
async def test_reconnect_capability_refresh():
    """Verify ExecutorRegistry updates capability advertisement on reconnect."""
    registry = ExecutorRegistry.get_instance()

    # Initial connect
    registry.on_device_connected("dev_reconnect", websocket=MagicMock())
    registry.on_device_authenticated(
        device_id="dev_reconnect",
        capabilities=["app.launch"],
        platform="windows",
        agent_version="1.0.0",
    )
    h1 = registry.get_device_handle("dev_reconnect")
    assert h1.capabilities == ["app.launch"]
    assert h1.agent_version == "1.0.0"

    # Reconnect with upgraded version and additional capabilities
    registry.on_device_authenticated(
        device_id="dev_reconnect",
        capabilities=["app.launch", "screen.capture", "fs.open"],
        platform="windows",
        agent_version="1.1.0",
    )
    h2 = registry.get_device_handle("dev_reconnect")
    assert "screen.capture" in h2.capabilities
    assert "fs.open" in h2.capabilities
    assert h2.agent_version == "1.1.0"


# ===========================================================================
# Unit Test: Workload Telemetry & Advisory Sorting
# ===========================================================================
@pytest.mark.asyncio
async def test_workload_telemetry_advisory_selection():
    """Verify workload telemetry is advisory and helps break ties between capable executors."""
    registry = ExecutorRegistry.get_instance()

    # Two trusted workers with app.launch
    worker_busy = DeviceHandle(
        device_id="worker_busy",
        display_name="Worker Busy",
        role="trusted_worker",
        trust_level="trusted",
        capabilities=["app.launch"],
        workload={"active_task_count": 5},
    )
    worker_idle = DeviceHandle(
        device_id="worker_idle",
        display_name="Worker Idle",
        role="trusted_worker",
        trust_level="trusted",
        capabilities=["app.launch"],
        workload={"active_task_count": 0},
    )
    registry.register_device(worker_busy)
    registry.register_device(worker_idle)
    registry.set_device_online("worker_busy", True)
    registry.set_device_online("worker_idle", True)

    selector = ExecutorSelector(registry)
    # When source_device has no app.launch, selector routes to alternate capable executor
    sel = selector.select_executor(
        required_capability="app.launch",
        source_device="external_trigger",
    )
    assert sel.is_success
    # Idle worker must be preferred over busy worker
    assert sel.selected_device == "worker_idle"
    assert "worker_busy" in sel.alternatives


# ===========================================================================
# Unit Test: SystemRegistry Exposure
# ===========================================================================
@pytest.mark.asyncio
async def test_system_registry_exposure():
    """Verify SystemRegistry.list_devices exposes Phase A5 metadata."""
    registry = ExecutorRegistry.get_instance()
    sys_reg = SystemRegistry.get_instance()

    handle = DeviceHandle(
        device_id="asus",
        display_name="ASUS Zenbook",
        role="primary_admin",
        trust_level="admin",
        device_type="laptop",
        platform="windows",
        capabilities=["app.launch", "screen.capture"],
        transports={"websocket": True, "lan": True, "ble": False, "wifi_direct": False},
        workload={"active_task_count": 1},
    )
    registry.register_device(handle)
    registry.set_device_online("asus", True)

    devices = await sys_reg.list_devices(current_node="asus")
    assert len(devices) >= 1
    asus_info = next((d for d in devices if d["device_id"] == "asus"), None)
    assert asus_info is not None
    assert asus_info["role"] == "primary_admin"
    assert asus_info["trust_level"] == "admin"
    assert asus_info["device_type"] == "laptop"
    assert asus_info["transports"]["websocket"] is True
    assert asus_info["workload_summary"]["active_task_count"] == 1
    assert "screen.capture" in asus_info["advertised_capabilities"]


# ===========================================================================
# Unit Test: Self-Knowledge Queries
# ===========================================================================
@pytest.mark.asyncio
async def test_self_knowledge_queries():
    """Verify conversational self-knowledge queries for devices."""
    ctrl = CortexControlLayer()
    from model_contract_fixtures import runtime_harness
    harness, model = await runtime_harness()
    registry = ExecutorRegistry.get_instance()

    lenovo = DeviceHandle(
        device_id="lenovo",
        display_name="Lenovo ThinkPad",
        role="trusted_worker",
        trust_level="trusted",
        capabilities=["app.launch"],
    )
    registry.register_device(lenovo)
    registry.set_device_online("lenovo", True)

    phone = DeviceHandle(
        device_id="phone",
        display_name="My Phone",
        role="standard",
        trust_level="restricted",
        capabilities=[],
    )
    registry.register_device(phone)
    registry.set_device_online("phone", False)

    # 1. "Can my Lenovo open apps?"
    res1 = await harness.process_request("asus", "Can my Lenovo open apps?", context={})
    assert res1.status == "success", res1

    # 2. "Why can't you run this on my phone?"
    res2 = await harness.process_request("asus", "Why can't you run this on my phone?", context={})
    assert res2.status == "success", res2

    # 3. "Which devices are online?"
    res3 = await harness.process_request("asus", "Which devices are online?", context={})
    assert res3.status == "success", res3

    # 4. "Which device would run this?"
    res4 = await harness.process_request("asus", "Which device would run this?", context={})
    assert res4.status == "success", res4
    assert model.calls[-1]["mode"] == "result_interpretation"
    evidence=model.calls[-1]["tool_result"]
    assert evidence["status"] == "success"
    assert evidence["data"]["source_device"] == "asus"
    assert evidence["data"]["automatic_action_replay"] is False
    assert "devices" in evidence["data"]


# ===========================================================================
# Unit Test: Planner Executor-Aware Context
# ===========================================================================
@pytest.mark.asyncio
async def test_planner_executor_aware_context():
    """Verify GoalPlanner receives per-device capability context and validates targets."""
    registry = ExecutorRegistry.get_instance()
    planner = GoalPlanner.get_instance()

    # ASUS has app.launch, Lenovo has screen.capture
    asus = DeviceHandle(
        device_id="asus",
        display_name="ASUS",
        role="primary_admin",
        capabilities=["app.launch"],
    )
    lenovo = DeviceHandle(
        device_id="lenovo",
        display_name="Lenovo",
        role="trusted_worker",
        capabilities=["screen.capture"],
    )
    registry.register_device(asus)
    registry.register_device(lenovo)
    registry.set_device_online("asus", True)
    registry.set_device_online("lenovo", True)

    ctx = await planner.build_capability_context()
    app_launch_cap = next((c for c in ctx if c["name"] == "app.launch"), None)
    assert app_launch_cap is not None
    dev_avail = app_launch_cap.get("device_availability", {})
    assert dev_avail.get("asus") is True
    assert dev_avail.get("lenovo") is False

    mock_goal = MagicMock(max_steps=5)

    # Valid plan: app.launch on asus
    valid_plan = {
        "steps": [
            {"action": "app.launch", "arguments": {"app": "notepad", "target_device": "asus"}}
        ]
    }
    is_valid, err = planner.validate_plan(valid_plan, ctx, mock_goal)
    assert is_valid is True
    assert err is None

    # Invalid plan: app.launch on lenovo (Lenovo lacks app.launch)
    invalid_plan = {
        "steps": [
            {"action": "app.launch", "arguments": {"app": "notepad", "target_device": "lenovo"}}
        ]
    }
    is_valid, err = planner.validate_plan(invalid_plan, ctx, mock_goal)
    assert is_valid is False
    assert "not supported or available on device 'lenovo'" in err


# ===========================================================================
# Unit Test: Production DB Isolation
# ===========================================================================
def test_production_db_isolation():
    """Verify tests run strictly against test_cortex.db and production DB is protected."""
    import os
    db_path = os.environ.get("CORTEX_DB_PATH", "")
    assert "test_cortex.db" in db_path
    assert not db_path.endswith("data/cortex.db")


# ===========================================================================
# Unit Test: Liveness Heartbeat Lifecycle & Reconnection
# ===========================================================================
@pytest.mark.asyncio
async def test_liveness_heartbeat_lifecycle_and_reconnection():
    """
    Verify the complete runtime path for liveness:
    1. Authenticated connection marks node online
    2. Heartbeat refreshes monotonic timestamp
    3. Expiry after timeout marks device offline
    4. Disconnection marks device offline
    5. Reconnection restores online state without restart
    """
    registry = ExecutorRegistry(heartbeat_timeout=2.0)
    
    # 1. Device connects
    mock_ws = MagicMock()
    registry.on_device_connected("lenovo", mock_ws)
    registry.on_device_authenticated(
        device_id="lenovo",
        capabilities=["app.launch"],
        platform="windows",
        role="trusted_worker",
    )
    assert registry.is_device_online("lenovo") is True
    freshness = registry.get_heartbeat_freshness("lenovo")
    assert freshness is not None
    assert freshness <= 0.5

    # 2. Heartbeat arrives
    registry.on_heartbeat("lenovo", telemetry={"battery": 90})
    handle = registry.get_device_handle("lenovo")
    assert handle is not None
    assert handle.online is True
    assert handle.resource_profile.get("battery") == 90

    # 3. Simulate heartbeat timeout (simulate time advancement)
    # Artificially age the heartbeat past the 2.0s timeout
    registry._last_heartbeat_time["lenovo"] = time.monotonic() - 5.0
    assert registry.is_device_online("lenovo") is False
    handle = registry.get_device_handle("lenovo")
    assert handle.online is False

    # 4. Heartbeat arrives again -> restores online
    registry.on_heartbeat("lenovo")
    assert registry.is_device_online("lenovo") is True
    assert registry.get_device_handle("lenovo").online is True

    # 5. Disconnect -> marks offline
    registry.on_device_disconnected("lenovo")
    assert registry.is_device_online("lenovo") is False
    assert registry.get_device_handle("lenovo").online is False
    assert registry.get_heartbeat_freshness("lenovo") is None

    # 6. Reconnect -> restores online without restart
    registry.on_device_connected("lenovo", mock_ws)
    registry.on_device_authenticated(
        device_id="lenovo",
        capabilities=["app.launch"],
        platform="windows",
        role="trusted_worker",
    )
    assert registry.is_device_online("lenovo") is True
    assert registry.get_device_handle("lenovo").online is True

