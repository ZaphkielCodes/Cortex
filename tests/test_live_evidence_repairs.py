"""Runtime evidence boundaries found by the third blind run; simulated providers."""
from dataclasses import asdict
from unittest.mock import Mock

import pytest

from scripts.live_benchmark import permitted_fixture_share


def test_fixture_share_does_not_require_calculator_permission(tmp_path):
    fixture = tmp_path / "fixture"
    fixture.mkdir()
    origin = {"capability": "fs.open", "device_id": "source", "resource_path": str(fixture)}
    assert permitted_fixture_share({"allow_fixture_data": True}, origin, "source", fixture)
    assert not permitted_fixture_share({}, origin, "source", fixture)
    assert not permitted_fixture_share({"allow_calculator_ui": True}, origin, "source", fixture)
    for change in [{"device_id": "other"}, {"contains_image": True},
                   {"resource_path": str(tmp_path / "private")}, {"capability": "fs.delete"}]:
        assert not permitted_fixture_share({"allow_fixture_data": True}, origin | change, "source", fixture)


def test_calculator_scope_does_not_authorize_generic_window_hosts(tmp_path):
    origin = {"capability": "ui.observe", "device_id": "source", "window_id": 42, "foreground_app": "CalculatorApp"}
    assert permitted_fixture_share({"allow_calculator_ui": True}, origin, "source", tmp_path)
    for change in [{"foreground_app": "ApplicationFrameHost"}, {"device_id": "other"},
                   {"contains_image": True}, {"window_id": True}]:
        assert not permitted_fixture_share({"allow_calculator_ui": True}, origin | change, "source", tmp_path)


@pytest.mark.asyncio
async def test_successful_execution_survives_failed_interpretation_and_session_reload():
    from hub.agent_turn import AgentTurn
    from hub.llm_provider import ReasoningProviderUnavailable
    from hub.session_context import SessionContextManager
    from hub.pipeline.pipeline import RequestPipeline
    from hub.reasoning_context import build_reasoning_context
    from test_agent_turn_runtime import ScriptedModel, call, control

    class FailingInterpretation(ScriptedModel):
        async def generate_result(self, text, context, on_chunk=None):
            if context["mode"] == "result_interpretation":
                raise ReasoningProviderUnavailable("RATE_LIMIT_429")
            return await super().generate_result(text, context, on_chunk)

    ctrl = control([{"status": "success", "command_id": "native-once", "data": {"opened": True,
        "window_title": "PRIVATE-CANARY"}, "provenance": {"retrieved_at": "2026-10-19T00:00:00Z"}}])
    result = await AgentTurn(ctrl, FailingInterpretation([call("app.launch", app="example")])).run("Start example", "asus", {})
    assert result.status == "error"
    assert result.data["execution_receipts"][0]["status"] == "success"
    assert result.data["execution_receipts"][0]["command_id"] == "native-once"
    assert "PRIVATE-CANARY" not in str(result.data)
    await SessionContextManager.update_session("asus", "Start example", {"status": "in_progress", "task_id": result.task_id})
    await SessionContextManager.update_session("asus", "Start example", asdict(result))
    SessionContextManager._memory_cache.clear()
    session = await SessionContextManager.get_session("asus")
    assert len(session["turns"]) == 1
    assert session["turns"][0]["response_status"] == "error"
    assert session["turns"][0]["execution_receipts"][0]["executor_device"] == "asus"
    model = ScriptedModel([{"decision": "RESPOND", "response_text": "The action ran; the response failed."}])
    followup = await RequestPipeline(ctrl, model).execute("asus", "Did the action run?", {"session": session})
    assert followup.status == "success"
    assert "recent_execution" in model.calls[0][1]["system_prompt"]
    assert "native-once" in model.calls[0][1]["system_prompt"]
    assert "PRIVATE-CANARY" not in model.calls[0][1]["system_prompt"]
    ctrl.execute_and_observe.assert_awaited_once()
    denied = build_reasoning_context({"session": session}, lambda *args: False)
    assert "recent_execution" not in denied


def test_cpu_uses_a_real_first_sample_and_does_not_fabricate_zero(monkeypatch):
    import psutil
    from hub.world_state import DeviceSignalAdapter
    sample = Mock(return_value=37.5)
    monkeypatch.setattr(psutil, "cpu_percent", sample)
    assert DeviceSignalAdapter.inspect_system_telemetry("local")["cpu_percent"] == 37.5
    sample.assert_called_once_with(interval=0.1)
    sample.side_effect = OSError("unavailable")
    assert DeviceSignalAdapter.inspect_system_telemetry("local")["cpu_percent"] is None


@pytest.mark.parametrize("header,seconds", [("1m2.5s", 62.5), ("250ms", 1), ("2h3m", 7380),
                                         ("0s", 1), ("not-a-duration", 300)])
def test_provider_reset_duration_is_respected(header, seconds):
    from hub.direct_router import retry_delay
    assert retry_delay(header, "RATE_LIMIT_429") == seconds


@pytest.mark.asyncio
@pytest.mark.parametrize("headers,expected", [
    ({"x-ratelimit-remaining-requests": "900", "x-ratelimit-reset-tokens": "25s"}, "25s"),
    ({"x-ratelimit-remaining-requests": "0", "x-ratelimit-reset-requests": "2h"}, "2h"),
    ({"retry-after": "45", "x-ratelimit-reset-tokens": "25s"}, "45"),
])
async def test_rate_limit_uses_reset_headers_without_exposing_provider_body(headers, expected, caplog):
    import httpx
    from hub.direct_provider import DirectLLMProvider
    from hub.llm_provider import ProviderHTTPError
    provider = DirectLLMProvider(api_key="test", transport=httpx.MockTransport(lambda request:
        httpx.Response(429, headers=headers, json={"error": {"code": "rate_limit_exceeded",
            "message": "PRIVATE-CREDENTIAL-CANARY"}})))
    with pytest.raises(ProviderHTTPError) as error:
        await provider.generate_result("Synthetic public request", {"force_response_only": True})
    assert error.value.retry_after == expected
    assert "PRIVATE-CREDENTIAL-CANARY" not in caplog.text


@pytest.mark.asyncio
async def test_daily_token_limit_does_not_use_minute_reset_as_daily_recovery():
    import httpx
    from hub.direct_provider import DirectLLMProvider
    from hub.llm_provider import ProviderHTTPError
    provider = DirectLLMProvider(api_key="test", transport=httpx.MockTransport(lambda request:
        httpx.Response(429, headers={"x-ratelimit-reset-tokens": "1s"}, json={"error": {
            "code": "rate_limit_exceeded", "message": "Rate limit on tokens per day (TPD): Limit 200000 Used 199000 Requested 5000"}})))
    with pytest.raises(ProviderHTTPError) as error:
        await provider.generate_result("Synthetic public request", {"force_response_only": True})
    assert error.value.retry_after is None
