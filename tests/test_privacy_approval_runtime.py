"""Offline behavioral checks at the model, confirmation, and voice boundaries."""
import asyncio
import json
import time
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from hub.agent_turn import AgentTurn
from hub.control_layer import CortexControlLayer
from hub.database import get_confirmation, get_pending_confirmations
from hub.main import PENDING_FUTURES, _execute_resolve_confirmation
from hub.pipeline.deterministic_router import DeterministicSafetyRouter
from hub.pipeline.normalizer import InputNormalizer
from hub.llm_provider import CircuitBreakerProvider, ReasoningProviderUnavailable, get_llm_provider
from test_agent_turn_runtime import ScriptedModel, call, control


@pytest.fixture(autouse=True)
def isolate_provider_factory(monkeypatch):
    monkeypatch.setattr("hub.llm_provider._active_provider", None)
    monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **k: None)


def private_turn():
    model = ScriptedModel([call("ui.observe"), {"decision": "RESPOND", "response_text": "You're using the terminal."}])
    model.locality = "cloud_worker"
    ctrl = control([{"status": "success", "data": {"device_id": "asus", "observation_id": "obs_private",
        "window_id":42,"pid":47,"timestamp":datetime.now(timezone.utc).isoformat(),
        "foreground_app": "Terminal", "elements": [{"label": "private-canary-text"}]}}])
    ctrl.request_approval = CortexControlLayer().request_approval
    agent = AgentTurn(ctrl, model)
    agent.ACK_SECONDS = .03
    return agent, ctrl, model


async def pending():
    for _ in range(100):
        rows = await get_pending_confirmations()
        if rows and rows[0]["command_id"] in PENDING_FUTURES:
            return rows[0]
        await asyncio.sleep(.01)
    raise AssertionError("No active approval request")


async def finish(task_id):
    job = AgentTurn._running[task_id]
    result = await job
    await asyncio.gather(*list(AgentTurn._running.values()))
    return result


@pytest.mark.asyncio
async def test_private_observation_waits_then_resumes_without_reexecution():
    agent, ctrl, model = private_turn()
    ack = await agent.run("Describe my current interface", "asus", {}, "req_privacy")
    row = await pending()
    assert ack.status == "in_progress"
    assert len(model.calls) == 1
    assert "private-canary-text" not in json.dumps(model.calls)
    assert "private-canary-text" not in row["args"]
    assert row["command"] == "model.share"
    assert not (await _execute_resolve_confirmation(row["command_id"], "approve", "lenovo"))[0]
    assert not PENDING_FUTURES[row["command_id"]].done()
    # Voice approval is scoped to the originating device.
    await DeterministicSafetyRouter.route(InputNormalizer.normalize("yes"), "asus", {}, ctrl)
    final = await finish(ack.task_id)
    assert final.status == "success"
    assert ctrl.execute_and_observe.await_count == 1
    assert "private-canary-text" in json.dumps(model.calls[1])
    assert model.calls[1][1]["allow_provider_fallback"] is False
    assert not await get_pending_confirmations()
    assert row["command_id"] not in PENDING_FUTURES


@pytest.mark.asyncio
@pytest.mark.parametrize("choice", ["deny", "expire", "cancel"])
async def test_denial_expiry_and_cancellation_never_share_data(choice):
    agent, ctrl, model = private_turn()
    if choice == "expire":
        agent.APPROVAL_SECONDS = .15
    ack = await agent.run("Inspect my interface", "asus", {}, "req_denied")
    row = await pending()
    if choice == "deny":
        assert (await _execute_resolve_confirmation(row["command_id"], "deny", "asus"))[0]
    elif choice == "cancel":
        AgentTurn.cancel_task(ack.task_id)
    result = await finish(ack.task_id)
    assert result.status in ("denied", "cancelled")
    assert len(model.calls) == 1
    assert ctrl.execute_and_observe.await_count == 1
    assert not await get_pending_confirmations()
    assert row["command_id"] not in PENDING_FUTURES


@pytest.mark.asyncio
async def test_provider_change_invalidates_approval():
    agent, _, model = private_turn()
    ack = await agent.run("Inspect", "asus", {})
    row = await pending()
    model.name = "different-provider"
    await _execute_resolve_confirmation(row["command_id"], "approve", "asus")
    result = await finish(ack.task_id)
    assert result.status == "denied"
    assert len(model.calls) == 1


@pytest.mark.asyncio
async def test_policy_revocation_while_waiting_denies_sharing():
    from hub.privacy import PrivacyPolicyEngine
    agent, _, model = private_turn()
    ack = await agent.run("Inspect", "asus", {})
    row = await pending()
    PrivacyPolicyEngine.get_instance().policy.allow_cloud_approval_override = False
    await _execute_resolve_confirmation(row["command_id"], "approve", "asus")
    result = await finish(ack.task_id)
    assert result.status == "denied"
    assert len(model.calls) == 1


@pytest.mark.asyncio
async def test_privacy_approval_is_not_reused_for_next_task():
    agent, _, model = private_turn()
    ack = await agent.run("Inspect", "asus", {})
    row = await pending()
    await _execute_resolve_confirmation(row["command_id"], "approve", "asus")
    await finish(ack.task_id)
    other, _, other_model = private_turn()
    ack2 = await other.run("Inspect again", "asus", {})
    row2 = await pending()
    assert row2["command_id"] != row["command_id"]
    assert len(other_model.calls) == 1
    await _execute_resolve_confirmation(row2["command_id"], "deny", "asus")
    await finish(ack2.task_id)


@pytest.mark.asyncio
async def test_orphaned_privacy_request_cannot_be_approved_after_restart():
    from hub.database import create_confirmation
    await create_confirmation("orphan", "asus", "model.share", ["Share private data?"], "asus", 2)
    result = await DeterministicSafetyRouter.route(InputNormalizer.normalize("yes"), "asus", {}, control([]))
    assert result.status == "error"
    assert (await get_confirmation("orphan"))["status"] == "expired"


@pytest.mark.asyncio
async def test_sensitive_initial_request_cannot_self_approve_from_metadata():
    agent, _, model = private_turn()
    ack = await agent.run("private input", "asus", {"privacy_class": "SENSITIVE", "user_approved": True, "approved_executor_id": "agent_model"})
    row = await pending()
    assert not model.calls
    await _execute_resolve_confirmation(row["command_id"], "deny", "asus")
    await finish(ack.task_id)
    assert not model.calls


@pytest.mark.asyncio
async def test_minimal_foreground_metadata_needs_no_screen_sharing_approval():
    agent, ctrl, model = private_turn()
    model.decisions = [call("ui.observe", include_tree=False)]
    result = await agent.run("Read foreground metadata", "asus", {})
    if result.is_async:
        result = await finish(result.task_id)
    assert result.status == "success"
    assert not await get_pending_confirmations()
    assert "private-canary-text" not in json.dumps(model.calls)
    assert "Terminal" in json.dumps(model.calls)


@pytest.mark.asyncio
async def test_late_approval_prompt_and_completion_keep_voice_correlation():
    from agent.voice.response_router import route_voice_response
    agent, ctrl, model = private_turn()
    agent.ACK_SECONDS = .001
    ws = SimpleNamespace(send_text=AsyncMock())
    ctrl.connected_devices = {"asus": ws}
    execute = ctrl.execute_and_observe.side_effect
    async def delayed(*args):
        await asyncio.sleep(.03)
        return next(execute)
    ctrl.execute_and_observe.side_effect = delayed
    ack = await agent.run("Inspect", "asus", {}, "request_late")
    row = await pending()
    await asyncio.sleep(.03)
    state = {"voice_background_tasks": {ack.task_id: "request_late"}}
    progress = json.loads(ws.send_text.await_args_list[0].args[0])
    assert route_voice_response(progress, state)
    assert route_voice_response(progress, state) == []
    assert ack.task_id in state["voice_background_tasks"]
    await _execute_resolve_confirmation(row["command_id"], "approve", "asus")
    await finish(ack.task_id)
    done = json.loads(ws.send_text.await_args_list[-1].args[0])
    assert route_voice_response(done, state) == ["You're using the terminal."]
    assert route_voice_response(done, state) == []


@pytest.mark.asyncio
async def test_prompt_racing_initial_ack_is_spoken_only_once():
    from agent.voice.response_router import route_voice_response
    future = asyncio.get_running_loop().create_future()
    state = {"voice_futures": {"req": future}}
    progress = {"command_id": "task", "task_id": "task", "request_id": "req",
        "is_task_result": True, "status": "in_progress", "message": "May I share?",
        "data": {"task_id": "task", "approval_id": "approval"}}
    assert route_voice_response(progress, state) == []
    ack = dict(progress, command_id="req", is_task_result=False)
    assert route_voice_response(ack, state) == []
    assert future.result()["message"] == "May I share?"
    assert route_voice_response(progress, state) == []
    final = dict(progress, status="success", message="Finished.")
    assert route_voice_response(final, state) == ["Finished."]


def test_gemini_has_no_implicit_backup(monkeypatch):
    monkeypatch.setenv("CORTEX_LLM_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "test-primary")
    model = get_llm_provider()
    assert len(model.routes) == 1 and model.routes[0].provider.name == "gemini"


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["gemini", "misspelled"])
async def test_configuration_error_does_not_select_mock(monkeypatch, provider):
    monkeypatch.setenv("CORTEX_LLM_PROVIDER", provider)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    model = get_llm_provider()
    assert model.name == "unavailable"
    with pytest.raises(ReasoningProviderUnavailable) as error:
        await model.generate_result("hello", {"mode": "agent_decision"})
    assert "not configured" in error.value.user_message


@pytest.mark.asyncio
@pytest.mark.parametrize("circuit_open", [False, True])
async def test_approved_private_payload_cannot_fail_over(circuit_open):
    primary = SimpleNamespace(name="primary", model="test", generate_result=AsyncMock(side_effect=TimeoutError()))
    fallback = SimpleNamespace(name="backup", generate_result=AsyncMock())
    provider = CircuitBreakerProvider(primary, fallback)
    if circuit_open:
        provider.circuit_open_until = time.time() + 30
    with pytest.raises(ReasoningProviderUnavailable):
        await provider.generate_result("private payload", {"mode": "agent_decision", "allow_provider_fallback": False})
    fallback.generate_result.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("capability", ["window.list", "process.list", "app.list_running"])
async def test_workspace_inventory_requires_approval_before_cloud_interpretation(capability):
    # Titles, paths and process arguments can reveal private workspace contents.
    model = ScriptedModel([call(capability)])
    model.locality = "cloud_worker"
    ctrl = control([{"status": "success", "data": [{"title": "private-inventory-canary"}]}])
    ctrl.request_approval = CortexControlLayer().request_approval
    agent = AgentTurn(ctrl, model)
    agent.ACK_SECONDS = .03
    ack = await agent.run("Inspect the workspace inventory", "asus", {}, "req_inventory")
    row = await pending()
    assert row["command"] == "model.share"
    assert "private-inventory-canary" not in json.dumps(model.calls)
    assert len(model.calls) == 1
    assert (await _execute_resolve_confirmation(row["command_id"], "deny", "asus"))[0]
    final = await finish(ack.task_id)
    assert final.status == "denied"
    assert len(model.calls) == 1
    assert ctrl.execute_and_observe.await_count == 1
