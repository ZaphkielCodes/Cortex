"""Task result/session correlation and restart recovery without model or device actions."""
import asyncio
import json
from dataclasses import asdict
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from hub.agent_turn import AgentTurn
from hub.intent_schema import ModelResult
from hub.session_context import SessionContextManager as Sessions


def reply(status, message, task_id="turn_test"):
    return {"status": status, "message": message, "data": {"task_id": task_id}}


@pytest.mark.asyncio
async def test_ack_completion_and_duplicate_result_share_one_persisted_turn():
    speaker = {"speaker_id": "person1", "label": "Original speaker"}
    await Sessions.update_session("source", "Do work", reply("in_progress", "Working"), speaker_info=speaker)
    original_time = (await Sessions.get_session("source"))["turns"][0]["timestamp"]
    await Sessions.update_session("source", "Do work", reply("success", "Finished"))
    await Sessions.update_session("source", "Do work", reply("success", "Finished"))
    Sessions._memory_cache.clear()
    turns = (await Sessions.get_session("source"))["turns"]
    assert len(turns) == 1
    assert turns[0]["response_message"] == "Finished"
    assert turns[0]["task_id"] == "turn_test"
    assert turns[0]["timestamp"] == original_time
    assert turns[0]["speaker_id"] == "person1"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["success", "error", "cancelled", "denied"])
async def test_late_ack_never_overwrites_a_final_result(status):
    await Sessions.update_session("source", "Do work", reply(status, "Final result"))
    await Sessions.update_session("source", "Do work", reply("in_progress", "Working"))
    turns = (await Sessions.get_session("source"))["turns"]
    assert len(turns) == 1 and turns[0]["response_status"] == status


@pytest.mark.asyncio
async def test_concurrent_updates_with_empty_cache_preserve_completion():
    await asyncio.gather(
        Sessions.update_session("source", "Do work", reply("in_progress", "Working")),
        Sessions.update_session("source", "Do work", reply("success", "Finished")))
    Sessions._memory_cache.clear()
    turns = (await Sessions.get_session("source"))["turns"]
    assert len(turns) == 1 and turns[0]["response_status"] == "success"


@pytest.mark.asyncio
async def test_late_ack_can_supply_original_speaker_without_downgrading_result():
    await Sessions.update_session("source", "Do work", reply("success", "Final result"))
    await Sessions.update_session("source", "Do work", reply("in_progress", "Working"),
        speaker_info={"speaker_id": "original", "label": "Original speaker"})
    turn = (await Sessions.get_session("source"))["turns"][0]
    assert turn["response_status"] == "success" and turn["speaker_id"] == "original"


@pytest.mark.asyncio
async def test_same_words_different_tasks_remain_distinct_and_source_scoped():
    await Sessions.update_session("source", "Repeat", reply("success", "First", "one"))
    await Sessions.update_session("source", "Repeat", reply("success", "Second", "two"))
    await Sessions.update_session("other", "Repeat", reply("success", "Other source", "one"))
    assert len((await Sessions.get_session("source"))["turns"]) == 2
    assert len((await Sessions.get_session("other"))["turns"]) == 1


@pytest.mark.asyncio
async def test_busy_reply_cannot_replace_the_original_task_request():
    await Sessions.update_session("source", "Original request", reply("in_progress", "Working"))
    await Sessions.update_session("source", "Another request", reply("in_progress", "Still working"))
    turns = (await Sessions.get_session("source"))["turns"]
    assert len(turns) == 1 and turns[0]["request"] == "Original request"


class GatedModel:
    locality = "local_device"
    name = "recovery_contract_probe"

    def __init__(self, release):
        self.release = release

    async def generate_result(self, *args, **kwargs):
        await self.release.wait()
        return ModelResult(type="RESPONSE", response_text=json.dumps({
            "decision": "RESPOND", "response_text": "Finished response"}))


@pytest.mark.asyncio
async def test_real_background_completion_updates_ack_and_keeps_request_correlation():
    from hub.database import get_task
    release = asyncio.Event()
    agent = AgentTurn(SimpleNamespace(connected_devices={}), GatedModel(release))
    agent.ACK_SECONDS = .001
    ack = await agent.run("Explain compilers", "source", {}, request_id="voice-request")
    assert ack.status == "in_progress"
    notify = AgentTurn._running[ack.task_id + "_notify"]
    await Sessions.update_session("source", "Explain compilers", asdict(ack))
    release.set()
    await asyncio.wait_for(notify, 2)
    saved = json.loads((await get_task(ack.task_id))["context"])
    assert saved["request_id"] == "voice-request"
    assert saved["request"] == "Explain compilers"
    assert saved["result"]["task_id"] == ack.task_id
    turns = (await Sessions.get_session("source"))["turns"]
    assert len(turns) == 1 and turns[0]["response_message"] == "Finished response"


@pytest.mark.asyncio
async def test_restart_persists_readable_error_updates_original_turn_and_never_replays():
    from hub.database import add_task, get_task
    from hub.task_orchestrator import TaskOrchestrator
    await Sessions.update_session("source", "Original full request", reply("in_progress", "Working"))
    await add_task({"id": "turn_test", "title": "Short title", "type": "agent_turn", "status": "RUNNING",
        "command": "agent_turn", "assigned_device": "source", "context": {"request_id": "request-123"}})
    control = SimpleNamespace(execute_and_observe=AsyncMock())
    orchestrator = TaskOrchestrator(control_layer=control)
    await orchestrator.reload_active_tasks()
    await orchestrator.reload_active_tasks()
    task = await get_task("turn_test")
    context = json.loads(task["context"])
    assert task["status"] == "WAITING"
    assert context["request_id"] == "request-123"
    assert context["result"]["data"]["error_code"] == "hub_restart_unverified"
    assert context["result"]["status"] == "error"
    turns = (await Sessions.get_session("source"))["turns"]
    assert len(turns) == 1 and turns[0]["request"] == "Original full request"
    assert "unverified" in turns[0]["response_message"]
    control.execute_and_observe.assert_not_awaited()
    status = await AgentTurn(control, None)._task_status("source")
    assert status.status == "error" and status.task_id == "turn_test"


@pytest.mark.asyncio
async def test_restart_preserves_completed_and_approval_waiting_tasks():
    from hub.database import add_task, get_task
    from hub.task_orchestrator import TaskOrchestrator
    for task_id, status in [("done", "COMPLETED"), ("approval", "WAITING")]:
        await add_task({"id": task_id, "title": "Request", "type": "agent_turn", "status": status,
            "command": "agent_turn", "assigned_device": "source", "context": {"marker": task_id}})
    await TaskOrchestrator(control_layer=SimpleNamespace()).reload_active_tasks()
    for task_id, status in [("done", "COMPLETED"), ("approval", "WAITING")]:
        task = await get_task(task_id)
        assert task["status"] == status
        assert json.loads(task["context"]) == {"marker": task_id}


@pytest.mark.asyncio
@pytest.mark.parametrize("status,stored_status", [("success", "COMPLETED"), ("cancelled", "CANCELLED")])
async def test_restart_reconciles_a_persisted_result_when_notification_was_lost(status, stored_status):
    from hub.database import add_task, get_task
    from hub.task_orchestrator import TaskOrchestrator
    await Sessions.update_session("source", "Do work", reply("in_progress", "Working"))
    await add_task({"id": "turn_test", "title": "Do work", "type": "agent_turn", "status": stored_status,
        "command": "agent_turn", "assigned_device": "source", "context": {"result": reply(status, "Persisted final result")}})
    await TaskOrchestrator(control_layer=SimpleNamespace()).reload_active_tasks()
    assert (await get_task("turn_test"))["status"] == stored_status
    turns = (await Sessions.get_session("source"))["turns"]
    assert len(turns) == 1 and turns[0]["response_status"] == status
    assert turns[0]["response_message"] == "Persisted final result"


@pytest.mark.asyncio
async def test_restart_handles_malformed_legacy_task_context():
    from hub.database import add_task, get_task, get_db
    from hub.task_orchestrator import TaskOrchestrator
    await add_task({"id": "broken", "title": "Old request", "type": "agent_turn", "status": "RUNNING",
        "command": "agent_turn", "assigned_device": "source", "context": {}})
    async with get_db() as db:
        await db.execute("UPDATE tasks SET context = ? WHERE id = ?", ("broken-json", "broken"))
        await db.commit()
    await TaskOrchestrator(control_layer=SimpleNamespace()).reload_active_tasks()
    assert json.loads((await get_task("broken"))["context"])["result"]["status"] == "error"
