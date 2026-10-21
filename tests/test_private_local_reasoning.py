import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from hub.local_model import OllamaLLMProvider, PrivacyAwareRouter
from hub.intent_schema import ModelResult
from hub.llm_provider import ReasoningProviderUnavailable


@pytest.mark.parametrize("endpoint", ["https://127.0.0.1:11434", "http://localhost:11434", "http://example.com", "http://127.0.0.1/evil", "http://u:p@127.0.0.1", "http://127.0.0.1?q=x"])
def test_nonlocal_or_ambiguous_local_endpoint_rejected(endpoint):
    with pytest.raises(ValueError):
        OllamaLLMProvider("test", endpoint)


@pytest.mark.asyncio
async def test_actual_ollama_protocol_no_proxy_or_redirect_and_structured_result():
    received = []
    async def respond(request):
        received.append(json.loads(request.content))
        assert str(request.url) == "http://127.0.0.1:11434/api/chat"
        return httpx.Response(200, json={"done": True, "message": {"content": '{"decision":"RESPOND","response_text":"Hello"}'}})
    provider = OllamaLLMProvider("test", transport=httpx.MockTransport(respond))
    result = await provider.generate_result("hello", {"mode": "agent_decision", "system_prompt": "schema", "secret": "excluded"})
    assert json.loads(result.response_text)["decision"] == "RESPOND"
    assert received[0]["stream"] is False and received[0]["format"] == "json"
    assert "excluded" not in json.dumps(received)


@pytest.mark.asyncio
async def test_ollama_failure_never_becomes_mock_success():
    provider = OllamaLLMProvider("missing", transport=httpx.MockTransport(lambda _: httpx.Response(404)))
    with pytest.raises(ReasoningProviderUnavailable):
        await provider.generate_result("hello", {})


@pytest.mark.asyncio
@pytest.mark.parametrize("classification", ["PERSONAL", "SENSITIVE", "HIGHLY_SENSITIVE"])
async def test_private_topics_and_history_choose_private_provider(classification, monkeypatch):
    monkeypatch.setenv("CORTEX_REASONING_ROUTE", "auto")
    cloud = SimpleNamespace(generate_result=AsyncMock())
    local = SimpleNamespace(locality="local_device", generate_result=AsyncMock(return_value=ModelResult(
        type="RESPONSE", response_text=json.dumps({"privacy_class": classification}))))
    router = PrivacyAwareRouter(cloud, local)
    selected, context = await router.select_for_turn("and what about that?", {}, [{"role": "user", "text": "private earlier topic"}])
    assert selected is local and context["privacy_class"] == classification
    assert local.generate_result.call_args.args[1]["conversation_history"][0]["text"] == "private earlier topic"
    cloud.generate_result.assert_not_called()


@pytest.mark.asyncio
async def test_public_can_use_primary_but_explicit_private_label_cannot_be_downgraded(monkeypatch):
    monkeypatch.setenv("CORTEX_REASONING_ROUTE", "auto")
    cloud = SimpleNamespace(generate_result=AsyncMock())
    local = SimpleNamespace(locality="local_device", generate_result=AsyncMock(return_value=ModelResult(
        type="RESPONSE", response_text='{"privacy_class":"PUBLIC"}')))
    router = PrivacyAwareRouter(cloud, local)
    assert (await router.select_for_turn("public question", {}, []))[0] is cloud
    assert (await router.select_for_turn("public question", {"privacy_class": "SENSITIVE"}, []))[0] is local


@pytest.mark.asyncio
async def test_cloud_hosted_ollama_cannot_claim_private_compute():
    local = SimpleNamespace(locality="cloud_worker", generate_result=AsyncMock())
    with pytest.raises(ReasoningProviderUnavailable):
        await PrivacyAwareRouter(SimpleNamespace(), local).select_for_turn("private", {}, [])
    local.generate_result.assert_not_called()


@pytest.mark.asyncio
async def test_configured_local_defaults_to_private_without_cloud_screening(monkeypatch):
    monkeypatch.delenv("CORTEX_REASONING_ROUTE", raising=False)
    cloud = SimpleNamespace(generate_result=AsyncMock())
    local = SimpleNamespace(locality="local_device", generate_result=AsyncMock())
    selected, _ = await PrivacyAwareRouter(cloud, local).select_for_turn("any unforeseen wording", {}, [])
    assert selected is local
    cloud.generate_result.assert_not_called()
    local.generate_result.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("output", ['not json', '{"privacy_class":"UNKNOWN"}', '{"privacy_class":"PUBLIC","extra":"untrusted"}'])
async def test_auto_classifier_invalid_output_fails_closed(output, monkeypatch):
    monkeypatch.setenv("CORTEX_REASONING_ROUTE", "auto")
    cloud = SimpleNamespace(generate_result=AsyncMock())
    local = SimpleNamespace(locality="local_device", generate_result=AsyncMock(return_value=ModelResult(type="RESPONSE", response_text=output)))
    with pytest.raises(ReasoningProviderUnavailable):
        await PrivacyAwareRouter(cloud, local).select_for_turn("private", {}, [])
    cloud.generate_result.assert_not_called()
