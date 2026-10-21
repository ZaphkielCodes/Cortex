import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from hub.agent_turn import AgentTurn, AgentTurnResult
from hub.database import add_task, get_task
from hub.device_handle import DeviceHandle
from hub.executor_registry import ExecutorRegistry


async def setup_wait():
    registry = ExecutorRegistry.get_instance()
    registry.register_device(DeviceHandle(device_id="phone", display_name="Android"))
    registry.set_device_online("phone", False)
    await add_task({"id": "waiting", "title": "Work", "type": "agent_turn", "status": "RUNNING",
        "assigned_device": "source", "command": "agent_turn", "context": {"request_id": "original"}})
    return registry


@pytest.mark.asyncio
async def test_offline_wait_excludes_idle_time_from_budget_and_resumes_once():
    registry = await setup_wait()
    turn = AgentTurn(SimpleNamespace(), None)
    turn.TOTAL_SECONDS = .3
    sent = []
    async def loop(*_):
        assert await turn._wait_for_executor("waiting", "phone")
        sent.append("phone")
        return AgentTurnResult("success", "Verified")
    turn._loop = loop
    job = asyncio.create_task(turn._finish("waiting", "Work", "source", {}))
    await asyncio.sleep(.4)
    stored = await get_task("waiting")
    assert stored["status"] == "WAITING" and not sent and not job.done()
    assert json.loads(stored["context"])["waiting_device"] == "phone"
    registry.set_device_online("phone", True)
    result = await asyncio.wait_for(job, 2)
    assert result.status == "success" and sent == ["phone"]
    assert (await get_task("waiting"))["status"] == "COMPLETED"


@pytest.mark.asyncio
async def test_wait_can_be_cancelled_without_dispatching():
    await setup_wait()
    turn = AgentTurn(SimpleNamespace(), None)
    async def loop(*_):
        await turn._wait_for_executor("waiting", "phone")
        raise AssertionError("Cancelled wait must not dispatch")
    turn._loop = loop
    job = asyncio.create_task(turn._finish("waiting", "Work", "source", {}))
    await asyncio.sleep(.02)
    job.cancel()
    assert (await job).status == "cancelled"
    assert (await get_task("waiting"))["status"] == "CANCELLED"


@pytest.mark.asyncio
async def test_restart_wait_reports_interruption_without_replaying():
    await setup_wait()
    from hub.database import update_task
    from hub.task_orchestrator import TaskOrchestrator
    await update_task("waiting", {"status": "WAITING", "context": {"waiting_device": "phone", "request": "Work"}})
    control = SimpleNamespace(execute_and_observe=AsyncMock())
    await TaskOrchestrator(control_layer=control).reload_active_tasks()
    result = json.loads((await get_task("waiting"))["context"])["result"]
    assert result["data"]["error_code"] == "hub_restart_unverified"
    control.execute_and_observe.assert_not_awaited()
