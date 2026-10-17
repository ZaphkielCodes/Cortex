"""Exercise the real device receiver and hub message serialization without audio or APIs."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
import pytest

from agent.voice.response_router import route_voice_response
from hub.models import VoiceResponseMessage
from hub.agent_turn import AgentTurn
from hub.llm_provider import CircuitBreakerProvider, ProviderHTTPError, ReasoningProviderUnavailable
from test_agent_turn_runtime import ScriptedModel, control, call


def wire(**kwargs):
    return json.loads(VoiceResponseMessage(**kwargs).model_dump_json())


@pytest.mark.asyncio
@pytest.mark.parametrize("early", [False, True])
@pytest.mark.parametrize("status", ["success", "error", "cancelled", "needs_approval"])
async def test_completion_is_delivered_once_in_either_order(early, status):
    future = asyncio.get_running_loop().create_future()
    state = {"voice_futures": {"request1": future}}
    ack = wire(command_id="request1", message="Working", status="in_progress", data={"task_id": "turn1"})
    final = wire(command_id="turn1", task_id="turn1", request_id="request1", is_task_result=True,
                 status=status, message="The actual final answer")
    if early:
        assert route_voice_response(final, state) == []
        assert not future.done()
        assert route_voice_response(ack, state) == ["The actual final answer"]
    else:
        assert route_voice_response(ack, state) == []
        assert future.result()["status"] == "in_progress"
        state["voice_futures"].pop("request1")
        assert route_voice_response(final, state) == ["The actual final answer"]
    assert route_voice_response(final, state) == []


@pytest.mark.asyncio
async def test_unknown_and_timed_out_tasks_are_rejected():
    final = wire(command_id="turn1", task_id="turn1", request_id="unknown", is_task_result=True,
                 message="Must not be spoken")
    assert route_voice_response(final, {}) == []
    future = asyncio.get_running_loop().create_future()
    state = {"voice_futures": {"unknown": future}, "timed_out_cmd_ids": {"unknown"}}
    assert route_voice_response(final, state) == []
    assert not future.done()


@pytest.mark.asyncio
async def test_hub_background_sender_to_real_device_router():
    gate = asyncio.Event()
    ctrl = control([])
    async def execute(*args):
        await gate.wait()
        return {"status": "success", "data": {"cpu_percent": 34}}
    ctrl.execute_and_observe.side_effect = execute
    sent = []
    async def send_text(raw):
        sent.append(json.loads(raw))
    ctrl.connected_devices["asus"] = SimpleNamespace(send_text=send_text)
    agent = AgentTurn(ctrl, ScriptedModel([call("telemetry.get_system_usage")], "CPU is at 34%."))
    agent.ACK_SECONDS = .01
    result = await agent.run("CPU usage", "asus", {}, request_id="voice-request")
    future = asyncio.get_running_loop().create_future()
    state = {"voice_futures": {"voice-request": future}}
    ack = wire(command_id="voice-request", status=result.status, message=result.message,
               data=result.data, task_id=result.task_id, request_id="voice-request")
    assert route_voice_response(ack, state) == []
    assert (await future)["data"]["task_id"] == result.task_id
    state["voice_futures"].clear()
    gate.set()
    await asyncio.gather(*list(AgentTurn._running.values()))
    assert len(sent) == 1
    assert route_voice_response(sent[0], state) == ["CPU is at 34%."]
    assert route_voice_response(sent[0], state) == []


@pytest.mark.asyncio
async def test_actual_agent_session_queues_correlated_background_result():
    from agent.agent import _session
    future = asyncio.get_running_loop().create_future()
    state = {"voice_futures": {"request": future}, "ws": None}
    stop = asyncio.Event()
    messages = [wire(command_id="request", status="in_progress", message="Working", data={"task_id": "turn"}),
                wire(command_id="turn", task_id="turn", request_id="request", is_task_result=True,
                     status="success", message="CPU is at 34%.")]
    class Socket:
        async def send(self, raw):
            pass
        async def recv(self):
            item = messages.pop(0)
            if not messages:
                stop.set()
            return json.dumps(item)
    class Connection:
        async def __aenter__(self):
            return Socket()
        async def __aexit__(self, *args):
            pass
    voice = SimpleNamespace(start=lambda: None, stop=lambda: None, speak_result=AsyncMock(), state="idle")
    ui = SimpleNamespace(send=lambda value: None, on_message_callback=None)
    with patch("agent.agent.websockets.connect", return_value=Connection()), \
         patch("agent.agent._authenticate", new=AsyncMock(return_value=True)):
        await _session("ws://test", "http://test", "asus", "synthetic", stop, ui, voice, state)
    await asyncio.sleep(0)
    assert "CPU is at 34%." in [c.args[0] for c in voice.speak_result.await_args_list]
    assert future.result()["data"]["task_id"] == "turn"


@pytest.mark.asyncio
async def test_provider_failure_details_survive_runtime():
    primary = SimpleNamespace(name="primary", generate_result=AsyncMock(side_effect=TimeoutError("deadline exceeded")))
    backup = SimpleNamespace(name="backup", generate_result=AsyncMock(side_effect=ProviderHTTPError("OpenRouter", 402)))
    provider = CircuitBreakerProvider(primary, backup)
    # Match the production deadline category without an external model call.
    from hub.llm_provider import ModelDeadlineExceeded
    primary.generate_result.side_effect = ModelDeadlineExceeded("deadline exceeded")
    ctrl = control([])
    result = await AgentTurn(ctrl, provider).run("What is the CPU usage?", "asus", {})
    assert result.status == "error"
    assert "timed out" in result.message and "402" in result.message
    assert result.data["fallback_http_status"] == 402
    ctrl.execute_and_observe.assert_not_awaited()


def test_gemini_http_request_has_one_attempt_and_a_timeout():
    from hub.llm_provider import GeminiLLMProvider
    with patch("google.genai.Client") as client:
        GeminiLLMProvider(api_key="synthetic")._get_client()
    options = client.call_args.kwargs["http_options"]
    # Gemini rejects explicitly supplied deadlines below ten seconds.
    assert options["timeout"] >= 10000
    assert options["retry_options"] == {"attempts": 1}


@pytest.mark.asyncio
async def test_actual_hub_endpoint_preserves_task_correlation(monkeypatch):
    import hub.main as main
    from hub.agent_harness import AgentHarness
    from hub.auth import hash_secret
    from hub.database import enroll_device
    from fastapi import WebSocketDisconnect
    await enroll_device("asus", "ASUS", "admin", hash_secret("synthetic-test-secret"))
    future = asyncio.get_running_loop().create_future()
    state = {"voice_futures": {"voice-request": future}}
    incoming = asyncio.Queue()
    for msg in [{"type": "auth_response", "device_id": "asus", "response": "synthetic-test-secret",
                 "platform": "test", "capabilities": ["telemetry.get_system_usage"]},
                {"type": "voice_request", "command_id": "voice-request", "text": "CPU usage"}]:
        incoming.put_nowait(json.dumps(msg))
    speech, replies = [], []
    class Socket:
        async def accept(self):
            pass
        async def close(self, **kwargs):
            pass
        async def receive_text(self):
            item = await incoming.get()
            if isinstance(item, Exception):
                raise item
            return item
        async def send_text(self, raw):
            msg = json.loads(raw)
            if msg.get("type") == "voice_response":
                replies.append(msg)
                speech.extend(route_voice_response(msg, state))
                if msg.get("is_task_result"):
                    incoming.put_nowait(WebSocketDisconnect())
    ctrl = control([])
    async def execute(*args):
        await asyncio.sleep(.05)
        return {"status": "success", "data": {"cpu_percent": 34}}
    ctrl.execute_and_observe.side_effect = execute
    socket = Socket()
    ctrl.connected_devices["asus"] = socket
    monkeypatch.setattr(main, "_agent_harness", AgentHarness(ctrl, ScriptedModel(
        [call("telemetry.get_system_usage")], "CPU is at 34%.")), raising=False)
    monkeypatch.setattr(AgentTurn, "ACK_SECONDS", .01)
    await asyncio.wait_for(main.agent_endpoint(socket, "asus"), 3)
    assert len(replies) == 2
    assert replies[0]["status"] == "in_progress"
    assert replies[0]["task_id"] == replies[1]["task_id"]
    assert replies[1]["request_id"] == "voice-request"
    assert speech == ["CPU is at 34%."]
    await asyncio.gather(*list(AgentTurn._running.values()))


@pytest.mark.asyncio
async def test_circuit_keeps_usage_limit_reason_for_followup_requests():
    primary = SimpleNamespace(name="primary", generate_result=AsyncMock(side_effect=ProviderHTTPError("Gemini", 429)))
    backup = SimpleNamespace(name="backup", generate_result=AsyncMock(side_effect=ProviderHTTPError("OpenRouter", 402)))
    provider = CircuitBreakerProvider(primary, backup)
    for _ in range(2):
        with pytest.raises(ReasoningProviderUnavailable) as error:
            await provider.generate_result("A question", {"mode": "agent_decision"})
        assert "usage limit" in error.value.user_message
        assert "402" in error.value.user_message
    assert primary.generate_result.await_count == 1
