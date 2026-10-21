"""Exercise dashboard state using real ownership records and delayed GUI turns."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
import httpx
from hub.main import app


@pytest.fixture(autouse=True)
def authenticated_component_session(monkeypatch):
    monkeypatch.setattr("hub.main._authenticate_http_headers", AsyncMock(
        return_value={"device_id": "asus", "role": "admin"}))
from hub.api_control import build_canonical_state
from hub.task_ownership import TaskOwnershipManager
from hub.executor_registry import ExecutorRegistry
from hub.database import add_task, get_task
from hub.agent_turn import AgentTurn
from test_agent_turn_runtime import ScriptedModel, call, control


@pytest.mark.asyncio
async def test_dashboard_state_survives_real_completed_execution():
    owner=TaskOwnershipManager.get_instance()
    execution=await owner.create_execution(goal_id="turn_real",executor_device_id="test-node",
        owner_device_id="test-node",required_capabilities=["ui.observe"],movable=False)
    execution.execution_state="COMPLETED"
    await owner.release_lease(execution.task_execution_id,execution.lease_id)
    registry=ExecutorRegistry.get_instance()
    registry.on_device_connected("test-node",object())
    registry.on_device_authenticated("test-node",["ui.observe"],role="trusted_worker")
    await add_task({"id":"turn_real","title":"A real GUI turn","type":"agent_turn",
        "command":"agent_turn","assigned_device":"test-node","status":"COMPLETED",
        "context":{"result":{"status":"success","message":"The verified final answer."}}})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url="http://test") as client:
        response=await client.get("/api/control/state")
    assert response.status_code==200
    data=response.json()
    step=next(t for t in data["tasks"] if t["id"]==execution.task_execution_id)
    assert step["status"]=="COMPLETED"
    assert step["executor"]=="test-node"
    assert step["progress"]==1.0
    assert step["parent_task_id"]=="turn_real"
    assert next(d for d in data["devices"] if d["device_id"]=="test-node")["online"]
    assert next(t for t in data["tasks"] if t["id"]=="turn_real")["status"]=="COMPLETED"


@pytest.mark.asyncio
async def test_delayed_typed_turn_is_retrievable_without_voice_delivery():
    from hub import api_control, api_tasks
    from hub.agent_harness import AgentHarness
    done=asyncio.Event()
    ctrl=control([])
    ctrl.connected_devices["asus"]=SimpleNamespace(send_text=AsyncMock())
    async def execute(*args):
        await done.wait()
        return {"status":"success","data":{"cpu_percent":23}}
    ctrl.execute_and_observe.side_effect=execute
    harness=AgentHarness(ctrl,ScriptedModel([call("telemetry.get_system_usage")],"CPU is at 23%."))
    from hub.request_lifecycle import RequestLifecycle
    with patch.object(RequestLifecycle,"ACK_SECONDS",.01),patch("hub.main._agent_harness",harness,create=True), \
         patch("hub.context.ContextManager.get_active_context",AsyncMock(return_value={})), \
         patch("hub.api_tasks._auth_wrapper",AsyncMock(return_value={"device_id":"asus"})):
        ack=await api_control.send_message_endpoint(api_control.MessageRequest(text="CPU usage rn",device_id="asus"))
        assert ack["status"]=="in_progress"
        job=RequestLifecycle._jobs[ack["task_id"]].task
        done.set()
        await job
        await asyncio.gather(*list(AgentTurn._running.values()))
        task=await api_tasks.retrieve_task(ack["task_id"])
    import json
    result=json.loads(task["context"])["result"]
    assert result["status"]=="success"
    assert result["message"]=="CPU is at 23%."
    assert result["task_id"]==ack["task_id"]
    assert ctrl.execute_and_observe.await_count==1
    ctrl.connected_devices["asus"].send_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_failed_gui_turn_keeps_failure_in_saved_result():
    agent=AgentTurn(control([]),ScriptedModel([RuntimeError("offline")]))
    agent.ACK_SECONDS=.001
    result=await agent.run("An ordinary request","asus",{"response_channel":"gui"})
    if result.is_async:
        result=await AgentTurn._running[result.task_id]
        await asyncio.gather(*list(AgentTurn._running.values()))
    import json
    task=await get_task(result.task_id)
    assert task["status"]=="FAILED"
    assert json.loads(task["context"])["result"]["status"]=="error"
