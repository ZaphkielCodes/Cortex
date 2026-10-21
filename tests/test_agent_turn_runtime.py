"""Behavioral contract probes; model and device boundaries are explicitly simulated."""
import asyncio
import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
import pytest

from hub.agent_turn import AgentTurn, AgentDecision, _build_conversation_history
from hub.capability_registry import CanonicalCapabilityRegistry
from hub.device_handle import DeviceHandle
from hub.executor_registry import ExecutorRegistry
from hub.intent_schema import ModelResult
from hub.pipeline.pipeline import RequestPipeline


class ScriptedModel:
    locality = "local_device"
    name = "scripted_contract_probe"

    def __init__(self, decisions, answer="The action completed."):
        self.decisions = list(decisions)
        self.answer = answer
        self.calls = []

    async def generate_result(self, text, context, on_chunk=None):
        self.calls.append((text, context))
        if context["mode"] == "result_interpretation":
            return ModelResult(type="RESPONSE", response_text=self.answer)
        value = self.decisions.pop(0)
        if isinstance(value, Exception):
            raise value
        return ModelResult(type="RESPONSE", response_text=value if isinstance(value, str) else json.dumps(value))


def call(capability, **arguments):
    # Scripted control-tree fixtures opt in; runtime omission tests use a direct decision dict.
    if capability == "ui.observe":
        arguments.setdefault("include_tree",True)
    return {"decision": "CALL_CAPABILITY", "capability": capability, "arguments": arguments}


def ui_condition(label, value):
    return [{"source":"ui","selector":{"label":label},"path":["text"],"operator":"equals","value":value}]


def cap_condition(capability, field, value):
    return [{"source":"capability","capability":capability,"path":["data",field],"operator":"equals","value":value}]


def control(results):
    ctrl = SimpleNamespace(connected_devices={},
        check_permissions=AsyncMock(return_value=SimpleNamespace(allowed=True, needs_confirmation=False)))
    ctrl.execute_and_observe = AsyncMock(side_effect=results)
    reg = ExecutorRegistry.get_instance()
    caps = [c.name for c in CanonicalCapabilityRegistry.get_instance().list_capabilities()]
    reg.register_device(DeviceHandle(device_id="asus", display_name="ASUS", role="primary_admin",
                                    trust_level="admin", capabilities=caps))
    reg.set_device_online("asus", True)
    return ctrl


@pytest.mark.asyncio
@pytest.mark.parametrize("utterance,decision,data,answer", [
    ("What app am I currently using?", call("ui.observe", include_tree=False, include_vision=False),
     {"foreground_app": "Chrome", "window_title": "Example"}, "You're currently using Chrome."),
    ("What's my CPU usage currently?", call("telemetry.get_system_usage"),
     {"cpu_percent": 34}, "CPU is at 34%."),
    ("What is the weather in Fairfax?", call("weather.current", location="Fairfax"),
     {"temperature": 21, "conditions": "Clear", "location": "Fairfax"}, "It's 21 degrees and clear in Fairfax."),
    ("Take me to my Downloads folder", call("fs.open", path="shell:Downloads"),
     {"path": "shell:Downloads", "opened": True}, "Your Downloads folder is open."),
])
async def test_requested_single_step_probes(utterance, decision, data, answer):
    from hub.weather_provider import set_weather_provider_override, MockWeatherProvider
    set_weather_provider_override(MockWeatherProvider())
    ctrl = control([{"status": "success", "data": data}])
    model = ScriptedModel([decision], answer)
    result = await RequestPipeline(ctrl, model).execute("asus", utterance, {})
    assert result.status == "success", result
    assert result.message == answer
    assert ctrl.execute_and_observe.await_count == 1
    assert [c[1]["mode"] for c in model.calls] == ["agent_decision", "result_interpretation"]
    interpreted = model.calls[-1][1]["tool_result"]["data"]
    assert interpreted == ({"foreground_app": "Chrome"} if decision["capability"] == "ui.observe" else data)


@pytest.mark.asyncio
async def test_calculator_compound_observes_and_reasons_after_each_step():
    final_condition=ui_condition("Mode", "Scientific")
    steps = [call("app.launch", app="calculator"), call("ui.observe", include_vision=False),
             dict(call("ui.hotkey", keys=["alt", "2"], observation_id="obs1"), success_condition=final_condition,
                  expected_effect=final_condition), call("ui.observe", include_vision=False)]
    model = ScriptedModel([
        {"decision": "PLAN", "goal": "Open Calculator and select scientific mode", "success_condition":final_condition,
         "plan_steps": [{"capability": "app.launch", "arguments": {"app": "calculator"}}]},
        *steps[1:], {"decision": "RESPOND", "response_text": "Calculator is open in scientific mode."}])
    ctrl = control([{"status": "success", "data": {"opened": True}},
                    {"status": "success", "data": {"observation_id": "obs1", "device_id":"asus", "window_id":42, "pid":47,"timestamp":datetime.now(timezone.utc).isoformat(), "foreground_app": "Calculator", "mode": "Standard",
                        "elements":[{"element_id":"el_mode","role":"text","label":"Mode","text":"Standard"}]}},
                    {"status": "success", "data": {}},
                    {"status": "success", "data": {"observation_id":"obs2", "device_id":"asus", "window_id":42, "pid":47,"timestamp":datetime.now(timezone.utc).isoformat(), "foreground_app": "Calculator", "mode": "Scientific",
                        "elements":[{"element_id":"el_mode","role":"text","label":"Mode","text":"Scientific"}]}}])
    result = await RequestPipeline(ctrl, model).execute("asus", "open calculator and switch to scientific mode", {})
    assert result.status == "success", result
    assert "scientific" in result.message
    assert [c.args[1] for c in ctrl.execute_and_observe.await_args_list] == [s["capability"] for s in steps]
    assert "Scientific" in model.calls[-1][1]["system_prompt"]


@pytest.mark.asyncio
async def test_conversation_correction_and_unsupported_probes():
    ctrl = control([])
    model = ScriptedModel([{"decision": "RESPOND", "response_text": "A compiler translates source code."}])
    result = await RequestPipeline(ctrl, model).execute("asus", "What does a compiler do?", {})
    assert result.status == "success"
    ctrl.execute_and_observe.assert_not_awaited()
    context = {"session": {"turns": [{"request": "Open Settings", "response_message": "Settings is open."}]}}
    model = ScriptedModel([call("app.launch", app="calculator")], "Calculator is open instead.")
    ctrl.execute_and_observe = AsyncMock(return_value={"status": "success", "data": {"app": "calculator"}})
    result = await RequestPipeline(ctrl, model).execute("asus", "No, I meant Calculator", context)
    assert result.status == "success"
    assert model.calls[0][1]["conversation_history"][0]["text"] == "Open Settings"
    assert ctrl.execute_and_observe.await_args.args[2] == {"app": "calculator", "target_device": "asus"}
    model = ScriptedModel([{"decision": "FAIL_TRUTHFULLY", "response_text": "I can't control that appliance."}])
    result = await RequestPipeline(ctrl, model).execute("asus", "Teleport my laptop to Mars", {})
    assert result.status == "unsupported"


@pytest.mark.asyncio
@pytest.mark.parametrize("decision", [call("invented.tool"), call("app.launch"),
    call("app.launch", app=42), call("app.launch", app="calculator", shell="bad"),
    {"decision": "PLAN", "plan_steps": [{"capability": "app.launch", "arguments": {"app": "calculator"}},
                                        {"capability": "invented.tool"}]}])
async def test_invalid_calls_never_dispatch(decision):
    ctrl = control([])
    result = await AgentTurn(ctrl, ScriptedModel([decision])).run("request", "asus", {})
    assert result.status == "error"
    ctrl.execute_and_observe.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("raw", [None, {}, {"status": "failure", "message": "Device failed"},
    {"status": "needs_approval", "message": "Approval required"}, {"status": "in_progress", "message": "Pending"}])
async def test_missing_pending_and_failed_results_do_not_claim_success(raw):
    ctrl = control([raw])
    model = ScriptedModel([call("app.launch", app="calculator")], "fabricated success")
    result = await AgentTurn(ctrl, model).run("Open calculator", "asus", {})
    assert result.status != "success"
    assert len(model.calls) == 1


@pytest.mark.asyncio
async def test_weather_incomplete_current_and_valid_forecast():
    from hub.weather_provider import set_weather_provider_override, MockWeatherProvider
    set_weather_provider_override(MockWeatherProvider())
    ctrl = control([{"status": "success", "data": {"temperature": None, "conditions": "Clear"}}])
    model = ScriptedModel([call("weather.current", location="Fairfax")])
    result = await AgentTurn(ctrl, model).run("Weather", "asus", {})
    assert result.status == "error" and "None" not in result.message
    ctrl.execute_and_observe = AsyncMock(return_value={"status": "success", "data": {"forecast": [{"high": 21}]}})
    result = await AgentTurn(ctrl, ScriptedModel([call("weather.forecast", location="Fairfax")])).run("Forecast", "asus", {})
    assert result.status == "success"


@pytest.mark.asyncio
async def test_explicit_offline_device_never_uses_hub(monkeypatch):
    monkeypatch.setattr(AgentTurn, "ACK_SECONDS", .01)
    ctrl = control([])
    reg = ExecutorRegistry.get_instance()
    reg.register_device(DeviceHandle(device_id="lenovo", display_name="Lenovo", capabilities=["app.launch"]))
    reg.set_device_online("lenovo", False)
    result = await AgentTurn(ctrl, ScriptedModel([call("app.launch", app="calculator", target_device="lenovo")])).run("Open on Lenovo", "asus", {})
    assert result.status == "in_progress"
    ctrl.execute_and_observe.assert_not_awaited()
    job = AgentTurn._running[result.task_id]
    notification = AgentTurn._running.get(result.task_id + "_notify")
    AgentTurn.cancel_task(result.task_id)
    assert (await job).status == "cancelled"
    if notification:
        await notification


@pytest.mark.asyncio
async def test_real_confirmation_resolver_resumes_exactly_one_approved_action(monkeypatch):
    from hub.database import create_confirmation
    from hub.main import _execute_resolve_confirmation, PENDING_FUTURES
    monkeypatch.setattr(AgentTurn, "ACK_SECONDS", .01)
    ctrl = control([{"status": "success", "data": {"copied": True}}])
    ctrl.check_permissions.return_value = SimpleNamespace(allowed=True, needs_confirmation=True)
    requested = asyncio.Event()
    async def request_approval(target, capability, args, source, tier):
        await create_confirmation("approval_real", target, capability, [str(args)], source, tier)
        requested.set()
        return "approval_real"
    ctrl.request_approval = request_approval
    turn = AgentTurn(ctrl, ScriptedModel([call("fs.copy", src="source.txt", dst="copy.txt")], "The file was copied."))
    ack = await turn.run("Copy the selected file", "asus", {"response_channel": "gui"})
    await asyncio.wait_for(requested.wait(), 1)
    ctrl.execute_and_observe.assert_not_awaited()
    job = AgentTurn._running[ack.task_id]
    notification = AgentTurn._running.get(ack.task_id + "_notify")
    success, _ = await _execute_resolve_confirmation("approval_real", "approve", "asus")
    assert success
    result = await job
    if notification:
        await notification
    assert result.status == "success", result
    assert ctrl.execute_and_observe.await_count == 1
    assert "approval_real" not in PENDING_FUTURES


@pytest.mark.asyncio
async def test_permission_and_ui_risk_gate():
    ctrl = control([])
    ctrl.check_permissions.return_value = SimpleNamespace(allowed=False)
    result = await AgentTurn(ctrl, ScriptedModel([call("app.launch", app="calculator")])).run("Open app", "asus", {})
    assert result.status == "denied"
    ctrl.execute_and_observe.assert_not_awaited()
    ctrl.check_permissions.return_value = SimpleNamespace(allowed=True, needs_confirmation=False)
    result = await AgentTurn(ctrl, ScriptedModel([call("fs.delete", path="C:\\example.txt")])).run("Delete file", "asus", {})
    assert result.status == "needs_approval"
    ctrl.execute_and_observe.assert_not_awaited()


@pytest.mark.asyncio
async def test_async_ack_persistence_notification_and_no_duplicate():
    from hub.database import get_task
    ready = asyncio.Event()
    async def execute(*args, **kwargs):
        await ready.wait()
        return {"status": "success", "data": {"opened": True}}
    ctrl = control([])
    ctrl.execute_and_observe.side_effect = execute
    ws = SimpleNamespace(send_text=AsyncMock())
    ctrl.connected_devices["asus"] = ws
    agent = AgentTurn(ctrl, ScriptedModel([call("app.launch", app="calculator")], "Calculator is open."))
    agent.ACK_SECONDS = .01
    result = await agent.run("Open calculator", "asus", {})
    assert result.status == "in_progress" and result.task_id
    again = await agent.run("Open calculator", "asus", {})
    assert again.task_id == result.task_id
    ready.set()
    await AgentTurn._running[result.task_id]
    await asyncio.gather(*list(AgentTurn._running.values()))
    saved = await get_task(result.task_id)
    assert saved["status"] == "COMPLETED"
    message = json.loads(ws.send_text.await_args.args[0])
    assert message["message"] == "Calculator is open." and message["status"] == "success"
    assert ctrl.execute_and_observe.await_count == 1


@pytest.mark.asyncio
async def test_model_failure_never_reexecutes_via_legacy():
    ctrl = control([])
    with patch('hub.pipeline.semantic_interpreter.SemanticInterpreter.interpret', new_callable=AsyncMock) as legacy:
        result = await RequestPipeline(ctrl, ScriptedModel([ValueError("provider unavailable")])).execute("asus", "Open app", {})
    assert result.status == "error"
    legacy.assert_not_awaited()
    ctrl.execute_and_observe.assert_not_awaited()


@pytest.mark.asyncio
async def test_stale_observation_and_cross_device_observation_never_click():
    for obs_arg in (None, "wrong"):
        ctrl = control([{"status": "success", "data": {"observation_id": "fresh", "elements": []}}])
        model = ScriptedModel([{"decision": "PLAN", "success_condition":ui_condition("State","ready"), "plan_steps": [
            {"capability": "ui.observe", "arguments": {}}]},
            call("ui.click", target="element", **({"observation_id": obs_arg} if obs_arg else {}))])
        result = await AgentTurn(ctrl, model).run("Click a control", "asus", {})
        assert result.status == "error"
        assert ctrl.execute_and_observe.await_count == 1


@pytest.mark.asyncio
async def test_cloud_privacy_stops_before_model_request():
    ctrl = control([])
    model = ScriptedModel([])
    model.locality = "cloud_worker"
    result = await AgentTurn(ctrl, model).run("Sensitive request", "asus", {"privacy_class": "HIGHLY_SENSITIVE"})
    assert result.status == "denied"
    assert not model.calls
    ctrl.execute_and_observe.assert_not_awaited()


@pytest.mark.asyncio
async def test_confirmation_is_source_scoped_and_resumes_only_one_action():
    from hub.control_layer import CortexControlLayer
    from hub.database import get_pending_confirmations
    from hub.main import PENDING_FUTURES
    ctrl = control([{"status": "success", "data": {"deleted": True}}])
    real = CortexControlLayer()
    ctrl.request_approval = real.request_approval
    agent = AgentTurn(ctrl, ScriptedModel([call("fs.delete", path="C:\\test.txt")], "The file was deleted."))
    agent.ACK_SECONDS = .01
    result = await agent.run("Delete that file", "asus", {})
    assert result.status == "in_progress"
    for _ in range(100):
        confirmations = await get_pending_confirmations()
        if confirmations:
            break
        await asyncio.sleep(.01)
    assert len(confirmations) == 1
    command_id = confirmations[0]["command_id"]
    from hub.pipeline.deterministic_router import DeterministicSafetyRouter
    from hub.pipeline.normalizer import InputNormalizer
    # Another device cannot approve the source's pending action.
    await DeterministicSafetyRouter.route(InputNormalizer.normalize("yes"), "lenovo", {}, ctrl)
    assert not PENDING_FUTURES[command_id].done()
    ctrl.execute_and_observe.assert_not_awaited()
    await DeterministicSafetyRouter.route(InputNormalizer.normalize("yes"), "asus", {}, ctrl)
    final = await AgentTurn._running[result.task_id]
    await asyncio.gather(*list(AgentTurn._running.values()))
    assert final.status == "success", final
    assert ctrl.execute_and_observe.await_count == 1
    assert command_id not in PENDING_FUTURES


@pytest.mark.asyncio
async def test_cancel_and_global_kill_switch():
    from hub.database import get_task
    from hub.goals import GoalManager
    ctrl = control([])
    ready = asyncio.Event()
    async def execute(*args):
        await ready.wait()
        return {"status": "success"}
    ctrl.execute_and_observe.side_effect = execute
    agent = AgentTurn(ctrl, ScriptedModel([call("app.launch", app="calculator")]))
    agent.ACK_SECONDS = .01
    result = await agent.run("Open calculator", "asus", {})
    job = AgentTurn._running[result.task_id]
    AgentTurn.cancel_for_device("asus")
    final = await job
    await asyncio.gather(*list(AgentTurn._running.values()))
    assert final.status == "cancelled"
    assert (await get_task(result.task_id))["status"] == "CANCELLED"
    await GoalManager.get_instance().set_autonomy_enabled(False)
    ctrl.execute_and_observe.reset_mock()
    result = await AgentTurn(ctrl, ScriptedModel([call("app.launch", app="calculator")])).run("Open calculator", "asus", {})
    assert result.status == "denied"
    ctrl.execute_and_observe.assert_not_awaited()


@pytest.mark.asyncio
async def test_restart_does_not_replay_pending_agent_turn():
    from hub.database import add_task, get_task
    from hub.task_orchestrator import TaskOrchestrator
    await add_task({"id": "interrupted", "title": "Action", "type": "agent_turn", "status": "RUNNING",
                    "command": "agent_turn", "assigned_device": "asus", "context": {}})
    ctrl = control([])
    await TaskOrchestrator(control_layer=ctrl).reload_active_tasks()
    task = await get_task("interrupted")
    assert task["status"] == "WAITING" and "unverified" in task["last_error"]
    ctrl.execute_and_observe.assert_not_awaited()


@pytest.mark.asyncio
async def test_stale_execution_result_is_rejected():
    from hub.task_ownership import TaskOwnershipManager
    ctrl = control([{"status": "success", "data": {"opened": True}}])
    manager = TaskOwnershipManager.get_instance()
    with patch.object(manager, "validate_fencing_token", side_effect=[(True, "Valid"), (False, "Stale")]):
        result = await AgentTurn(ctrl, ScriptedModel([call("app.launch", app="calculator")])).run("Open calculator", "asus", {})
    assert result.status == "error" and "unverified" in result.message


@pytest.mark.asyncio
async def test_provider_contract_parity_and_circuit_failure():
    from hub.llm_provider import GeminiLLMProvider, CircuitBreakerProvider
    from hub.direct_provider import DirectLLMProvider
    from unittest.mock import MagicMock
    decision = json.dumps({"decision": "RESPOND", "response_text": "Hello"})
    client = MagicMock()
    client.aio.models.generate_content = AsyncMock(return_value=SimpleNamespace(text=decision))
    gemini = GeminiLLMProvider(api_key="test")
    ctx = {"mode": "agent_decision", "system_prompt": "agent schema", "conversation_history": []}
    with patch.object(gemini, "_get_client", return_value=client):
        result = await gemini.generate_result("Hi", ctx)
    assert result.response_text == decision
    assert client.aio.models.generate_content.call_args.kwargs["config"]["response_mime_type"] == "application/json"
    client.aio.models.generate_content.return_value = SimpleNamespace(text="CPU is at 34%.")
    interp = {"mode": "result_interpretation", "system_prompt": "Only use evidence", "tool_result": {"data": {"cpu_percent": 34}}}
    with patch.object(gemini, "_get_client", return_value=client):
        await gemini.generate_result("CPU?", interp)
    assert "cpu_percent" in client.aio.models.generate_content.call_args.kwargs["contents"]
    response = SimpleNamespace(status_code=200, headers={}, json=lambda: {"choices": [{"message": {"content": decision}}]})
    http = AsyncMock()
    http.__aenter__.return_value.post.return_value = response
    with patch("httpx.AsyncClient", return_value=http):
        result = await DirectLLMProvider(api_key="test").generate_result("Hi", ctx)
    assert result.response_text == decision
    payload = http.__aenter__.return_value.post.call_args.kwargs["json"]
    assert payload["messages"][0]["content"].startswith("agent schema")
    failing = SimpleNamespace(name="unavailable", generate_result=AsyncMock(side_effect=RuntimeError("offline")))
    with pytest.raises(RuntimeError):
        await CircuitBreakerProvider(failing).generate_result("Hi", ctx)


def test_canonical_arguments_survive_privacy_minimization():
    from hub.privacy import minimize_execution_payload
    result = minimize_execution_payload("screen.analyze", {
        "image_ref": "screen.png", "question": "What is visible?", "mode": "inspect", "credentials": "secret"}, "asus")
    assert result.minimized_payload == {"image_ref": "screen.png", "question": "What is visible?", "mode": "inspect"}


@pytest.mark.asyncio
async def test_bad_cpu_reading_does_not_reach_response_model():
    ctrl = control([{"status": "success", "data": {"cpu_percent": None}}])
    model = ScriptedModel([call("telemetry.get_system_usage")])
    result = await AgentTurn(ctrl, model).run("CPU usage", "asus", {})
    assert result.status == "error"
    assert len(model.calls) == 1


@pytest.mark.asyncio
async def test_loop_protection_stops_duplicate_mutation():
    from test_compound_ui_runtime import scene
    from hub.database import get_task
    ctrl = control([{"status": "success", "data": {"opened": True}},scene("fresh")])
    model = ScriptedModel([{"decision": "PLAN", "success_condition":cap_condition("app.launch","opened",True), "plan_steps": [
        {"capability": "app.launch", "arguments": {"app": "calculator"}}]},
        call("app.launch", app="calculator"),{"decision":"RESPOND","response_text":"The launch was verified."}])
    result = await AgentTurn(ctrl, model).run("Open app and inspect it", "asus", {})
    assert result.status == "success"
    assert [c.args[1] for c in ctrl.execute_and_observe.await_args_list]==["app.launch","ui.observe"]
    snapshot=json.loads((await get_task(result.task_id))["context"])
    assert snapshot["compound_ui"]["recovery_counts"]["NO_PROGRESS"]==1


@pytest.mark.asyncio
async def test_changed_cached_lease_cannot_accept_old_result_or_release_new_lease():
    from hub.task_ownership import TaskOwnershipManager
    manager = TaskOwnershipManager.get_instance()
    ctrl = control([])
    async def execute(*args):
        execution = list(manager._executions.values())[-1]
        await manager.acquire_lease(execution.task_execution_id, "asus")
        return {"status": "success", "data": {"opened": True}}
    ctrl.execute_and_observe.side_effect = execute
    result = await AgentTurn(ctrl, ScriptedModel([call("app.launch", app="calculator")])).run("Open calculator", "asus", {})
    assert result.status == "error" and "unverified" in result.message
    execution = list(manager._executions.values())[-1]
    assert execution.generation == 2 and execution.lease_id
    assert execution.execution_state == "ACTIVE"
