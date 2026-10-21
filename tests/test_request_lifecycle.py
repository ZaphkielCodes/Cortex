"""Real async boundaries with simulated providers/devices; no external API calls."""
import asyncio
import json
import threading
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio

from hub.database import get_task
from hub.intent_schema import ConversationalResponse
from hub.request_lifecycle import RequestLifecycle


@pytest_asyncio.fixture(autouse=True)
async def lifecycle(monkeypatch):
    monkeypatch.setattr(RequestLifecycle, "ACK_SECONDS", .01)
    monkeypatch.setattr(RequestLifecycle, "PREPARATION_SECONDS", .5)
    yield
    jobs = [record.task for record in list(RequestLifecycle._jobs.values())]
    for job in jobs:
        job.cancel()
    await asyncio.gather(*jobs, return_exceptions=True)
    await asyncio.gather(*list(RequestLifecycle._deliveries), return_exceptions=True)
    RequestLifecycle._jobs.clear()


def harness(message="Finished"):
    return SimpleNamespace(process_request=AsyncMock(return_value=ConversationalResponse(status="success", message=message)))


async def settle():
    await asyncio.gather(*(record.task for record in list(RequestLifecycle._jobs.values())), return_exceptions=True)
    await asyncio.sleep(0)
    await asyncio.gather(*list(RequestLifecycle._deliveries), return_exceptions=True)


@pytest.mark.asyncio
async def test_slow_preparation_acknowledges_and_delivers_once():
    release = asyncio.Event()
    async def prepare():
        await release.wait()
        return {}
    model = harness()
    deliver = AsyncMock()
    started = time.monotonic()
    response = await RequestLifecycle.submit("source", "req", "an unforeseen request", prepare, model, deliver=deliver)
    assert response.status == "in_progress" and time.monotonic() - started < .3
    model.process_request.assert_not_awaited()
    task_id = response.data["task_id"]
    assert (await get_task(task_id))["status"] == "RUNNING"
    release.set()
    await settle()
    assert deliver.await_count == 1
    assert deliver.call_args.args[0] == task_id
    final = json.loads((await get_task(task_id))["context"])["result"]
    assert final["status"] == "success" and final["task_id"] == task_id


@pytest.mark.asyncio
async def test_duplicate_request_never_replays_preparation_or_execution():
    release = asyncio.Event()
    async def prepare():
        await release.wait()
        return {}
    prepare = AsyncMock(side_effect=prepare)
    model = harness()
    first = await RequestLifecycle.submit("source", "req", "same input", prepare, model)
    second = await RequestLifecycle.submit("source", "req", "same input", prepare, model)
    assert first.data["task_id"] == second.data["task_id"]
    release.set()
    await settle()
    cached = await RequestLifecycle.submit("source", "req", "same input", prepare, model)
    assert cached.status == "success" and model.process_request.await_count == 1
    assert prepare.await_count == 1
    wrong = await RequestLifecycle.submit("source", "req", "changed input", prepare, model)
    assert wrong.status == "error" and model.process_request.await_count == 1


@pytest.mark.asyncio
async def test_preparation_timeout_is_a_correlated_final_failure(monkeypatch):
    monkeypatch.setattr(RequestLifecycle, "PREPARATION_SECONDS", .03)
    async def prepare():
        await asyncio.Event().wait()
    model, deliver = harness(), AsyncMock()
    ack = await RequestLifecycle.submit("source", "req", "test", prepare, model, deliver=deliver)
    await settle()
    model.process_request.assert_not_awaited()
    assert deliver.call_args.args[0] == ack.data["task_id"]
    assert deliver.call_args.args[1].data["error_code"] == "request_preparation_timeout"
    assert (await get_task(ack.data["task_id"]))["status"] == "FAILED"


@pytest.mark.asyncio
async def test_cancel_while_preparing_does_not_start_model_or_action():
    async def prepare():
        await asyncio.Event().wait()
    model, deliver = harness(), AsyncMock()
    ack = await RequestLifecycle.submit("source", "req", "test", prepare, model, deliver=deliver)
    RequestLifecycle.cancel_task(ack.data["task_id"])
    await settle()
    assert (await get_task(ack.data["task_id"]))["status"] == "CANCELLED"
    model.process_request.assert_not_awaited()
    assert deliver.call_args.args[1].status == "cancelled"


@pytest.mark.asyncio
async def test_real_agent_turn_has_only_outer_ack_and_outer_final():
    from hub.agent_harness import AgentHarness
    from test_agent_turn_runtime import ScriptedModel, control
    release = asyncio.Event()
    class SlowModel(ScriptedModel):
        async def generate_result(self, text, context, on_chunk=None):
            await release.wait()
            return await super().generate_result(text, context, on_chunk)
    actual = AgentHarness(control([]), SlowModel([{"decision": "RESPOND", "response_text": "Hello"}]))
    deliver = AsyncMock()
    ack = await RequestLifecycle.submit("asus", "req", "hi", AsyncMock(return_value={}), actual, deliver=deliver)
    assert ack.status == "in_progress"
    release.set()
    await settle()
    assert deliver.await_count == 1 and deliver.call_args.args[0] == ack.data["task_id"]
    assert deliver.call_args.args[1].message == "Hello"


@pytest.mark.asyncio
async def test_voice_handler_does_not_wait_for_preparation_before_ack():
    from hub import main
    from hub.models import VoiceRequestMessage
    from agent.voice.response_router import route_voice_response
    release = asyncio.Event()
    async def context(**kwargs):
        await release.wait()
        return {}
    ws = SimpleNamespace(send_text=AsyncMock())
    model = harness()
    future = asyncio.get_running_loop().create_future()
    state = {"voice_futures": {"req": future}}
    with patch.object(main, "_agent_harness", model, create=True), patch("hub.context.ContextManager.get_active_context", context), \
            patch("hub.speakers.SpeakerManager.resolve_speaker", AsyncMock(return_value={})), \
            patch.dict(main.CONNECTED_DEVICES, {"source": ws}, clear=True):
        await main._process_voice_request(ws, "source", VoiceRequestMessage(command_id="req", text="hi"))
        ack = json.loads(ws.send_text.call_args.args[0])
        assert ack["status"] == "in_progress"
        assert route_voice_response(ack, state) == []
        assert future.done()
        release.set()
        await settle()
        final = json.loads(ws.send_text.call_args.args[0])
        assert final["is_task_result"] and final["task_id"] == ack["task_id"]
        assert route_voice_response(final, state) == ["Finished"]
        assert route_voice_response(final, state) == []


@pytest.mark.asyncio
async def test_dispatcher_keeps_event_loop_responsive_and_deduplicates():
    from agent.command_dispatcher import CommandDispatcher
    dispatcher = CommandDispatcher()
    entered, release = threading.Event(), threading.Event()
    calls = []
    def blocking():
        calls.append("action")
        entered.set()
        release.wait(2)
        return {"status": "success", "message": "Finished"}
    async def execute(pool):
        return await asyncio.get_running_loop().run_in_executor(pool, blocking)
    try:
        first = asyncio.create_task(dispatcher.dispatch("cmd", "arbitrary.capability", {}, execute))
        while not entered.is_set():
            await asyncio.sleep(.001)
        # This runs while the native action is still blocked.
        assert not first.done()
        second = asyncio.create_task(dispatcher.dispatch("cmd", "arbitrary.capability", {}, execute))
        await asyncio.sleep(.005)
        release.set()
        assert (await first)["status"] == "success" and await first == await second
        assert calls == ["action"]
        mismatch = await dispatcher.dispatch("cmd", "different.capability", {}, execute)
        assert mismatch["status"] == "failure" and calls == ["action"]
    finally:
        release.set()
        dispatcher.close()


def test_unknown_device_never_becomes_an_implicit_admin_or_online():
    from hub.executor_registry import ExecutorRegistry
    registry = ExecutorRegistry.get_instance()
    assert registry.get_device_handle("asus") is None
    assert not registry.is_device_online("asus")
    assert registry.list_device_handles() == []


@pytest.mark.asyncio
async def test_legacy_flag_cannot_enable_phrase_routing(monkeypatch):
    from hub.pipeline.pipeline import RequestPipeline
    from test_agent_turn_runtime import ScriptedModel, control
    monkeypatch.setenv("CORTEX_LEGACY_COMPATIBILITY", "1")
    ctrl = control([])
    response = await RequestPipeline(ctrl, ScriptedModel([{"decision": "RESPOND", "response_text": "Which app?"}])).execute(
        "asus", "please don't open anything rn", {})
    assert response.message == "Which app?"
    ctrl.execute_and_observe.assert_not_awaited()


@pytest.mark.asyncio
async def test_database_migration_never_promotes_a_device_by_name():
    from hub.auth import hash_secret
    from hub.database import enroll_device, get_device, init_db
    await enroll_device("asus", "A restricted enrolled device", "restricted", hash_secret("test-secret"))
    await init_db()
    device = await get_device("asus")
    assert device["role"] == "restricted" and device["trust_level"] != "admin"


@pytest.mark.asyncio
async def test_goal_planning_uses_model_output_and_never_keyword_fallback():
    from hub.planner import GoalPlanner
    from hub.goals import Goal
    from hub.intent_schema import ModelResult
    model = SimpleNamespace(locality="local_device", generate_result=AsyncMock(return_value=ModelResult(
        type="PLAN", plan={"steps": [{"capability": "ui.observe", "arguments": {}}]})))
    planner = GoalPlanner(model_router=model)
    goal = Goal(goal_id="test-goal", title="Unseen workflow", original_request="an entirely new workflow")
    result = await planner._generate_plan(goal, [], "source")
    assert result["steps"][0]["capability"] == "ui.observe"
    assert model.generate_result.await_count == 1
    model.generate_result.side_effect = ValueError("malformed output")
    failed = await planner._generate_plan(goal, [], "source")
    assert failed["steps"] == [] and failed["unachievable_reason"]
    assert planner._reason_from_capabilities("open Settings and type text")["steps"] == []


@pytest.mark.asyncio
async def test_private_goal_observation_cannot_leak_to_cloud_replanning():
    from hub.planner import GoalPlanner
    from hub.goals import Goal
    model = SimpleNamespace(locality="cloud_worker", generate_result=AsyncMock())
    planner = GoalPlanner(model_router=model)
    goal = Goal(goal_id="test-goal", title="Private work", original_request="continue")
    result = await planner._generate_plan(goal, [], "source", observation={"text": "private-canary"})
    assert not result["steps"]
    model.generate_result.assert_not_awaited()


@pytest.mark.asyncio
async def test_remote_world_state_never_uses_hub_telemetry_or_foreground(monkeypatch):
    from hub.world_state import WorldStateManager, DeviceSignalAdapter, ActivitySignalAdapter
    monkeypatch.setenv("CORTEX_HUB_DEVICE_ID", "actual-hub")
    telemetry = AsyncMock()  # Deliberately should never be called at all.
    monkeypatch.setattr(DeviceSignalAdapter, "inspect_system_telemetry", telemetry)
    foreground = AsyncMock()
    monkeypatch.setattr(ActivitySignalAdapter, "inspect_windows_foreground", foreground)
    state = await WorldStateManager.get_instance().get_fused_world_state("remote-phone")
    telemetry.assert_not_called()
    foreground.assert_not_called()
    assert not state.system_telemetry


@pytest.mark.asyncio
async def test_cancelling_socket_waiter_does_not_reexecute_native_command():
    from agent.command_dispatcher import CommandDispatcher
    dispatcher = CommandDispatcher()
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []
    async def execute(pool):
        calls.append("once")
        entered.set()
        await release.wait()
        return {"status": "success", "message": "Done"}
    try:
        waiter = asyncio.create_task(dispatcher.dispatch("cmd", "arbitrary.action", {}, execute))
        await entered.wait()
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        replacement = asyncio.create_task(dispatcher.dispatch("cmd", "arbitrary.action", {}, execute))
        release.set()
        assert (await replacement)["status"] == "success"
        assert calls == ["once"]
    finally:
        release.set()
        dispatcher.close()


@pytest.mark.asyncio
async def test_hub_socket_can_receive_executor_result_during_context_preparation(monkeypatch):
    from hub import main
    from hub.auth import hash_secret
    from hub.database import enroll_device
    from fastapi import WebSocketDisconnect
    await enroll_device("source", "Source", "admin", hash_secret("test-secret"))
    incoming = asyncio.Queue()
    for data in [{"type": "auth_response", "device_id": "source", "response": "test-secret", "platform": "test", "capabilities": []},
                 {"type": "voice_request", "command_id": "voice", "text": "general question"},
                 {"type": "result", "command_id": "executor", "command": "ui.observe", "status": "success", "message": "Observed"}]:
        incoming.put_nowait(json.dumps(data))
    executor_result = asyncio.get_running_loop().create_future()
    main.PENDING_FUTURES["executor"] = executor_result
    release = asyncio.Event()
    async def prepare(**kwargs):
        await release.wait()
        return {}
    class Socket:
        async def accept(self): pass
        async def close(self, **kwargs): pass
        async def receive_text(self):
            item = await incoming.get()
            if isinstance(item, Exception): raise item
            return item
        async def send_text(self, raw):
            data = json.loads(raw)
            if data.get("type") == "voice_response" and data.get("status") == "success":
                incoming.put_nowait(WebSocketDisconnect())
    socket = Socket()
    async def release_after_result():
        await executor_result
        release.set()
    unblock = asyncio.create_task(release_after_result())
    try:
        with patch.object(main, "_agent_harness", harness(), create=True), \
                patch("hub.context.ContextManager.get_active_context", prepare), \
                patch("hub.speakers.SpeakerManager.resolve_speaker", AsyncMock(return_value={})):
            await asyncio.wait_for(main.agent_endpoint(socket, "source"), 1)
        assert executor_result.done() and release.is_set()
        await unblock
    finally:
        unblock.cancel()
        main.PENDING_FUTURES.pop("executor", None)


@pytest.mark.asyncio
async def test_shutdown_persists_cancelled_preparation_and_drains_delivery():
    blocked = asyncio.Event()
    async def prepare():
        await blocked.wait()
        return {}
    model = harness()
    deliver = AsyncMock()
    response = await RequestLifecycle.submit("source", "shutdown", "arbitrary work", prepare, model, deliver=deliver)
    await RequestLifecycle.shutdown()
    result = json.loads((await get_task(response.data["task_id"]))["context"])["result"]
    assert result["status"] == "cancelled"
    assert not RequestLifecycle._jobs and not RequestLifecycle._deliveries
    model.process_request.assert_not_awaited()
    assert deliver.await_count == 1


def test_legacy_controller_does_not_infer_actions_from_target_phrases():
    from hub.computer_use_controller import ComputerUseTaskController
    controller = ComputerUseTaskController.get_instance()
    for target in ["go back", "continue", "click that", "an unforeseen target"]:
        assert controller.resolve_contextual_target(target, None, None, None) == (target, None)


@pytest.mark.asyncio
async def test_mock_provider_never_turns_keywords_into_actions():
    from hub.llm_provider import MockLLMProvider, ReasoningProviderUnavailable
    for prompt in ["open calculator", "delete a file", "a completely new request"]:
        with pytest.raises(ReasoningProviderUnavailable):
            await MockLLMProvider().generate_result(prompt, {})


@pytest.mark.asyncio
async def test_execution_timeout_does_not_claim_no_action_was_started():
    model = SimpleNamespace(process_request=AsyncMock(side_effect=TimeoutError))
    result = await RequestLifecycle.submit("source", "execution-timeout", "arbitrary action", AsyncMock(return_value={}), model)
    if result.status == "in_progress":
        await settle()
        result = ConversationalResponse.model_validate(json.loads((await get_task(result.data["task_id"]))["context"])["result"])
    assert result.data["error_code"] == "request_execution_timeout"
    assert "No new action" not in result.message


def test_compatibility_composer_does_not_invent_answer_or_provenance():
    from hub.pipeline.response_composer import ResponseComposer
    intent = SimpleNamespace(response_text="I opened it", candidate_capabilities=["arbitrary.action"])
    result = ResponseComposer.compose(intent, {"status": "success", "data": {"unknown": 42}})
    assert result.status == "error" and result.provenance is None
    assert result.message != intent.response_text
    result = ResponseComposer.compose(intent, {"status": "failure", "message": "Executor offline"})
    assert result.status == "error" and result.message == "Executor offline"
