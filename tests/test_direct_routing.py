"""No external inference: exercise transport, routing, privacy and actual turn wiring."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from hub.direct_provider import DirectLLMProvider
from hub.direct_router import DirectModelRouter, retry_delay
from hub.intent_schema import ModelResult
from hub.llm_provider import ReasoningProviderUnavailable


def provider(handler, name="groq", model=None):
    return DirectLLMProvider(name, model_name=model, api_key="test-private-key", transport=httpx.MockTransport(handler))


def reply(text, finish="stop"):
    return httpx.Response(200, json={"choices": [{"message": {"content": text}, "finish_reason": finish}]})


@pytest.mark.asyncio
async def test_protocol_preserves_schema_history_evidence_and_hides_credentials():
    received = []
    def respond(request):
        received.append(json.loads(request.content))
        assert str(request.url) == "https://api.groq.com/openai/v1/chat/completions"
        assert request.headers["authorization"] == "Bearer test-private-key"
        return reply('{"decision":"RESPOND","response_text":"Hello"}')
    p = provider(respond)
    ctx = {"mode": "agent_decision", "system_prompt": "catalog-and-privacy-checked-facts",
           "conversation_history": [{"role": "user", "text": "previous correction"}],
           "trace": SimpleNamespace(), "api_key": "DO-NOT-SEND"}
    result = await p.generate_result("follow up", ctx)
    assert json.loads(result.response_text)["decision"] == "RESPOND"
    body = received[0]
    assert body["response_format"] == {"type": "json_object"}
    assert "previous correction" in body["messages"][1]["content"]
    assert body["messages"][0]["content"].startswith("catalog-and-privacy-checked-facts")
    assert "DO-NOT-SEND" not in json.dumps(body)
    assert "test-private-key" not in json.dumps(body)


@pytest.mark.asyncio
async def test_result_interpretation_includes_goal_and_actual_evidence():
    bodies = []
    def respond(request):
        bodies.append(json.loads(request.content))
        return reply("CPU usage is 34%.")
    result = await provider(respond).generate_result("usage?", {"mode": "result_interpretation",
        "system_prompt": "Use evidence", "tool_result": {"cpu_percent": 34}, "goal": "Report usage"})
    assert result.response_text == "CPU usage is 34%."
    assert "response_format" not in bodies[0]
    assert "cpu_percent" in bodies[0]["messages"][1]["content"]


@pytest.mark.asyncio
@pytest.mark.parametrize("text,finish", [("", "stop"), ("partial", "length"), ("{}", "tool_calls"), ("not JSON", "stop")])
async def test_invalid_or_incomplete_decisions_fail(text, finish):
    with pytest.raises(ValueError):
        await provider(lambda _: reply(text, finish)).generate_result("request", {"mode": "agent_decision"})


@pytest.mark.asyncio
async def test_quota_fallback_keeps_context_and_pins_winner():
    contexts = []
    first = provider(lambda _: httpx.Response(429, headers={"retry-after": "120", "x-ratelimit-scope":"model"}))
    def respond(request):
        contexts.append(json.loads(request.content))
        response = reply('{"decision":"RESPOND","response_text":"Done"}')
        body = json.loads(response.content)
        body["usage"] = {"total_tokens": 500}
        return httpx.Response(200, json=body)
    second = provider(respond, model="openai/gpt-oss-120b")
    router = DirectModelRouter([first, second])
    selected, _ = await router.select_for_turn("follow up", {}, [])
    ctx = {"mode": "agent_decision", "conversation_history": [{"role": "user", "text": "remember this"}]}
    await selected.generate_result("follow up", ctx)
    assert selected.primary is second
    assert router.status()[0]["cooldown_seconds"] >= 119
    assert "remember this" in contexts[0]["messages"][1]["content"]
    await selected.generate_result("again", ctx)
    assert len(router.routes[0].calls) == 1
    assert len(contexts) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("privacy", ["PERSONAL", "SENSITIVE"])
async def test_personal_or_approved_data_never_switches_recipient(privacy):
    second_calls = []
    first = provider(lambda _: httpx.Response(503))
    second = provider(lambda request: second_calls.append(request) or reply("wrong"), model="openai/gpt-oss-120b")
    with pytest.raises(ReasoningProviderUnavailable):
        selected, _ = await DirectModelRouter([first, second]).select_for_turn("private", {"privacy_class": privacy}, [])
        await selected.generate_result("private", {"mode": "agent_decision"})
    assert second_calls == []


@pytest.mark.asyncio
async def test_explicit_no_fallback_also_applies_to_public_calls():
    second_calls = []
    first = provider(lambda _: httpx.Response(429))
    second = provider(lambda request: second_calls.append(request) or reply("wrong"), model="openai/gpt-oss-120b")
    router = DirectModelRouter([first, second])
    selected, _ = await router.select_for_turn("public", {}, [])
    with pytest.raises(ReasoningProviderUnavailable):
        await selected.generate_result("public", {"mode": "agent_decision", "allow_provider_fallback": False})
    assert second_calls == []


@pytest.mark.asyncio
async def test_nvidia_only_receives_explicit_public_data_and_supports_profiles():
    calls = []
    p = provider(lambda r: calls.append(r) or reply('{"decision":"RESPOND","response_text":"ok"}'), "nvidia")
    glm = provider(lambda _: reply("ok"), "nvidia", "z-ai/glm-5.3")
    router = DirectModelRouter([p, glm])
    assert router.candidates({}) == []
    assert router.candidates({"privacy_class": "SENSITIVE"}) == []
    assert router.candidates({"privacy_class": "PUBLIC", "routing_profile": "coding"})[0].provider is glm
    assert router.candidates({"privacy_class": "PUBLIC", "routing_profile": "vision"})[0].provider is p
    with pytest.raises(ReasoningProviderUnavailable):
        await p.generate_result("private", {"privacy_class": "SENSITIVE"})
    assert calls == []
    await router.generate_result("public", {"mode": "agent_decision", "privacy_class": "PUBLIC"})
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_estimated_usage_cannot_manufacture_a_provider_rate_limit():
    calls = []
    p = provider(lambda r: calls.append(r) or reply("Hello"))
    router = DirectModelRouter([p])
    for _ in range(35):
        assert router.routes[0].reserve(100000)
    result = await router.generate_result("Hello", {"force_response_only": True})
    assert result.response_text == "Hello" and len(calls) == 1


@pytest.mark.asyncio
async def test_cancellation_propagates_without_fallback():
    entered = asyncio.Event()
    async def blocked(_):
        entered.set()
        await asyncio.Event().wait()
    p = provider(blocked)
    router = DirectModelRouter([p])
    task = asyncio.create_task(router.generate_result("hello", {"force_response_only": True}))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


def test_factory_requires_configuration_and_explicit_trial_opt_in(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY",raising=False)
    from hub.llm_provider import _create_default_provider
    monkeypatch.setattr("dotenv.load_dotenv", lambda: None)
    monkeypatch.setenv("CORTEX_LLM_PROVIDER", "direct")
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.setenv("NVIDIA_API_KEY", "not-printed")
    monkeypatch.setenv("CORTEX_ENABLE_NVIDIA_TRIAL", "0")
    assert _create_default_provider().name == "unavailable"
    monkeypatch.setenv("GROQ_API_KEY", "not-printed")
    model = _create_default_provider()
    assert {r.provider.name for r in model.routes} == {"groq"}
    monkeypatch.setenv("CORTEX_ENABLE_NVIDIA_TRIAL", "1")
    assert {r.provider.name for r in _create_default_provider().routes} == {"groq", "nvidia"}


@pytest.mark.parametrize("model", ["paid/arbitrary-model", "https://elsewhere", "qwen/qwen3.8-27b:free"])
def test_arbitrary_models_rejected(model):
    with pytest.raises(ValueError):
        DirectLLMProvider(model_name=model)


@pytest.mark.asyncio
async def test_agent_turn_uses_direct_route_end_to_end():
    from hub.agent_turn import AgentTurn
    from test_agent_turn_runtime import control
    p = provider(lambda _: native_reply({"decision":"RESPOND","response_text":"A recursive function calls itself."}))
    result = await AgentTurn(control([]), DirectModelRouter([p])).run("Explain recursion", "asus", {})
    assert result.status == "success", result
    assert result.message == "A recursive function calls itself."


@pytest.mark.asyncio
async def test_direct_turn_executes_once_and_interprets_the_returned_measurement():
    from hub.agent_turn import AgentTurn
    from test_agent_turn_runtime import control
    calls = []
    def respond(request):
        body = json.loads(request.content)
        calls.append(body)
        message = json.loads(body["messages"][1]["content"])
        if message.get("tool_result"):
            assert message["tool_result"]["data"]["cpu_percent"] == 34
            return reply("CPU usage is 34%.")
        body = json.loads(native_reply({"decision":"CALL_CAPABILITY","capability":"telemetry.get_system_usage","arguments":{}}).content)
        body["usage"] = {"total_tokens": 2500}
        return httpx.Response(200, json=body)
    ctrl = control([{"status": "success", "data": {"cpu_percent": 34}}])
    result = await AgentTurn(ctrl, DirectModelRouter([provider(respond)])).run("Check CPU usage", "asus", {})
    assert result.status == "success", result
    assert result.message == "CPU usage is 34%."
    assert ctrl.execute_and_observe.await_count == 1
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_nvidia_policy_cannot_be_overridden_by_cloud_approval():
    from hub.agent_turn import AgentTurn
    from test_agent_turn_runtime import control
    p = provider(lambda _: reply("should not run"), "nvidia")
    turn = AgentTurn(control([]), p)
    result = turn._model_privacy({"privacy_class": "SENSITIVE"}, approved=True)
    assert not result.is_allowed and not result.requires_approval


@pytest.mark.asyncio
async def test_private_classifier_delegates_to_direct_selector_for_public_turns(monkeypatch):
    from hub.local_model import PrivacyAwareRouter
    monkeypatch.setenv("CORTEX_REASONING_ROUTE", "auto")
    local = SimpleNamespace(locality="local_device", generate_result=AsyncMock(return_value=ModelResult(
        type="RESPONSE", response_text='{"privacy_class":"PUBLIC"}')))
    p = provider(lambda _: reply("test"))
    direct = DirectModelRouter([p])
    selected, context = await PrivacyAwareRouter(direct, local).select_for_turn("general question", {"privacy_class": "PUBLIC"}, [])
    assert selected.primary is p and context["privacy_class"] == "PUBLIC"


def test_unknown_or_retired_provider_is_not_implicitly_enabled(monkeypatch):
    from hub.llm_provider import _create_default_provider
    monkeypatch.setattr("dotenv.load_dotenv", lambda: None)
    monkeypatch.setenv("CORTEX_LLM_PROVIDER", "retired-provider")
    assert _create_default_provider().name == "unavailable"


def test_invalid_model_configuration_reports_unavailable(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY",raising=False)
    monkeypatch.delenv("NVIDIA_API_KEY",raising=False)
    from hub.llm_provider import _create_default_provider
    monkeypatch.setattr("dotenv.load_dotenv", lambda: None)
    monkeypatch.setenv("CORTEX_LLM_PROVIDER", "direct")
    monkeypatch.setenv("GROQ_API_KEY", "not-printed")
    monkeypatch.setenv("CORTEX_GROQ_MODEL", "unknown/model")
    assert _create_default_provider().name == "unavailable"


@pytest.mark.asyncio
async def test_network_errors_do_not_leak_provider_body_or_keys(caplog):
    def fail(request):
        return httpx.Response(401, json={"error": "DO-NOT-LOG-THIS-PRIVATE-BODY"})
    with pytest.raises(ReasoningProviderUnavailable):
        await DirectModelRouter([provider(fail)]).generate_result("private prompt", {"force_response_only": True})
    assert "DO-NOT-LOG" not in caplog.text and "test-private-key" not in caplog.text


@pytest.mark.asyncio
async def test_screen_analysis_sends_image_and_fails_on_invalid_observation(tmp_path):
    from hub.privacy import PrivacyPolicyEngine
    PrivacyPolicyEngine.get_instance().policy.sensitive_data_cloud_allowed = True
    from PIL import Image
    from hub.direct_vision import DirectVisionProvider
    path = tmp_path / "screen.png"
    Image.new("RGB", (32, 32)).save(path)
    sent = []
    def respond(request):
        sent.append(json.loads(request.content))
        return reply(json.dumps({"summary": "A blank screen", "visible_apps": [],
            "visible_text_fragments": [], "regions": [], "confidence": 0.5}))
    vision = DirectVisionProvider(DirectModelRouter([provider(respond)]))
    obs = await vision.analyze_image(str(path), device_id="asus")
    assert obs.device_id == "asus" and obs.provenance["provider"] == "groq"
    assert sent[0]["messages"][1]["content"][1]["image_url"]["url"].startswith("data:image/jpeg;base64,")
    bad = DirectVisionProvider(DirectModelRouter([provider(lambda _: reply('{}'))]))
    with pytest.raises(ValueError):
        await bad.analyze_image(str(path))


def test_retry_after_is_bounded():
    assert retry_delay("120", "RATE_LIMIT_429") == 120
    assert retry_delay("invalid", "RATE_LIMIT_429") == 300
    assert retry_delay("-1", "RATE_LIMIT_429") == 1


def native_reply(proposal):
    return httpx.Response(200,json={"choices":[{"finish_reason":"tool_calls","message":{"tool_calls":[{
        "type":"function","function":{"name":"agent_decision","arguments":json.dumps(proposal)}}]}}]})
