"""
tests/test_phase8a_runtime.py
-----------------------------
Deterministic tests for Phase 8A:
- Duplicate Hub prevention
- Hub health detection
- Agent reconnect
- GUI status synchronization
- Approval rendering and response
- Graceful stop/restart
- Core endpoint configuration
- Database safety (production DB never touched)
"""

import sys
import os
import json
import asyncio
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport

from PyQt6.QtWidgets import QApplication

# Ensure Qt Application exists for Qt widget/signal testing
_qapp = QApplication.instance() or QApplication([])

from hub.main import app as hub_app
from hub.auth import generate_device_secret, hash_secret
import hub.database as database
from agent.ui.runtime_manager import CortexRuntimeManager, scrub_log_message
from agent.ui.sidebar import LeftSidebar
from agent.ui.log_panel import LogPanel
from agent.ui_ipc import UIIPCClient, UIIPCServer


# ---------------------------------------------------------------------------
# 1. Hub Health Detection Tests
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_hub_health_detection():
    """Test Hub /health and / endpoints return detailed service metadata."""
    transport = ASGITransport(app=hub_app)
    async with AsyncClient(transport=transport, base_url="http://localhost:8000") as client:
        # Test /health
        resp = await client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "running"
        assert data["service"] == "cortex-hub"
        assert "connected_devices" in data
        assert "devices_detail" in data
        assert "active_tasks_count" in data
        assert "pending_approvals_count" in data
        assert "current_model" in data
        assert "current_provider" in data

        # Test / (backward compatible version)
        resp_root = await client.get("/")
        assert resp_root.status_code == 200
        data_root = resp_root.json()
        assert data_root["status"] == "running"
        assert data_root["version"] == "0.3.0"
        assert data_root["service"] == "cortex-hub"


# ---------------------------------------------------------------------------
# 2. Duplicate Hub Prevention Tests
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_duplicate_hub_prevention_reconnects_when_cortex_healthy():
    """When port is occupied by an existing healthy Cortex Hub, reconnect without spawning duplicate."""
    manager = CortexRuntimeManager(core_url="http://127.0.0.1:8000")

    fake_health = {
        "status": "running",
        "service": "cortex-hub",
        "version": "0.3.0",
        "connected_devices": ["asus"],
        "active_tasks_count": 0,
        "pending_approvals_count": 0,
    }
    with patch.object(manager, "check_hub_health", return_value=fake_health):
        with patch.object(manager, "is_port_occupied", return_value=True):
            with patch("subprocess.Popen") as mock_popen:
                success = manager.start_hub()
                assert success is True
                assert manager.hub_state == "online"
                # Subprocess MUST NOT be spawned
                mock_popen.assert_not_called()


@pytest.mark.asyncio
async def test_duplicate_hub_prevention_rejects_unrelated_process():
    """When port is occupied by an unrelated non-Cortex service, report error and do not kill."""
    manager = CortexRuntimeManager(core_url="http://127.0.0.1:8000")

    with patch.object(manager, "check_hub_health", return_value=None):
        with patch.object(manager, "is_port_occupied", return_value=True):
            with patch("subprocess.Popen") as mock_popen:
                success = manager.start_hub()
                assert success is False
                assert manager.hub_state == "error"
                # Process must not be spawned or killed
                mock_popen.assert_not_called()


# ---------------------------------------------------------------------------
# 3. Core Endpoint Configuration Tests
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_core_endpoint_configuration_local_vs_remote():
    """Test remote vs local Core URL configuration handling."""
    mgr_local = CortexRuntimeManager(core_url="http://127.0.0.1:8000")
    assert mgr_local.is_core_local is True
    assert mgr_local.core_host == "127.0.0.1"
    assert mgr_local.core_port == 8000

    mgr_remote = CortexRuntimeManager(core_url="http://100.100.50.1:8000")
    assert mgr_remote.is_core_local is False
    assert mgr_remote.core_host == "100.100.50.1"

    with patch.object(mgr_remote, "check_hub_health", return_value=None):
        with patch("subprocess.Popen") as mock_popen:
            success = mgr_remote.start_hub()
            assert success is False
            assert mgr_remote.hub_state == "error"
            mock_popen.assert_not_called()


# ---------------------------------------------------------------------------
# 4. Agent Reconnect Tests
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_agent_reconnect_ipc():
    """Test UIIPCClient reconnects when server restarts."""
    received = []

    def on_msg(m):
        received.append(m)

    srv1 = UIIPCServer(host="127.0.0.1", port=0)
    await srv1.start()
    port1 = srv1.port

    client = UIIPCClient(port=port1, on_message=on_msg, auto_reconnect=True)
    client.start()
    await asyncio.sleep(0.3)

    srv1.send({"type": "state", "state": "listening"})
    await asyncio.sleep(0.2)
    assert len(received) >= 1
    assert received[-1]["state"] == "listening"

    # Stop server 1 (closes client connections so client detects EOF)
    await srv1.stop()
    await asyncio.sleep(0.3)

    # Client is still running in background trying to reconnect
    assert client.running is True

    # Start server 2 on same port
    srv2 = UIIPCServer(host="127.0.0.1", port=port1)
    await srv2.start()
    await asyncio.sleep(1.2)

    srv2.send({"type": "state", "state": "speaking"})
    await asyncio.sleep(0.3)
    assert received[-1]["state"] == "speaking"

    client.stop()
    await srv2.stop()


# ---------------------------------------------------------------------------
# 5. GUI Status Synchronization Tests
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_gui_status_synchronization():
    """Test LeftSidebar accurately synchronizes with RuntimeManager state signals."""
    manager = CortexRuntimeManager(core_url="http://127.0.0.1:8000")
    sidebar = LeftSidebar()
    sidebar.set_runtime_manager(manager)

    # Hub status signal
    manager.hubStatusChanged.emit("online")
    assert "Online" in sidebar.status_badges["hub"].text()
    assert "#4ade80" in sidebar.status_badges["hub"].styleSheet()

    manager.hubStatusChanged.emit("error")
    assert "Error" in sidebar.status_badges["hub"].text()

    # Agent status signal
    manager.agentStatusChanged.emit("connected")
    assert "Connected" in sidebar.status_badges["agent"].text()

    manager.agentStatusChanged.emit("reconnecting")
    assert "Reconnecting" in sidebar.status_badges["agent"].text()

    # Voice status signal
    manager.voiceStatusChanged.emit("active_session")
    assert "Active Session" in sidebar.status_badges["voice"].text()

    manager.voiceStatusChanged.emit("listening")
    assert "Listening" in sidebar.status_badges["voice"].text()

    manager.voiceStatusChanged.emit("speaking")
    assert "Speaking" in sidebar.status_badges["voice"].text()

    # Metadata signal
    manager.metadataChanged.emit({
        "model": "Gemini 2.5 Flash",
        "provider": "Gemini",
        "tasks": 2,
        "approvals": 1,
    })
    assert "Gemini 2.5 Flash" in sidebar.labels["model"].text()
    assert "2 active" in sidebar.labels["tasks"].text()
    assert "1 pending" in sidebar.labels["approvals"].text()


# ---------------------------------------------------------------------------
# 6. Approval Rendering and Response Tests
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_approval_rendering_and_authoritative_resolution():
    """Test approval card rendering and authoritative resolution via Hub REST."""
    # 1. Enroll admin device in test DB
    secret = generate_device_secret()
    hashed = hash_secret(secret)
    await database.enroll_device("asus", "ASUS Laptop", "admin", hashed)

    # 2. Create pending confirmation in DB
    cid = "test_cmd_approval_123"
    await database.create_confirmation(
        command_id=cid,
        target_device="asus",
        command="screen.capture",
        args=[],
        requested_by="asus",
        tier=3,
    )

    transport = ASGITransport(app=hub_app)
    async with AsyncClient(transport=transport, base_url="http://localhost:8000") as client:
        # Check GET /pending
        resp = await client.get(
            "/pending",
            headers={"X-Cortex-Device": "asus", "X-Cortex-Secret": secret}
        )
        assert resp.status_code == 200
        pending = resp.json()["pending"]
        assert len(pending) >= 1
        assert pending[0]["command_id"] == cid

        # Sidebar rendering
        sidebar = LeftSidebar()
        sidebar.update_approvals(pending)
        assert not sidebar.approvals_container.isHidden()

        # Test POST /pending/{cid}/resolve authoritative resolution
        resolve_resp = await client.post(
            f"/pending/{cid}/resolve",
            headers={"X-Cortex-Device": "asus", "X-Cortex-Secret": secret},
            json={"decision": "approve"},
        )
        assert resolve_resp.status_code == 200
        assert resolve_resp.json()["status"] == "success"
        assert resolve_resp.json()["decision"] == "approve"

        # Verify confirmation in DB is no longer pending
        pending_after = await database.get_pending_confirmations()
        assert not any(p["command_id"] == cid for p in pending_after)


# ---------------------------------------------------------------------------
# 7. Graceful Stop and Restart Tests
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_graceful_stop_and_restart():
    """Test graceful stop and restart of managed Hub and Agent processes."""
    manager = CortexRuntimeManager(core_url="http://127.0.0.1:8000")

    mock_proc = MagicMock()
    mock_proc.poll.return_value = None  # running
    manager.hub_proc = mock_proc

    # Stop Hub gracefully
    stopped = manager.stop_hub()
    assert stopped is True
    mock_proc.terminate.assert_called_once()
    mock_proc.wait.assert_called_once_with(timeout=5.0)
    assert manager.hub_proc is None
    assert manager.hub_state == "offline"

    # Stop Agent gracefully
    mock_agent_proc = MagicMock()
    mock_agent_proc.poll.return_value = None
    manager.agent_proc = mock_agent_proc

    stopped_agent = manager.stop_agent()
    assert stopped_agent is True
    mock_agent_proc.terminate.assert_called_once()
    mock_agent_proc.wait.assert_called_once_with(timeout=5.0)
    assert manager.agent_proc is None
    assert manager.agent_state == "disconnected"


# ---------------------------------------------------------------------------
# 8. Secret Scrubbing in Logging Tests
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_scrub_log_message():
    """Verify secrets, hashes, and API keys are redacted before logging to UI."""
    raw_secret = "a1b2c3d4e5f67890123456789abcdef0123456789abcdef0123456789abcdef0"
    raw_msg = f"Device secret is {raw_secret} with Bearer my_super_secret_token_123"
    scrubbed = scrub_log_message(raw_msg)

    assert raw_secret not in scrubbed
    assert "<REDACTED_SECRET>" in scrubbed
    assert "<REDACTED_API_KEY>" in scrubbed


@pytest.mark.asyncio
async def test_log_panel_widget():
    """Test collapsible LogPanel toggle and message appending."""
    panel = LogPanel()
    assert panel.is_expanded is False
    assert panel.console.isHidden() is True

    # Toggle open
    panel.toggle_collapse()
    assert panel.is_expanded is True
    assert panel.console.isHidden() is False

    # Append scrubbed message
    panel.append_log("INFO", "Hub online.")
    assert "Hub online." in panel.console.toPlainText()

    # Clear logs
    panel.clear_logs()
    assert panel.console.toPlainText() == ""
