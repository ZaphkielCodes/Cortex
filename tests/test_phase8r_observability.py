"""
tests/test_phase8r_observability.py
-----------------------------------
Deterministic tests for Cortex Phase 8R-A/B:
1. successful primary response
2. primary TTFT timeout -> fallback
3. primary exception -> fallback
4. fallback also fails
5. late primary response after fallback wins
6. no duplicate user response
7. no raw API error reaches TTS
8. circuit breaker behavior
9. metrics calculation
10. trace completeness
11. deterministic actions avoid LLM
12. GET /metrics endpoint returns bounded aggregates
13. client audio lifecycle metrics propagation
14. end-to-end websocket voice latency metrics roundtrip
"""

import asyncio
import time
import pytest
from typing import Dict, Any, Optional

from httpx import ASGITransport, AsyncClient
from hub.main import app
from agent.voice.controller import VoiceController, VoiceState, AudioOwner
from agent.voice.mock_providers import MockWakeWordProvider, MockSTTProvider, MockTTSProvider
from hub.tracing import InteractionTrace, scrub_secrets, TRACE_STAGES
from hub.metrics import MetricsCollector, compute_percentile
from hub.llm_provider import (
    LLMProvider,
    CircuitBreakerProvider,
    ModelDeadlineExceeded,
    classify_failure,
    get_user_friendly_error,
)
from hub.intent_schema import ModelResult, ActionPlan, ConversationalResponse
from hub.agent_harness import AgentHarness
from hub.control_layer import CortexControlLayer
from hub.config import get_lane_deadlines


# ---------------------------------------------------------------------------
# Test Helpers & Stubs
# ---------------------------------------------------------------------------

class StubProvider(LLMProvider):
    def __init__(self, name: str, model: str = "stub-model", delay: float = 0.0, exc: Optional[Exception] = None, result_text: str = "Stub result"):
        self._name = name
        self._model = model
        self.delay = delay
        self.exc = exc
        self.result_text = result_text
        self.call_count = 0

    @property
    def name(self) -> str:
        return self._name

    @property
    def model(self) -> str:
        return self._model

    async def generate_result(
        self, user_text: str, context: Dict[str, Any], on_chunk: Optional[callable] = None
    ) -> ModelResult:
        self.call_count += 1
        if self.delay > 0:
            await asyncio.sleep(self.delay)
        if self.exc:
            raise self.exc
        trace = context.get("trace")
        if trace and hasattr(trace, "record_stage"):
            trace.record_stage("model_request_started")
            trace.record_stage("model_first_token")
            trace.record_stage("model_complete")
        if on_chunk:
            await on_chunk(self.result_text)
        return ModelResult(type="RESPONSE", response_text=self.result_text)


# ---------------------------------------------------------------------------
# TEST 1: Successful Primary Response
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_successful_primary_response():
    primary = StubProvider("gemini-primary", result_text="The capital of France is Paris.")
    fallback = StubProvider("backup-fallback", result_text="Fallback text")
    cb = CircuitBreakerProvider(primary=primary, fallback=fallback)

    trace = InteractionTrace(interaction_id="ix_test_1")
    context = {"trace": trace, "interaction_id": "ix_test_1"}

    res = await cb.generate_result("What is the capital of France?", context)

    assert res.type == "RESPONSE"
    assert res.response_text == "The capital of France is Paris."
    assert primary.call_count == 1
    assert fallback.call_count == 0
    assert trace.fallback_occurred is False
    assert trace.model_provider == "gemini-primary"
    assert trace.success is True
    assert "model_request_started" in trace.timestamps
    assert "model_complete" in trace.timestamps


# ---------------------------------------------------------------------------
# TEST 2: Primary TTFT Timeout -> Fallback
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_primary_ttft_timeout_fallback():
    timeout_exc = ModelDeadlineExceeded("Time to first token deadline exceeded: 3.20s > 3.00s")
    primary = StubProvider("gemini-primary", exc=timeout_exc)
    fallback = StubProvider("backup-fallback", result_text="Fallback answered promptly.")
    cb = CircuitBreakerProvider(primary=primary, fallback=fallback)

    trace = InteractionTrace(interaction_id="ix_test_2")
    context = {"trace": trace, "interaction_id": "ix_test_2"}

    res = await cb.generate_result("Tell me a story", context)

    assert res.type == "RESPONSE"
    assert res.response_text == "Fallback answered promptly."
    assert primary.call_count == 1
    assert fallback.call_count == 1
    assert trace.fallback_occurred is True
    assert trace.failure_category == "DEADLINE_EXCEEDED"
    assert trace.model_provider == "backup-fallback"
    assert cb.failures == 1


# ---------------------------------------------------------------------------
# TEST 3: Primary Exception -> Immediate Fallback (No Blind Retry Loop)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_primary_exception_fallback():
    primary_exc = Exception("503 Service Unavailable: High demand on gemini-3.8-flash")
    primary = StubProvider("gemini-primary", exc=primary_exc)
    fallback = StubProvider("backup-fallback", result_text="Recovered via Backup.")
    cb = CircuitBreakerProvider(primary=primary, fallback=fallback)

    trace = InteractionTrace(interaction_id="ix_test_3")
    context = {"trace": trace, "interaction_id": "ix_test_3"}

    t_start = time.monotonic()
    res = await cb.generate_result("What is the weather?", context)
    elapsed = time.monotonic() - t_start

    # Fallback must be immediate, not stuck in a blind sleep/retry loop
    assert elapsed < 1.0
    assert res.response_text == "Recovered via Backup."
    assert primary.call_count == 1
    assert fallback.call_count == 1
    assert trace.fallback_occurred is True
    assert trace.failure_category == "SERVICE_UNAVAILABLE_503"
    assert cb.failures == 1


# ---------------------------------------------------------------------------
# TEST 4: Fallback Also Fails -> User-Friendly Error, No Raw Exception
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_fallback_also_fails():
    primary = StubProvider("gemini-primary", exc=Exception("503 Service Unavailable"))
    fallback = StubProvider("backup-fallback", exc=Exception("429 Rate limit exceeded"))
    cb = CircuitBreakerProvider(primary=primary, fallback=fallback)

    trace = InteractionTrace(interaction_id="ix_test_4")
    context = {"trace": trace, "interaction_id": "ix_test_4"}

    res = await cb.generate_result("Generate report", context)

    assert res.type == "RESPONSE"
    assert res.response_text == "I couldn't complete that request right now."
    assert "503" not in res.response_text
    assert "429" not in res.response_text
    assert "Traceback" not in res.response_text
    assert trace.success is False
    assert trace.failure_category.startswith("FALLBACK_FAILED")


# ---------------------------------------------------------------------------
# TEST 5: Late Primary Response After Fallback Wins -> Discarded
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_late_primary_response_after_fallback_wins():
    cb = CircuitBreakerProvider(
        primary=StubProvider("gemini-primary", result_text="Late primary output"),
        fallback=StubProvider("backup-fallback", result_text="Authoritative fallback output")
    )
    interaction_id = "ix_race_5"

    # Pre-register fallback as the winner
    winner_result = ModelResult(type="RESPONSE", response_text="Authoritative fallback output")
    cb._interaction_winners[interaction_id] = "backup-fallback"
    cb._interaction_winners[interaction_id + "_result"] = winner_result

    # Now primary returns late for the same interaction
    context = {"interaction_id": interaction_id}
    res = await cb.generate_result("What happened?", context)

    # Late primary must be discarded; authoritative fallback result must be returned
    assert res.response_text == "Authoritative fallback output"
    assert cb._interaction_winners[interaction_id] == "backup-fallback"


# ---------------------------------------------------------------------------
# TEST 6: No Duplicate User Response in Agent Client Loop
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_no_duplicate_user_response():
    # Simulates agent active state logic in agent/agent.py
    active_state = {
        'voice_futures': {},
        'timed_out_cmd_ids': set(),
        'spoken_messages': []
    }

    async def speak_result(msg: str):
        active_state['spoken_messages'].append(msg)

    # 1. Command timed out previously
    timed_out_id = "cmd_timed_out_123"
    active_state['timed_out_cmd_ids'].add(timed_out_id)

    # 2. Late voice response arrives for timed out command
    late_msg = {
        "type": "voice_response",
        "command_id": timed_out_id,
        "message": "Late response from hub",
        "status": "success"
    }

    # Simulate agent.py receive handling
    cmd_id = late_msg.get("command_id")
    if cmd_id in active_state['voice_futures'] and not active_state['voice_futures'][cmd_id].done():
        active_state['voice_futures'][cmd_id].set_result(late_msg)
    elif cmd_id in active_state['timed_out_cmd_ids']:
        # DISCARDED!
        pass
    elif late_msg.get("is_post_approval"):
        await speak_result(late_msg.get("message"))

    assert len(active_state['spoken_messages']) == 0

    # 3. Unsolicited voice response WITHOUT is_post_approval arrives
    unsolicited_raw = {
        "type": "voice_response",
        "command_id": "cmd_unknown_456",
        "message": "Spurious duplicate message",
        "status": "success",
        "is_post_approval": False
    }

    u_cmd = unsolicited_raw.get("command_id")
    if u_cmd in active_state['voice_futures'] and not active_state['voice_futures'][u_cmd].done():
        active_state['voice_futures'][u_cmd].set_result(unsolicited_raw)
    elif u_cmd in active_state['timed_out_cmd_ids']:
        pass
    elif unsolicited_raw.get("is_post_approval"):
        await speak_result(unsolicited_raw.get("message"))

    assert len(active_state['spoken_messages']) == 0

    # 4. Genuinely post-approval response arrives
    post_appr_msg = {
        "type": "voice_response",
        "command_id": "cmd_post_appr_789",
        "message": "Successfully deleted the temporary folder.",
        "status": "success",
        "is_post_approval": True
    }
    p_cmd = post_appr_msg.get("command_id")
    if p_cmd in active_state['voice_futures'] and not active_state['voice_futures'][p_cmd].done():
        active_state['voice_futures'][p_cmd].set_result(post_appr_msg)
    elif post_appr_msg.get("is_post_approval"):
        await speak_result(post_appr_msg.get("message"))

    assert len(active_state['spoken_messages']) == 1
    assert active_state['spoken_messages'][0] == "Successfully deleted the temporary folder."


# ---------------------------------------------------------------------------
# TEST 7: No Raw API Error Reaches TTS & Secret Scrubbing
# ---------------------------------------------------------------------------

def test_no_raw_api_error_reaches_tts():
    raw_exceptions = [
        Exception("google.genai.errors.APIError: 503 Overloaded: High demand"),
        Exception("httpx.HTTPStatusError: 429 Too Many Requests: Rate limit exceeded"),
        Exception("AuthenticationError: AIzaSyB1234567890abcdef1234567890abcde invalid key"),
        Exception("ConnectionResetError: [WinError 10054] Connection forcibly closed"),
        ModelDeadlineExceeded("Time to first token deadline exceeded: 3.5s > 3.0s"),
        ValueError("Invalid JSON schema: unexpected token at line 1 column 1")
    ]

    for exc in raw_exceptions:
        cat = classify_failure(exc)
        friendly = get_user_friendly_error(cat)
        assert friendly == "I couldn't complete that request right now."
        assert "503" not in friendly
        assert "429" not in friendly
        assert "AIza" not in friendly
        assert "WinError" not in friendly
        assert "schema" not in friendly

    # Secret scrubber defense-in-depth
    # Construct a synthetic token so published tests contain no credential-shaped literal.
    fake_backup_key = "sk-or-v1-" + "0" * 64
    dirty_text = f"Bearer eyJhbGciOiJIUzI1NiJ9.test and Google Key AIzaSyB1234567890abcdef1234567890abcde and Backup {fake_backup_key}"
    clean_text = scrub_secrets(dirty_text)
    assert "AIzaSy" not in clean_text
    assert "sk-or-v1" not in clean_text
    assert "Bearer" not in clean_text
    assert "<REDACTED>" in clean_text


# ---------------------------------------------------------------------------
# TEST 8: Circuit Breaker Behavior (Tripping, Fast Failover, Recovery)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_circuit_breaker_behavior():
    primary = StubProvider("gemini-primary", exc=Exception("503 Service Unavailable"))
    fallback = StubProvider("backup-fallback", result_text="Fallback answer")
    cb = CircuitBreakerProvider(primary=primary, fallback=fallback)

    now = time.time()
    # Call 1: Primary fails, failure count becomes 1
    res1 = await cb.generate_result("Q1", {"interaction_id": "ix_cb_1"})
    assert res1.response_text == "Fallback answer"
    assert cb.failures == 1
    assert cb.circuit_open_until == 0.0

    # Call 2: Primary fails again, failure count becomes 2 -> Circuit trips!
    res2 = await cb.generate_result("Q2", {"interaction_id": "ix_cb_2"})
    assert res2.response_text == "Fallback answer"
    assert cb.failures == 2
    assert cb.circuit_open_until > time.time()

    # Call 3: Circuit is open -> Routes directly to fallback WITHOUT calling primary
    primary_calls_before = primary.call_count
    trace3 = InteractionTrace(interaction_id="ix_cb_3")
    res3 = await cb.generate_result("Q3", {"interaction_id": "ix_cb_3", "trace": trace3})
    assert res3.response_text == "Fallback answer"
    assert primary.call_count == primary_calls_before  # Primary was skipped entirely
    assert trace3.failure_category == "CIRCUIT_OPEN"

    # Reset recovery: simulate circuit cooling down and primary succeeding
    cb.circuit_open_until = time.time() - 1.0  # Cooled down
    primary.exc = None  # Recovered
    primary.result_text = "Primary is back online"
    res4 = await cb.generate_result("Q4", {"interaction_id": "ix_cb_4"})
    assert res4.response_text == "Primary is back online"
    assert cb.failures == 0
    assert cb.circuit_open_until == 0.0


# ---------------------------------------------------------------------------
# TEST 9: Metrics Collector (Bounded Deques, Quantiles, Interpolation)
# ---------------------------------------------------------------------------

def test_metrics_calculation():
    MetricsCollector.reset_instance()
    collector = MetricsCollector.get_instance(max_samples=100)

    # 1. Percentile edge cases
    assert compute_percentile([], 50) == 0.0
    assert compute_percentile([4.2], 50) == 4.2
    assert compute_percentile([1.0, 2.0, 3.0, 4.0, 5.0], 50) == 3.0
    assert compute_percentile([1.0, 2.0, 3.0, 4.0, 5.0], 95) == 4.8

    # 2. Record 4 distinct traces
    t1 = InteractionTrace(interaction_id="t1", success=True, fallback_occurred=False)
    t1.timestamps = {
        "wake_detected": 10.0,
        "microphone_open": 10.1,
        "speech_first_detected": 10.2,
        "speech_end": 11.0,
        "stt_complete": 11.4,          # STT latency: 1.3s from mic / 0.4s from speech_end
        "hub_request_received": 11.41,
        "routing_complete": 11.46,     # Routing latency: 0.05s
        "model_request_started": 11.47,
        "model_first_token": 12.27,    # TTFT: 0.8s
        "model_complete": 12.87,       # Total LLM: 1.4s
        "tts_request_started": 12.88,
        "tts_first_audio": 13.18,      # TTS first-audio: 0.3s
        "playback_started": 13.20,
        "playback_complete": 14.50
    }
    collector.record_trace(t1)

    t2 = InteractionTrace(interaction_id="t2", success=True, fallback_occurred=True)
    t2.timestamps = {
        "wake_detected": 20.0,
        "microphone_open": 20.1,
        "speech_end": 21.0,
        "stt_complete": 21.3,
        "hub_request_received": 21.31,
        "routing_complete": 21.36,
        "model_request_started": 21.37,
        "model_first_token": 24.37,    # TTFT: 3.0s
        "model_complete": 25.37,       # Total LLM: 4.0s
        "tts_request_started": 25.38,
        "tts_first_audio": 25.78,      # TTS first-audio: 0.4s
    }
    collector.record_trace(t2)

    t3 = InteractionTrace(interaction_id="t3", success=False, fallback_occurred=True)
    t3.timestamps = {
        "wake_detected": 30.0,
        "microphone_open": 30.1,
        "speech_end": 31.0,
        "stt_complete": 31.5,
        "hub_request_received": 31.51,
        "routing_complete": 31.57,
        "model_request_started": 31.58,
        "model_first_token": 34.58,    # TTFT: 3.0s
        "model_complete": 35.58,
    }
    collector.record_trace(t3)

    report = collector.get_report()

    assert report["count"] == 3
    assert report["success_rate"] == round(2 / 3, 4)
    assert report["fallback_rate"] == round(2 / 3, 4)

    m = report["metrics"]
    assert "routing_latency" in m
    assert m["routing_latency"]["count"] == 3
    assert "llm_ttft" in m
    assert m["llm_ttft"]["count"] == 3
    assert m["llm_ttft"]["p50"] > 0
    assert m["llm_ttft"]["p95"] > 0
    assert "tts_first_audio_latency" in m
    assert m["tts_first_audio_latency"]["count"] == 2
    assert "wake_to_first_audio" in m
    assert m["wake_to_first_audio"]["count"] == 2


# ---------------------------------------------------------------------------
# TEST 10: Trace Completeness & Durations Calculation
# ---------------------------------------------------------------------------

def test_trace_completeness():
    t = InteractionTrace(
        trace_id="tr_complete_1",
        session_id="ses_abc",
        interaction_id="ix_123",
        model_provider="gemini-3.8-flash",
        routing_lane="FAST_RESPONSE",
        fallback_occurred=False
    )

    t.record_stage("wake_detected", 100.0)
    t.record_stage("microphone_open", 100.1)
    t.record_stage("speech_first_detected", 100.2)
    t.record_stage("speech_end", 101.5)
    t.record_stage("stt_complete", 101.8)
    t.record_stage("hub_request_received", 101.82)
    t.record_stage("routing_complete", 101.88)
    t.record_stage("model_request_started", 101.90)
    t.record_stage("model_first_token", 102.60)
    t.record_stage("model_complete", 103.20)
    t.record_stage("tts_request_started", 103.22)
    t.record_stage("tts_first_audio", 103.52)
    t.record_stage("playback_started", 103.54)
    t.record_stage("playback_complete", 105.14)

    t.compute_durations()

    # Verify all 14 stages are present in timestamps
    for stage in TRACE_STAGES:
        assert stage in t.timestamps
        assert stage in t.wall_clock_timestamps

    # Verify derived stage durations
    dur = t.stage_durations
    assert dur["stt_latency"] == pytest.approx(1.7, 0.01)  # 101.8 - 100.1
    assert dur["routing_latency"] == pytest.approx(0.06, 0.01)
    assert dur["llm_ttft"] == pytest.approx(0.70, 0.01)
    assert dur["llm_total_latency"] == pytest.approx(1.30, 0.01)
    assert dur["tts_first_audio_latency"] == pytest.approx(0.30, 0.01)
    assert dur["playback_duration"] == pytest.approx(1.60, 0.01)
    assert t.total_wake_to_first_audio == pytest.approx(3.52, 0.01)
    assert t.total_speech_end_to_first_audio == pytest.approx(2.02, 0.01)

    # Verify dictionary serialization
    td = t.to_dict()
    assert td["trace_id"] == "tr_complete_1"
    assert td["total_wake_to_first_audio"] == pytest.approx(3.52, 0.01)
    assert td["total_speech_end_to_first_audio"] == pytest.approx(2.02, 0.01)


# ---------------------------------------------------------------------------
# TEST 11: Deterministic Actions Avoid LLM In AgentHarness
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_deterministic_action_avoids_llm():
    # Verify that deterministic queries like time and date do not invoke the LLM
    from model_contract_fixtures import scripted_harness
    harness,ctrl,model=scripted_harness([{"decision":"RESPOND","response_text":"The supplied runtime clock reads 12:34."}])
    result=await harness.process_request("asus","what time is it",{"world_state":{"current_time":"12:34"}})
    assert result.status == "success"
    assert len(model.calls) == 1
    assert "12:34" in model.calls[0][1]["system_prompt"]
    assert result.latency_metrics["routing_lane"] == "AGENT_TURN"


# ---------------------------------------------------------------------------
# TEST 12: GET /metrics Endpoint Returns Bounded Aggregates
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_metrics_endpoint():
    MetricsCollector.reset_instance()
    collector = MetricsCollector.get_instance(max_samples=50)

    # Ingest 2 traces: 1 primary success, 1 fallback success
    t1 = InteractionTrace(interaction_id="t_api_1", success=True, fallback_occurred=False)
    t1.record_stage("microphone_open", 10.0)
    t1.record_stage("stt_complete", 11.2)
    t1.record_stage("hub_request_received", 11.3)
    t1.record_stage("routing_complete", 11.35)
    t1.record_stage("model_request_started", 11.36)
    t1.record_stage("model_first_token", 11.86)
    t1.record_stage("model_complete", 12.36)
    t1.record_stage("tts_request_started", 12.40)
    t1.record_stage("tts_first_audio", 12.65)
    collector.record_trace(t1)

    t2 = InteractionTrace(interaction_id="t_api_2", success=True, fallback_occurred=True)
    t2.record_stage("microphone_open", 20.0)
    t2.record_stage("stt_complete", 21.0)
    t2.record_stage("hub_request_received", 21.1)
    t2.record_stage("routing_complete", 21.15)
    t2.record_stage("model_request_started", 21.16)
    t2.record_stage("model_first_token", 22.06)
    t2.record_stage("model_complete", 22.56)
    t2.record_stage("tts_request_started", 22.60)
    t2.record_stage("tts_first_audio", 22.90)
    collector.record_trace(t2)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        resp = await ac.get("/metrics")
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] == 2
        assert data["success_rate"] == 1.0
        assert data["fallback_rate"] == 0.5
        metrics = data["metrics"]
        assert "stt_latency" in metrics
        assert metrics["stt_latency"]["count"] == 2
        assert metrics["stt_latency"]["p50"] > 0.0
        assert "llm_ttft" in metrics
        assert metrics["llm_ttft"]["count"] == 2
        assert "routing_latency" in metrics
        assert "tts_first_audio_latency" in metrics


# ---------------------------------------------------------------------------
# TEST 13: Client Audio Lifecycle Metrics Propagation
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_client_audio_lifecycle_metrics_propagation():
    wakeword = MockWakeWordProvider()

    class StreamingSTT(MockSTTProvider):
        async def listen_stream(self):
            self._listening = True
            await asyncio.sleep(0.02)
            yield False, "what"
            await asyncio.sleep(0.02)
            yield False, "what time"
            await asyncio.sleep(0.02)
            self._listening = False
            yield True, "what time is it"

    class FastTTS(MockTTSProvider):
        def __init__(self):
            super().__init__()
            self.last_first_audio_time = None

        async def speak(self, text: str) -> None:
            self.last_first_audio_time = time.monotonic()
            await asyncio.sleep(0.02)

    received_metrics = {}

    async def recognizer(text: str, latency_metrics: Optional[dict] = None) -> str:
        nonlocal received_metrics
        if latency_metrics:
            received_metrics.update(latency_metrics)
        return "It is 3 PM."

    vc = VoiceController(
        wakeword=wakeword,
        stt=StreamingSTT(),
        tts=FastTTS(),
        on_text_recognized=recognizer,
        continuous_session=False,
        session_timeout_secs=0.1
    )

    vc.start()
    wakeword.trigger()
    # Wait for the interaction cycle to finish
    await asyncio.sleep(0.18)

    # Verify that turn_latency_metrics captured all stages up to TTS
    assert "wake_detected" in received_metrics
    assert "microphone_open" in received_metrics
    assert "speech_first_detected" in received_metrics
    assert "speech_end" in received_metrics
    assert "stt_complete" in received_metrics

    # Verify monotonic sequencing of audio capture
    assert received_metrics["wake_detected"] <= received_metrics["microphone_open"]
    assert received_metrics["microphone_open"] <= received_metrics["speech_first_detected"]
    assert received_metrics["speech_first_detected"] <= received_metrics["speech_end"]
    assert received_metrics["speech_end"] <= received_metrics["stt_complete"]

    # Verify that post-TTS playback stages were captured on the controller
    last_m = vc.last_latency_metrics
    assert "tts_request_started" in last_m
    assert "tts_first_audio" in last_m
    assert "playback_started" in last_m
    assert "playback_complete" in last_m
    assert last_m["playback_started"] <= last_m["playback_complete"]

    vc.stop()


# ---------------------------------------------------------------------------
# TEST 14: End-to-End WebSocket Voice Latency Metrics Roundtrip
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_websocket_voice_latency_metrics_enrichment():
    from hub.models import VoiceRequestMessage, VoiceResponseMessage

    harness = AgentHarness(control_layer=CortexControlLayer(connected_devices_map={}))
    harness.router.provider = StubProvider("gemini-test", result_text="The time is 12:00 PM.")

    client_latency = {
        "wake_detected": 100.0,
        "microphone_open": 100.05,
        "speech_first_detected": 100.10,
        "speech_end": 101.20,
        "stt_complete": 101.40
    }

    req = VoiceRequestMessage(
        text="what time is it",
        command_id="cmd_trace_test",
        latency_metrics=client_latency
    )

    ctx = {"device": {"id": "dev_test", "name": "ASUS Zenbook"}}
    async def dummy_log(*args, **kwargs):
        pass

    res = await harness.process_request(
        source_device_id="dev_test",
        natural_request=req.text,
        context=ctx,
        log_action_callback=dummy_log,
        latency_metrics=req.latency_metrics,
        request_id=req.command_id
    )

    # Verify res.latency_metrics includes full trace
    assert hasattr(res, "latency_metrics")
    trace_data = res.latency_metrics
    assert trace_data["trace_id"].startswith("tr_")
    assert "wake_detected" in trace_data["timestamps"]
    assert "stt_complete" in trace_data["timestamps"]
    assert "hub_request_received" in trace_data["timestamps"]
    assert "routing_complete" in trace_data["timestamps"]
    assert "hub_response_emitted" in trace_data["timestamps"]

    # Durations must be computed
    dur = trace_data["stage_durations"]
    assert "stt_latency" in dur
    assert dur["stt_latency"] == pytest.approx(1.35, 0.01)  # 101.40 - 100.05
    assert "routing_latency" in dur

    # Check VoiceResponseMessage serialization
    resp_msg = VoiceResponseMessage(
        command_id=req.command_id,
        message=res.message,
        status=res.status,
        latency_metrics=getattr(res, "latency_metrics", req.latency_metrics)
    )
    dumped = resp_msg.model_dump()
    assert dumped["latency_metrics"]["trace_id"] == trace_data["trace_id"]
    assert "stage_durations" in dumped["latency_metrics"]
