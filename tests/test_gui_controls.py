"""Desktop controls: real transport contracts with isolated hub/database boundaries."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import httpx
import pytest
from fastapi import HTTPException

from agent.ui.control_transport import DesktopControlTransport


@pytest.fixture
def desktop():
    runtime = SimpleNamespace(core_url="http://localhost:8000", device_id="actual-source",
        hub_state="offline", agent_state="disconnected", voice_state="idle",
        model_name="unknown", provider_name="unknown", hub_proc=None, agent_proc=None,
        get_device_secret=Mock(return_value="test-credential"), start_cortex=Mock(return_value=True),
        stop_cortex=Mock(return_value=True), restart_agent=Mock(return_value=True), restart_hub=Mock(return_value=True))
    ipc = SimpleNamespace(connected=False, send=Mock())
    transport = DesktopControlTransport(runtime, ipc, Mock())
    yield transport
    transport.close()


@pytest.mark.parametrize("method,path", [("GET", "https://external.example/"),
    ("GET", "/memory/../secrets"), ("DELETE", "/memory/id"),
    ("POST", "/desktop/shell"), ("POST", "/tasks/id/../../run")])
def test_transport_rejects_non_allowlisted_operations(desktop, method, path):
    with patch("agent.ui.control_transport.httpx.Client") as client:
        with pytest.raises(ValueError, match="not supported"):
            desktop.perform(method, path)
        client.assert_not_called()


def test_source_identity_is_native_and_target_is_separate(desktop):
    with patch("agent.ui.control_transport.httpx.Client") as factory:
        client = factory.return_value.__enter__.return_value
        client.request.return_value = httpx.Response(200, json={"status": "in_progress", "task_id": "turn"})
        result = desktop.perform("POST", "/api/control/message", {
            "device_id": "forged-source", "target_device": "other-device", "text": "test"})
        args, kwargs = client.request.call_args
        assert args == ("POST", "http://localhost:8000/api/control/message")
        assert kwargs["json"]["device_id"] == "actual-source"
        assert kwargs["json"]["target_device"] == "other-device"
        assert kwargs["headers"]["X-Cortex-Secret"] == "test-credential"
        assert "test-credential" not in str(result)


def test_approval_uses_authenticated_resolver_once(desktop):
    with patch("agent.ui.control_transport.httpx.Client") as factory:
        client = factory.return_value.__enter__.return_value
        client.request.return_value = httpx.Response(200, json={"status": "success"})
        desktop.perform("POST", "/api/control/approvals/id/resolve", {"decision": "deny"})
        assert client.request.call_count == 1
        assert client.request.call_args.args[1].endswith("/pending/id/resolve")
        desktop.ipc.send.assert_not_called()


def test_missing_enrollment_and_http_failure_never_report_success(desktop):
    desktop.runtime.get_device_secret.return_value = None
    with pytest.raises(ValueError, match="credential"):
        desktop.perform("GET", "/memory/")
    desktop.runtime.get_device_secret.return_value = "test-credential"
    with patch("agent.ui.control_transport.httpx.Client") as factory:
        factory.return_value.__enter__.return_value.request.return_value = httpx.Response(403, json={"detail": "Denied"})
        with pytest.raises(ValueError, match="Denied.*403"):
            desktop.perform("POST", "/memory/", {})


def test_offline_runtime_state_and_voice_delivery(desktop):
    assert desktop.perform("GET", "/desktop/state")["hub"] == "offline"
    with pytest.raises(ValueError, match="disconnected"):
        desktop.perform("POST", "/desktop/voice", {"action": "mute"})
    desktop.ipc.send.assert_not_called()
    desktop.ipc.connected = True
    assert desktop.perform("POST", "/desktop/voice", {"action": "mute"})["status"] == "sent"
    desktop.ipc.send.assert_called_once_with({"type": "action", "action": "mute"})
    desktop.ipc.send.side_effect = lambda _: setattr(desktop.ipc, "connected", False)
    with pytest.raises(ValueError, match="could not be delivered"):
        desktop.perform("POST", "/desktop/voice", {"action": "unmute"})


@pytest.mark.parametrize("action", ["stop", "restart_hub", "restart_agent"])
def test_runtime_does_not_claim_control_of_external_processes(desktop, action):
    with pytest.raises(ValueError):
        desktop.perform("POST", "/desktop/runtime", {"action": action})
    desktop.runtime.stop_cortex.assert_not_called()
    desktop.runtime.restart_hub.assert_not_called()
    desktop.runtime.restart_agent.assert_not_called()


def test_runtime_start_available_while_hub_offline(desktop):
    desktop.perform("POST", "/desktop/runtime", {"action": "start"})
    desktop.runtime.start_cortex.assert_called_once()


def test_qt_bridge_returns_correlated_response_from_worker(desktop):
    import json
    from PyQt6.QtCore import QCoreApplication, QEventLoop, QTimer
    from agent.ui.bridge import Bridge
    app = QCoreApplication.instance() or QCoreApplication([])
    bridge = Bridge()
    bridge.configure_controls(desktop.runtime, desktop.ipc)
    replies = []
    loop = QEventLoop()
    def receive(raw):
        replies.append(json.loads(raw))
        loop.quit()
    bridge.controlResponse.connect(receive)
    bridge.controlRequest(json.dumps({"id": "qt-request", "method": "GET", "path": "/desktop/state"}))
    QTimer.singleShot(2000, loop.quit)
    loop.exec()
    bridge.transport.close()
    assert replies[0]["id"] == "qt-request"
    assert replies[0]["ok"] is True
    assert replies[0]["data"]["device_id"] == "actual-source"
    assert "test-credential" not in str(replies)


@pytest.mark.asyncio
async def test_memory_and_schedule_controls_persist_to_isolated_database():
    from hub import api_memory, api_tasks
    from hub.database import add_task, get_task
    auth = AsyncMock()
    with patch.object(api_memory, "_auth_wrapper", auth), patch.object(api_tasks, "_auth_wrapper", auth):
        saved = await api_memory.create_memory(api_memory.MemoryCreate(category="note", key="test", value="before"))
        await api_memory.update_memory(saved["id"], api_memory.MemoryUpdate(value="after"))
        rows = (await api_memory.list_memory(category=None))["memory"]
        assert any(row["id"] == saved["id"] and row["value"] == "after" for row in rows)
        await add_task({"id": "schedule", "type": "scheduled", "title": "Test", "command": "notify", "status": "pending"})
        await api_tasks.pause_task("schedule")
        assert (await get_task("schedule"))["status"] == "paused"
        await api_tasks.resume_task("schedule")
        assert (await get_task("schedule"))["status"] == "pending"
        assert auth.await_count == 5


@pytest.mark.asyncio
async def test_message_returns_background_id_and_updates_source_session():
    from hub import api_control
    from hub.request_lifecycle import RequestLifecycle
    from hub.intent_schema import ConversationalResponse
    release = asyncio.Event()
    async def finish(**kwargs):
        await release.wait()
        return ConversationalResponse(status="success", message="Finished")
    harness = SimpleNamespace(process_request=AsyncMock(side_effect=finish))
    with patch("hub.main._agent_harness", harness, create=True), \
         patch("hub.context.ContextManager.get_active_context", AsyncMock(return_value={})), \
         patch.object(RequestLifecycle, "ACK_SECONDS", .01), \
         patch("hub.session_context.SessionContextManager.update_session", AsyncMock()) as session:
        response = await api_control.send_message_endpoint(api_control.MessageRequest(
            text="test", device_id="source", target_device="target"))
        assert response["status"] == "in_progress" and response["task_id"].startswith("request_")
        assert harness.process_request.call_args.kwargs["source_device_id"] == "source"
        assert harness.process_request.call_args.kwargs["context"]["requested_device"] == "target"
        release.set()
        await asyncio.gather(*(record.task for record in list(RequestLifecycle._jobs.values())))
        await asyncio.sleep(0)
        await asyncio.gather(*list(RequestLifecycle._deliveries))
        assert session.call_args.args[0] == "source"


@pytest.mark.asyncio
async def test_cancel_task_cancels_agent_turn_and_unknown_id_is_404():
    from hub import api_control
    from hub.database import add_task, get_task
    from hub.agent_turn import AgentTurn
    await add_task({"id": "turn", "type": "agent_turn", "title": "Test", "command": "test", "status": "RUNNING"})
    with patch.object(AgentTurn, "cancel_task") as cancel, \
         patch.object(api_control, "build_canonical_state", AsyncMock(return_value={"tasks": []})):
        await api_control.stop_task_endpoint("turn")
        cancel.assert_called_once_with("turn")
        assert (await get_task("turn"))["status"] == "CANCELLED"
        with pytest.raises(HTTPException) as error:
            await api_control.stop_task_endpoint("missing")
        assert error.value.status_code == 404


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["COMPLETED", "FAILED", "CANCELLED", "DENIED", "interrupted"])
async def test_stale_legacy_cancel_preserves_durable_outcome(status):
    from hub import api_control
    from hub.database import add_task, get_task
    from hub.agent_turn import AgentTurn
    from hub.request_lifecycle import RequestLifecycle
    await add_task({"id": "finished-request", "type": "request", "title": "Finished",
        "command": "request", "status": status,
        "context": {"result": {"status": "success", "message": "Recorded outcome"}}})
    before = await get_task("finished-request")
    with patch.object(AgentTurn, "cancel_task") as turn, \
         patch.object(RequestLifecycle, "cancel_task") as request, \
         patch.object(api_control, "broadcast_control", AsyncMock()) as broadcast:
        with pytest.raises(HTTPException) as error:
            await api_control.stop_task_endpoint("finished-request")
    assert error.value.status_code == 409
    assert await get_task("finished-request") == before
    turn.assert_not_called()
    request.assert_not_called()
    broadcast.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["goal", "execution"])
@pytest.mark.parametrize("status", ["COMPLETED", "FAILED"])
async def test_stale_cancel_does_not_reopen_goals_or_execution_receipts(kind, status):
    from hub import api_control
    goal = SimpleNamespace(status=status) if kind == "goal" else None
    execution = SimpleNamespace(execution_state=status) if kind == "execution" else None
    goals = SimpleNamespace(get_goal=AsyncMock(return_value=goal), cancel_goal=AsyncMock())
    ownership = SimpleNamespace(get_execution=AsyncMock(return_value=execution), expire_lease=AsyncMock())
    with patch.object(api_control.GoalManager, "get_instance", return_value=goals), \
         patch.object(api_control.TaskOwnershipManager, "get_instance", return_value=ownership), \
         patch("hub.database.get_task", AsyncMock(return_value=None)):
        with pytest.raises(HTTPException) as error:
            await api_control.stop_task_endpoint("already-ended")
    assert error.value.status_code == 409
    goals.cancel_goal.assert_not_awaited()
    ownership.expire_lease.assert_not_awaited()
    if execution:
        assert execution.execution_state == status


@pytest.mark.asyncio
async def test_http_kill_switch_cancels_agent_turns():
    from hub import api_control
    from hub.agent_turn import AgentTurn
    manager = SimpleNamespace(set_autonomy_enabled=AsyncMock(), pause_all_goals=AsyncMock())
    with patch.object(api_control.GoalManager, "get_instance", return_value=manager), \
         patch.object(AgentTurn, "_devices", {"source": "turn"}), \
         patch.object(AgentTurn, "cancel_for_device") as cancel:
        await api_control.set_autonomy_endpoint(api_control.AutonomyRequest(enabled=False))
        cancel.assert_called_once_with("source")
        manager.pause_all_goals.assert_awaited_once()
