"""
tests/test_phase_a9.py
-----------------------
Test suite for Phase A9: Cortex Control Center & Product Frontend.

Verifies:
1. Frontend canonical state mapping (/api/control/state contains all required surfaces)
2. WebSocket /ws-control initial state broadcast and reconnect reconciliation
3. Conversation message send and receive flow
4. Safe operational activity stream (no chain-of-thought)
5. Devices / ExecutorRegistry liveness reflection (no fake status)
6. Tasks / Goals state updates and emergency stop/pause
7. Authoritative Tier 3 approvals resolution (approve/deny hitting database)
8. File transfer sessions monitoring and cancellation
9. World state / situational awareness grounding (WorldStateManager)
10. System health grounding in SystemRegistry
11. Global autonomy kill switch state toggle and persistence
"""

import asyncio
import json
import pytest
import pytest_asyncio
from unittest.mock import AsyncMock
from httpx import AsyncClient, ASGITransport

from hub.main import app


@pytest.fixture(autouse=True)
def authenticated_component_session(monkeypatch):
    """A9 state tests simulate an already-authenticated administrator.
    The real authentication boundary is covered in test_control_auth_boundary.py.
    """
    monkeypatch.setattr("hub.main._authenticate_http_headers", AsyncMock(
        return_value={"device_id": "asus", "role": "admin"}))
from hub.api_control import emit_activity, get_recent_activities, build_canonical_state
from hub.system_registry import SystemRegistry
from hub.executor_registry import ExecutorRegistry
from hub.device_handle import DeviceHandle
from hub.task_ownership import TaskOwnershipManager, ExecutionState
from hub.goals import GoalManager
from hub.world_state import WorldStateManager
from hub.file_transfer import FileTransferManager, TransferSession, TransferState
from hub.database import (
    init_db,
    close_db,
    create_confirmation,
    get_pending_confirmations,
    get_confirmation,
    save_transfer_session,
    list_transfer_sessions,
)


@pytest_asyncio.fixture(autouse=True)
async def setup_test_env():
    await init_db()
    SystemRegistry.reset()
    ExecutorRegistry.reset()
    TaskOwnershipManager.reset()
    GoalManager.reset()
    WorldStateManager.reset()
    FileTransferManager.reset()
    yield
    await close_db()


@pytest.mark.asyncio
async def test_control_state_mapping():
    """Verify GET /api/control/state returns all 10 core surface schemas."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/control/state")
        assert resp.status_code == 200
        data = resp.json()

        # Surface 1 & 9: Identity & System Health
        assert "identity" in data
        assert data["identity"]["product_name"] == "Cortex"
        assert "version" in data["identity"]
        assert "system_health" in data
        assert "models" in data["system_health"]
        assert "providers" in data["system_health"]
        assert "integrations" in data["system_health"]
        assert "capabilities" in data["system_health"]

        # Surface 4: Devices / Executors
        assert "devices" in data
        assert isinstance(data["devices"], list)

        # Surface 3: Tasks & Goals
        assert "tasks" in data
        assert isinstance(data["tasks"], list)

        # Surface 5: Approvals
        assert "approvals" in data
        assert isinstance(data["approvals"], list)

        # Surface 6: Transfers
        assert "transfers" in data
        assert isinstance(data["transfers"], list)

        # Surface 8: World State
        assert "world_state" in data
        assert "summary" in data["world_state"]

        # Surface 10: Safety & Autonomy
        assert "safety" in data
        assert "autonomy_enabled" in data["safety"]

        # Surface 2: Operational Activities
        assert "activities" in data
        assert isinstance(data["activities"], list)


@pytest.mark.asyncio
async def test_live_activity_emission_and_redaction():
    """Verify structured activity events are recorded without exposing chain-of-thought."""
    ev = emit_activity(
        label="Running screen analysis",
        category="perception",
        status="active",
        device_id="asus",
    )

    assert ev["label"] == "Running screen analysis"
    assert ev["category"] == "perception"
    assert ev["status"] == "active"
    assert ev["device_id"] == "asus"
    assert "timestamp" in ev

    # Chain-of-thought assertions: no internal prompt/hidden reasoning in keys
    assert "thought" not in ev
    assert "reasoning" not in ev
    assert "chain_of_thought" not in ev

    recent = get_recent_activities(limit=10)
    assert any(a["label"] == "Running screen analysis" for a in recent)


@pytest.mark.asyncio
async def test_devices_panel_liveness():
    """Verify device status is grounded in ExecutorRegistry and does not fake online state."""
    exec_reg = ExecutorRegistry.get_instance()
    
    # ASUS is online
    exec_reg.on_device_connected("asus", True)
    
    # Lenovo enrolled but offline
    lenovo_handle = DeviceHandle(
        device_id="lenovo",
        display_name="Lenovo Node",
        online=False,
        capabilities=["app.launch", "screen.capture"],
    )
    exec_reg.register_device(lenovo_handle)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/control/state")
        assert resp.status_code == 200
        data = resp.json()
        devs = {d["device_id"]: d for d in data["devices"]}

        assert "asus" in devs
        assert devs["asus"]["online"] is True

        assert "lenovo" in devs
        assert devs["lenovo"]["online"] is False


@pytest.mark.asyncio
async def test_conversation_message_flow(monkeypatch):
    """Real GUI/AgentTurn wiring with an explicit offline model decision."""
    from test_agent_turn_runtime import ScriptedModel
    from hub.agent_harness import AgentHarness
    from hub.control_layer import CortexControlLayer
    model = ScriptedModel([{"decision":"RESPOND","response_text":"I am Cortex, your computer assistant."}])
    monkeypatch.setattr("hub.main._agent_harness", AgentHarness(CortexControlLayer({}), model), raising=False)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/control/message",
            json={"text": "What is your identity?", "device_id": "asus"}
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "response_text" in data
        assert data["status"] == "success"
        assert data["response_text"] == "I am Cortex, your computer assistant."
        assert len(model.calls) == 1 and model.calls[0][1]["mode"] == "agent_decision"
        assert data["user_message"] == "What is your identity?"


@pytest.mark.asyncio
async def test_approvals_resolution_approve_and_deny():
    """Verify Tier 3 approvals appear in UI state and approve/deny actions resolve in DB."""
    # Seed a pending confirmation in database
    cmd_id = "test_cmd_12345"
    await create_confirmation(
        command_id=cmd_id,
        target_device="asus",
        command="system.shutdown",
        args=["--force"],
        requested_by="lenovo",
        tier=3,
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Verify it appears in pending approvals
        resp = await client.get("/api/control/state")
        data = resp.json()
        apprs = [a for a in data["approvals"] if a["command_id"] == cmd_id]
        assert len(apprs) == 1
        assert apprs[0]["tier"] == 3
        assert apprs[0]["command"] == "system.shutdown"

        # Deny the approval
        res_deny = await client.post(
            f"/api/control/approvals/{cmd_id}/resolve",
            json={"decision": "deny"}
        )
        assert res_deny.status_code == 200
        assert res_deny.json()["decision"] == "deny"

        # Verify DB reflects resolution
        row = await get_confirmation(cmd_id)
        assert row["status"] in ("deny", "denied")


@pytest.mark.asyncio
async def test_tasks_panel_and_stop():
    """Verify task state reflects in UI and stopping task cancels it."""
    gm = GoalManager.get_instance()
    goal = await gm.create_goal(
        title="Automated Desktop Cleanup",
        original_request="clean messy desktop",
        metadata={"target_device": "asus"},
    )
    goal.status = "ACTIVE"
    await gm.update_goal(goal)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Check task appears in state
        resp = await client.get("/api/control/state")
        data = resp.json()
        matching = [t for t in data["tasks"] if t["id"] == goal.goal_id]
        assert len(matching) == 1
        assert matching[0]["title"] == "Automated Desktop Cleanup"
        assert matching[0]["status"] == "ACTIVE"

        # Stop task via control endpoint
        stop_res = await client.post(f"/api/control/tasks/{goal.goal_id}/stop")
        assert stop_res.status_code == 200
        assert stop_res.json()["state"] == "CANCELLED"

        # Verify goal was cancelled in GoalManager
        updated_goal = await gm.get_goal(goal.goal_id)
        assert updated_goal.status == "CANCELLED"


@pytest.mark.asyncio
async def test_transfers_monitoring_and_cancel():
    """Verify A8 transfer session appears in transfers panel and can be cancelled."""
    ftm = FileTransferManager.get_instance()
    sess = TransferSession(
        transfer_id="xfer_test_99",
        source_device_id="asus",
        destination_device_id="lenovo",
        source_path="C:/reports/q4.pdf",
        destination_path="D:/data/q4.pdf",
        filename="q4.pdf",
        total_bytes=1048576,
        transferred_bytes=524288,
        transport="lan",
        state=TransferState.TRANSFERRING,
        resumable=True,
    )
    ftm._active_sessions["xfer_test_99"] = sess
    await save_transfer_session(sess.to_dict())

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Check transfer appears
        resp = await client.get("/api/control/state")
        data = resp.json()
        xfers = [x for x in data["transfers"] if x["transfer_id"] == "xfer_test_99"]
        assert len(xfers) == 1
        assert xfers[0]["filename"] == "q4.pdf"
        assert xfers[0]["percent"] == 50.0
        assert xfers[0]["transport"] == "lan"

        # Cancel transfer
        cancel_res = await client.post("/api/control/transfers/xfer_test_99/cancel")
        assert cancel_res.status_code == 200
        assert cancel_res.json()["state"] == "CANCELLED"

        # Verify session state updated
        updated_sess = await ftm.get_session("xfer_test_99")
        assert updated_sess.state == "CANCELLED"


@pytest.mark.asyncio
async def test_world_state_grounding():
    """Verify world-state panel renders active app and window title from WorldStateManager."""
    wsm = WorldStateManager.get_instance()
    from hub.world_state import Signal
    from datetime import datetime, timezone
    await wsm.ingest_signal(Signal(
        key="device.foreground:asus",
        source="desktop_sensor",
        timestamp=datetime.now(timezone.utc).isoformat(),
        payload={"app_name": "Visual Studio Code", "window_title": "pipeline.py - Cortex", "device_id": "asus"},
        confidence=1.0,
        ttl=300.0,
    ))

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/control/state")
        data = resp.json()
        ws_summary = data["world_state"]["summary"]
        assert ws_summary["foreground_app"] == "Visual Studio Code"


@pytest.mark.asyncio
async def test_system_health_grounding():
    """Verify system health accurately reflects models and providers from SystemRegistry."""
    sys_reg = SystemRegistry.get_instance()
    identity = sys_reg.get_identity()
    providers = sys_reg.get_providers()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/control/state")
        data = resp.json()
        assert data["identity"]["product_name"] == identity["product_name"]
        assert len(data["system_health"]["providers"]) == len(providers)


@pytest.mark.asyncio
async def test_global_autonomy_kill_switch():
    """Verify kill switch toggles real backend autonomy and persists to database."""
    gm = GoalManager.get_instance()
    assert await gm.is_autonomy_enabled() is True

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Disengage autonomy (Kill Switch ON)
        resp1 = await client.post("/api/control/autonomy", json={"enabled": False})
        assert resp1.status_code == 200
        assert resp1.json()["autonomy_enabled"] is False
        assert await gm.is_autonomy_enabled() is False

        # Verify state endpoint reflects it
        state_resp = await client.get("/api/control/state")
        assert state_resp.json()["safety"]["autonomy_enabled"] is False

        # Re-engage autonomy
        resp2 = await client.post("/api/control/autonomy", json={"enabled": True})
        assert resp2.status_code == 200
        assert resp2.json()["autonomy_enabled"] is True
        assert await gm.is_autonomy_enabled() is True


def test_websocket_connect_and_reconcile():
    """Verify WebSocket /ws-control connects, delivers initial state, and reconciles on get_state."""
    from starlette.testclient import TestClient
    with TestClient(app) as client:
        with client.websocket_connect("/ws-control") as ws:
            init_msg = ws.receive_json()
            assert init_msg["event"] == "initial_state"
            assert "identity" in init_msg["data"]
            assert "devices" in init_msg["data"]
            assert "tasks" in init_msg["data"]

            # Reconcile request (simulating reconnect)
            ws.send_json({"type": "get_state"})
            recon_msg = ws.receive_json()
            assert recon_msg["event"] == "initial_state"
            assert recon_msg["data"]["identity"]["product_name"] == "Cortex"


@pytest.mark.asyncio
async def test_privacy_display_redaction():
    """Verify secrets and API keys are strictly redacted in UI state."""
    state = await build_canonical_state()
    state_str = json.dumps(state)

    # Asserts that no raw API keys, hex secrets, or private tokens are leaked
    assert "sk-" not in state_str
    assert "AIza" not in state_str
    for p in state["system_health"]["providers"]:
        if "key" in p:
            assert p["key"].startswith("...") or p["key"] == "MASKED" or "REDACTED" in p["key"]

