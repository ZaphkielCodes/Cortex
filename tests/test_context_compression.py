"""Compression preserves evidence contracts; transport probes never use real keys."""
import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from hub.context_compression import (compress_history, history_messages, compress_evidence,
    pack_catalog, byte_size, budget_evidence)
from hub.agent_turn import _build_conversation_history, _build_agent_system_prompt
from hub.direct_provider import DirectLLMProvider
from hub.direct_router import DirectModelRouter
from hub.llm_provider import ReasoningProviderUnavailable


def test_catalog_interning_is_lossless_for_constraints_and_devices():
    entries = [{"name": "arbitrary." + str(i), "description": "Any future capability",
        "arguments": {"target": "required string", "mode": 'optional string {"enum":["x","y"]}'},
        "devices": ["source", "worker"]} for i in range(20)]
    packed = pack_catalog(entries)
    restored = [{"name": row[0], "description": row[1], "arguments": {
        k: packed["argument_definitions"][v] for k, v in row[2].items()},
        "devices": packed["device_sets"][row[3]]} for row in packed["rows"]]
    assert restored == entries
    assert byte_size(packed) < byte_size(entries) * .75


def test_history_has_constant_budget_and_keeps_latest_exchange():
    history = [{"request": "old " + str(i), "response_message": "a" * 2000} for i in range(1000)]
    history += [{"request": "No, use the other window", "response_message": "Latest answer"}]
    packed = compress_history(history)
    assert len(packed) <= 8 and sum(len(m["text"]) for m in packed) <= 6000
    assert packed[-2]["text"] == "No, use the other window"
    assert packed[-1]["text"] == "Latest answer"
    assert packed[0]["earlier_messages_omitted"] > 0


def test_long_recent_answer_is_explicit_excerpt_with_head_and_tail():
    text = "START" + "x" * 20000 + "TAIL"
    packed = compress_history([{"request": "Keep the quotation exact", "response_message": text}])
    assert packed[-1]["text"].startswith("START") and packed[-1]["text"].endswith("TAIL")
    assert "middle omitted" in packed[-1]["text"]
    assert packed[0]["text"] == "Keep the quotation exact"
    assert sum(len(m["text"]) for m in packed) <= 6000


def test_privacy_classification_receives_history_before_lossy_compression():
    raw = {"session": {"turns": [{"request": "medical-private-canary " + "x" * 20000,
        "response_message": "reply"}, {"request": "general", "response_message": "answer"}]}}
    uncompressed = _build_conversation_history(raw, for_privacy=True)
    assert "medical-private-canary" in json.dumps(uncompressed)
    assert len(uncompressed[0]["text"]) > 20000
    assert sum(len(m["text"]) for m in _build_conversation_history(raw)) <= 6000


def test_older_tool_trees_do_not_accumulate_or_become_fresh_facts():
    older = [{"capability": "ui.observe", "result": {"status": "success", "data": {
        "elements": [{"element_id": "stale", "text": "x" * 1000}] * 100}}} for _ in range(9)]
    latest = {"capability": "ui.observe", "result": {"status": "success", "data": {
        "observation_id": "fresh", "device_id": "worker", "elements": [{"element_id": "live", "editable": True}]}}}
    packed = compress_evidence(older + [latest])
    assert byte_size(packed) <= 12000
    assert packed[-1] == latest and "stale" not in json.dumps(packed)
    assert all(row["evidence_omitted"] for row in packed[:-1])


def test_evidence_omissions_keep_failure_and_source_metadata_and_do_not_mutate_input():
    original = {"status": "failure", "observation_id": "obs", "device_id": "worker",
        "error": "not verified", "data": [{"element_id": str(i), "text": "x" * 1000} for i in range(100)]}
    packed = budget_evidence(original, 2000)
    assert byte_size(packed) <= 2000
    assert packed["status"] == "failure" and packed["error"] == "not verified"
    assert packed["observation_id"] == "obs" and packed["device_id"] == "worker"
    assert packed["data"][-1]["omitted_items"] > 0
    assert len(original["data"]) == 100


def test_current_request_is_not_compressed_and_unicode_uses_compact_json():
    p = DirectLLMProvider(api_key="test")
    request = "Do not send this exact text: " + "你好" * 10000
    payload = p.payload(request, {"force_response_only": True, "conversation_history": [
        {"role": "assistant", "text": "x" * 100000}]})
    sent = json.loads(payload["messages"][1]["content"])
    assert sent["request"] == request
    assert len(sent["conversation_history"][0]["text"]) <= 6000
    assert "\\u4f60" not in payload["messages"][1]["content"]


@pytest.mark.asyncio
async def test_real_provider_429_is_distinct_from_local_cooldown(monkeypatch):
    monkeypatch.setenv("CORTEX_PROVIDER_RATE_WAIT_SECONDS", "0")
    calls = []
    def respond(request):
        calls.append(request)
        return httpx.Response(429, headers={"retry-after": "12"})
    p = DirectLLMProvider(api_key="test", transport=httpx.MockTransport(respond))
    router = DirectModelRouter([p])
    with pytest.raises(ReasoningProviderUnavailable) as actual:
        await router.generate_result("Hello", {"force_response_only": True})
    assert actual.value.primary_category == "RATE_LIMIT_429"
    with pytest.raises(ReasoningProviderUnavailable) as cooldown:
        await router.generate_result("Hello", {"force_response_only": True})
    assert cooldown.value.primary_category == "PROVIDER_COOLDOWN"
    assert "No new model request was sent" in cooldown.value.user_message
    assert "backup" not in cooldown.value.user_message
    assert len(calls) == 1
    assert 10 <= router.status()[0]["cooldown_seconds"] <= 12


@pytest.mark.asyncio
async def test_usage_and_headers_are_observed_without_guessing_a_limit():
    calls = []
    def respond(request):
        calls.append(request)
        return httpx.Response(200, headers={"x-ratelimit-remaining-tokens": "42", "Authorization": "never-show"},
            json={"choices": [{"message": {"content": "Hello"}}], "usage": {"total_tokens": 90000}})
    router = DirectModelRouter([DirectLLMProvider(api_key="test", transport=httpx.MockTransport(respond))])
    for _ in range(4):
        await router.generate_result("Hello", {"force_response_only": True})
    assert len(calls) == 4
    assert router.status()[0]["rate_limits"] == {"x-ratelimit-remaining-tokens": "42"}
    assert "never-show" not in json.dumps(router.status())
    assert router.routes[0].calls[-1][1] == 90000


@pytest.mark.asyncio
@pytest.mark.parametrize("continue_turn", [False, True])
async def test_three_questions_then_observation_continuation_cannot_be_locally_blocked(continue_turn):
    from hub.agent_turn import AgentTurn
    from test_agent_turn_runtime import control
    calls = []
    def respond(request):
        calls.append(request)
        if len(calls) <= 3:
            result = {"decision": "RESPOND", "response_text": "Answer"}
        elif len(calls) == 4:
            result = {"decision": "CALL_CAPABILITY", "capability": "ui.observe", "arguments": {"include_tree": False},
                      "continue_after_result": continue_turn}
        else:
            assert "ChatGPT" in request.content.decode()
            result = {"decision": "RESPOND", "response_text": "ChatGPT is in the foreground."}
        content = "ChatGPT is in the foreground." if len(calls) == 5 and not continue_turn else json.dumps(result)
        if json.loads(request.content).get("tools"):
            choice={"finish_reason":"tool_calls","message":{"tool_calls":[{"type":"function",
                "function":{"name":"agent_decision","arguments":json.dumps(result)}}]}}
        else:
            choice={"finish_reason":"stop","message":{"content":content}}
        return httpx.Response(200, json={"choices": [choice],"usage": {"total_tokens":7000}})
    router = DirectModelRouter([DirectLLMProvider(api_key="test", transport=httpx.MockTransport(respond))])
    ctrl = control([{"status": "success", "data": {"observation_id": "obs", "app_name": "ChatGPT", "elements": []}}])
    for i in range(3):
        assert (await AgentTurn(ctrl, router).run("Question " + str(i), "asus", {})).status == "success"
    result = await AgentTurn(ctrl, router).run("What am I using?", "asus", {})
    assert result.status == "success", result
    assert "ChatGPT" in result.message and len(calls) == 5
    assert ctrl.execute_and_observe.await_count == 1


@pytest.mark.asyncio
async def test_skipped_primary_does_not_hide_a_real_backup_rejection():
    import time
    primary = DirectLLMProvider(api_key="test", transport=httpx.MockTransport(lambda _: pytest.fail("cooldown sent a request")))
    backup = DirectLLMProvider(model_name="openai/gpt-oss-120b", api_key="test",
        transport=httpx.MockTransport(lambda _: httpx.Response(401)))
    router = DirectModelRouter([primary, backup])
    router.routes[0].cooldown_until = time.monotonic() + 100
    # Direct TurnRoute preserves the blocked primary to exercise mixed outcomes.
    from hub.direct_router import TurnRoute
    with pytest.raises(ReasoningProviderUnavailable) as failure:
        await TurnRoute(router, router.routes, "INTERNAL").generate_result("Hello", {"force_response_only": True})
    assert failure.value.primary_category == "AUTH_ERROR"
    assert "No new model request was sent" not in failure.value.user_message


def test_input_verification_flag_survives_large_observation_packing():
    packed = budget_evidence({"status": "success", "data": ["x" * 5000] * 100,
                              "text_entry_verified": False, "input_attempted": True}, 1500)
    assert packed["text_entry_verified"] is False
    assert packed["input_attempted"] is True
    assert byte_size(packed) <= 1500


def test_large_historical_errors_cannot_exhaust_the_newest_evidence_budget():
    history = [{"capability": "arbitrary.action", "result": {"status": "failure", "error": "x" * 2000}} for _ in range(9)]
    latest = {"capability": "telemetry.get_system_usage", "result": {"status": "success", "data": {"cpu_percent": 34}}}
    packed = compress_evidence(history + [latest])
    assert byte_size(packed) <= 12000
    assert packed[-1] == latest
    assert any("omitted_items" in row for row in packed[:-1])
