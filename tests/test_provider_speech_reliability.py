"""Offline probes for model recovery and audio capture boundaries."""
import asyncio
import json
import struct
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from hub.llm_provider import GeminiLLMProvider, ProviderHTTPError, ModelDeadlineExceeded
from agent.voice.groq_providers import GroqSTTProvider, transcription_uncertain
from agent.voice.real_providers import LocalWakeWordProvider
from agent.voice.controller import VoiceController
from agent.voice.mock_providers import MockWakeWordProvider, MockTTSProvider


def gemini(side_effect):
    provider = GeminiLLMProvider(api_key="synthetic")
    request = AsyncMock(side_effect=side_effect)
    provider._client = SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(generate_content=request)))
    return provider, request


@pytest.mark.asyncio
async def test_one_explicit_503_is_retried_as_inference_only():
    provider, request = gemini([ProviderHTTPError("Gemini",503), SimpleNamespace(text='{"decision":"RESPOND","response_text":"Hello"}')])
    result = await provider.generate_result("Hello", {"mode":"agent_decision"})
    assert "Hello" in result.response_text
    assert request.await_count == 2


@pytest.mark.asyncio
async def test_inference_retry_never_replays_the_native_action():
    from test_agent_turn_runtime import control, call
    from hub.pipeline.pipeline import RequestPipeline
    ctrl = control([{"status":"success","data":{"opened":True}}])
    provider, request = gemini([ProviderHTTPError("Gemini",503),
        SimpleNamespace(text=json.dumps(call("app.launch",app="calculator"))),
        SimpleNamespace(text="The application opened.")])
    result = await RequestPipeline(ctrl,provider).execute("asus","Open the application",{})
    assert result.status == "success"
    assert request.await_count == 3
    assert ctrl.execute_and_observe.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 403, 429, 400])
async def test_non_transient_http_errors_are_not_retried(status):
    provider, request = gemini(ProviderHTTPError("Gemini",status))
    with pytest.raises(ProviderHTTPError):
        await provider.generate_result("Hello", {"mode":"agent_decision"})
    assert request.await_count == 1


@pytest.mark.asyncio
async def test_repeated_503_is_bounded_to_two_calls():
    provider, request = gemini(ProviderHTTPError("Gemini",503))
    with pytest.raises(ProviderHTTPError):
        await provider.generate_result("Hello", {"mode":"agent_decision"})
    assert request.await_count == 2


@pytest.mark.asyncio
async def test_model_timeout_cancels_request_without_retry():
    cancelled = asyncio.Event()
    async def slow(**kwargs):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
    provider, request = gemini(slow)
    with patch("hub.config.get_lane_deadlines",return_value=(.01,.02)):
        with pytest.raises(ModelDeadlineExceeded):
            await provider.generate_result("Hello", {"mode":"agent_decision"})
    assert cancelled.is_set()
    assert request.await_count == 1


@pytest.mark.asyncio
async def test_retry_uses_same_total_deadline():
    provider, request = gemini(ProviderHTTPError("Gemini",503))
    # Insufficient budget for backoff: fail immediately rather than resetting it.
    with patch("hub.config.get_lane_deadlines",return_value=(.01,.02)):
        with pytest.raises(ProviderHTTPError):
            await provider.generate_result("Hello", {"mode":"agent_decision"})
    assert request.await_count == 1


def test_default_structured_budget_is_longer_than_background_ack():
    from hub.config import DEADLINE_STRUCTURED_TOTAL
    from hub.agent_turn import AgentTurn
    assert DEADLINE_STRUCTURED_TOTAL == 30.0
    assert AgentTurn.ACK_SECONDS < DEADLINE_STRUCTURED_TOTAL < AgentTurn.TOTAL_SECONDS


def test_wake_detection_uses_partial_result_and_hands_off_audio_once():
    provider = LocalWakeWordProvider()
    frame = struct.pack('<4000h', *([500]*4000))
    rec = SimpleNamespace(AcceptWaveform=lambda _:False,
        PartialResult=lambda:json.dumps({"partial":"yo cortex"}))
    called=[]
    class Stream:
        def __enter__(self):
            provider._q.put(frame)
        def __exit__(self,*args):
            pass
    provider._running=True
    provider._on_wake=lambda:called.append(provider.take_audio())
    with patch("agent.voice.real_providers.model",object()), patch("agent.voice.real_providers.KaldiRecognizer",return_value=rec), patch("agent.voice.real_providers.sd.RawInputStream",return_value=Stream()):
        provider._listen_loop()
    assert called == [frame]
    assert provider.take_audio() == b""
    assert not provider._running


@pytest.mark.parametrize("segment", [
    {"avg_logprob":-1.3}, {"no_speech_prob":.9}, {"compression_ratio":3.0},
    {"avg_logprob":float('nan')}, {"no_speech_prob":True}])
def test_transcription_quality_uses_measurements_not_phrases(segment):
    assert transcription_uncertain({"segments":[segment]})
    assert not transcription_uncertain({"segments":[{"avg_logprob":-.1,"no_speech_prob":.01,"compression_ratio":1.4}]})


@pytest.mark.asyncio
async def test_unclear_speech_asks_repeat_before_dispatching():
    forwarded=[]
    class Speech:
        def __init__(self): self.calls=0
        def stop_listening(self): pass
        async def listen_stream(self):
            self.calls+=1
            self.last_transcription_uncertain=self.calls==1
            yield True, "unreliable words" if self.calls==1 else "What time is it?"
    class TTS(MockTTSProvider):
        def __init__(self): self.spoken=[]
        async def speak(self,text): self.spoken.append(text)
    async def on_text(text,**kwargs):
        forwarded.append(text)
        return "The verified response."
    stt,tts=Speech(),TTS()
    controller=VoiceController(MockWakeWordProvider(),stt,tts,on_text)
    controller._stopped=False
    await controller._session_loop("probe")
    assert forwarded == ["What time is it?"]
    assert "repeat" in tts.spoken[0]
    assert tts.spoken[-1] == "The verified response."


@pytest.mark.asyncio
async def test_wake_audio_delivered_only_to_first_capture():
    class Speech:
        def __init__(self): self.audio=[]
        def set_initial_audio(self,audio): self.audio.append(audio)
        def stop_listening(self): pass
        async def listen_stream(self): yield True,"yo cortex Open the application"
    speech=Speech()
    forward=AsyncMock(return_value="")
    controller=VoiceController(MockWakeWordProvider(),speech,MockTTSProvider(),forward)
    controller._pending_wake_audio=b"retained-audio"
    controller._stopped=False
    await controller._session_loop("probe")
    assert speech.audio==[b"retained-audio"]
    assert controller._pending_wake_audio==b""
    assert forward.await_args.args[0]=="Open the application"


@pytest.mark.asyncio
async def test_groq_retains_audio_across_vosk_segment_boundary(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY","synthetic")
    GroqSTTProvider._circuit_broken_until=0
    provider=GroqSTTProvider()
    speech=struct.pack('<4000h',*([500]*4000))
    quiet=bytes(8000)
    provider.set_initial_audio(speech+speech)
    rec=MagicMock()
    rec.AcceptWaveform.side_effect=[True]+[False]*10
    rec.Result.return_value='{"text":"yo cortex"}'
    rec.PartialResult.return_value='{"partial":""}'
    rec.FinalResult.return_value='{"text":"complete command"}'
    class Response:
        status=200
        async def __aenter__(self):return self
        async def __aexit__(self,*a):pass
        async def json(self):return {"text":"yo cortex complete command","segments":[{"avg_logprob":-.1}]}
    captured=[]
    class Session:
        async def __aenter__(self):return self
        async def __aexit__(self,*a):pass
        def post(self,*a,**k):
            captured.append(k['data'])
            return Response()
    class Stream:
        def __enter__(self):
            for frame in [speech,speech]+[quiet]*7: provider._q.put_nowait(frame)
        def __exit__(self,*a):pass
    with patch("agent.voice.groq_providers.vosk_model",object()),patch("vosk.KaldiRecognizer",return_value=rec),patch("sounddevice.RawInputStream",return_value=Stream()),patch("aiohttp.ClientSession",return_value=Session()):
        results=[r async for r in provider.listen_stream()]
    assert results[-1]==(True,"complete command")
    assert rec.AcceptWaveform.call_count>4
    assert len(captured)==1
    fields={field[0]['name']:field[2] for field in captured[0]._fields}
    assert fields['response_format']=='verbose_json'
    assert speech in fields['file']


@pytest.mark.asyncio
async def test_cancelled_capture_never_uploads_audio(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY","synthetic")
    provider=GroqSTTProvider()
    class Stream:
        def __enter__(self):
            provider.stop_listening()
        def __exit__(self,*a):pass
    with patch("agent.voice.groq_providers.vosk_model",object()),patch("vosk.KaldiRecognizer"),patch("sounddevice.RawInputStream",return_value=Stream()),patch("aiohttp.ClientSession") as client:
        results=[r async for r in provider.listen_stream()]
    client.assert_not_called()
    assert results==[(True,"")]
